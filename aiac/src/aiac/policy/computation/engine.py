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

Drift GC. Because Keycloak UUIDs churn on delete/recreate, an append-only merge would let stale
edges pile up beside their superseded generations. So after routing, ``_reconcile`` prunes each
*touched* SPM against that same ``get_services()`` catalog (no extra IdP read) — dropping edges
whose scope or agent-role no longer exists and collapsing churned/duplicate user-role generations.
It removes only edges whose entity is gone, so order-independence is preserved.

Offboard / decommission. Reconcile is passive and catalog-anchored: it never wipes an SPM whose
owning service is absent from ``get_services()`` (a transient miss must not destroy state). That
leaves the *decommission* drift species uncovered — once a service's Keycloak client is deleted it
falls out of the catalog forever, so its own ``SPM(X)`` and its outbound footprint (``X_role →
other_scope`` edges on *other* SPMs) would linger, and its ``APM(X)`` would stay in the PDP.
``decommission(service_id)`` is the authoritative counterpart: it acts on an explicit offboard
signal (not the catalog-miss guard), tears down X's entire footprint, and re-derives every agent
whose policy changed. It is keyed by the **clientId (SPM key)**, not the Keycloak UUID — after the
client is deleted, ``get_services()`` can no longer resolve UUID→clientId.

Fire-and-forget — ``compute_and_apply`` and ``decommission`` re-raise dependency failures.
"""

import logging
from typing import TypeVar

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, RoleKind, Scope, Service, ServiceType
from aiac.pdp.policy.library.api import apply_policy, delete_agent_policy
from aiac.policy.model.models import (
    AgentPolicyModel,
    PolicyModel,
    PolicyRule,
    RuleEffect,
    ServicePolicyModel,
)
from aiac.policy.model_store.library.api import (
    apply_service_policy,
    delete_service_policy,
    get_service_policies_by_role,
    get_service_policy,
)

logger = logging.getLogger(__name__)

_Entity = TypeVar("_Entity", Role, Scope)


def _add_rule(rules: list[PolicyRule], rule: PolicyRule) -> None:
    """Append ``rule`` unless one with the same dedup identity ``(role.id, scope.id, effect)`` is
    present. Each list this is called on is single-effect (routing splits by ``effect`` first), so
    within a list the check reduces to ``(role.id, scope.id)``; carrying ``effect`` keeps the
    identity aligned with the model's canonical dedup key."""
    if any(
        r.role.id == rule.role.id and r.scope.id == rule.scope.id and r.effect == rule.effect
        for r in rules
    ):
        return
    rules.append(rule)


def _add_by_id(items: list[_Entity], item: _Entity) -> None:
    """Append ``item`` unless one with the same ``.id`` is already present."""
    if any(existing.id == item.id for existing in items):
        return
    items.append(item)


def _inbound_list(model: ServicePolicyModel, effect: RuleEffect) -> list[PolicyRule]:
    """The inbound list on ``model`` matching ``effect`` — the deny list for ``Deny``, else allow."""
    return model.inbound_deny_rules if effect == RuleEffect.DENY else model.inbound_allow_rules


def _all_inbound(model: ServicePolicyModel) -> list[PolicyRule]:
    """A read-only concatenation of both inbound lists — every edge touching ``model``'s scopes,
    ``Allow`` and ``Deny`` alike. For scanning (classification, targeter discovery); never mutate."""
    return model.inbound_allow_rules + model.inbound_deny_rules


def _route(model: ServicePolicyModel, rule: PolicyRule) -> bool:
    """Append ``rule`` to ``model``'s effect-matching inbound list (append-dedup). True iff added."""
    target = _inbound_list(model, rule.effect)
    before = len(target)
    _add_rule(target, rule)
    return len(target) != before


