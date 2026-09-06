"""Unit tests for the Service Onboarding Orchestrator (UC1, issues 4.5 + 171).

The Orchestrator is a plain function that sequences exactly two stages:
Service Provision (a compiled StateGraph) -> Service Policy Builder. Both are mocked
here via the module-level `build_provision_graph` / `ServicePolicyBuilder` seams, and the
idp-library `Configuration` is mocked via the `_config` seam -- no live graph, IdP,
Kubernetes, or LLM. The Orchestrator applies nothing (no PCE call); it returns
`(list[PolicyRule], override=False, default_effect)` to the Controller, which makes the
single `compute_and_apply` call afterwards.

Issue 171 adds a compensating rollback (UC1-only): on any of the four typed build
failures the Orchestrator tears down exactly what Provision *created this run* (the
created-manifest), unsets the client type, disables the client (failed-service marker),
and re-raises. On success it re-enables the client (idempotent).
"""

from unittest.mock import MagicMock, call, patch

import pytest
from fastapi import HTTPException

from aiac.agent.policy_rules_builder.conflict_detection import PolicyConflictError
from aiac.agent.policy_rules_builder.diagnostic_models import ConflictReport
from aiac.agent.policy_rules_builder.graph import (
    LLMAccessError,
    PolicyRulesBuilderError,
    UnparseableLLMResponseError,
)
from aiac.agent.uc.onboarding import orchestrator
from aiac.idp.configuration.models import Role, Scope, ServiceType
from aiac.policy.model.models import RuleEffect

SERVICE_ID = "svc-1"


def _graph(*, created_roles=(), created_scopes=(), service_type=ServiceType.AGENT):
    """A mocked Service Provision graph whose invoke() returns the final state dict,
    including the created-manifest (`created_roles` / `created_scopes`)."""
    graph = MagicMock()
    graph.invoke.return_value = {
        "service_type": service_type,
        "created_roles": list(created_roles),
        "created_scopes": list(created_scopes),
    }
    return graph


def _config_returning(service):
    """A mocked Configuration whose get_service() yields `service`."""
    config = MagicMock()
    config.get_service.return_value = service
    return config


def _rollback_errors():
    """The four typed build failures that trigger the UC1 compensating rollback.

    Instances (not classes): PolicyConflictError needs a ConflictReport, so all four are
    built here and passed as `build` side effects. `from_survey([], [], 0)` is the minimal
    valid report (see test_error_logging.py)."""
    return [
        PolicyConflictError(ConflictReport.from_survey([], [], evaluated_count=0)),
        PolicyRulesBuilderError("auditor rejected after retries"),
        LLMAccessError("LLM endpoint unreachable"),
        UnparseableLLMResponseError("schema validation failed"),
    ]


class TestBothStagesSucceed:
    def test_provision_result_fed_to_builder_and_rules_returned_with_override_false(self):
        # The Orchestrator treats the rule list as opaque -- the builder (mocked) owns
        # PolicyRule construction, so a sentinel list is enough to prove pass-through.
        rules = [object()]
        graph = _graph(service_type=ServiceType.AGENT)

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=_config_returning(object())),
        ):
            spb.build.return_value = rules
            result = orchestrator.onboard_service(SERVICE_ID)

        # service_type produced by Provision is fed into the Service Policy Builder
        spb.build.assert_called_once_with(SERVICE_ID, ServiceType.AGENT)
        # Orchestrator returns the builder's rules paired with the append flag and the
        # default_effect (least-privilege DENY when the caller does not request otherwise).
        assert result == (rules, False, RuleEffect.DENY)


class TestDefaultEffectForwarding:
    def test_caller_requested_default_effect_is_returned_for_forwarding(self):
        # A caller onboarding a service that should default to ALLOW passes default_effect through;
        # the orchestrator returns it verbatim so the Controller forwards it to compute_and_apply.
        graph = _graph(service_type=ServiceType.AGENT)

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=_config_returning(object())),
        ):
            spb.build.return_value = [object()]
            rules, override, default_effect = orchestrator.onboard_service(
                SERVICE_ID, default_effect=RuleEffect.ALLOW
            )

        assert override is False
        assert default_effect is RuleEffect.ALLOW

    def test_provision_graph_invoked_with_service_id_in_trigger(self):
        # The service_id must reach Provision as the trigger's entity_id (Keycloak
        # client_id) -- otherwise Provision classifies the wrong service. The other
        # tests never inspect the graph's argument, so this guards that wiring.
        graph = _graph(service_type=ServiceType.AGENT)

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=_config_returning(object())),
        ):
            spb.build.return_value = [object()]
            orchestrator.onboard_service(SERVICE_ID)

        (state,), _ = graph.invoke.call_args
        assert state.trigger.entity_id == SERVICE_ID


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


