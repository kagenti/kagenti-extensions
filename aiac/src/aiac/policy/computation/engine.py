"""Policy Computation Engine (SPM-based, order-independent).

A pure library that folds partial ``list[PolicyRule]`` updates into the persistent,
per-service source of truth — ``ServicePolicyModel`` (SPM) — and then **derives** each
affected agent's ``AgentPolicyModel`` (APM) entirely from those SPMs before partial-upserting
them to the PDP Policy Writer.

Why SPMs. The previous design persisted only per-agent APMs with rules denormalised onto the
agent, which made the merge outcome depend on onboarding order (``UR→TS`` was dropped when the
tool onboarded before any agent targeted it). Here every rule ``(role → scope)`` is stored as an
inbound edge on ``SPM(scope.serviceId)`` — the service that *owns* the scope — so the fact
survives regardless of which services already exist, and both onboarding orders converge to the
same derived ``APM(A)``.

Input contract. Each ``PolicyRule`` arrives with ``scope.serviceId``, ``role.kind`` and
``role.actorIds`` already populated and with roles already flattened to their closure. The PCE
performs no IdP lookup for routing/classification and no role flattening — the only runtime IdP
read is ``Configuration.get_services()`` for the identity (P2) seed.

Fire-and-forget — ``compute_and_apply`` never raises.
"""

import logging
from typing import TypeVar

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, RoleKind, Scope, ServiceType
from aiac.pdp.policy.library.api import apply_policy
from aiac.policy.model.models import (
    AgentPolicyModel,
    PolicyModel,
    PolicyRule,
    ServicePolicyModel,
)
from aiac.policy.store.library.api import (
    apply_service_policy,
    get_service_policies_by_role,
    get_service_policy,
)

logger = logging.getLogger(__name__)

_Entity = TypeVar("_Entity", Role, Scope)


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


def _fresh_apm(agent_id: str) -> AgentPolicyModel:
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


def compute_and_apply(rules: list[PolicyRule], override: bool = False) -> None:
    """Route, persist, derive, and apply ``rules`` — fire-and-forget.

    ``override`` selects the merge mode at the SPM layer. ``False`` (default) appends each rule
    additively to ``SPM(scope.serviceId).inbound_rules`` (dedup by ``role.id`` + ``scope.id``).
    ``True`` authoritatively replaces every input role's mappings: the distinct input-role set is
    purged from **every** SPM containing it, once, up-front, before the fresh rules are appended
    (role-level revocation).

    Exceptions from any dependency (IdP, Policy Store, PDP) are logged and **re-raised** so the
    caller (the Controller) surfaces the failure — e.g. as a 500 — instead of returning success
    while silently applying nothing.
    """
    try:
        _run(rules, override)
    except Exception:
        logger.exception("compute_and_apply failed for %d rule(s)", len(rules))
        raise


