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
from aiac.idp.configuration.models import Role, Scope
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

    models: dict[str, AgentPolicyModel] = {}

    def model(agent_id: str) -> AgentPolicyModel:
        if agent_id not in models:
            models[agent_id] = _fresh(agent_id)
        return models[agent_id]

    for rule in rules:
        # Rules arrive pre-flattened from the UC — the PCE queries the IdP once
        # per rule's role as-is (no composite expansion).
        role = rule.role
        targets = config.get_services_by_scope(rule.scope)
        for target in targets:
            _add_rule(model(target.serviceId).inbound_rules, rule)

        for source in config.get_services_by_role(role):
            source_model = model(source.serviceId)
            _add_rule(source_model.outbound_rules, rule)
            for target in targets:
                _add_by_id(source_model.target_scopes.setdefault(target.serviceId, []), rule.scope)
                _add_by_id(model(target.serviceId).source_roles.setdefault(source.serviceId, []), role)

        for subject in config.get_subjects_by_role(role):
            for target in targets:
                _add_by_id(model(target.serviceId).subject_roles.setdefault(subject.username, []), role)

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
        apply_agent_policy(agent_id, existing)
        written.append(existing)

    apply_policy(PolicyModel(agents=written))
