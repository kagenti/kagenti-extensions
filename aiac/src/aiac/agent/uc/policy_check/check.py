"""Policy Conflict Check survey use-case (feature #154, task #158).

A **sequential, read-only** survey that runs EVERY focal entity of a target service through the
Conflict-Check diagnostic graph (#157) to completion, accumulates every run's ``conflicts`` +
``unevaluated``, and returns a single :class:`ConflictReport`. Unlike the live ``/apply`` path,
the first genuine conflict does NOT abort the survey — all entities are always run, so the report
lists ALL of that service's conflicts at once.

This is the diagnostic counterpart of ``ServicePolicyBuilder.build()`` (the live fan-out loop in
``uc/onboarding/policy_builder/builder.py``): it resolves the SAME typed entity set via the shared
``resolve_focal_entities`` (#155) and mirrors that loop's fan-out EXACTLY (scope-focal over every
own scope; role-focal over every flattened own role, AGENT services only). It differs in three
deliberate ways:

  * it is READ-ONLY — it never runs Provision (which MUTATES the IdP) and never calls the PCE /
    orchestrator. The target service is a PRE-EXISTING catalog entry, so its ``service_type`` is
    read straight from the catalog (``focus.type``) rather than being (re)discovered by Provision.
  * it drives ``run_scope_diagnostic`` / ``run_role_diagnostic`` (the record-not-raise diagnostic
    graph), not ``build_scope_rules`` / ``build_role_rules`` (the live raise-on-conflict builder).
  * a found conflict is a SUCCESSFUL diagnosis, not an error — it is recorded, never raised. Only
    the pre-survey resolver boundary (``HTTPException(502)`` IdP-unreachable /
    ``HTTPException(404)`` unknown-service) propagates.

``service_type`` note (the one real design decision — done READ-ONLY): the resolver needs a
``service_type``, but we must NOT run Provision to (re)discover it. For a pre-existing catalog
entry ``focus.type`` IS the authoritative classification, so we resolve the focus service by
``id`` from the same ``get_services()`` catalog the resolver reads and take ``focus.type``. The
builder's "never conflate service_type with focus.type" caution applies ONLY to the live
onboarding path (where a NEW service's type is being discovered), not to this read-only diagnostic
over an existing service. The same ``Configuration`` seam (``focal_entities._config`` /
``Configuration.for_default_realm``) is reused for BOTH the type lookup and the resolver, so the
``HTTPException(502/404)`` pre-survey boundary is preserved and tests patch a single seam.
"""

from fastapi import HTTPException

from aiac.agent.policy_rules_builder.diagnostic import run_role_diagnostic, run_scope_diagnostic
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictReport
from aiac.agent.shared import focal_entities as _focal_entities
from aiac.agent.shared.focal_entities import resolve_focal_entities
from aiac.agent.shared.roles import flatten_role
from aiac.idp.configuration.models import ServiceType


def check_policy_conflicts(policy_text: str, service_id: str) -> ConflictReport:
    """Survey a target service's focal entities for grant/prohibit contradictions in ``policy_text``.

    ``service_id`` is the Keycloak internal client UUID (``Service.id``), matching the
    ``/apply/service/{service_id}`` route. Returns one :class:`ConflictReport` with every conflict
    found across ALL of the service's focal entities, every entity that could not be evaluated, and
    the derived ``status``. Never raises on a found conflict or a non-converging entity; only the
    resolver's pre-survey ``HTTPException(502/404)`` propagates.
    """
    # Reuse the resolver's own Configuration seam so a single patch point drives both the
    # read-only service_type lookup and resolve_focal_entities (and the 502/404 boundary).
    config = _focal_entities._config()

    # Read-only service_type derivation: the focus service pre-exists in the catalog, so its
    # own catalog type is authoritative. Wrap the lookup in the SAME 502/404 boundary the
    # resolver uses (it re-reads the catalog itself for the entity split).
    try:
        services = config.get_services()
    except Exception as e:
        raise HTTPException(
            502, f"IdP Configuration Service unavailable for service {service_id!r}: {e}"
        )
    focus = next((s for s in services if s.id == service_id), None)
    if focus is None:
        raise HTTPException(404, f"service {service_id!r} not found in IdP catalog")
    service_type = focus.type

    focal = resolve_focal_entities(service_id, service_type, config=config)

    all_conflicts = []
    all_unevaluated = []
    evaluated_count = 0

    def _accumulate(result) -> None:
        # An entity is "evaluated" iff its run did NOT land in unevaluated (a run yields either a
        # verdict — clean or recorded conflicts — or a nonconvergence mark, never both). This is
        # exactly the count ConflictReport.from_survey's precedence expects for the zero-evaluated
        # guard: >=1 evaluated + no conflicts + nothing unevaluated => no_conflict.
        nonlocal evaluated_count
        all_conflicts.extend(result.conflicts)
        all_unevaluated.extend(result.unevaluated)
        if not result.unevaluated:
            evaluated_count += 1

    # Fan-out mirrors builder.py EXACTLY. First conflict never aborts — every entity runs.
    for scope in focal.own_scopes:
        _accumulate(
            run_scope_diagnostic(
                policy_text, focal.candidate_roles, scope, focal_entities=focal
            )
        )
    if service_type is ServiceType.AGENT:
        for own_role in focal.own_roles:
            for role in flatten_role(own_role):
                _accumulate(
                    run_role_diagnostic(
                        policy_text, role, focal.other_scopes, focal_entities=focal
                    )
                )

    return ConflictReport.from_survey(all_conflicts, all_unevaluated, evaluated_count)