def _run(rules: list[PolicyRule], override: bool) -> None:
    config = Configuration.for_default_realm()

    # (1) Catalog once — the only runtime IdP read. Carries each service's type (agent vs tool,
    # for P4) and its own roles/scopes (embedded on the APM for P2, filtered to aiac.managed).
    catalog = {svc.serviceId: svc for svc in config.get_services()}

    # SPM cache: fetch each SPM from the store at most once, seed its identity from the catalog,
    # mutate in place, and persist the changed ones. ``get_service_policy`` returns a fresh empty
    # SPM on 404, so a brand-new service is seeded here too.
    spms: dict[str, ServicePolicyModel] = {}

    def spm(service_id: str) -> ServicePolicyModel:
        if service_id not in spms:
            model = get_service_policy(service_id)
            svc = catalog.get(service_id)
            if svc is not None:
                if svc.type is not None:
                    model.service_type = svc.type
                model.owned_roles = [r for r in svc.roles if r.aiac_managed]
                model.owned_scopes = [s for s in svc.scopes if s.aiac_managed]
            spms[service_id] = model
        return spms[service_id]

    def is_agent(service_id: str) -> bool:
        svc = catalog.get(service_id)
        if svc is not None:
            return svc.type == ServiceType.AGENT
        model = spms.get(service_id)
        return model is not None and model.service_type == ServiceType.AGENT

    # Distinct input roles (dedup by id) — the set purged under override and the seed of the
    # affected-agent set.
    distinct_roles: dict[str, Role] = {}
    for rule in rules:
        distinct_roles.setdefault(rule.role.id, rule.role)

    changed: set[str] = set()

    # (3) Override — role-level revocation, once up-front, BEFORE any fresh append (so a role
    # shared across the input is not wiped after being added).
    if override:
        for role in distinct_roles.values():
            for stored in get_service_policies_by_role(role):
                model = spm(stored.service_id)
                kept = [r for r in model.inbound_rules if r.role.id != role.id]
                if len(kept) != len(model.inbound_rules):
                    model.inbound_rules = kept
                    changed.add(model.service_id)

    # (2) Route each rule to the SPM of the service that owns its scope. Append-dedup by
    # role.id + scope.id. No write-time classification — kind only matters at derive time.
    for rule in rules:
        model = spm(rule.scope.serviceId)
        before = len(model.inbound_rules)
        _add_rule(model.inbound_rules, rule)
        if len(model.inbound_rules) != before or override:
            changed.add(model.service_id)

    # (4) Persist every changed SPM.
    for service_id in changed:
        apply_service_policy(service_id, spms[service_id])

    # (5) Affected-agent set — from the batch's roles/scopes, never a full scan.
    affected: set[str] = set()
    for role in distinct_roles.values():
        if role.kind == RoleKind.AGENT:
            affected.update(role.actorIds)  # owning agents — their outbound changed
    for rule in rules:
        scope = rule.scope
        owner = scope.serviceId
        if is_agent(owner):
            affected.add(owner)  # the scope's owner is an agent — its inbound changed
        # every agent targeting this scope: owners of the Agent-kind inbound rules on the
        # owning SPM whose scope is this one.
        for edge in spm(owner).inbound_rules:
            if edge.scope.id == scope.id and edge.role.kind == RoleKind.AGENT:
                affected.update(edge.role.actorIds)

    # (6) Derive each affected agent's APM (zero IdP) and partial-upsert once. Tools get an SPM
    # but no APM (P4).
    derived = [_derive(agent_id, spm) for agent_id in sorted(affected) if is_agent(agent_id)]
    if derived:
        apply_policy(PolicyModel(agents=derived))


def _derive(agent_id, spm) -> AgentPolicyModel:
    """Build ``APM(agent_id)`` entirely from the persisted SPMs (zero IdP)."""
    sa = spm(agent_id)
    apm = _fresh_apm(agent_id)

    # Identity (P2) — the agent's own aiac.managed roles/scopes, seeded from the catalog.
    apm.agent_roles = list(sa.owned_roles)
    apm.agent_scopes = list(sa.owned_scopes)

    # Inbound — every edge on SPM(A), split by role.kind.
    for edge in sa.inbound_rules:
        _add_rule(apm.inbound_rules, edge)
        if edge.role.kind == RoleKind.USER:
            for username in edge.role.actorIds:
                _add_by_id(apm.subject_roles.setdefault(username, []), edge.role)
        else:  # Agent
            for source_id in edge.role.actorIds:
                _add_by_id(apm.source_roles.setdefault(source_id, []), edge.role)

    # Outbound — for each of A's own roles, the edges on other services' SPMs that reference it.
    # Relevance is directional: only A's *agent* roles confer an outbound edge, so a merely
    # shared user role never creates a false edge to a service A does not target.
    for role in sa.owned_roles:
        for stored in get_service_policies_by_role(role):
            for edge in stored.inbound_rules:
                if edge.role.id != role.id:
                    continue
                scope = edge.scope
                _add_rule(apm.outbound_rules, edge)
                _add_by_id(apm.target_scopes.setdefault(scope.serviceId, []), scope)
                # Outbound subject gate — the User-kind inbound rules on the SAME owning SPM
                # whose scope is this target scope (the users allowed to reach it through A).
                for user_edge in stored.inbound_rules:
                    if user_edge.scope.id == scope.id and user_edge.role.kind == RoleKind.USER:
                        _add_rule(apm.outbound_subject_rules, user_edge)
                        for username in user_edge.role.actorIds:
                            _add_by_id(apm.subject_roles.setdefault(username, []), user_edge.role)

    return apm
