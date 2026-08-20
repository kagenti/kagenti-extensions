"""Unit tests for aiac.pdp.service.policy.opa.main.

Targets the always-on Custom Resource writer. The module builds a
``CustomObjectsApi`` at import (kube-config load is guarded, so import needs no
cluster); every test patches that module-level ``_api`` with a ``MagicMock`` so
no real Kubernetes API is contacted. The additive ``POLICY_WRITER_DUMP_REGO``
local-dump toggle is covered here too (it never gates or replaces the CR write).

Note on the delete-by-id endpoint: its route param ``{agent_id}`` is a single
path segment, and a valid namespaced id (``<ns>/<name>`` or a SPIFFE URI) carries
slashes. The library client percent-encodes them and the ASGI server decodes the
segment back, but the ``TestClient``/httpx transport collapses ``%2F`` -> ``/``
before the request is sent, so a namespaced id cannot reach the param through
``TestClient``. Those cases therefore call the route handler function directly
(the FastAPI decorators leave the functions callable), which still exercises the
full write + error-mapping path through the mocked ``_api``.
"""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from kubernetes.client import ApiException

from aiac.pdp.service.policy.opa import main
from aiac.pdp.service.policy.opa.main import app
from aiac.policy.model.models import AgentPolicyModel

# A valid SPIFFE agent id -> namespace "team1", name "github-agent".
SPIFFE = "spiffe://localtest.me/ns/team1/sa/github-agent"


@pytest.fixture
def api(monkeypatch):
    """Patch the module-level Kubernetes client and default the dump toggle off."""
    mock = MagicMock()
    monkeypatch.setattr(main, "_api", mock)
    monkeypatch.delenv("POLICY_WRITER_DUMP_REGO", raising=False)
    monkeypatch.delenv("REGO_OUTPUT_DIR", raising=False)
    return mock


def _agent(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "agent_roles": [],
        "agent_scopes": [],
        "subject_roles": {},
        "source_roles": {},
        "target_scopes": {},
        "inbound_rules": [],
        "outbound_rules": [],
        "outbound_subject_rules": [],
    }


def _model(agent_id: str) -> AgentPolicyModel:
    return AgentPolicyModel(**_agent(agent_id))


def _body(resp) -> dict:
    """Decode a raw starlette Response body (direct-handler-call path)."""
    return json.loads(resp.body)


# ---------------------------------------------------------------------------
# _build_cr shape
# ---------------------------------------------------------------------------


class TestBuildCR:
    def test_cr_shape(self):
        cr = main._build_cr(_model(SPIFFE))
        assert cr["apiVersion"] == "agent.rossoctl.dev/v1alpha1"
        assert cr["kind"] == "AuthorizationPolicy"
        assert cr["metadata"]["name"] == "github-agent"
        assert cr["metadata"]["namespace"] == "team1"
        assert cr["metadata"]["labels"] == {
            "app.kubernetes.io/managed-by": "aiac-pdp-policy-writer"
        }
        assert cr["spec"]["scope"] == "client"
        assert cr["spec"]["clientID"] == "github-agent"

    def test_cr_policies(self):
        cr = main._build_cr(_model(SPIFFE))
        policies = cr["spec"]["policies"]
        assert [p["path"] for p in policies] == [
            "inbound/request.rego",
            "outbound/request.rego",
        ]
        inbound, outbound = policies
        assert "package authbridge.client.inbound.request" in inbound["content"]
        assert "import rego.v1" in inbound["content"]
        assert "package authbridge.client.outbound.request" in outbound["content"]
        assert "import rego.v1" in outbound["content"]


# ---------------------------------------------------------------------------
# POST /policy/agents/{agent_id} -> 204 + server-side-apply
# ---------------------------------------------------------------------------


class TestUpsertAgent:
    def test_returns_204_and_ssa_args(self, api):
        resp = TestClient(app).post("/policy/agents/ignored", json=_agent(SPIFFE))
        assert resp.status_code == 204
        api.patch_namespaced_custom_object.assert_called_once()
        kwargs = api.patch_namespaced_custom_object.call_args.kwargs
        assert kwargs["field_manager"] == "aiac-pdp-policy-writer"
        assert kwargs["force"] is True
        assert kwargs["_content_type"] == "application/apply-patch+yaml"
        assert kwargs["group"] == "agent.rossoctl.dev"
        assert kwargs["version"] == "v1alpha1"
        assert kwargs["plural"] == "authorizationpolicies"
        assert kwargs["namespace"] == "team1"
        assert kwargs["name"] == "github-agent"

    def test_502_on_api_exception(self, api):
        api.patch_namespaced_custom_object.side_effect = ApiException(status=500)
        resp = TestClient(app).post("/policy/agents/ignored", json=_agent(SPIFFE))
        assert resp.status_code == 502
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# POST /policy (batch) -> one patch per agent
# ---------------------------------------------------------------------------


class TestUpsertBatch:
    def test_one_patch_per_agent_and_204(self, api):
        body = {"agents": [_agent(SPIFFE), _agent("team1/other-agent")]}
        resp = TestClient(app).post("/policy", json=body)
        assert resp.status_code == 204
        assert api.patch_namespaced_custom_object.call_count == 2

    def test_bad_agent_id_returns_400_and_no_patch(self, api):
        # "github-agent" has no derivable namespace -> 400 naming it, no patch.
        resp = TestClient(app).post(
            "/policy", json={"agents": [_agent("github-agent")]}
        )
        assert resp.status_code == 400
        assert "github-agent" in resp.json()["error"]
        api.patch_namespaced_custom_object.assert_not_called()


