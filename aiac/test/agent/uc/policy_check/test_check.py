"""Deterministic unit tests for the Policy Conflict Check survey use-case (#158).

The survey drives the #157 diagnostic graph over EVERY focal entity of a target service. Two
seams are patched, exactly as the existing suites do:

  * the IdP catalog seam ``focal_entities._config`` (a ``MagicMock`` backs ``get_services`` /
    ``get_subjects``) — the SAME seam ``test_focal_entities.py`` / ``test_builder.py`` use, reused
    for both the read-only ``service_type`` lookup and ``resolve_focal_entities``;
  * the LLM seam ``graph._structured_call`` — every proposer / auditor / explain turn of every
    entity flows through it, so ONE ``side_effect`` (dispatched on the requested schema) drives all
    of them (mirrors ``test_diagnostic.py`` / ``test_graph.py``). No live LLM, no cluster.

Coverage: clean policy over >=1 evaluated entity -> ``no_conflict``; ALL conflicts across ALL
focal entities in one report with the first conflict NOT aborting; a non-converging entity ->
``unevaluated`` with status != ``no_conflict``; the ``unevaluated`` disjunct load-bearing even with
an evaluated entity; zero focal entities -> ``incomplete`` (never ``no_conflict``, no LLM call); the
AGENT role-focal fan-out; and the resolver's ``HTTPException(502/404)`` pre-survey boundary.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.policy_rules_builder.diagnostic import ExplainResult
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictKind, ConflictStatus
from aiac.agent.policy_rules_builder.graph import (
    AuditVerdict,
    Contradiction,
    RoleSelection,
    ScopeSelection,
)
from aiac.agent.shared import focal_entities
from aiac.agent.uc.policy_check.check import check_policy_conflicts
from aiac.idp.configuration.models import RoleKind, Scope, Service, ServiceType, Subject
from aiac.idp.configuration.models import Role as RoleModel

_SEAM = "aiac.agent.policy_rules_builder.graph._structured_call"

FOCUS_ID = "svc-focus"
OTHER_ID = "svc-other"


# --------------------------------------------------------------------------- #
# Fixture builders (mirror test_builder.py / test_focal_entities.py).          #
# --------------------------------------------------------------------------- #
def _role(name, *, role_id=None, composite=False, children=None, kind=RoleKind.USER, aiac_managed=True):
    return RoleModel(
        id=role_id or f"{name}-id",
        name=name,
        description=name,
        composite=composite,
        childRoles=children or [],
        attributes={"aiac.managed": ["true"]} if aiac_managed else {},
        kind=kind,
    )


def _scope(name, *, scope_id=None, service_id="", aiac_managed=True):
    return Scope(
        id=scope_id or f"{name}-id",
        name=name,
        description=name,
        attributes={"aiac.managed": "true"} if aiac_managed else {},
        serviceId=service_id,
    )


def _service(service_id, *, ref=None, roles=None, scopes=None, service_type=ServiceType.TOOL):
    return Service(
        id=service_id,
        serviceId=ref or service_id,
        enabled=True,
        type=service_type,
        roles=roles or [],
        scopes=scopes or [],
    )


def _subject(username, *, roles=None):
    return Subject(id=f"{username}-id", username=username, enabled=True, roles=roles or [])


def _run(policy, *, services, subjects=None, side_effect, service_id=FOCUS_ID):
    """Run ``check_policy_conflicts`` with both the catalog seam and the LLM seam patched."""
    conf = MagicMock()
    conf.get_services.return_value = services
    conf.get_subjects.return_value = subjects or []
    with (
        patch.object(focal_entities, "_config", return_value=conf),
        patch(_SEAM, side_effect=side_effect),
    ):
        return check_policy_conflicts(policy, service_id)


# --------------------------------------------------------------------------- #
# 1 — clean policy over >=1 evaluated entity => no_conflict.                    #
# --------------------------------------------------------------------------- #
def test_clean_policy_over_one_entity_is_no_conflict():
    focus = _service(FOCUS_ID, scopes=[_scope("reports", service_id=FOCUS_ID)])
    other = _service(OTHER_ID, roles=[_role("intern", kind=RoleKind.AGENT)])

    def se(schema, messages):
        if schema is ScopeSelection:
            return ScopeSelection(roles_with_access_names=["intern"], reasoning="ok")
        if schema is AuditVerdict:
            return AuditVerdict(approved=True)
        raise AssertionError(f"unexpected schema {schema}")

    report = _run("Interns may access reports.", services=[focus, other], side_effect=se)

    assert report.status is ConflictStatus.NO_CONFLICT
    assert report.conflicts == []
    assert report.unevaluated == []


# --------------------------------------------------------------------------- #
# 2 — ALL conflicts across ALL focal entities in ONE report; the first         #
#     conflict does NOT abort (two own scopes, each surfacing a conflict).      #
# --------------------------------------------------------------------------- #
def test_all_conflicts_reported_first_conflict_does_not_abort():
    policy = "Interns may access reports. Interns may not access reports."
    focus = _service(
        FOCUS_ID,
        scopes=[_scope("scope-a", service_id=FOCUS_ID), _scope("scope-b", service_id=FOCUS_ID)],
    )
    other = _service(OTHER_ID, roles=[_role("intern", kind=RoleKind.AGENT)])

    def se(schema, messages):
        if schema is ScopeSelection:
            return ScopeSelection(
                roles_with_access_names=["intern"],
                roles_denied_access_names=["intern"],
                reasoning="both granted and denied",
            )
        if schema is AuditVerdict:
            return AuditVerdict(
                approved=False,
                contradictions=[Contradiction(candidate_name="intern", description="direct conflict")],
            )
        if schema is ExplainResult:
            return ExplainResult(
                kind=ConflictKind.DIRECT,
                granting_quotes=["Interns may access reports."],
                prohibiting_quotes=["Interns may not access reports."],
                explanation="the same access is both granted and prohibited",
            )
        raise AssertionError(f"unexpected schema {schema}")

    report = _run(policy, services=[focus, other], side_effect=se)

    assert report.status is ConflictStatus.CONFLICTS_FOUND
    # BOTH own-scope entities ran and each surfaced a conflict — the first did not abort the survey.
    assert len(report.conflicts) == 2
    assert {c.focal.name for c in report.conflicts} == {"scope-a", "scope-b"}
    for c in report.conflicts:
        assert c.kind is ConflictKind.DIRECT
        assert c.quotes_verified is True
        assert (c.role.name, c.scope.name) == ("intern", c.focal.name)


# --------------------------------------------------------------------------- #
# 3 — a non-converging entity appears under unevaluated; status != no_conflict. #
# --------------------------------------------------------------------------- #
def test_nonconverging_entity_is_unevaluated_and_not_no_conflict():
    focus = _service(FOCUS_ID, scopes=[_scope("reports", service_id=FOCUS_ID)])
    other = _service(OTHER_ID, roles=[_role("intern", kind=RoleKind.AGENT)])

    def se(schema, messages):
        if schema is ScopeSelection:
            return ScopeSelection(roles_with_access_names=["intern"], reasoning="r")
        if schema is AuditVerdict:
            return AuditVerdict(approved=False, reason="still not right")
        raise AssertionError(f"unexpected schema {schema}")

    report = _run("Some policy about reports.", services=[focus, other], side_effect=se)

    assert report.status is not ConflictStatus.NO_CONFLICT
    assert report.status is ConflictStatus.INCOMPLETE
    assert report.conflicts == []
    assert len(report.unevaluated) == 1
    assert report.unevaluated[0].focal.name == "reports"
    assert report.unevaluated[0].reason.value == "nonconvergence"


# --------------------------------------------------------------------------- #
# 3b — the unevaluated disjunct is load-bearing: one entity evaluated clean +   #
#      one non-converging => incomplete (evaluated_count>=1 but unevaluated!=[]).#
# --------------------------------------------------------------------------- #
def test_mixed_evaluated_and_unevaluated_is_incomplete():
    focus = _service(
        FOCUS_ID,
        scopes=[_scope("clean-scope", service_id=FOCUS_ID), _scope("bad-scope", service_id=FOCUS_ID)],
    )
    other = _service(OTHER_ID, roles=[_role("intern", kind=RoleKind.AGENT)])

    def se(schema, messages):
        if schema is ScopeSelection:
            return ScopeSelection(roles_with_access_names=["intern"], reasoning="r")
        if schema is AuditVerdict:
            # The focal scope name is embedded in the auditor messages; bad-scope never converges.
            if "bad-scope" in str(messages):
                return AuditVerdict(approved=False, reason="nope")
            return AuditVerdict(approved=True)
        raise AssertionError(f"unexpected schema {schema}")

    report = _run("Policy about scopes.", services=[focus, other], side_effect=se)

    assert report.status is ConflictStatus.INCOMPLETE  # not no_conflict, despite one clean entity
    assert report.conflicts == []
    assert [u.focal.name for u in report.unevaluated] == ["bad-scope"]


# --------------------------------------------------------------------------- #
# 4 — zero focal entities => incomplete (never no_conflict); no LLM call.       #
# --------------------------------------------------------------------------- #
def test_zero_focal_entities_is_incomplete_never_no_conflict():
    focus = _service(FOCUS_ID)  # no own scopes, TOOL => no role-focal runs, no candidates

    def se(schema, messages):  # pragma: no cover - must never be reached
        raise AssertionError("no focal entities => the LLM seam must never be called")

    report = _run("Any candidate policy.", services=[focus], side_effect=se)

    assert report.status is ConflictStatus.INCOMPLETE
    assert report.conflicts == []
    assert report.unevaluated == []


# --------------------------------------------------------------------------- #
# 5 — AGENT service: the role-focal fan-out runs in addition to scope-focal.    #
# --------------------------------------------------------------------------- #
def test_agent_service_runs_scope_and_role_focal_entities():
    focus = _service(
        FOCUS_ID,
        roles=[_role("weather.agent")],
        scopes=[_scope("weather.forecast", service_id=FOCUS_ID)],
        service_type=ServiceType.AGENT,
    )
    other = _service(
        OTHER_ID,
        roles=[_role("github.agent", kind=RoleKind.AGENT)],
        scopes=[_scope("github.issue", service_id=OTHER_ID)],
    )
    seen_schemas = []

    def se(schema, messages):
        seen_schemas.append(schema)
        if schema is ScopeSelection:
            return ScopeSelection(roles_with_access_names=["github.agent"], reasoning="ok")
        if schema is RoleSelection:
            return RoleSelection(granted_scope_names=["github.issue"], reasoning="ok")
        if schema is AuditVerdict:
            return AuditVerdict(approved=True)
        raise AssertionError(f"unexpected schema {schema}")

    report = _run("Weather agent policy.", services=[focus, other], side_effect=se)

    assert report.status is ConflictStatus.NO_CONFLICT
    assert report.conflicts == []
    assert report.unevaluated == []
    # Both the scope-focal (ScopeSelection) and role-focal (RoleSelection) fan-outs were exercised.
    assert ScopeSelection in seen_schemas
    assert RoleSelection in seen_schemas


# --------------------------------------------------------------------------- #
# 6 — resolver pre-survey HTTP boundary propagates (unknown service / IdP down).#
# --------------------------------------------------------------------------- #
def test_unknown_service_raises_404():
    def se(schema, messages):  # pragma: no cover - resolution fails before any LLM turn
        raise AssertionError("unreachable")

    with pytest.raises(HTTPException) as ei:
        _run("policy", services=[_service(OTHER_ID)], side_effect=se, service_id="nope")
    assert ei.value.status_code == 404


def test_idp_unreachable_raises_502():
    conf = MagicMock()
    conf.get_services.side_effect = RuntimeError("HTTP 503")
    with (
        patch.object(focal_entities, "_config", return_value=conf),
        patch(_SEAM, side_effect=AssertionError("unreachable")),
    ):
        with pytest.raises(HTTPException) as ei:
            check_policy_conflicts("policy", FOCUS_ID)
    assert ei.value.status_code == 502
