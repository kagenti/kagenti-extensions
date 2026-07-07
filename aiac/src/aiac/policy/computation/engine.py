"""Policy Computation Engine.

A pure library that turns partial ``list[PolicyRule]`` updates into merged
``AgentPolicyModel`` records: it resolves IdP relationships, merges into the
Policy Store (additive append by default, or authoritative role-keyed replace
when ``override`` is set), and pushes the resulting ``PolicyModel`` to the PDP
Policy Writer. Rules arrive pre-flattened from the calling sub-agent — the PCE
performs no composite-role expansion. Fire-and-forget — ``compute_and_apply``
never raises.
"""

import logging
import os
from typing import TypeVar

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, Scope, Service
from aiac.pdp.policy.library.api import apply_policy
from aiac.policy.model.models import AgentPolicyModel, PolicyModel, PolicyRule
from aiac.policy.store.library.api import apply_agent_policy, get_agent_policy

logger = logging.getLogger(__name__)

_Entity = TypeVar("_Entity", Role, Scope)


def _fresh(agent_id: str) -> AgentPolicyModel:
    return AgentPolicyModel(
        agent_id=agent_id,
        agent_roles=[],
        agent_scopes=[],
        source_roles={},
        subject_roles={},
        target_scopes={},
        inbound_rules=[],
        outbound_rules=[],
        outbound_subject_rules=[],
    )


def _add_rule(rules: list[PolicyRule], rule: PolicyRule) -> None:
    """Append ``rule`` unless one with the same ``role.id`` + ``scope.id`` is present."""
    if any(r.role.id == rule.role.id and r.scope.id == rule.scope.id for r in rules):
        return
    rules.append(rule)


def _add_by_id(items: list[_Entity], item: _Entity) -> None:
    """Append ``item`` unless one with the same ``.id`` is already present."""
    if any(existing.id == item.id for existing in items):
        return
    items.append(item)


def _merge_map(dest: dict[str, list[_Entity]], src: dict[str, list[_Entity]]) -> None:
    for key, values in src.items():
        target = dest.setdefault(key, [])
        for value in values:
            _add_by_id(target, value)


def _merge(existing: AgentPolicyModel, delta: AgentPolicyModel) -> None:
    """Additively fold ``delta`` into ``existing`` (mutating ``existing``)."""
    for rule in delta.inbound_rules:
        _add_rule(existing.inbound_rules, rule)
    for rule in delta.outbound_rules:
        _add_rule(existing.outbound_rules, rule)
    for rule in delta.outbound_subject_rules:
        _add_rule(existing.outbound_subject_rules, rule)
    _merge_map(existing.source_roles, delta.source_roles)
    _merge_map(existing.subject_roles, delta.subject_roles)
    _merge_map(existing.target_scopes, delta.target_scopes)


def _drop_roles_from_map(mapping: dict[str, list[Role]], role_ids: set[str]) -> None:
    """Drop every role whose ``id`` is in ``role_ids`` from each list; prune empty keys."""
    for key in list(mapping):
        mapping[key] = [role for role in mapping[key] if role.id not in role_ids]
        if not mapping[key]:
            del mapping[key]


def _purge_roles(model: AgentPolicyModel, role_ids: set[str]) -> None:
    """Remove every trace of ``role_ids`` from ``model`` (authoritative replace).

    Drops matching rules from both ``inbound_rules`` and ``outbound_rules``, drops
    the roles from ``source_roles`` / ``subject_roles``, and reconciles
    ``target_scopes`` to only the scopes still justified by a surviving outbound rule.
    """
    model.inbound_rules = [r for r in model.inbound_rules if r.role.id not in role_ids]
    model.outbound_rules = [r for r in model.outbound_rules if r.role.id not in role_ids]
    model.outbound_subject_rules = [
        r for r in model.outbound_subject_rules if r.role.id not in role_ids
    ]
    _drop_roles_from_map(model.source_roles, role_ids)
    _drop_roles_from_map(model.subject_roles, role_ids)
    surviving_scope_ids = {r.scope.id for r in model.outbound_rules}
    for key in list(model.target_scopes):
        model.target_scopes[key] = [s for s in model.target_scopes[key] if s.id in surviving_scope_ids]
        if not model.target_scopes[key]:
            del model.target_scopes[key]