# ---------------------------------------------------------------------------
# DELETE /policy/agents/{agent_id} -> delete_namespaced_custom_object
# (driven via the handler directly; see module docstring)
# ---------------------------------------------------------------------------


class TestDeleteAgent:
    def test_calls_delete_and_returns_204(self, api):
        resp = main.delete_agent("team1/github-agent")
        assert resp.status_code == 204
        kwargs = api.delete_namespaced_custom_object.call_args.kwargs
        assert kwargs["group"] == "agent.rossoctl.dev"
        assert kwargs["version"] == "v1alpha1"
        assert kwargs["plural"] == "authorizationpolicies"
        assert kwargs["namespace"] == "team1"
        assert kwargs["name"] == "github-agent"

    def test_404_is_idempotent_204(self, api):
        api.delete_namespaced_custom_object.side_effect = ApiException(status=404)
        resp = main.delete_agent("team1/github-agent")
        assert resp.status_code == 204

    def test_other_api_exception_502(self, api):
        api.delete_namespaced_custom_object.side_effect = ApiException(status=500)
        resp = main.delete_agent("team1/github-agent")
        assert resp.status_code == 502
        assert "error" in _body(resp)


# ---------------------------------------------------------------------------
# DELETE /policy -> list-by-label then delete each
# ---------------------------------------------------------------------------


class TestDeleteAll:
    def test_lists_by_managed_by_label_then_deletes_each(self, api):
        api.list_cluster_custom_object.return_value = {
            "items": [
                {"metadata": {"namespace": "team1", "name": "github-agent"}},
                {"metadata": {"namespace": "team2", "name": "weather-agent"}},
            ]
        }
        resp = TestClient(app).delete("/policy")
        assert resp.status_code == 204
        selector = api.list_cluster_custom_object.call_args.kwargs["label_selector"]
        assert "app.kubernetes.io/managed-by" in selector
        assert api.delete_namespaced_custom_object.call_count == 2

    def test_empty_listing_returns_204(self, api):
        api.list_cluster_custom_object.return_value = {"items": []}
        resp = TestClient(app).delete("/policy")
        assert resp.status_code == 204
        api.delete_namespaced_custom_object.assert_not_called()


# ---------------------------------------------------------------------------
# GET /health -> 200 when the API is reachable, 503 otherwise
# ---------------------------------------------------------------------------


class TestHealth:
    def test_200_when_list_succeeds(self, api):
        api.list_cluster_custom_object.return_value = {"items": []}
        resp = TestClient(app).get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert api.list_cluster_custom_object.call_args.kwargs["limit"] == 1

    def test_503_when_list_raises(self, api):
        api.list_cluster_custom_object.side_effect = ApiException(status=500)
        resp = TestClient(app).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "error" in body


# ---------------------------------------------------------------------------
# POLICY_WRITER_DUMP_REGO additive local dump (never gates the CR write)
# ---------------------------------------------------------------------------


class TestDumpToggle:
    def test_dump_on_writes_files_and_still_patches(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("POLICY_WRITER_DUMP_REGO", "1")
        monkeypatch.setenv("REGO_OUTPUT_DIR", str(tmp_path))
        resp = TestClient(app).post("/policy/agents/ignored", json=_agent(SPIFFE))
        assert resp.status_code == 204
        api.patch_namespaced_custom_object.assert_called_once()
        inbound = tmp_path / "team1" / "github-agent" / "inbound" / "request.rego"
        outbound = tmp_path / "team1" / "github-agent" / "outbound" / "request.rego"
        assert inbound.exists()
        assert outbound.exists()
        assert "package authbridge.client.inbound.request" in inbound.read_text()
        assert "package authbridge.client.outbound.request" in outbound.read_text()

    def test_dump_off_writes_no_files_but_still_patches(
        self, api, tmp_path, monkeypatch
    ):
        # Toggle unset (deleted by the api fixture); REGO_OUTPUT_DIR set but unused.
        monkeypatch.setenv("REGO_OUTPUT_DIR", str(tmp_path))
        resp = TestClient(app).post("/policy/agents/ignored", json=_agent(SPIFFE))
        assert resp.status_code == 204
        api.patch_namespaced_custom_object.assert_called_once()
        assert list(tmp_path.rglob("*.rego")) == []

    def test_dump_os_error_maps_to_502(self, api, tmp_path, monkeypatch):
        # REGO_OUTPUT_DIR under a regular file -> mkdir raises OSError.
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        monkeypatch.setenv("POLICY_WRITER_DUMP_REGO", "1")
        monkeypatch.setenv("REGO_OUTPUT_DIR", str(blocker / "out"))
        resp = TestClient(app).post("/policy/agents/ignored", json=_agent(SPIFFE))
        assert resp.status_code == 502
        assert "error" in resp.json()
        # The SSA write happened before the additive dump failed.
        api.patch_namespaced_custom_object.assert_called_once()

    def test_dump_on_delete_removes_agent_tree(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("POLICY_WRITER_DUMP_REGO", "1")
        monkeypatch.setenv("REGO_OUTPUT_DIR", str(tmp_path))
        TestClient(app).post("/policy/agents/ignored", json=_agent(SPIFFE))
        tree = tmp_path / "team1" / "github-agent"
        assert tree.exists()
        resp = main.delete_agent("team1/github-agent")
        assert resp.status_code == 204
        assert not tree.exists()