def _purge_role(model: ServicePolicyModel, role_id: str) -> bool:
    """Drop every inbound edge whose role is ``role_id`` from **both** lists (allow and deny) — the
    role-level revocation / footprint-purge primitive. Returns ``True`` iff any edge was removed."""
    removed = False
    for attr in ("inbound_allow_rules", "inbound_deny_rules"):
        rules: list[PolicyRule] = getattr(model, attr)
        kept = [r for r in rules if r.role.id != role_id]
        if len(kept) != len(rules):
            setattr(model, attr, kept)
            removed = True
    return removed


def _reconcile(
    model: ServicePolicyModel,
    catalog: dict[str, Service],
    catalog_agent_role_ids: set[str],
    batch_user_role_ids: set[str],
) -> bool:
    """Drop dangling inbound edges from a touched SPM against current IdP truth.

    Prevents cross-run drift accumulation (Keycloak UUIDs churn on delete/recreate, so an
    append-only merge grows stale edges beside the superseded ones). Uses only the ``get_services()``
    catalog the PCE already loads — no additional IdP read — so the ``get_services()``-only invariant
    holds. Order-independent: it removes **only** edges whose entity no longer exists, never a live
    edge, so onboarding-order convergence is preserved.

    An edge on ``SPM(X)`` is kept iff:

    1. its scope is still one of ``X``'s current ``aiac.managed`` scopes (``model.owned_scopes``,
       seeded from the catalog) — drops retired/churned scopes (e.g. ``*-aud``);
    2. for an ``Agent``-kind role, the role id is still in the catalog — drops retired/churned agent
       client roles (e.g. a focus-agent self-reference the current builder can no longer emit);
    3. for a ``User``-kind role, it is not a superseded generation: user realm roles are
       membership-derived (absent from the catalog, and the PCE must not read ``get_subjects()``), so
       among the ``User`` edges sharing ``(scope.id, role.name)`` a stale edge is dropped only when
       this batch carries a *different* id for that same ``(scope, name)`` — the fresh batch's
       current-generation id supersedes the old one.

    Runs over **both** inbound lists (allow and deny) independently: the churn collapse is computed
    per list, so a live ``Deny`` edge is never dropped because an unrelated ``Allow`` edge in the
    batch shares its ``(scope, name)``. Skips pruning entirely when ``X`` is absent from the catalog
    (a transient miss must never wipe an SPM). Returns ``True`` iff it removed at least one edge.
    """
    if catalog.get(model.service_id) is None:
        return False

    owner_scope_ids = {s.id for s in model.owned_scopes}

    def _prune(edges: list[PolicyRule]) -> list[PolicyRule]:
        # (1)+(2) existence prune.
        survivors = [
            edge
            for edge in edges
            if edge.scope.id in owner_scope_ids
            and not (
                edge.role.kind == RoleKind.AGENT and edge.role.id not in catalog_agent_role_ids
            )
        ]

        # (3) user-role churn collapse: a stale generation is dropped only when this batch carries a
        # different id for the same (scope, name).
        batch_ids_by_key: dict[tuple[str, str], set[str]] = {}
        for edge in survivors:
            if edge.role.kind == RoleKind.USER and edge.role.id in batch_user_role_ids:
                batch_ids_by_key.setdefault((edge.scope.id, edge.role.name), set()).add(edge.role.id)

        return [
            edge
            for edge in survivors
            if not (
                edge.role.kind == RoleKind.USER
                and edge.role.id not in batch_ids_by_key.get((edge.scope.id, edge.role.name), set())
                and batch_ids_by_key.get((edge.scope.id, edge.role.name))
            )
        ]

    changed = False
    for attr in ("inbound_allow_rules", "inbound_deny_rules"):
        edges: list[PolicyRule] = getattr(model, attr)
        kept = _prune(edges)
        if len(kept) != len(edges):
            setattr(model, attr, kept)
            changed = True
    return changed