def compute_and_apply(rules: list[PolicyRule], override: bool = False) -> None:
    """Resolve, merge, and apply ``rules`` — fire-and-forget.

    ``override`` selects the merge mode. ``False`` (default) appends additively,
    preserving existing mappings. ``True`` authoritatively replaces every input
    role's mappings: the distinct set of input roles is purged from each affected
    model up-front (both directions, plus ``source_roles`` / ``subject_roles``
    drop and ``target_scopes`` reconciliation) before the fresh rules are applied.

    Exceptions from any dependency (IdP, Policy Store, PDP) are logged and
    swallowed so a transient failure never crashes the calling sub-agent.
    """
    try:
        _run(rules, override)
    except Exception:
        logger.exception("compute_and_apply failed for %d rule(s)", len(rules))


def _run(rules: list[PolicyRule], override: bool) -> None:
    config = Configuration.for_realm(os.environ["AIAC_REALM"])

    # Full service catalog keyed by serviceId (Keycloak clientId). It carries
    # each service's type — letting us tell agents from pure-target tools (P4) —
    # and each service's own roles/scopes, which the agent models embed (P2).
    catalog: dict[str, Service] = {svc.serviceId: svc for svc in config.get_services()}

    def is_agent(service_id: str) -> bool:
        svc = catalog.get(service_id)
        return svc is not None and svc.type == "Agent"

    models: dict[str, AgentPolicyModel] = {}

    def model(agent_id: str) -> AgentPolicyModel:
        if agent_id not in models:
            models[agent_id] = _fresh(agent_id)
        return models[agent_id]

    def record_subjects(m: AgentPolicyModel, role: Role) -> None:
        for subject in config.get_subjects_by_role(role):
            _add_by_id(m.subject_roles.setdefault(subject.username, []), role)

    # (user role, tool scope) rules are deferred: they attach to whichever agent
    # targets the tool, which is only known once the (agent role, tool scope)
    # rules below have populated target_scopes.
    pending_subject_tool_rules: list[PolicyRule] = []

    for rule in rules:
        # Rules arrive pre-flattened from the UC — the PCE queries the IdP once
        # per rule's role/scope as-is (no composite expansion). Each rule is
        # routed by kind (P5b):
        #   (user role,  agent scope) -> inbound_rules              [mapping a]
        #   (user role,  tool scope)  -> outbound_subject_rules     [mapping b]
        #   (agent role, tool scope)  -> outbound_rules + target_scopes [mapping c]
        role, scope = rule.role, rule.scope
        agent_role_owners = [s for s in config.get_services_by_role(role) if is_agent(s.serviceId)]
        scope_services = config.get_services_by_scope(scope)
        agent_scope_owners = [s for s in scope_services if is_agent(s.serviceId)]
        tool_scope_targets = [s for s in scope_services if not is_agent(s.serviceId)]

        if agent_role_owners:
            # (c) agent role -> tool scope: the agent may reach the tool.
            for owner in agent_role_owners:
                owner_model = model(owner.serviceId)
                _add_rule(owner_model.outbound_rules, rule)
                for tool in tool_scope_targets:
                    _add_by_id(owner_model.target_scopes.setdefault(tool.serviceId, []), scope)
        elif agent_scope_owners:
            # (a) user role -> agent scope: the user may call the agent.
            for agent in agent_scope_owners:
                agent_model = model(agent.serviceId)
                _add_rule(agent_model.inbound_rules, rule)
                record_subjects(agent_model, role)
        else:
            # (b) user role -> tool scope: deferred until target_scopes is built.
            pending_subject_tool_rules.append(rule)

    for rule in pending_subject_tool_rules:
        for agent_model in models.values():
            targets_scope = any(
                rule.scope.id == s.id
                for scopes in agent_model.target_scopes.values()
                for s in scopes
            )
            if targets_scope:
                _add_rule(agent_model.outbound_subject_rules, rule)
                record_subjects(agent_model, rule.role)

    # Distinct set of input roles, purged once up-front per model under override
    # (so a role shared across the input is not wiped after being appended).
    input_role_ids = {rule.role.id for rule in rules}

    written: list[AgentPolicyModel] = []
    for agent_id, delta in models.items():
        try:
            existing = get_agent_policy(agent_id)
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
            existing = _fresh(agent_id)  # agent not yet in the store
        if override:
            _purge_roles(existing, input_role_ids)
        _merge(existing, delta)
        # P2: each written agent embeds its own service-account roles and exposed
        # scopes. Realm-level agents (no owning service in the catalog) keep [].
        svc = catalog.get(agent_id)
        if svc is not None:
            existing.agent_roles = list(svc.roles)
            existing.agent_scopes = list(svc.scopes)
        apply_agent_policy(agent_id, existing)
        written.append(existing)

    apply_policy(PolicyModel(agents=written))
