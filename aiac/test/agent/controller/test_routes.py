"""Unit tests for aiac.agent.controller.routes (the Controller).

The orchestrator/sub-agent handlers and the Policy Computation Engine are
mocked at the routes module boundary — no live services, no real graphs.
"""

import os
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from aiac.agent.controller.routes import app
from aiac.agent.policy_rules_builder.conflict_detection import (
    PolicyConflictError,
    detect_conflicts,
)
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictStatus, FocalType
from aiac.agent.policy_rules_builder.graph import (
    Contradiction,
    LLMAccessError,
    PolicyContradictionError,
    PolicyRulesBuilderBaseError,
    PolicyRulesBuilderError,
    UnparseableLLMResponseError,
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
        patch("aiac.agent.controller.routes.reenable_service") as reenable,
        patch.dict("os.environ", {}, clear=False) as _env,
    ):
        os.environ.pop("AIAC_DEFAULT_EFFECT", None)
        resp = client.post("/apply/service/svc-123")

    assert resp.status_code == 200
    orch.assert_called_once_with("svc-123", RuleEffect.DENY)
    # The onboard route forwards the orchestrator's default_effect to the PCE (least-privilege here).
    pce.assert_called_once_with([], False, RuleEffect.DENY)
    # The client is re-enabled only after the PCE apply succeeds.
    reenable.assert_called_once_with("svc-123")


def test_apply_service_default_effect_env_allow_reaches_orchestrator_and_pce():
    # The #149 harness patches AIAC_DEFAULT_EFFECT=Allow onto the Controller before onboarding;
    # the on-ramp translates it to RuleEffect.ALLOW and threads it to onboard_service + the PCE.
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            return_value=([], False, RuleEffect.ALLOW),
        ) as orch,
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
        patch("aiac.agent.controller.routes.reenable_service"),
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
        patch("aiac.agent.controller.routes.reenable_service"),
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
        patch("aiac.agent.controller.routes.reenable_service"),
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


def test_apply_service_does_not_reenable_when_pce_apply_raises():
    # The re-enable is strictly post-apply: if compute_and_apply raises, the route never reaches
    # reenable_service, so the client stays disabled (the failed-service marker) — never left
    # enabled with no applied policy.
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            return_value=([], False, RuleEffect.DENY),
        ),
        patch(
            "aiac.agent.controller.routes.compute_and_apply",
            side_effect=HTTPException(status_code=500),
        ),
        patch("aiac.agent.controller.routes.reenable_service") as reenable,
    ):
        resp = client.post("/apply/service/svc-pce-boom")

    assert resp.status_code == 500
    reenable.assert_not_called()


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