def _spm_cache(catalog: dict[str, Service]):
    """Build a store-backed SPM cache seeded from the ``get_services()`` catalog.

    Returns ``(spms, spm, is_agent)`` shared by ``_run`` and ``_decommission``. ``spm(id)`` fetches
    each SPM from the store at most once (``get_service_policy`` returns a fresh empty SPM on 404, so
    a brand-new — or already-deleted — service is handled), seeds its identity (type + own
    ``aiac.managed`` roles/scopes) from the catalog when the service is still present, and mutates in
    place. ``is_agent(id)`` prefers the catalog and falls back to the cached SPM's ``service_type``
    (so a decommissioned service, absent from the catalog, is still classifiable from its persisted
    SPM).
    """
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

    return spms, spm, is_agent


def _fresh_apm(
    agent_id: str, default_effect: RuleEffect = RuleEffect.DENY
) -> AgentPolicyModel:
    # Identity/aggregate maps are the only required fields; the split target maps and the eight
    # entity x effect rule lists default to empty and are filled by ``_derive``. ``default_effect``
    # rides through onto the derived projection (see ``_derive``); it defaults to ``DENY`` so every
    # existing caller keeps today's least-privilege behavior.
    return AgentPolicyModel(
        agent_id=agent_id,
        default_effect=default_effect,
        agent_roles=[],
        agent_scopes=[],
        source_roles={},
        subject_roles={},
    )


def compute_and_apply(
    rules: list[PolicyRule],
    override: bool = False,
    default_effect: RuleEffect = RuleEffect.DENY,
) -> None:
    """Route, persist, derive, and apply ``rules`` — fire-and-forget.

    ``override`` selects the merge mode at the SPM layer. ``False`` (default) appends each rule
    additively to the effect-matching inbound list on ``SPM(scope.serviceId)`` (``Deny`` →
    ``inbound_deny_rules``, else ``inbound_allow_rules``; dedup by ``role.id`` + ``scope.id`` +
    ``effect``). ``True`` authoritatively replaces every input role's mappings: the distinct
    input-role set is purged from **both** inbound lists of **every** SPM containing it, once,
    up-front, before the fresh rules are appended (role-level revocation).

    ``default_effect`` is stamped onto **every** ``AgentPolicyModel`` this run derives — it decides
    how the deployed Rego treats a ``(role, scope)`` pair that no rule mentions. It defaults to
    ``DENY`` (today's least-privilege behavior), so all existing call sites — the four Controller
    ``/apply/*`` routes and the eventbus consumer — keep compiling unchanged; a caller opts into
    ``ALLOW`` explicitly (e.g. the onboarding path forwarding a caller-requested value).

    NON-DURABILITY CAVEAT: ``AgentPolicyModel`` is a pure derived projection, rebuilt from the
    persisted SPMs on every relevant recompute — ``default_effect`` is **not** persisted here. A
    later, *unrelated* recompute that re-derives the same agent (another onboarding, a role update)
    rebuilds its APM with ``DENY`` unless that call also passes ``ALLOW``. Making the value survive
    independent re-derivation would require persisting it on ``ServicePolicyModel`` — a separate,
    out-of-scope decision.

    Exceptions from any dependency (IdP, Policy Store, PDP) are logged and **re-raised** so the
    caller (the Controller) surfaces the failure — e.g. as a 500 — instead of returning success
    while silently applying nothing.
    """
    try:
        _run(rules, override, default_effect)
    except Exception:
        logger.exception("compute_and_apply failed for %d rule(s)", len(rules))
        raise


def decommission(service_id: str) -> None:
    """Authoritatively remove a decommissioned service's entire policy footprint.

    ``service_id`` is the **clientId (the SPM key)**, not the Keycloak internal UUID: an offboarded
    client is gone from ``get_services()``, so UUID→clientId resolution is impossible — the offboard
    contract carries the clientId directly (the documented asymmetry with onboard's
    ``/apply/service/{uuid}``).

    Tears down everything reconcile's catalog-anchored GC cannot: deletes ``SPM(X)`` (removing every
    user→X and agent→X inbound edge), purges X's **outbound footprint** (``X_role → other_scope``
    edges stored on other services' SPMs), deletes ``APM(X)`` from the PDP if X was an agent, and
    re-derives every agent whose policy changed (agents that targeted X; agents X sourced into) in a
    single partial upsert. A never-onboarded / already-removed service is a no-op.

    Exceptions from any dependency (IdP, Policy Store, PDP) are logged and **re-raised** so the
    Controller surfaces the failure instead of reporting a phantom success.
    """
    try:
        _decommission(service_id)
    except Exception:
        # Strip CR/LF before logging so a hostile service_id cannot forge log records.
        safe_id = service_id.replace("\r", "").replace("\n", "")
        logger.exception("decommission failed for service %r", safe_id)
        raise


