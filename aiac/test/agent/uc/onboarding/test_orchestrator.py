"""Unit tests for the Service Onboarding Orchestrator (UC1, issue 4.5).

The Orchestrator is a plain function that sequences exactly two stages:
Service Provision (a compiled StateGraph) -> Service Policy Builder. Both are mocked
here via the module-level `build_provision_graph` / `ServicePolicyBuilder` seams — no
live graph, IdP, Kubernetes, or LLM. The Orchestrator applies nothing (no PCE call); it
returns `(list[PolicyRule], override=False)` to the Controller, which makes the single
`compute_and_apply` call afterwards.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding import orchestrator
from aiac.idp.configuration.models import ServiceType

SERVICE_ID = "svc-1"


class TestBothStagesSucceed:
    def test_provision_result_fed_to_builder_and_rules_returned_with_override_false(self):
        # The Orchestrator treats the rule list as opaque — the builder (mocked) owns
        # PolicyRule construction, so a sentinel list is enough to prove pass-through.
        rules = [object()]
        graph = MagicMock()
        graph.invoke.return_value = {"service_type": ServiceType.AGENT}

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
        ):
            spb.build.return_value = rules
            result = orchestrator.onboard_service(SERVICE_ID)

        # service_type produced by Provision is fed into the Service Policy Builder
        spb.build.assert_called_once_with(SERVICE_ID, ServiceType.AGENT)
        # Orchestrator returns the builder's rules paired with the append flag
        assert result == (rules, False)


class TestProvisionFails:
    def test_builder_not_called_and_provision_error_propagates(self):
        graph = MagicMock()
        graph.invoke.side_effect = HTTPException(502, "IdP config unavailable")

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
        ):
            with pytest.raises(HTTPException) as exc:
                orchestrator.onboard_service(SERVICE_ID)

        assert exc.value.status_code == 502
        spb.build.assert_not_called()


class TestBuilderFails:
    def test_builder_error_propagates_after_provision_ran(self):
        graph = MagicMock()
        graph.invoke.return_value = {"service_type": ServiceType.TOOL}

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
        ):
            spb.build.side_effect = HTTPException(502, "IdP Configuration Service unavailable")
            with pytest.raises(HTTPException) as exc:
                orchestrator.onboard_service(SERVICE_ID)

        assert exc.value.status_code == 502
        # Provision ran before the builder was reached
        graph.invoke.assert_called_once()