def test_policy_conflict_error_surfaces_422_with_conflict_report_body_and_skips_pce():
    # A structural PolicyConflictError (the cross-pass detector, already enriched) surfaces as 422
    # whose BODY IS the structured ConflictReport (not a bare {"detail": ...}), and the PCE is never
    # reached. onboard_service raises the carrier exception so the route handler is exercised.
    tester = Role(id="r-tester", name="tester", composite=False)
    issues = Scope(id="s-iss", name="issues")
    report = detect_conflicts(
        [
            PolicyRule(role=tester, scope=issues, effect=RuleEffect.ALLOW),
            PolicyRule(role=tester, scope=issues, effect=RuleEffect.DENY),
        ]
    )
    with (
        patch(
            "aiac.agent.controller.routes.onboard_service",
            side_effect=PolicyConflictError(report),
        ),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-conflict")

    assert resp.status_code == 422
    pce.assert_not_called()
    body = resp.json()
    assert body["status"] == ConflictStatus.CONFLICTS_FOUND.value
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert (c["role"]["id"], c["scope"]["id"]) == ("r-tester", "s-iss")
    assert c["focal"]["type"] == FocalType.SCOPE.value


def test_policy_contradiction_error_surfaces_422_conflict_report_and_skips_pce():
    # A genuine intra-pass grant/deny contradiction (the LLM auditor) is likewise a policy finding
    # surfaced as 422 — and mapped into the SAME ConflictReport body shape (Q15), with no LLM at the
    # boundary (lower fidelity: no ids, quotes_verified=False, auditor description as explanation).
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
    body = resp.json()
    assert body["status"] == ConflictStatus.CONFLICTS_FOUND.value
    assert body["conflicts"][0]["scope"]["name"] == "issues"
    assert body["conflicts"][0]["quotes_verified"] is False


# --------------------------------------------------------------------------- #
# Sanitized PRB-error mapping (#172): status per exception type + leak-free body.
# A poisoned endpoint/host/API-key is fed into each raised error; the client body
# must never echo it — full detail goes to the named loggers via log_by_type.     #
# --------------------------------------------------------------------------- #
_LEAK_ENDPOINT = "http://secret-endpoint.internal:9443"
_LEAK_HOST = "secret-host.example"
_LEAK_KEY = "sk-DEADBEEFcafef00d"
_POISON = f"unreachable at {_LEAK_ENDPOINT} host={_LEAK_HOST} api_key={_LEAK_KEY}"


def _assert_sanitized(resp, expected_status: int) -> None:
    # The body is exactly a {"detail": <safe summary>} envelope with no leaked substring.
    assert resp.status_code == expected_status
    body = resp.json()
    assert set(body.keys()) == {"detail"}
    assert isinstance(body["detail"], str) and body["detail"]
    for secret in (_LEAK_ENDPOINT, _LEAK_HOST, _LEAK_KEY):
        assert secret not in resp.text


def test_llm_access_error_surfaces_502_with_sanitized_body_and_no_leak():
    # An unreachable LLM endpoint after the transport retry budget is a bad-gateway condition
    # (502), and the poisoned endpoint/host/key never reaches the client body.
    err = LLMAccessError(_POISON)
    with (
        patch("aiac.agent.controller.routes.onboard_service", side_effect=err),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-llm")

    _assert_sanitized(resp, 502)
    pce.assert_not_called()


def test_unparseable_llm_response_error_surfaces_502_with_sanitized_body_and_no_leak():
    # A reachable-but-unparseable LLM response is likewise an upstream fault (502), sanitized.
    err = UnparseableLLMResponseError(_POISON)
    with (
        patch("aiac.agent.controller.routes.onboard_service", side_effect=err),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-llm")

    _assert_sanitized(resp, 502)
    pce.assert_not_called()


def test_policy_rules_builder_error_body_is_sanitized_to_detail_without_leak():
    # PolicyRulesBuilderError stays 422 (policy-input problem) but its body is now a leak-free
    # summary, not str(exc) — a poisoned message must not escape to the client.
    err = PolicyRulesBuilderError(f"Auditor rejected after 3 retries: {_POISON}")
    with (
        patch("aiac.agent.controller.routes.onboard_service", side_effect=err),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-reject")

    _assert_sanitized(resp, 422)
    pce.assert_not_called()


def test_policy_rules_builder_base_error_safety_net_surfaces_500():
    # A bare base error (an unmapped PRB subclass, or the base itself) hits the LAST-registered
    # safety-net handler → 500, sanitized. The specific subclass handlers still win (see the
    # 502/422 tests above), because Starlette resolves the most-specific registered handler.
    err = PolicyRulesBuilderBaseError(_POISON)
    with (
        patch("aiac.agent.controller.routes.onboard_service", side_effect=err),
        patch("aiac.agent.controller.routes.compute_and_apply") as pce,
    ):
        resp = client.post("/apply/service/svc-boom")

    _assert_sanitized(resp, 500)
    pce.assert_not_called()


def test_log_by_type_invoked_with_the_raised_error_for_each_sanitized_handler():
    # Every sanitized handler routes the FULL exception to the per-persona named loggers via
    # log_by_type(exc) — the client sees only the safe summary, the operator gets the detail.
    cases = [
        (LLMAccessError(_POISON), 502),
        (UnparseableLLMResponseError(_POISON), 502),
        (PolicyRulesBuilderError(_POISON), 422),
        (PolicyRulesBuilderBaseError(_POISON), 500),
    ]
    for err, expected_status in cases:
        with (
            patch("aiac.agent.controller.routes.onboard_service", side_effect=err),
            patch("aiac.agent.controller.routes.compute_and_apply"),
            patch("aiac.agent.controller.routes.log_by_type") as log,
        ):
            resp = client.post("/apply/service/svc-log")

        assert resp.status_code == expected_status
        log.assert_called_once_with(err)


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