def _run(
    rules: list[PolicyRule], override: bool, default_effect: RuleEffect = RuleEffect.DENY
) -> None:
    config = Configuration.for_default_realm()

    # (1) Catalog once — the only runtime IdP read. Carries each service's type (agent vs tool,
    # for P4) and its own roles/scopes (embedded on the APM for P2, filtered to aiac.managed).
    catalog = {svc.serviceId: svc for svc in config.get_services()}

    # SPM cache: fetch each SPM from the store at most once, seed its identity from the catalog,
    # mutate in place, and persist the changed ones.
    spms, spm, is_agent = _spm_cache(catalog)

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
                if _purge_role(model, role.id):
                    changed.add(model.service_id)

    # (2) Route each rule to the SPM of the service that owns its scope, into the inbound list
    # matching its ``effect`` (Deny → inbound_deny_rules, else inbound_allow_rules). Append-dedup by
    # role.id + scope.id + effect. No role-kind classification here — kind only matters at derive.
    for rule in rules:
        model = spm(rule.scope.serviceId)
        if _route(model, rule) or override:
            changed.add(model.service_id)

    # (3.5) Reconcile touched SPMs against current IdP truth (get_services()-only — no extra IdP
    # read) so drift cannot accumulate across re-onboarding. At this point ``spms`` holds exactly
    # the touched SPMs (routed + override-purged); agent-derive SPMs are not loaded yet. Runs under
    # both merge modes; order-independent (drops only edges whose entity no longer exists).
    catalog_agent_role_ids = {r.id for svc in catalog.values() for r in svc.roles if r.aiac_managed}
    batch_user_role_ids = {rule.role.id for rule in rules if rule.role.kind == RoleKind.USER}
    for service_id, model in list(spms.items()):
        if _reconcile(model, catalog, catalog_agent_role_ids, batch_user_role_ids):
            changed.add(service_id)

    # (4) Persist every changed SPM.
    for service_id in changed:
        apply_service_policy(service_id, spms[service_id])

    # (5) Affected-agent set — from the batch's roles plus every owner whose stored SPM was
    # modified during this run (routed, override-purged, or reconciled), never a full scan.
    # Folding ``changed`` into the seed is what makes revocation propagate: under override an
    # owner that only *lost* edges appears in ``changed`` but carries no fresh rule, so driving
    # the recompute off the incoming ``rules`` alone would leave that owner's APM (and the APMs
    # of agents that targeted it) stale.
    touched_owners = {rule.scope.serviceId for rule in rules} | changed

    affected: set[str] = set()
    for role in distinct_roles.values():
        if role.kind == RoleKind.AGENT:
            affected.update(role.actorIds)  # owning agents — their outbound changed
    for owner in touched_owners:
        if is_agent(owner):
            affected.add(owner)  # the touched owner is an agent — its inbound changed
        # every agent targeting a scope on this touched SPM: owners of its Agent-kind inbound
        # edges (allow AND deny — a deny edge also changed that agent's outbound), a superset of
        # the exact-scope match (re-deriving is idempotent, so safe).
        for edge in _all_inbound(spm(owner)):
            if edge.role.kind == RoleKind.AGENT:
                affected.update(edge.role.actorIds)

    # (6) Derive each affected agent's APM (zero IdP) and partial-upsert once. Tools get an SPM
    # but no APM (P4).
    derived = [
        _derive(agent_id, spm, default_effect)
        for agent_id in sorted(affected)
        if is_agent(agent_id)
    ]
    if derived:
        apply_policy(PolicyModel(agents=derived))


