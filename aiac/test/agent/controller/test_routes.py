"""Unit tests for aiac.agent.controller.routes (issue 4.1)."""

import warnings
from unittest.mock import MagicMock, patch

import pytest

# Suppress starlette httpx deprecation warning
warnings.filterwarnings("ignore", category=DeprecationWarning, module="starlette")

from fastapi.testclient import TestClient


def _make_role_result(role_id: str = "r1") -> dict:
    return {
        "trigger": {"trigger_type": f"role/{role_id}", "entity_id": role_id},
        "realm": "master",
        "policy_chunks": [],
        "domain_knowledge_chunks": [],
        "pdp_snapshot": None,
        "proposed_diff": None,
        "validation_errors": [],
        "added": [],
        "removed": [],
        "summary": "applied role update",
        "provisioned": None,
    }


# ---------------------------------------------------------------------------
# Route dispatch
# ---------------------------------------------------------------------------


class TestRoutesDispatch:
    def test_policy_build_returns_200(self):
        from aiac.agent.controller.routes import app

        client = TestClient(app)
        resp = client.post("/apply/policy/build")
        assert resp.status_code == 200

    def test_policy_rebuild_returns_200(self):
        from aiac.agent.controller.routes import app

        client = TestClient(app)
        resp = client.post("/apply/policy/rebuild")
        assert resp.status_code == 200

    def test_role_route_calls_roles_dispatch(self):
        from aiac.agent.controller.routes import app

        mock_result = _make_role_result("role-uuid-1")
        with patch("aiac.agent.controller.routes.roles_dispatch", return_value=mock_result) as mock_dispatch:
            client = TestClient(app)
            resp = client.post("/apply/role/role-uuid-1")

        assert resp.status_code == 200
        mock_dispatch.assert_called_once()

    def test_role_route_passes_role_id_to_orchestrator(self):
        from aiac.agent.controller.routes import app

        captured = {}
        mock_result = _make_role_result("my-role-id")

        def capturing_dispatch(state):
            captured["trigger_type"] = state["trigger"]["trigger_type"]
            captured["entity_id"] = state["trigger"]["entity_id"]
            return mock_result

        with patch("aiac.agent.controller.routes.roles_dispatch", side_effect=capturing_dispatch):
            client = TestClient(app)
            client.post("/apply/role/my-role-id")

        assert captured["trigger_type"] == "role/my-role-id"
        assert captured["entity_id"] == "my-role-id"

    def test_service_route_returns_200(self):
        from aiac.agent.controller.routes import app

        client = TestClient(app)
        resp = client.post("/apply/service/svc-abc")
        assert resp.status_code == 200

    def test_service_route_receives_service_id(self):
        from aiac.agent.controller.routes import app

        client = TestClient(app)
        resp = client.post("/apply/service/my-service")

        assert resp.status_code == 200
        # stub must echo back enough info to verify the right ID was received
        body = resp.json()
        assert "my-service" in str(body)

    def test_unknown_route_returns_404(self):
        from aiac.agent.controller.routes import app

        client = TestClient(app)
        resp = client.post("/apply/unknown/foo")
        assert resp.status_code == 404
