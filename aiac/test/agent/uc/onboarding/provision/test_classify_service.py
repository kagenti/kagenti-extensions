"""Unit tests for the Service Provision `classify_service` node (UC1, issue 4.3).

The idp-library `Configuration` (via the `_config` seam) and the Kubernetes API (via the
`_core_v1` seam) are mocked — no live services. All provision nodes are non-LLM.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding.provision import kube, nodes
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger
from aiac.idp.configuration.models import Service, ServiceType

ENTITY = "svc-123"


def _state():
    return OnboardingProvisionState(trigger=Trigger(entity_id=ENTITY))


def _service(name="team-a/weather"):
    return Service.model_validate({"id": ENTITY, "clientId": ENTITY, "name": name, "enabled": True})


def _pod(labels, owner_kind="ReplicaSet", owner_name="weather-abc123"):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            labels=labels,
            owner_references=[SimpleNamespace(kind=owner_kind, name=owner_name)],
        )
    )


def _core(pods):
    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    return core


def _run(service=None, pods=None, get_service_exc=None, list_pods_exc=None):
    with patch.object(nodes, "_config") as cfg, patch.object(kube, "_core_v1") as core_v1:
        if get_service_exc is not None:
            cfg.return_value.get_service.side_effect = get_service_exc
        else:
            cfg.return_value.get_service.return_value = service
        core = _core(pods or [])
        if list_pods_exc is not None:
            core.list_namespaced_pod.side_effect = list_pods_exc
        core_v1.return_value = core
        return nodes.classify_service(_state())


class TestClassifyServiceHappyPaths:
    def test_agent_label_routes_to_agent_and_sets_identity(self):
        result = _run(service=_service(), pods=[_pod({"rossoctl.io/type": "agent"})])
        assert result["service_id"] == ENTITY
        assert result["namespace"] == "team-a"
        assert result["workload_name"] == "weather"
        assert result["service_type"] is ServiceType.AGENT

    def test_tool_label_routes_to_tool(self):
        result = _run(service=_service(), pods=[_pod({"rossoctl.io/type": "tool"})])
        assert result["service_type"] is ServiceType.TOOL
        assert result["namespace"] == "team-a"
        assert result["workload_name"] == "weather"

    def test_service_id_stored_from_trigger_entity_id(self):
        result = _run(service=_service(), pods=[_pod({"rossoctl.io/type": "agent"})])
        assert result["service_id"] == ENTITY

    def test_statefulset_owner_matched_by_exact_name(self):
        pod = _pod({"rossoctl.io/type": "tool"}, owner_kind="StatefulSet", owner_name="weather")
        result = _run(service=_service(), pods=[pod])
        assert result["service_type"] is ServiceType.TOOL


class TestClassifyService502s:
    def test_label_absent_is_502_naming_workload_and_label(self):
        with pytest.raises(HTTPException) as ei:
            _run(service=_service(), pods=[_pod({})])
        assert ei.value.status_code == 502
        assert "weather" in ei.value.detail
        assert "rossoctl.io/type" in ei.value.detail

    def test_label_unknown_value_is_502(self):
        with pytest.raises(HTTPException) as ei:
            _run(service=_service(), pods=[_pod({"rossoctl.io/type": "sidecar"})])
        assert ei.value.status_code == 502

    def test_client_name_without_slash_is_502(self):
        with pytest.raises(HTTPException) as ei:
            _run(service=_service(name="no-slash-name"), pods=[_pod({"rossoctl.io/type": "agent"})])
        assert ei.value.status_code == 502

    def test_config_api_down_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with pytest.raises(HTTPException) as ei:
            _run(get_service_exc=RuntimeError("HTTP 503"), pods=[])
        assert ei.value.status_code == 502

    def test_k8s_pod_list_failure_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with pytest.raises(HTTPException) as ei:
            _run(service=_service(), list_pods_exc=RuntimeError("boom"))
        assert ei.value.status_code == 502

    def test_no_pod_owned_by_workload_is_502(self):
        unrelated = _pod({"rossoctl.io/type": "agent"}, owner_name="other-xyz")
        with pytest.raises(HTTPException) as ei:
            _run(service=_service(), pods=[unrelated])
        assert ei.value.status_code == 502
        assert "weather" in ei.value.detail