def _decommission(service_id: str) -> None:
    config = Configuration.for_default_realm()

    # (1) Catalog once — the only runtime IdP read. X itself is absent (it was offboarded); the
    # catalog is used to seed/classify the still-live agents we re-derive.
    catalog = {svc.serviceId: svc for svc in config.get_services()}
    spms, spm, is_agent = _spm_cache(catalog)

    # (2) Load SPM(X) — X is gone from the catalog, so spm() does not reseed it; the persisted SPM
    # carries the roles/scopes X owned when it was onboarded. Content guard: a 404 fresh-empty SPM
    # (never onboarded / already removed) is a no-op — no spurious PDP delete.
    spm_x = spm(service_id)
    if not (
        spm_x.owned_roles or spm_x.owned_scopes or spm_x.inbound_allow_rules or spm_x.inbound_deny_rules
    ):
        return

    # (3) Targeters — agents whose outbound loses X: they hold an Agent-kind inbound edge (allow or
    # deny) on SPM(X) (their_role → X_scope), which vanishes when SPM(X) is deleted in step 5.
    affected: set[str] = {
        actor
        for edge in _all_inbound(spm_x)
        if edge.role.kind == RoleKind.AGENT
        for actor in edge.role.actorIds
    }

    changed: set[str] = set()

    # (4) Purge X's outbound footprint — X_role → other_scope edges (allow AND deny) stored on OTHER
    # services' SPMs.
    for role in spm_x.owned_roles:
        for stored in get_service_policies_by_role(role):
            if stored.service_id == service_id:
                continue
            model = spm(stored.service_id)
            if _purge_role(model, role.id):
                changed.add(model.service_id)
                if is_agent(model.service_id):
                    affected.add(model.service_id)  # its inbound source_roles[X] vanished

    # (5) Delete SPM(X) — removes every user→X and agent→X inbound edge in one shot — and evict it
    # from the cache so re-derive cannot resurrect it.
    was_agent = spm_x.service_type == ServiceType.AGENT
    delete_service_policy(service_id)
    spms.pop(service_id, None)

    # (6) Persist every changed (footprint-purged) SPM.
    for changed_id in changed:
        apply_service_policy(changed_id, spms[changed_id])

    # (7) Delete APM(X) from the PDP iff X was an agent (tools have an SPM but no APM).
    if was_agent:
        delete_agent_policy(service_id)

    # (8) Re-derive every affected agent (X excluded) from the freshly-persisted, X-deleted store —
    # outbound/target_scopes/source_roles referencing X drop automatically. One partial upsert.
    affected.discard(service_id)
    # Re-derive with the ``DENY`` default: decommission carries no caller-supplied ``default_effect``,
    # and per the non-durability caveat (``compute_and_apply``) an unrelated recompute like this one
    # rebuilds each APM at least-privilege unless ``default_effect`` is persisted on the SPM.
    derived = [_derive(agent_id, spm) for agent_id in sorted(affected) if is_agent(agent_id)]
    if derived:
        apply_policy(PolicyModel(agents=derived))


def _register_identity(apm: AgentPolicyModel, edge: PolicyRule) -> None:
    """Register an inbound edge's role into the **effect-agnostic** identity maps — ``subject_roles``
    for a User role, ``source_roles`` for an Agent role. Called for allow AND deny edges alike: a
    role/subject that appears **only** in a DENY edge must still land here, or the generated deny
    lookup cannot resolve the role at request time and the prohibition silently never fires."""
    target = apm.subject_roles if edge.role.kind == RoleKind.USER else apm.source_roles
    for actor in edge.role.actorIds:
        _add_by_id(target.setdefault(actor, []), edge.role)


