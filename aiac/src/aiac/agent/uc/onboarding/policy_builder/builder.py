"""Service Policy Builder sub-agent (UC1).

Second of the two stages sequenced by the Service Onboarding Orchestrator, run after
Service Provision. Deterministic (non-LLM): sources candidates from the same worldview as
the Policy Computation Engine — ``get_services()`` for correct ``kind``/ownership, plus
``get_subjects()`` for membership-derived user roles — flattens roles to their closure via
``flatten_role`` (3.2) before any PRB call, invokes the Policy Rules Builder for each
applicable pair, and returns a single ``list[PolicyRule]``. It applies nothing — the
Orchestrator/Controller make the single ``compute_and_apply`` (PCE) call afterwards.

IdP access is via the **idp-library** ``Configuration`` (the ``_config`` seam), never the
IdP Configuration Service directly. The focus service is resolved from ``get_services()``
by ``id`` (the Keycloak internal client UUID the ``/apply/service/{id}`` route and
``Trigger.entity_id`` carry — **not** ``serviceId``/clientId, which may be a slash-bearing
SPIFFE URI), so its own roles/scopes are id-bearing ``Role``/``Scope`` usable as PRB inputs
and flattenable.

Candidates are excluded/included by **ownership** (role id / ``scope.serviceId``), never by
name: the focus service's own ``aiac.managed`` roles/scopes are never candidates; other
services' ``aiac.managed`` roles carry ``kind=Agent``; realm roles held by at least one user
(composite-expanded, and not owned by any service) carry ``kind=User``. This keeps
``subject_roles``/``source_roles`` routing correct downstream in the PCE.

The focal-entity resolution itself (the own-scope / candidate-role / other-scope split, and
the IdP-unreachable / unknown-service ``HTTPException(502/404)`` boundary) lives in the shared
``resolve_focal_entities`` (D13) so the read-only Policy Conflict Check diagnostic can reuse
the exact same entity set. This module keeps only the fan-out loop over that set. The local
``_config`` seam is preserved and threaded into the resolver so existing tests patch it as
before.
"""

import logging

from aiac.agent.policy_rules_builder.conflict_detection import (
    PolicyConflictError,
    detect_conflicts,
    report_from_contradictions,
)
from aiac.agent.policy_rules_builder.conflict_enrichment import enrich_report
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictReport
from aiac.agent.policy_rules_builder.graph import (
    PolicyContradictionError,
    build_role_denies,
    build_role_rules,
    build_scope_rules,
)
from aiac.agent.policy_rules_builder.policy_source import get_policy_source
from aiac.agent.shared.focal_entities import resolve_focal_entities
from aiac.agent.shared.roles import flatten_role
from aiac.agent.uc.onboarding.policy_builder.cross_service import applied_rules_for_scopes
from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import RoleKind, ServiceType
from aiac.policy.model.models import PolicyRule

logger = logging.getLogger(__name__)


def _config() -> Configuration:
    return Configuration.for_default_realm()