class TestSuccessReEnablesClient:
    def test_success_sets_enabled_true_and_does_not_tear_down(self):
        # On a successful onboarding the Orchestrator re-enables the client (idempotent),
        # clearing any failed-disable left by a prior attempt. Nothing is torn down.
        service = object()
        config = _config_returning(service)
        graph = _graph(
            created_roles=[Role(id="r1", name="weather.forecast", composite=False)],
            created_scopes=[Scope(id="s1", name="weather.history")],
        )

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=config),
        ):
            spb.build.return_value = [object()]
            orchestrator.onboard_service(SERVICE_ID)

        config.set_service_enabled.assert_called_once_with(service, True)
        config.delete_service_role.assert_not_called()
        config.delete_service_scope.assert_not_called()
        config.unset_service_type.assert_not_called()


class TestRollbackOnBuildFailure:
    @pytest.mark.parametrize("error", _rollback_errors(), ids=lambda e: type(e).__name__)
    def test_rollback_tears_down_created_disables_and_reraises(self, error):
        role = Role(id="r1", name="weather.forecast", composite=False)
        scope = Scope(id="s1", name="weather.history")
        service = object()
        config = _config_returning(service)
        graph = _graph(created_roles=[role], created_scopes=[scope])

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=config),
        ):
            spb.build.side_effect = error
            with pytest.raises(type(error)) as ei:
                orchestrator.onboard_service(SERVICE_ID)

        # The ORIGINAL error instance is re-raised, not swallowed or re-wrapped.
        assert ei.value is error
        # Teardown of exactly what this run created (unmap-then-delete is done inside the
        # Configuration primitives), then unset type, then disable (failed-service marker).
        config.delete_service_role.assert_called_once_with(service, role)
        config.delete_service_scope.assert_called_once_with(service, scope)
        config.unset_service_type.assert_called_once_with(service)
        config.set_service_enabled.assert_called_once_with(service, False)

    def test_disable_is_the_last_rollback_action(self):
        # The failed-service marker (enabled=false) must land AFTER the teardown, so a
        # crash mid-teardown never leaves a disabled-but-still-provisioned client.
        role = Role(id="r1", name="weather.forecast", composite=False)
        scope = Scope(id="s1", name="weather.history")
        service = object()
        config = _config_returning(service)
        graph = _graph(created_roles=[role], created_scopes=[scope])

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=config),
        ):
            spb.build.side_effect = LLMAccessError("boom")
            with pytest.raises(LLMAccessError):
                orchestrator.onboard_service(SERVICE_ID)

        names = [c[0] for c in config.method_calls]
        assert names.index("set_service_enabled") > names.index("delete_service_role")
        assert names.index("set_service_enabled") > names.index("delete_service_scope")
        assert names.index("set_service_enabled") > names.index("unset_service_type")


class TestRollbackLogInjectionSanitized:
    def test_crlf_in_service_id_and_names_cannot_forge_log_lines(self, caplog):
        # service_id reaches the Orchestrator from the request / NATS trigger (user-controlled).
        # A crafted id or entity name carrying CR/LF must not inject or forge extra log lines
        # (CodeQL py/log-injection): each rollback record stays a single physical line.
        evil_id = "svc-1\r\nINFO forged: attacker-controlled entry"
        role = Role(id="r1", name="role\r\ninjected", composite=False)
        scope = Scope(id="s1", name="scope\ninjected")
        service = object()
        config = _config_returning(service)
        graph = _graph(created_roles=[role], created_scopes=[scope])

        with (
            caplog.at_level("INFO", logger=orchestrator.logger.name),
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=config),
        ):
            spb.build.side_effect = LLMAccessError("boom")
            with pytest.raises(LLMAccessError):
                orchestrator.onboard_service(evil_id)

        for record in caplog.records:
            assert "\n" not in record.getMessage()
            assert "\r" not in record.getMessage()


