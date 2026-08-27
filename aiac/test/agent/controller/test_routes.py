"""Unit tests for aiac.agent.controller.routes (the Controller).

The orchestrator/sub-agent handlers and the Policy Computation Engine are
mocked at the routes module boundary — no live services, no real graphs.
"""

import os
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from aiac.agent.controller.routes import app
from aiac.agent.policy_rules_builder.graph import (
    PolicyContradictionError,
    PolicyRulesBuilderError,
)
from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import PolicyRule, RuleEffect

client = TestClient(app)


def _rule(role_id: str = "r-1", scope_id: str = "s-1") -> PolicyRule:
    return PolicyRule(
        role=Role(id=role_id, name="editor", composite=False),
        scope=Scope(id=scope_id, name="write"),
    )


def test_health_returns_ok_without_touching_handlers_or_pce():
    # Liveness/readiness: the Controller is stateless, so /health answers 200 on its own
    # without dispatching to any use-case handler or the PCE.
    with (
        patch("aiac.agent.controller.routes.onboard_service") as orch,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    orch.assert_not_called()
    pce.assert_not_called()


def test_apply_service_dispatches_to_orchestrator_and_calls_pce_once():
    # No AIAC_DEFAULT_EFFECT env → the on-ramp resolves DENY (today's least-privilege default),
    # which the route passes to onboard_service and forwards to the PCE.
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            return_value=([], False, RuleEffect.DENY),
        ) as orch,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
        patch.dict("os.environ", {}, clear=False) as _env,
    ):
        os.environ.pop("AIAC_DEFAULT_EFFECT", None)
        resp = client.post("/apply/service/svc-123")

    assert resp.status_code == 200
    orch.assert_called_once_with("svc-123", RuleEffect.DENY)
    # The onboard route forwards the orchestrator's default_effect to the PCE (least-privilege here).
    pce.assert_called_once_with([], False, RuleEffect.DENY)


def test_apply_service_default_effect_env_allow_reaches_orchestrator_and_pce():
    # The #149 harness patches AIAC_DEFAULT_EFFECT=Allow onto the Controller before onboarding;
    # the on-ramp translates it to RuleEffect.ALLOW and threads it to onboard_service + the PCE.
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            return_value=([], False, RuleEffect.ALLOW),
        ) as orch,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
        patch.dict("os.environ", {"AIAC_DEFAULT_EFFECT": "Allow"}, clear=False),
    ):
        resp = client.post("/apply/service/svc-123")

    assert resp.status_code == 200
    orch.assert_called_once_with("svc-123", RuleEffect.ALLOW)
    pce.assert_called_once_with([], False, RuleEffect.ALLOW)


def test_apply_service_default_effect_env_unrecognised_falls_back_to_deny():
    # A garbage/empty env value must not crash onboarding — it degrades to the safe DENY default.
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            return_value=([], False, RuleEffect.DENY),
        ) as orch,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
        patch.dict("os.environ", {"AIAC_DEFAULT_EFFECT": "banana"}, clear=False),
    ):
        resp = client.post("/apply/service/svc-123")

    assert resp.status_code == 200
    orch.assert_called_once_with("svc-123", RuleEffect.DENY)
    pce.assert_called_once_with([], False, RuleEffect.DENY)


