"""Policy Computation Engine.

A pure library that turns partial ``list[PolicyRule]`` updates into merged
``AgentPolicyModel`` records: it resolves IdP relationships, additively merges
into the Policy Store, and pushes the resulting ``PolicyModel`` to the PDP
Policy Writer. Fire-and-forget — ``compute_and_apply`` never raises.
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


def _flatten_leaves(role: Role) -> list[Role]:
    """Flatten a (possibly composite) role to its non-composite leaf roles.

    A composite role contributes its recursively-collected leaf children but not
    itself; a non-composite role yields ``[role]``. De-duplicated by ``role.id``
    (``Role`` is unhashable, so we track seen ids rather than the objects).
    """
    leaves: list[Role] = []
    seen: set[str] = set()

    def visit(node: Role) -> None:
        if node.composite:
            for child in node.childRoles:
                visit(child)
        elif node.id not in seen:
            seen.add(node.id)
            leaves.append(node)

    visit(role)
    return leaves


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


def compute_and_apply(rules: list[PolicyRule]) -> None:
    """Resolve, merge, and apply ``rules`` — fire-and-forget.

    Exceptions from any dependency (IdP, Policy Store, PDP) are logged and
    swallowed so a transient failure never crashes the calling sub-agent.
    """
    try:
        _run(rules)
    except Exception:
        logger.exception("compute_and_apply failed for %d rule(s)", len(rules))


def _run(rules: list[PolicyRule]) -> None:
    config = Configuration.for_realm(os.environ["AIAC_REALM"])

    models: dict[str, AgentPolicyModel] = {}

    def model(agent_id: str) -> AgentPolicyModel:
        if agent_id not in models:
            models[agent_id] = _fresh(agent_id)
        return models[agent_id]

    for rule in rules:
        targets = config.get_services_by_scope(rule.scope)
        for target in targets:
            _add_rule(model(target.serviceId).inbound_rules, rule)

        for role in _flatten_leaves(rule.role):
            for source in config.get_services_by_role(role):
                source_model = model(source.serviceId)
                _add_rule(source_model.outbound_rules, rule)
                for target in targets:
                    _add_by_id(source_model.target_scopes.setdefault(target.serviceId, []), rule.scope)
                    _add_by_id(model(target.serviceId).source_roles.setdefault(source.serviceId, []), role)

            for subject in config.get_subjects_by_role(role):
                for target in targets:
                    _add_by_id(model(target.serviceId).subject_roles.setdefault(subject.username, []), role)

    written: list[AgentPolicyModel] = []
    for agent_id, delta in models.items():
        try:
            existing = get_agent_policy(agent_id)
        except RuntimeError as exc:
            if "404" not in str(exc):
                raise
            existing = _fresh(agent_id)  # agent not yet in the store
        _merge(existing, delta)
        apply_agent_policy(agent_id, existing)
        written.append(existing)

    apply_policy(PolicyModel(agents=written))
