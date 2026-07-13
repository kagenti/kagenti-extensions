"""Unit tests for the Service Provision `provision_service` node (UC1, issue 4.3).

The idp-library `Configuration` is mocked via the `_config` seam — no service HTTP layer.
"""

from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding.provision import nodes
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger
from aiac.agent.uc.onboarding.provision.types import (
    RoleDefinition,
    ScopeDefinition,
    ServiceProvision,
)
from aiac.idp.configuration.models import Service, ServiceType

SERVICE_ID = "svc-123"


def _state(roles, scopes, service_type=ServiceType.AGENT, reasoning="r"):
    return OnboardingProvisionState(
        trigger=Trigger(entity_id=SERVICE_ID),
        service_id=SERVICE_ID,
        namespace="team-a",
        workload_name="weather",
        service_type=service_type,
        service_provision=ServiceProvision(roles=roles, scopes=scopes, reasoning=reasoning),
    )


def _service():
    return Service.model_validate({"id": SERVICE_ID, "clientId": SERVICE_ID, "enabled": True})


def _run(state, *, get_service_exc=None):
    with patch.object(nodes, "_config") as cfg:
        conf = MagicMock()
        if get_service_exc is not None:
            conf.get_service.side_effect = get_service_exc
        else:
            conf.get_service.return_value = _service()
        cfg.return_value = conf
        result = nodes.provision_service(state)
        return result, conf


class TestProvisionServiceWrites:
    def test_create_service_role_called_once_per_role(self):
        roles = [
            RoleDefinition(name="weather.agent", description="Agent role"),
            RoleDefinition(name="weather.admin", description="Admin role"),
        ]
        _, conf = _run(_state(roles, []))
        assert conf.create_service_role.call_count == 2
        conf.create_service_role.assert_has_calls(
            [call(SERVICE_ID, roles[0]), call(SERVICE_ID, roles[1])]
        )

    def test_create_service_scope_called_once_per_scope(self):
        scopes = [
            ScopeDefinition(name="weather.forecast", description="Forecast"),
            ScopeDefinition(name="weather.history", description="History"),
        ]
        _, conf = _run(_state([], scopes))
        assert conf.create_service_scope.call_count == 2
        conf.create_service_scope.assert_has_calls(
            [call(SERVICE_ID, scopes[0]), call(SERVICE_ID, scopes[1])]
        )

    def test_no_entries_makes_no_create_calls(self):
        _, conf = _run(_state([], []))
        conf.create_service_role.assert_not_called()
        conf.create_service_scope.assert_not_called()


class TestProvisionServicePersistsType:
    def test_set_service_type_called_with_resolved_service_type(self):
        _, conf = _run(_state([], [], service_type=ServiceType.TOOL))
        assert conf.set_service_type.call_count == 1
        passed_service, passed_type = conf.set_service_type.call_args.args
        assert passed_type is ServiceType.TOOL
        assert passed_service is conf.get_service.return_value

    def test_agent_type_persisted_as_capitalized_value(self):
        _, conf = _run(_state([], [], service_type=ServiceType.AGENT))
        assert conf.set_service_type.call_args.args[1] == "Agent"


class TestProvisionServiceReturn:
    def test_returns_service_provision_and_service_type_to_orchestrator(self):
        roles = [RoleDefinition(name="weather.agent", description="Agent role")]
        state = _state(roles, [], service_type=ServiceType.AGENT)
        result, _ = _run(state)
        assert result["service_provision"] is state.service_provision
        assert result["service_type"] is ServiceType.AGENT


class TestProvisionService502:
    def test_idp_unavailable_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        roles = [RoleDefinition(name="weather.agent", description="Agent role")]
        state = _state(roles, [])
        with patch.object(nodes, "_config") as cfg:
            conf = MagicMock()
            conf.create_service_role.side_effect = RuntimeError("HTTP 503")
            cfg.return_value = conf
            with pytest.raises(HTTPException) as ei:
                nodes.provision_service(state)
        assert ei.value.status_code == 502
