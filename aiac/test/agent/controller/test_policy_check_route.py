"""Unit tests for the ``POST /policy/check`` conflict-check route (feature #154, task #159).

This is the diagnostic serialization shell: it calls the read-only survey use-case
(``check_policy_conflicts``) and serializes the returned :class:`ConflictReport` as a JSON
response body. The use-case is patched at the routes-module boundary — no live IdP, no LLM, no
real diagnostic graph. UNLIKE the live ``/apply`` path, a found conflict is a successful diagnosis
and returns 200 (never 422); only the survey's pre-survey ``HTTPException(502/404)`` propagates.
"""

from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from aiac.agent.controller.routes import app
from aiac.agent.policy_rules_builder.diagnostic_models import (
    Conflict,
    ConflictKind,
    ConflictReport,
    ConflictStatus,
    EntityRef,
    FocalRef,
    FocalType,
    Unevaluated,
    UnevaluatedReason,
)

client = TestClient(app)


def _focal(name: str = "editor", id: str = "r-1") -> FocalRef:
    return FocalRef(name=name, id=id, type=FocalType.ROLE)


def _conflict() -> Conflict:
    return Conflict(
        focal=_focal(),
        role=EntityRef(name="editor", id="r-1"),
        scope=EntityRef(name="write", id="s-1"),
        kind=ConflictKind.DIRECT,
        granting_quotes=["editors may write"],
        prohibiting_quotes=["editors must not write"],
        explanation="write is both granted and prohibited for editor",
        quotes_verified=True,
    )


def _unevaluated() -> Unevaluated:
    return Unevaluated(
        focal=_focal("viewer", "r-2"),
        reason=UnevaluatedReason.NONCONVERGENCE,
        detail="retry budget exhausted",
    )


def test_clean_report_returns_200_no_conflict():
    # A survey that evaluated ≥1 entity with nothing outstanding is a positive clean result.
    report = ConflictReport.from_survey([], [], evaluated_count=2)
    assert report.status is ConflictStatus.NO_CONFLICT
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts", return_value=report
    ):
        resp = client.post(
            "/policy/check", json={"policy_text": "editors may read", "service_id": "svc-1"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_conflict"
    assert body["conflicts"] == []
    assert body["unevaluated"] == []


def test_conflicts_found_returns_200_not_422():
    # A found conflict is a recorded diagnosis, NOT a policy-input error — it must be 200, unlike
    # the live /apply path which maps a contradiction to 422.
    report = ConflictReport.from_survey([_conflict()], [], evaluated_count=1)
    assert report.status is ConflictStatus.CONFLICTS_FOUND
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts", return_value=report
    ):
        resp = client.post(
            "/policy/check", json={"policy_text": "contradictory", "service_id": "svc-1"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "conflicts_found"
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert c["kind"] == "direct"
    assert c["role"]["name"] == "editor"
    assert c["scope"]["name"] == "write"
    assert c["granting_quotes"] == ["editors may write"]
    assert c["prohibiting_quotes"] == ["editors must not write"]


def test_unevaluated_present_returns_200_and_not_no_conflict():
    # A partial run (some entity did not converge) must never look clean — status is forced away
    # from no_conflict, and it is still a completed survey ⇒ 200.
    report = ConflictReport.from_survey([], [_unevaluated()], evaluated_count=1)
    assert report.status is not ConflictStatus.NO_CONFLICT
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts", return_value=report
    ):
        resp = client.post(
            "/policy/check", json={"policy_text": "some policy", "service_id": "svc-1"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] != "no_conflict"
    assert len(body["unevaluated"]) == 1
    assert body["unevaluated"][0]["reason"] == "nonconvergence"


def test_incomplete_zero_evaluated_returns_200():
    # Zero focal entities evaluated (empty-input / no-focal case) ⇒ incomplete, still 200.
    report = ConflictReport.from_survey([], [], evaluated_count=0)
    assert report.status is ConflictStatus.INCOMPLETE
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts", return_value=report
    ):
        resp = client.post(
            "/policy/check", json={"policy_text": "some policy", "service_id": "svc-1"}
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "incomplete"


def test_pre_survey_http_502_propagates_with_no_report_body():
    # The resolver's IdP-unreachable boundary must escape the diagnostic unchanged: bare 502,
    # no report (FastAPI renders the HTTPException as its default error body).
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts",
        side_effect=HTTPException(502, "IdP Configuration Service unavailable"),
    ):
        resp = client.post(
            "/policy/check", json={"policy_text": "p", "service_id": "svc-down"}
        )

    assert resp.status_code == 502
    body = resp.json()
    assert "status" not in body
    assert "conflicts" not in body


def test_pre_survey_http_404_propagates_with_no_report_body():
    # Unknown-service boundary likewise propagates as a bare 404 with no report body.
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts",
        side_effect=HTTPException(404, "service not found in IdP catalog"),
    ):
        resp = client.post(
            "/policy/check", json={"policy_text": "p", "service_id": "svc-missing"}
        )

    assert resp.status_code == 404
    body = resp.json()
    assert "status" not in body
    assert "conflicts" not in body


def test_missing_policy_text_is_422_validation_and_never_calls_survey():
    # policy_text is a required field on the request model — its absence is a FastAPI validation
    # 422 (no report body), and the survey is never invoked.
    with patch("aiac.agent.controller.routes.check_policy_conflicts") as survey:
        resp = client.post("/policy/check", json={"service_id": "svc-1"})

    assert resp.status_code == 422
    assert "status" not in resp.json()
    survey.assert_not_called()


def test_route_calls_survey_with_posted_policy_text_and_service_id():
    # The thin shell forwards exactly what was posted to the use-case.
    report = ConflictReport.from_survey([], [], evaluated_count=1)
    with patch(
        "aiac.agent.controller.routes.check_policy_conflicts", return_value=report
    ) as survey:
        resp = client.post(
            "/policy/check",
            json={"policy_text": "editors may read", "service_id": "svc-abc"},
        )

    assert resp.status_code == 200
    survey.assert_called_once_with("editors may read", "svc-abc")