def _derive(agent_id, spm, default_effect: RuleEffect = RuleEffect.DENY) -> AgentPolicyModel:
    """Build ``APM(agent_id)`` entirely from the persisted SPMs (zero IdP).

    Each inbound edge on ``SPM(A)`` is classified by ``role.kind`` (User → subject, Agent → source)
    **and** ``effect`` (allow/deny) into one of four inbound buckets; each outbound edge (one of A's
    own roles referenced on another SPM) is classified by ``effect`` into the target allow/deny
    bucket and grows ``target_allow_scopes`` / ``target_deny_scopes``. Identity/aggregate maps stay
    effect-agnostic — a deny-only role or subject still registers into them.

    ``default_effect`` is stamped onto the emitted APM (via ``_fresh_apm``); it is derived data, not
    read back from any store (see the non-durability caveat on ``compute_and_apply``)."""
    sa = spm(agent_id)
    apm = _fresh_apm(agent_id, default_effect)

    # Identity (P2) — the agent's own aiac.managed roles/scopes, seeded from the catalog.
    apm.agent_roles = list(sa.owned_roles)
    apm.agent_scopes = list(sa.owned_scopes)

    # Inbound — every edge on SPM(A), split by (role.kind, effect). Identity maps effect-agnostic.
    for effect, subject_bucket, source_bucket in (
        (RuleEffect.ALLOW, apm.inbound_subject_allow_rules, apm.inbound_source_allow_rules),
        (RuleEffect.DENY, apm.inbound_subject_deny_rules, apm.inbound_source_deny_rules),
    ):
        for edge in _inbound_list(sa, effect):
            bucket = subject_bucket if edge.role.kind == RoleKind.USER else source_bucket
            _add_rule(bucket, edge)
            _register_identity(apm, edge)

    # Outbound — for each of A's own roles, the edges on other services' SPMs that reference it,
    # split by effect. Relevance is directional: only A's *agent* roles confer an outbound edge, so
    # a merely shared user role never creates a false edge to a service A does not target.
    for role in sa.owned_roles:
        for stored in get_service_policies_by_role(role):
            _derive_outbound(apm, role, stored)

    return apm


def _derive_outbound(apm: AgentPolicyModel, role: Role, stored: ServicePolicyModel) -> None:
    """Project A's own ``role`` edges on ``stored`` into the outbound target + subject buckets,
    split by effect: an ``Allow`` target edge grows ``outbound_target_allow_rules`` /
    ``target_allow_scopes``; a ``Deny`` one grows the deny counterparts."""
    for effect, target_rules, target_scopes in (
        (RuleEffect.ALLOW, apm.outbound_target_allow_rules, apm.target_allow_scopes),
        (RuleEffect.DENY, apm.outbound_target_deny_rules, apm.target_deny_scopes),
    ):
        for edge in _inbound_list(stored, effect):
            if edge.role.id != role.id:
                continue
            scope = edge.scope
            _add_rule(target_rules, edge)
            _add_by_id(target_scopes.setdefault(scope.serviceId, []), scope)
            # Outbound subject gate — the User-kind edges on the SAME owning SPM whose scope is this
            # target scope (which users may / must not reach it through A).
            _derive_outbound_subject(apm, stored, scope)


def _derive_outbound_subject(
    apm: AgentPolicyModel, stored: ServicePolicyModel, scope: Scope
) -> None:
    """Gather ``stored``'s User-kind edges for ``scope`` into the outbound subject buckets (allow /
    deny), and register each such user into the effect-agnostic ``subject_roles`` map."""
    for effect, subject_rules in (
        (RuleEffect.ALLOW, apm.outbound_subject_allow_rules),
        (RuleEffect.DENY, apm.outbound_subject_deny_rules),
    ):
        for user_edge in _inbound_list(stored, effect):
            if user_edge.scope.id == scope.id and user_edge.role.kind == RoleKind.USER:
                _add_rule(subject_rules, user_edge)
                for username in user_edge.role.actorIds:
                    _add_by_id(apm.subject_roles.setdefault(username, []), user_edge.role)