class ServicePolicyBuilder:
    @staticmethod
    def build(service_id: str, service_type: ServiceType) -> list[PolicyRule]:
        # service_type is routed through the parameter (the requested classification for the
        # fan-out), never conflated with focus.type — see #154 AC#6.
        focal = resolve_focal_entities(service_id, service_type, config=_config())

        rules: list[PolicyRule] = []
        # #168: the fan-out must NOT abort on the FIRST policy contradiction. Each PRB call is
        # guarded: a ``PolicyContradictionError`` (the LLM auditor's *policy finding* -- this focal's
        # rule set is withheld) is accumulated as its ``(focal, contradictions)`` and the fan-out
        # CONTINUES, so every focal's contradiction is collected in one build rather than the report
        # stopping at whichever pair the loop happened to reach first. A HARD failure
        # (PolicyRulesBuilderError / LLMAccessError / UnparseableLLMResponseError -- sibling
        # PolicyRulesBuilderBaseError subclasses that are NOT PolicyContradictionError) is deliberately
        # NOT caught here: it aborts the build immediately and propagates so the Orchestrator rolls
        # back. We catch the narrow PolicyContradictionError ONLY -- never the base -- so a hard fault
        # can never be silently folded into the report.
        contradictions: list[PolicyContradictionError] = []

        def _guarded(fn, *args) -> None:
            try:
                rules.extend(fn(*args))
            except PolicyContradictionError as exc:
                contradictions.append(exc)

        for scope in focal.own_scopes:
            _guarded(build_scope_rules, focal.candidate_roles, scope)
        # Door B -- user-role-focal DENY-only pass at the focus's OWN-scope onboarding, alongside
        # the scope-focal pass above. Fan the kind=User subset of the (already flattened+deduped)
        # candidate roles over the focus's own scopes to surface each user role's exclusivity
        # ("Testers may access only issues") as the DENY rules the scope-focal pass structurally
        # cannot express. Deny-only: the scope-focal pass stays the single grant authority. Placing
        # it here -- own scopes always exist at the service's own onboarding -- keeps it
        # order-independent (tool-first vs agent-first yields identical denies), and produces both
        # the scope-focal (role, own-scope) grant and the Door B (role, own-scope) prohibition in
        # the same build.
        for user_role in (r for r in focal.candidate_roles if r.kind is RoleKind.USER):
            _guarded(build_role_denies, user_role, focal.own_scopes)
        if service_type is ServiceType.AGENT:
            for own_role in focal.own_roles:
                for role in flatten_role(own_role):
                    _guarded(build_role_rules, role, focal.other_scopes)
        # Inline, deterministic (non-LLM) conflict detection over the COMBINED state (#2504): this
        # build's fully assembled rules (scope-focal grants + Door B denies) PLUS the already-applied
        # inbound rules of the OTHER services on the scopes this build touches, read from the Policy
        # Store. Widening the input this way lets the SAME #2502 allow∩deny intersection surface a
        # cross-service overlap -- an Allow here and a Deny another service already applied on the
        # same (role.id, scope.id) -- that a single build's own rules could never reveal. Onboarding
        # appends (override=False), so ``combined`` is exactly the post-apply persisted state.
        #
        # Per ADR 0001 we surface, never reconcile: a (role, scope) carrying both an Allow and a Deny
        # raises HERE -- before the Orchestrator/Controller reach ``compute_and_apply`` -- so a
        # conflict leaves persisted state untouched (atomic-by-construction; the store read above is
        # side-effect-free). Detection is order-independent (keyed on ids), so tool-first vs
        # agent-first onboarding yields the identical outcome.
        combined = rules + applied_rules_for_scopes(rules)
        structural = detect_conflicts(combined)
        # #168: UNION the deterministic conflict survey (allow∩deny over the combined state) with the
        # auditor contradictions accumulated during the fan-out, into ONE ConflictReport. Each withheld
        # focal is reshaped via ``report_from_contradictions`` into the SAME Conflict-row shape the
        # structural detector emits (real-id-less rows with quotes_verified=False), so a focal withheld
        # for a genuine contradiction surfaces as Conflict rows alongside the structural overlaps rather
        # than being lost. ``evaluated_count`` mirrors both producers -- the distinct (role.id, scope.id)
        # pairs the detector examined plus one per accumulated focal -- but the raise decision below reads
        # only ``report.conflicts`` (any conflict => CONFLICTS_FOUND), so the merge is order-independent.
        conflicts = list(structural.conflicts)
        unevaluated = list(structural.unevaluated)
        for exc in contradictions:
            per_focal = report_from_contradictions(exc.focal, exc.contradictions)
            conflicts.extend(per_focal.conflicts)
            unevaluated.extend(per_focal.unevaluated)
        evaluated_count = len({(r.role.id, r.scope.id) for r in combined}) + len(contradictions)
        report = ConflictReport.from_survey(conflicts, unevaluated, evaluated_count=evaluated_count)
        if report.conflicts:
            # A conflict was found: NOW (and only now) run the LLM explain/quote survey over the
            # exact pairs detect_conflicts surfaced -- classifying each kind and extracting verbatim,
            # substring-validated quotes from the candidate policy text (#2503). Gating the LLM
            # behind report.conflicts keeps a clean apply fully deterministic and LLM-free (the
            # explain seam never fires) -- true for a clean cross-service apply too. ``combined`` is
            # passed so enrichment can resolve the typed Role/Scope of a pair whose sides came from
            # different services (a conflict may join a new rule to a stored one). Policy source is
            # read only on this path.
            #
            # Enrichment (policy fetch + LLM explain/quote) is best-effort: if the policy source is
            # unreadable/missing or the explain pass errors, we still raise with the STRUCTURAL
            # report (ADR 0001: surface, never drop). Letting the error escape here would bypass the
            # controller's PolicyConflictError handler and return a 500 instead of the required 422
            # ConflictReport.
            try:
                report = enrich_report(report, combined, get_policy_source().fetch())
            except Exception:
                # Best-effort only (ADR 0001: surface, never drop). Log at WARNING with the
                # traceback so a genuine bug in enrich_report (TypeError/AttributeError) is
                # distinguishable in production from an expected "policy source unavailable",
                # rather than being silently swallowed. We still raise the STRUCTURAL report.
                logger.warning("policy conflict enrichment failed; falling back to structural report", exc_info=True)
            raise PolicyConflictError(report)
        # Only this build's own rules are applied; the OTHER services' rules read above are already
        # persisted and are used solely to widen detection, never re-emitted.
        return rules