class TestRollbackDeletesOnlyCreated:
    def test_reused_by_name_entities_are_never_torn_down(self):
        # Provision's created-manifest lists ONLY what it created this run. A role/scope it
        # reused by name (created by another service / a prior run) is absent from the
        # manifest, so rollback -- which iterates the manifest -- never deletes it.
        created_role = Role(id="r-new", name="weather.new", composite=False)
        reused_role = Role(id="r-shared", name="shared.role", composite=False)
        created_scope = Scope(id="s-new", name="weather.new")
        reused_scope = Scope(id="s-shared", name="shared.scope")
        service = object()
        config = _config_returning(service)
        graph = _graph(created_roles=[created_role], created_scopes=[created_scope])

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=config),
        ):
            spb.build.side_effect = LLMAccessError("boom")
            with pytest.raises(LLMAccessError):
                orchestrator.onboard_service(SERVICE_ID)

        config.delete_service_role.assert_called_once_with(service, created_role)
        config.delete_service_scope.assert_called_once_with(service, created_scope)
        deleted_roles = [c.args[1] for c in config.delete_service_role.call_args_list]
        deleted_scopes = [c.args[1] for c in config.delete_service_scope.call_args_list]
        assert reused_role not in deleted_roles
        assert reused_scope not in deleted_scopes


class TestRollbackScopedToFourErrors:
    def test_non_rollback_builder_error_propagates_without_teardown(self):
        # A builder error that is NOT one of the four typed failures (e.g. an HTTPException
        # from IdP focus resolution) propagates untouched -- no teardown, no disable.
        service = object()
        config = _config_returning(service)
        graph = _graph(
            created_roles=[Role(id="r1", name="weather.forecast", composite=False)],
            created_scopes=[Scope(id="s1", name="weather.history")],
            service_type=ServiceType.TOOL,
        )

        with (
            patch.object(orchestrator, "build_provision_graph", return_value=graph),
            patch.object(orchestrator, "ServicePolicyBuilder") as spb,
            patch.object(orchestrator, "_config", return_value=config),
        ):
            spb.build.side_effect = HTTPException(502, "IdP Configuration Service unavailable")
            with pytest.raises(HTTPException) as exc:
                orchestrator.onboard_service(SERVICE_ID)

        assert exc.value.status_code == 502
        graph.invoke.assert_called_once()
        config.delete_service_role.assert_not_called()
        config.delete_service_scope.assert_not_called()
        config.unset_service_type.assert_not_called()
        config.set_service_enabled.assert_not_called()


class TestRetryableReRunRollsBackIdempotently:
    def test_second_attempt_rolls_back_only_its_own_created_objects(self):
        # A retryable LLMAccessError re-provisions on NATS redelivery and rolls back again.
        # The first attempt already deleted its objects; the second re-creates fresh ones
        # (its own created-manifest) and tears down ONLY those -- no crash on already-gone
        # objects (the Configuration deletes are idempotent; the mock never raises).
        service = object()
        config = _config_returning(service)

        def _attempt(role, scope):
            graph = _graph(created_roles=[role], created_scopes=[scope])
            with (
                patch.object(orchestrator, "build_provision_graph", return_value=graph),
                patch.object(orchestrator, "ServicePolicyBuilder") as spb,
                patch.object(orchestrator, "_config", return_value=config),
            ):
                spb.build.side_effect = LLMAccessError("transient")
                with pytest.raises(LLMAccessError):
                    orchestrator.onboard_service(SERVICE_ID)

        _attempt(
            Role(id="r-run1", name="weather.forecast", composite=False),
            Scope(id="s-run1", name="weather.history"),
        )
        config.reset_mock()
        config.get_service.return_value = service

        r2 = Role(id="r-run2", name="weather.forecast", composite=False)
        s2 = Scope(id="s-run2", name="weather.history")
        _attempt(r2, s2)

        # Second rollback targets ONLY its own re-created objects.
        config.delete_service_role.assert_called_once_with(service, r2)
        config.delete_service_scope.assert_called_once_with(service, s2)
        config.set_service_enabled.assert_called_once_with(service, False)
