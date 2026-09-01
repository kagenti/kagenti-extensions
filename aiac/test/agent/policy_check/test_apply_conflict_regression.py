"""Regression guard: the live ``/apply`` conflict path is UNCHANGED by the conflict-check diagnostic.

This test is **deterministic** (NOT marked ``integration`` / ``llm``) so it runs in the default
suite and under ``-m "not integration"``. Feature #154's design rests on D1/D8 -- the read-only
diagnostic is a *separate* assembly and the safety-critical live ``/apply`` graph stays
byte-for-byte unchanged, still **raising** ``PolicyContradictionError`` -> HTTP 422 on a genuine
grant/deny contradiction (the diagnostic *records* instead). This guard pins both ends of that live
contract, independent of the #159 ``/policy/check`` route:

  1. **Builder level** -- with ``graph._structured_call`` patched so the auditor returns a genuine
     ``Contradiction``, ``build_role_rules`` RAISES ``PolicyContradictionError`` and returns no rule
     set (fail-closed). Mirrors ``test_graph.py``'s genuine-overlap slice.
  2. **Route level** -- ``POST /apply/service/{id}`` maps that ``PolicyContradictionError`` to HTTP
     422 and never reaches the PCE. ``onboard_service`` is patched to raise (so no cluster / LLM is
     needed), mirroring how ``test/agent/controller/test_routes.py`` drives ``/apply``.
"""

from contextlib import ExitStack
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aiac.agent.controller.routes import app
from aiac.agent.policy_rules_builder.graph import (
    AuditVerdict,
    Contradiction,
    PolicyContradictionError,
    RoleSelection,
    build_role_rules,
)
from aiac.idp.configuration.models import Role, Scope

client = TestClient(app)


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
        stack.enter_context(
            patch("aiac.agent.policy_rules_builder.graph.get_policy_source", return_value=_Source())
        )
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


def test_apply_service_maps_policy_contradiction_to_422_and_skips_pce():
    # The Controller maps a PolicyContradictionError raised inside the onboarding handler to HTTP 422
    # (a policy finding, not a 500), and the PCE is never reached. onboard_service is patched to raise
    # directly so the route mapping is exercised without a cluster or the LLM.
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            side_effect=PolicyContradictionError("focal-svc", []),
        ),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-conflict")

    assert resp.status_code == 422
    pce.assert_not_called()
