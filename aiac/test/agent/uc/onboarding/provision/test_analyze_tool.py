"""Unit tests for the Service Provision `analyze_tool` node (UC1, issue 4.3).

Kubernetes Service lookup (`_core_v1` seam) and the MCP `tools/list` call (`_mcp_tools_list`
seam) are mocked. `namespace` + `workload_name` are pre-set on state by `classify_service`.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding.provision import kube, nodes
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger

NS = "team-a"
WORKLOAD = "github-tool"
MCP_LABEL = "protocol.rossoctl.io/mcp"


def _state():
    return OnboardingProvisionState(
        trigger=Trigger(entity_id="svc-9"), namespace=NS, workload_name=WORKLOAD
    )


def _svc(labels, port=8080):
    return SimpleNamespace(
        metadata=SimpleNamespace(labels=labels),
        spec=SimpleNamespace(ports=[SimpleNamespace(port=port)]),
    )


def _run(svc=None, read_exc=None, tools=None, mcp_exc=None):
    with (
        patch.object(kube, "_core_v1") as core_v1,
        patch.object(nodes, "_mcp_tools_list") as mcp,
        patch.object(nodes, "_discovery_token", return_value="disco-tok") as disco,
    ):
        core = MagicMock()
        if read_exc is not None:
            core.read_namespaced_service.side_effect = read_exc
        else:
            core.read_namespaced_service.return_value = svc
        core_v1.return_value = core
        if mcp_exc is not None:
            mcp.side_effect = mcp_exc
        else:
            mcp.return_value = tools or []
        result = nodes.analyze_tool(_state())
        return result, mcp, disco


class TestAnalyzeToolFound:
    def test_scopes_one_per_tool_roles_empty_and_endpoint_built(self):
        tools = [
            {"name": "create_issue", "description": "Open an issue"},
            {"name": "list_repos", "description": "List repos"},
        ]
        result, mcp, disco = _run(svc=_svc({MCP_LABEL: ""}), tools=tools)
        provision = result["service_provision"]

        assert provision.roles == []
        assert [s.name for s in provision.scopes] == [
            f"{WORKLOAD}.create_issue",
            f"{WORKLOAD}.list_repos",
        ]
        assert provision.scopes[0].description == "Open an issue"
        assert "derived from MCP manifest: 2 tools" == provision.reasoning
        mcp.assert_called_once_with(
            f"http://{WORKLOAD}.{NS}.svc.cluster.local:8080/mcp", token="disco-tok"
        )

    def test_endpoint_uses_services_first_port(self):
        _run(svc=_svc({MCP_LABEL: ""}, port=9000), tools=[])[1].assert_called_once_with(
            f"http://{WORKLOAD}.{NS}.svc.cluster.local:9000/mcp", token="disco-tok"
        )

    def test_authenticated_discovery_uses_minted_token(self):
        # The token comes from the _discovery_token seam (config service mints it) and is threaded
        # through to the MCP probe as the Bearer credential.
        _, mcp, disco = _run(svc=_svc({MCP_LABEL: ""}), tools=[])
        disco.assert_called_once()
        assert mcp.call_args.kwargs["token"] == "disco-tok"


class TestAnalyzeToolEmpty:
    def test_empty_tools_list_yields_no_roles_no_scopes(self):
        provision = _run(svc=_svc({MCP_LABEL: ""}), tools=[])[0]["service_provision"]
        assert provision.roles == []
        assert provision.scopes == []


class TestAnalyzeTool502:
    def test_service_without_mcp_label_is_502_naming_workload_and_label(self):
        with pytest.raises(HTTPException) as ei:
            _run(svc=_svc({"app": "x"}))
        assert ei.value.status_code == 502
        assert WORKLOAD in ei.value.detail
        assert MCP_LABEL in ei.value.detail

    def test_service_get_failure_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with pytest.raises(HTTPException) as ei:
            _run(read_exc=RuntimeError("404 not found"))
        assert ei.value.status_code == 502

    def test_mcp_call_failure_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with pytest.raises(HTTPException) as ei:
            _run(svc=_svc({MCP_LABEL: ""}), mcp_exc=RuntimeError("connection refused"))
        assert ei.value.status_code == 502

    def test_discovery_token_failure_is_502_and_skips_tools_list(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with (
            patch.object(kube, "_core_v1") as core_v1,
            patch.object(nodes, "_mcp_tools_list") as mcp,
            patch.object(nodes, "_discovery_token", side_effect=RuntimeError("mint failed")),
        ):
            core = MagicMock()
            core.read_namespaced_service.return_value = _svc({MCP_LABEL: ""})
            core_v1.return_value = core
            with pytest.raises(HTTPException) as ei:
                nodes.analyze_tool(_state())
        assert ei.value.status_code == 502
        assert "discovery token minting failed" in ei.value.detail
        mcp.assert_not_called()


class TestMcpToolsList:
    """Exercises the real `_mcp_tools_list` (not the seam) — the load-bearing auth change."""

    def _resp(self, tools=None):
        resp = MagicMock()
        resp.json.return_value = {"result": {"tools": tools or []}}
        resp.raise_for_status = MagicMock()
        return resp

    def test_sends_bearer_and_timeout_when_token_present(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with patch("requests.post", return_value=self._resp()) as post:
            nodes._mcp_tools_list("http://x/mcp", token="abc")
        kwargs = post.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer abc"
        assert kwargs["timeout"] == nodes._MCP_TIMEOUT

    def test_no_auth_header_when_token_absent(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with patch("requests.post", return_value=self._resp()) as post:
            nodes._mcp_tools_list("http://x/mcp")
        assert "Authorization" not in post.call_args.kwargs["headers"]

    def test_returns_tools_from_result(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        tools = [{"name": "t1", "description": "d"}]
        with patch("requests.post", return_value=self._resp(tools)):
            assert nodes._mcp_tools_list("http://x/mcp", token="abc") == tools