def test_apply_policy_build_dispatches_to_build_subagent():
    with (
        patch("aiac.agent.controller.routes.build_policy", return_value=([], False)) as build,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/policy/build")

    assert resp.status_code == 200
    build.assert_called_once_with()
    pce.assert_called_once_with([], False)


def test_apply_policy_rebuild_dispatches_to_rebuild_subagent():
    with (
        patch("aiac.agent.controller.routes.rebuild_policy", return_value=([], True)) as rebuild,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/policy/rebuild")

    assert resp.status_code == 200
    rebuild.assert_called_once_with()
    pce.assert_called_once_with([], True)


def test_apply_role_dispatches_to_role_subagent_with_role_id():
    with (
        patch("aiac.agent.controller.routes.update_role", return_value=([], True)) as role,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/role/role-42")

    assert resp.status_code == 200
    role.assert_called_once_with("role-42")
    pce.assert_called_once_with([], True)


def test_apply_offboard_dispatches_to_decommission_with_client_id():
    # Offboard resolves the service key through the UC stub then calls decommission directly
    # (no compute_and_apply — it is a whole-service teardown, not a rule fold).
    with (
        patch("aiac.agent.controller.routes.offboard_service", side_effect=lambda s: s) as off,
        patch("aiac.agent.controller.routes.decommission") as dec,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/offboard/github-tool")

    assert resp.status_code == 200
    off.assert_called_once_with("github-tool")
    dec.assert_called_once_with("github-tool")
    pce.assert_not_called()


def test_apply_offboard_carries_slash_bearing_spiffe_client_id():
    # The {service_id:path} converter must pass a slash-bearing SPIFFE-URI clientId through intact.
    spiffe_id = "spiffe://cluster.local/ns/team1/sa/github-tool"
    with (
        patch("aiac.agent.controller.routes.offboard_service", side_effect=lambda s: s),
        patch("aiac.agent.controller.routes.decommission") as dec,
    ):
        resp = client.post(f"/apply/offboard/{spiffe_id}")

    assert resp.status_code == 200
    dec.assert_called_once_with(spiffe_id)


def test_controller_forwards_handler_rules_and_override_verbatim():
    rules = [_rule("r-a"), _rule("r-b")]
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            return_value=(rules, False, RuleEffect.DENY),
        ),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-9")

    assert resp.status_code == 200
    # Exactly one PCE call, with the handler's own rules object and flag — not a rebuilt/empty one.
    pce.assert_called_once_with(rules, False, RuleEffect.DENY)
    forwarded_rules, forwarded_override, forwarded_default_effect = pce.call_args.args
    assert forwarded_rules is rules
    assert forwarded_override is False
    assert forwarded_default_effect is RuleEffect.DENY


def test_handler_upstream_error_surfaces_status_and_skips_pce():
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            side_effect=HTTPException(status_code=502),
        ),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-boom")

    assert resp.status_code == 502
    pce.assert_not_called()


def test_policy_rules_builder_error_surfaces_422_and_skips_pce():
    # The PRB auditor rejecting the proposed rules after its retry budget is a policy-input
    # problem, not a server fault — the Controller maps it to 422, not an uncaught 500, and the
    # PCE is never reached (the raise fires during rule construction inside the handler).
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            side_effect=PolicyRulesBuilderError("Auditor rejected after 3 retries: no grants"),
        ),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-reject")

    assert resp.status_code == 422
    pce.assert_not_called()


def test_policy_contradiction_error_surfaces_422_and_skips_pce():
    # A genuine grant/deny contradiction is likewise a policy finding surfaced as 422.
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


# --------------------------------------------------------------------------- #
# Stub contract: the per-route override each handler returns (no mocks).       #
# --------------------------------------------------------------------------- #
def test_build_policy_stub_returns_no_rules_and_override_false():
    from aiac.agent.uc.policy_update.build import build_policy

    assert build_policy() == ([], False)


def test_rebuild_policy_stub_returns_no_rules_and_override_true():
    from aiac.agent.uc.policy_update.rebuild import rebuild_policy

    assert rebuild_policy() == ([], True)


def test_update_role_stub_returns_no_rules_and_override_true():
    from aiac.agent.uc.role_update.role import update_role

    assert update_role("role-1") == ([], True)


def test_offboard_service_stub_returns_client_id_unchanged():
    from aiac.agent.uc.offboarding.offboard import offboard_service

    # Keyed by the clientId (SPM key), returned verbatim — including slash-bearing SPIFFE URIs.
    assert offboard_service("github-tool") == "github-tool"
    assert (
        offboard_service("spiffe://cluster.local/ns/team1/sa/github-tool")
        == "spiffe://cluster.local/ns/team1/sa/github-tool"
    )
