"""Apply-conflict guards: two disjoint mechanisms, both leaving persisted state untouched.

Deterministic (NOT ``integration`` / ``llm``) so it runs under ``-m "not integration"``. This
file pins BOTH grant/deny mechanisms and proves each one raises **before** the PCE, so a policy
problem never mutates persisted state:

  A. **Intra-pass ``PolicyContradictionError``** (the LLM auditor, ``graph.py``) — a self-
     contradicting single pass fails **closed** (raises, withholds its whole rule set). Kept
     valid unchanged: it is a *separate* mechanism from the structural detector (#2502), and the
     existing route handler still maps it to HTTP 422 without reaching the PCE.
  B. **Cross-pass structural ``PolicyConflictError``** (#2502) — after ``ServicePolicyBuilder.build``
     assembles every pass's rules, the pure ``detect_conflicts`` allow∩deny intersection surfaces
     a ``(role, scope)`` that is both granted and prohibited and **raises inside the build**,
     before the Orchestrator/Controller reach ``compute_and_apply`` (atomic-by-construction). This
     is the seed repurposed from the former apply-vs-diagnostic guard: its old premise (the #154
     read-only diagnostic doesn't touch ``/apply``) is moot after ``/policy/check`` was retired
     (#2500); it now guards the inline structural raise instead.

Both are proved with the apply seam (``compute_and_apply`` / the PCE) patched and asserted
**never called** on a conflict — the atomic proof — and the LLM/cluster stubbed out.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aiac.agent.controller.routes import app
from aiac.agent.policy_rules_builder.conflict_detection import PolicyConflictError
from aiac.agent.policy_rules_builder.diagnostic import ExplainResult
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictKind, ConflictStatus, FocalType
from aiac.agent.policy_rules_builder.graph import (
    AuditVerdict,
    Contradiction,
    PolicyContradictionError,
    RoleSelection,
    build_role_rules,
)
from aiac.agent.shared.focal_entities import FocalEntitySet
from aiac.agent.uc.onboarding.policy_builder.builder import ServicePolicyBuilder
from aiac.idp.configuration.models import Role, RoleKind, Scope, ServiceType
from aiac.policy.model.models import PolicyRule, RuleEffect

client = TestClient(app)
# Server-side exceptions with no route handler surface as 500 rather than propagating, so the
# atomic proof can assert on the PCE seam regardless of how #2503 later wires the 422 body.
tolerant_client = TestClient(app, raise_server_exceptions=False)

_BUILDER = "aiac.agent.uc.onboarding.policy_builder.builder"
# The explain/LLM seam the enrichment pass runs through — patched here so the deterministic suite
# never touches a live endpoint, and asserted NEVER called on a clean apply.
_EXPLAIN_SEAM = "aiac.agent.policy_rules_builder.conflict_enrichment._explain_pair"
# The Policy-Store read #2504 added to widen detection across services. Patched to [] in the
# within-service cases below (no other services applied) so they need no live store and their
# structural assertions are unchanged; the cross-service cases patch it to return applied rules.
_APPLIED_SEAM = f"{_BUILDER}.applied_rules_for_scopes"

_TESTER = Role(id="r-tester", name="tester", composite=False, kind=RoleKind.USER)
_ISSUES = Scope(id="s-iss", name="issues")


def _focal_own_scope() -> FocalEntitySet:
    """A minimal focal set for a Tool onboarding: one own scope, one kind=User candidate role,
    no own roles / other scopes — enough to drive the scope-focal grant + Door B deny passes."""
    return FocalEntitySet(
        own_scopes=[_ISSUES],
        own_roles=[],
        candidate_roles=[_TESTER],
        other_scopes=[],
        service_type=ServiceType.TOOL,
    )


class _Source:
    """Stub PolicySource whose ``fetch()`` returns a fixed policy string (mirrors ``test_graph.py``)."""

    def __init__(self, text: str = "POLICY"):
        self.text = text

    def fetch(self) -> str:
        return self.text


def test_live_build_role_rules_still_raises_policy_contradiction():
    # A genuine grant/deny overlap on the same coarse candidate: the proposer lists `issues` in BOTH
    # its grant and prohibit lists; the auditor adjudicates it GENUINE. The live builder must RAISE
    # (fail-closed) -- the diagnostic's record-not-raise fork must not have leaked into this path.
    role = Role(id="r-dev", name="developer", composite=False)
    issues = Scope(id="s-iss", name="issues")

    with ExitStack() as stack:
        stack.enter_context(patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source()))
        stack.enter_context(
            patch(
                "aiac.agent.policy_rules_builder.graph._structured_call",
                side_effect=[
                    RoleSelection(
                        granted_scope_names=["issues"],
                        denied_scope_names=["issues"],
                        grant_is_exclusive=False,
                        reasoning="may read issues but must not modify them",
                    ),
                    AuditVerdict(
                        approved=False,
                        contradictions=[
                            Contradiction(
                                candidate_name="issues",
                                description="coarse-scope granularity mismatch: issues covers read and write",
                            )
                        ],
                    ),
                ],
            )
        )
        with pytest.raises(PolicyContradictionError) as exc:
            build_role_rules(role, [issues])

    # The raise carries the focal identity and the genuine contradiction(s) -- no rule set comes back.
    assert role.name in exc.value.focal
    assert [c.candidate_name for c in exc.value.contradictions] == ["issues"]


def test_apply_service_maps_policy_contradiction_to_422_conflict_report_and_skips_pce():
    # The Controller maps a PolicyContradictionError raised inside the onboarding handler to HTTP 422
    # (a policy finding, not a 500), and the PCE is never reached. The 422 body is now the SAME
    # ConflictReport shape as the structural detector (Q15) — re-shaped from the auditor's name-string
    # contradictions with NO LLM (lower fidelity: no ids, quotes_verified=False). onboard_service is
    # patched to raise directly so the route mapping is exercised without a cluster or the LLM.
    err = PolicyContradictionError(
        "scope name=issues: the issue tracker",
        [Contradiction(candidate_name="tester", description="granted and prohibited")],
    )
    with (
        patch("aiac.agent.controller.routes.onboard_service", side_effect=err),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-conflict")

    assert resp.status_code == 422
    pce.assert_not_called()
    # Body is a structured ConflictReport, not a bare {"detail": ...}.
    body = resp.json()
    assert body["status"] == ConflictStatus.CONFLICTS_FOUND.value
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert c["focal"]["type"] == FocalType.SCOPE.value
    assert c["scope"]["name"] == "issues" and c["role"]["name"] == "tester"
    assert c["quotes_verified"] is False and c["explanation"] == "granted and prohibited"


# --- Mechanism B: cross-pass structural PolicyConflictError (#2502) --------------------------


def test_build_raises_structural_conflict_from_assembled_passes():
    # The scope-focal pass grants (tester, issues) and the Door B deny pass prohibits the SAME
    # (tester, issues): the assembled list carries both an Allow and a Deny on one pair. build()
    # must run the inline detector and RAISE PolicyConflictError — never reconcile (ADR 0001).
    # Enrichment is stubbed to identity here (the explain seam / policy source are exercised by the
    # dedicated enrichment test below); this pins the STRUCTURAL raise + report shape.
    with (
        patch(f"{_BUILDER}._config", return_value=MagicMock()),
        patch(f"{_BUILDER}.resolve_focal_entities", return_value=_focal_own_scope()),
        patch(
            f"{_BUILDER}.build_scope_rules",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        ),
        patch(
            f"{_BUILDER}.build_role_denies",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.DENY)],
        ),
        patch(f"{_BUILDER}.get_policy_source", return_value=_Source()),
        patch(_APPLIED_SEAM, return_value=[]),
        patch(f"{_BUILDER}.enrich_report", side_effect=lambda report, rules, text: report),
    ):
        with pytest.raises(PolicyConflictError) as exc:
            ServicePolicyBuilder.build("svc-tool", ServiceType.TOOL)

    report = exc.value.report
    assert report.status is ConflictStatus.CONFLICTS_FOUND
    assert len(report.conflicts) == 1
    c = report.conflicts[0]
    assert (c.role.id, c.scope.id) == ("r-tester", "s-iss")
    assert c.focal.type is FocalType.SCOPE
    assert c.quotes_verified is False


def test_conflicting_build_enriches_report_with_kind_and_verbatim_quotes():
    # On a detected conflict, build() runs the enrichment pass over the structural report: the
    # explain seam classifies the kind and returns quotes, which the engine substring-validates
    # against the candidate policy text. The raised report carries the enriched conflict.
    policy = "Testers may access issues. Testers must not access issues."
    explained = ExplainResult(
        kind=ConflictKind.COARSE_SCOPE,
        granting_quotes=["Testers may access issues."],
        prohibiting_quotes=["Testers must not access issues."],
        explanation="issues is both granted and prohibited for tester",
    )
    with (
        patch(f"{_BUILDER}._config", return_value=MagicMock()),
        patch(f"{_BUILDER}.resolve_focal_entities", return_value=_focal_own_scope()),
        patch(
            f"{_BUILDER}.build_scope_rules",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        ),
        patch(
            f"{_BUILDER}.build_role_denies",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.DENY)],
        ),
        patch(f"{_BUILDER}.get_policy_source", return_value=_Source(policy)),
        patch(_APPLIED_SEAM, return_value=[]),
        patch(_EXPLAIN_SEAM, return_value=explained) as explain,
    ):
        with pytest.raises(PolicyConflictError) as exc:
            ServicePolicyBuilder.build("svc-tool", ServiceType.TOOL)

    explain.assert_called_once()  # exactly one explain call for the one conflicting pair
    c = exc.value.report.conflicts[0]
    assert c.kind is ConflictKind.COARSE_SCOPE
    assert c.granting_quotes == ["Testers may access issues."]
    assert c.prohibiting_quotes == ["Testers must not access issues."]
    assert c.quotes_verified is True
    assert c.explanation == "issues is both granted and prohibited for tester"


def test_conflicting_build_falls_back_when_quotes_not_verbatim():
    # A non-verbatim quote (not a substring of the policy) fails validation: the conflict is KEPT
    # with quotes_verified=False and the explanation falls back to the structural synthesized one.
    policy = "Testers may access issues. Testers must not access issues."
    explained = ExplainResult(
        kind=ConflictKind.DIRECT,
        granting_quotes=["Testers are allowed full access"],  # paraphrase — NOT in the policy
        prohibiting_quotes=[],
        explanation="fabricated wording",
    )
    with (
        patch(f"{_BUILDER}._config", return_value=MagicMock()),
        patch(f"{_BUILDER}.resolve_focal_entities", return_value=_focal_own_scope()),
        patch(
            f"{_BUILDER}.build_scope_rules",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        ),
        patch(
            f"{_BUILDER}.build_role_denies",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.DENY)],
        ),
        patch(f"{_BUILDER}.get_policy_source", return_value=_Source(policy)),
        patch(_APPLIED_SEAM, return_value=[]),
        patch(_EXPLAIN_SEAM, return_value=explained),
    ):
        with pytest.raises(PolicyConflictError) as exc:
            ServicePolicyBuilder.build("svc-tool", ServiceType.TOOL)

    c = exc.value.report.conflicts[0]
    assert c.quotes_verified is False
    assert c.explanation != "fabricated wording"  # fell back to the structural description
    assert "tester" in c.explanation and "issues" in c.explanation


def _drive_apply_with_passes(scope_rules, deny_rules, applied=None) -> tuple[MagicMock, MagicMock]:
    """Drive ``POST /apply/service/{id}`` through the REAL onboarding sequence (provision graph
    stubbed to a Tool, the two PRB passes stubbed to the given rule lists, LLM/cluster untouched)
    and return ``(compute_and_apply, explain_seam)`` mocks so the caller can assert on the PCE seam
    AND that the explain/LLM enrichment seam fired only when a conflict was detected. ``applied`` is
    the OTHER services' already-applied rules the #2504 store read returns (default ``[]`` — a
    single-service apply); pass a conflicting rule to exercise the cross-service path. The policy
    source is stubbed and the explain seam returns a fixed ExplainResult, so no live endpoint or
    on-disk policy file is touched even on the conflicting path."""
    provision = MagicMock()
    # The provision graph now also returns the created-manifest (issue 171); onboard_service reads
    # both keys. Empty here — these cases pin the PCE/enrichment seams, not the UC1 rollback.
    provision.invoke.return_value = {
        "service_type": ServiceType.TOOL,
        "created_roles": [],
        "created_scopes": [],
    }
    explained = ExplainResult(kind=ConflictKind.DIRECT, granting_quotes=[], prohibiting_quotes=[])
    with (
        patch("aiac.agent.uc.onboarding.orchestrator.build_provision_graph", return_value=provision),
        # The Orchestrator's IdP seam: the success path re-enables the client and the failure path
        # rolls back through it (issue 171). Stubbed so neither touches a live IdP.
        patch("aiac.agent.uc.onboarding.orchestrator._config", return_value=MagicMock()),
        patch(f"{_BUILDER}._config", return_value=MagicMock()),
        patch(f"{_BUILDER}.resolve_focal_entities", return_value=_focal_own_scope()),
        patch(f"{_BUILDER}.build_scope_rules", return_value=scope_rules),
        patch(f"{_BUILDER}.build_role_denies", return_value=deny_rules),
        patch(f"{_BUILDER}.get_policy_source", return_value=_Source()),
        patch(_APPLIED_SEAM, return_value=applied or []),
        patch(_EXPLAIN_SEAM, return_value=explained) as explain,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        tolerant_client.post("/apply/service/svc-tool")
    return pce, explain


def test_conflict_raises_before_compute_and_apply_is_atomic():
    # ATOMIC PROOF: a conflicting build (Allow + Deny on the same pair) short-circuits inside
    # build() — the PCE (the persistence seam) is provably NEVER reached, so a conflict leaves
    # persisted state untouched. This exercises the real detect_conflicts, not a patched raise.
    pce, explain = _drive_apply_with_passes(
        scope_rules=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        deny_rules=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.DENY)],
    )
    pce.assert_not_called()
    explain.assert_called_once()  # enrichment fired for the one conflicting pair


def test_clean_build_reaches_compute_and_apply_without_touching_the_llm_seam():
    # Control: the SAME path with no allow∩deny overlap (Door B contributes no deny) is clean —
    # the build returns and the PCE IS reached. Proves the raise is conditional on a real conflict,
    # that clean policies still apply, AND that the explain/LLM enrichment seam is NEVER called on a
    # clean apply (enrichment is gated strictly behind a detected structural conflict).
    pce, explain = _drive_apply_with_passes(
        scope_rules=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        deny_rules=[],
    )
    pce.assert_called_once()
    explain.assert_not_called()


# --- Cross-service structural conflict (#2504) -----------------------------------------------
#
# The SAME structural mechanism (B), but the colliding Deny comes from ANOTHER service's already-
# applied rules (the store read the #2504 gatherer performs), not from this build's Door B pass.
# This build grants (tester, issues); the store already carries a Deny another service applied on
# the SAME (role.id, scope.id). Neither side alone reveals it — only the union that build() feeds
# to detect_conflicts does. These pin that cross-service detection raises with the same shape,
# fires enrichment on the joined pair, and stays atomic (PCE never reached), while a clean cross-
# service apply reaches the PCE with the LLM seam untouched.

# Another service's already-applied Deny on the same scope id (returned by the store read seam).
_APPLIED_DENY = PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.DENY)


def test_build_raises_cross_service_conflict_from_applied_store_rules():
    # This build's ONLY rule is an Allow (Door B contributes no deny), so its own rule set is clean.
    # The store read returns another service's applied Deny on the same (tester, issues): build()
    # unions the two and the #2502 core surfaces the overlap and RAISES — proving detection sees
    # across services, not just within one build. Enrichment fires over the joined pair.
    policy = "Testers may access issues."
    explained = ExplainResult(
        kind=ConflictKind.DIRECT,
        granting_quotes=["Testers may access issues."],
        prohibiting_quotes=[],
        explanation="issues is granted here but already prohibited for tester",
    )
    with (
        patch(f"{_BUILDER}._config", return_value=MagicMock()),
        patch(f"{_BUILDER}.resolve_focal_entities", return_value=_focal_own_scope()),
        patch(
            f"{_BUILDER}.build_scope_rules",
            return_value=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        ),
        patch(f"{_BUILDER}.build_role_denies", return_value=[]),
        patch(f"{_BUILDER}.get_policy_source", return_value=_Source(policy)),
        patch(_APPLIED_SEAM, return_value=[_APPLIED_DENY]),
        patch(_EXPLAIN_SEAM, return_value=explained) as explain,
    ):
        with pytest.raises(PolicyConflictError) as exc:
            ServicePolicyBuilder.build("svc-tool", ServiceType.TOOL)

    explain.assert_called_once()  # enrichment fired for the one cross-service pair
    report = exc.value.report
    assert report.status is ConflictStatus.CONFLICTS_FOUND
    assert len(report.conflicts) == 1
    c = report.conflicts[0]
    assert (c.role.id, c.scope.id) == ("r-tester", "s-iss")
    assert c.focal.type is FocalType.SCOPE


def test_cross_service_conflict_raises_before_compute_and_apply_is_atomic():
    # ATOMIC PROOF (cross-service): build() grants (tester, issues) with no own deny, but the store
    # already carries another service's Deny on the same pair. The union conflicts, so build() short-
    # circuits and the PCE is provably NEVER reached — a cross-service conflict leaves persisted state
    # untouched. Exercises the real detect_conflicts over the combined set, not a patched raise.
    pce, explain = _drive_apply_with_passes(
        scope_rules=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        deny_rules=[],
        applied=[_APPLIED_DENY],
    )
    pce.assert_not_called()
    explain.assert_called_once()  # enrichment fired for the one cross-service pair


def test_clean_cross_service_apply_reaches_pce_without_touching_the_llm_seam():
    # Control (cross-service): this build grants (tester, issues) and another service's applied rules
    # are disjoint (a deny on a DIFFERENT role). The union has no allow∩deny overlap — the build
    # returns, the PCE IS reached, and the explain/LLM seam is NEVER called. Proves a clean multi-
    # service apply stays deterministic and LLM-free.
    other_role = Role(id="r-dev", name="developer", composite=False, kind=RoleKind.USER)
    pce, explain = _drive_apply_with_passes(
        scope_rules=[PolicyRule(role=_TESTER, scope=_ISSUES, effect=RuleEffect.ALLOW)],
        deny_rules=[],
        applied=[PolicyRule(role=other_role, scope=_ISSUES, effect=RuleEffect.DENY)],
    )
    pce.assert_called_once()
    explain.assert_not_called()
