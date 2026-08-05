"""Unit tests for the Service Provision graph wiring (UC1, issue 3.4).

Covers the conditional routing on `service_type` and end-to-end node wiring with all
external seams (idp-library `Configuration`, Kubernetes, MCP) mocked.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from aiac.agent.uc.onboarding.provision import graph as graph_mod
from aiac.agent.uc.onboarding.provision import kube, nodes
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger
from aiac.idp.configuration.models import Service, ServiceType

ENTITY = "svc-1"


def _get(result, key):
    return result[key] if isinstance(result, dict) else getattr(result, key)


def _pod(type_value):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels={"rossoctl.io/type": type_value},
            owner_references=[SimpleNamespace(kind="ReplicaSet", name="weather-abc")],
        )
    )


def _service():
    return Service.model_validate(
        {"id": ENTITY, "clientId": ENTITY, "name": "team-a/weather", "enabled": True}
    )


class TestRoute:
    def test_agent_routes_to_analyze_agent(self):
        state = OnboardingProvisionState(trigger=Trigger(entity_id=ENTITY), service_type=ServiceType.AGENT)
        assert graph_mod._route(state) == "analyze_agent"

    def test_tool_routes_to_analyze_tool(self):
        state = OnboardingProvisionState(trigger=Trigger(entity_id=ENTITY), service_type=ServiceType.TOOL)
        assert graph_mod._route(state) == "analyze_tool"


class TestGraphCompiles:
    def test_build_returns_compiled_graph(self):
        assert graph_mod.build_provision_graph() is not None


class TestEndToEnd:
    def _invoke(self):
        return graph_mod.build_provision_graph().invoke(
            OnboardingProvisionState(trigger=Trigger(entity_id=ENTITY))
        )

    def test_agent_path_end_to_end(self):
        with (
            patch.object(nodes, "_config") as cfg,
            patch.object(kube, "_core_v1") as core_v1,
            patch.object(kube, "_custom_objects") as co,
        ):
            cfg.return_value.get_service.return_value = _service()
            core = MagicMock()
            core.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod("agent")])
            core_v1.return_value = core
            co.return_value.list_namespaced_custom_object.return_value = {
                "items": [
                    {
                        "metadata": {"name": "weather"},
                        "status": {"card": {"skills": [{"id": "forecast", "name": "F", "description": "d"}]}},
                    }
                ]
            }
            result = self._invoke()

        assert _get(result, "service_type") is ServiceType.AGENT
        provision = _get(result, "service_provision")
        # one per-skill operator role, mirroring the scope (no generic weather.agent role)
        assert [r.name for r in provision.roles] == ["weather.forecast"]
        assert [s.name for s in provision.scopes] == ["weather.forecast"]
        cfg.return_value.set_service_type.assert_called_once()

    def test_tool_path_end_to_end(self):
        with (
            patch.object(nodes, "_config") as cfg,
            patch.object(kube, "_core_v1") as core_v1,
            patch.object(nodes, "_mcp_tools_list", return_value=[{"name": "t1", "description": "d"}]),
        ):
            cfg.return_value.get_service.return_value = _service()
            core = MagicMock()
            core.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod("tool")])
            core.read_namespaced_service.return_value = SimpleNamespace(
                metadata=SimpleNamespace(labels={"protocol.rossoctl.io/mcp": ""}),
                spec=SimpleNamespace(ports=[SimpleNamespace(port=8080)]),
            )
            core_v1.return_value = core
            result = self._invoke()

        assert _get(result, "service_type") is ServiceType.TOOL
        provision = _get(result, "service_provision")
        assert provision.roles == []
        assert [s.name for s in provision.scopes] == ["weather.t1"]
