"""Unit tests for the Service Policy Builder sub-agent (UC1, issue 4.4).

The idp-library `Configuration` is mocked via the `_config` seam, and the PRB entry
points (`build_scope_rules` / `build_role_rules`) are patched on the builder module —
no live services, no LLM. The sub-agent is deterministic and applies nothing.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding.policy_builder import builder
from aiac.idp.configuration.models import Role, Scope, Service, ServiceType
from aiac.policy.model.models import PolicyRule

SERVICE_ID = "svc-123"


def _role(name, *, role_id=None, composite=False, children=None):
    return Role(
        id=role_id or f"{name}-id",
        name=name,
        description=name,
        composite=composite,
        childRoles=children or [],
    )


def _scope(name, *, scope_id=None):
    return Scope(id=scope_id or f"{name}-id", name=name, description=name)


def _service(own_roles, own_scopes):
    return Service.model_validate(
        {
            "id": SERVICE_ID,
            "clientId": SERVICE_ID,
            "enabled": True,
            "roles": [r.model_dump() for r in own_roles],
            "scopes": [s.model_dump() for s in own_scopes],
        }
    )


def _rule(role, scope):
    return PolicyRule(role=role, scope=scope)


def _invoke(
    service_type,
    *,
    own_roles,
    own_scopes,
    all_roles,
    all_scopes,
    scope_rules=None,
    role_rules=None,
    get_service_exc=None,
    get_roles_exc=None,
):
    """Run ServicePolicyBuilder.build with all IdP + PRB calls mocked.

    `scope_rules` / `role_rules` are optional side_effect callables; default to
    returning an empty list so calls are counted without inventing rule content.
    `get_service_exc` / `get_roles_exc` inject IdP-read failures.
    """
    with (
        patch.object(builder, "_config") as cfg,
        patch.object(builder, "build_scope_rules") as bsr,
        patch.object(builder, "build_role_rules") as brr,
    ):
        conf = MagicMock()
        if get_service_exc is not None:
            conf.get_service.side_effect = get_service_exc
        else:
            conf.get_service.return_value = _service(own_roles, own_scopes)
        if get_roles_exc is not None:
            conf.get_roles.side_effect = get_roles_exc
        else:
            conf.get_roles.return_value = all_roles
        conf.get_scopes.return_value = all_scopes
        cfg.return_value = conf
        bsr.side_effect = scope_rules or (lambda roles, scope: [])
        brr.side_effect = role_rules or (lambda role, scopes: [])
        result = builder.ServicePolicyBuilder.build(SERVICE_ID, service_type)
        return result, bsr, brr, conf


class TestTool:
    def test_single_scope_calls_build_scope_rules_once_and_merges(self):
        own_scope = _scope("weather.forecast")
        other_role = _role("github.agent")
        rule = _rule(other_role, own_scope)

        result, bsr, brr, _ = _invoke(
            ServiceType.TOOL,
            own_roles=[],
            own_scopes=[own_scope],
            all_roles=[other_role],
            all_scopes=[own_scope],
            scope_rules=lambda roles, scope: [rule],
        )

        assert bsr.call_count == 1
        passed_roles, passed_scope = bsr.call_args.args
        assert passed_scope.name == "weather.forecast"
        assert [r.name for r in passed_roles] == ["github.agent"]
        brr.assert_not_called()
        assert result == [rule]

    def test_build_scope_rules_once_per_own_scope_and_results_merged(self):
        s1, s2 = _scope("weather.forecast"), _scope("weather.history")
        other = _role("github.agent")
        r1, r2 = _rule(other, s1), _rule(other, s2)

        result, bsr, brr, _ = _invoke(
            ServiceType.TOOL,
            own_roles=[],
            own_scopes=[s1, s2],
            all_roles=[other],
            all_scopes=[s1, s2],
            scope_rules=lambda roles, scope: [r1] if scope.name == s1.name else [r2],
        )

        assert bsr.call_count == 2
        assert {c.args[1].name for c in bsr.call_args_list} == {s1.name, s2.name}
        brr.assert_not_called()
        assert result == [r1, r2]


class TestAgent:
    def test_scope_rules_per_own_scope_and_role_rules_per_own_role(self):
        own_role = _role("weather.agent")
        own_scope = _scope("weather.forecast")
        other_role = _role("github.agent")
        other_scope = _scope("github.issue")
        scope_rule = _rule(other_role, own_scope)
        role_rule = _rule(own_role, other_scope)

        result, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            own_roles=[own_role],
            own_scopes=[own_scope],
            all_roles=[own_role, other_role],
            all_scopes=[own_scope, other_scope],
            scope_rules=lambda roles, scope: [scope_rule],
            role_rules=lambda role, scopes: [role_rule],
        )

        # scope side: once per own scope, roles list is the other-role universe
        assert bsr.call_count == 1
        s_roles, s_scope = bsr.call_args.args
        assert s_scope.name == "weather.forecast"
        assert [r.name for r in s_roles] == ["github.agent"]

        # role side: once per own role, scopes list is the other-scope universe
        assert brr.call_count == 1
        r_role, r_scopes = brr.call_args.args
        assert r_role.name == "weather.agent"
        assert [s.name for s in r_scopes] == ["github.issue"]

        assert result == [scope_rule, role_rule]


class TestFlattening:
    def test_composite_other_role_expanded_to_closure_deduped_by_id(self):
        reader = _role("github.reader", role_id="reader-id")
        # composite whose closure includes reader, which also appears standalone
        admin = _role("github.admin", role_id="admin-id", composite=True, children=[reader])
        own_scope = _scope("weather.forecast")

        _, bsr, _, _ = _invoke(
            ServiceType.TOOL,
            own_roles=[],
            own_scopes=[own_scope],
            all_roles=[admin, reader],
            all_scopes=[own_scope],
        )

        passed_roles = bsr.call_args.args[0]
        # closure of admin (admin + reader) unioned with standalone reader, deduped by id
        assert [r.name for r in passed_roles] == ["github.admin", "github.reader"]
        assert [r.id for r in passed_roles] == ["admin-id", "reader-id"]

    def test_composite_own_agent_role_calls_build_role_rules_per_closure_member(self):
        sub = _role("weather.reader", role_id="wr-id")
        own_role = _role("weather.admin", role_id="wa-id", composite=True, children=[sub])
        other_scope = _scope("github.issue")

        _, _, brr, _ = _invoke(
            ServiceType.AGENT,
            own_roles=[own_role],
            own_scopes=[],
            all_roles=[own_role, sub],
            all_scopes=[other_scope],
        )

        assert brr.call_count == 2
        assert {c.args[0].name for c in brr.call_args_list} == {"weather.admin", "weather.reader"}


class TestSelfExclusion:
    def test_own_role_and_scope_excluded_from_other_universe(self):
        own_role, own_scope = _role("weather.agent"), _scope("weather.forecast")
        other_role, other_scope = _role("github.agent"), _scope("github.issue")

        _, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            own_roles=[own_role],
            own_scopes=[own_scope],
            all_roles=[own_role, other_role],
            all_scopes=[own_scope, other_scope],
        )

        # own role never in the roles list handed to build_scope_rules
        assert [r.name for r in bsr.call_args.args[0]] == ["github.agent"]
        # own scope never in the scopes list handed to build_role_rules
        assert [s.name for s in brr.call_args.args[1]] == ["github.issue"]


class TestSelfMappingInvariant:
    def test_no_own_role_in_any_scope_call_and_no_own_scope_in_any_role_call(self):
        own_roles = [_role("weather.agent"), _role("weather.admin")]
        own_scopes = [_scope("weather.forecast"), _scope("weather.history")]
        other_roles = [_role("github.agent"), _role("slack.bot")]
        other_scopes = [_scope("github.issue"), _scope("slack.post")]
        own_role_names = {r.name for r in own_roles}
        own_scope_names = {s.name for s in own_scopes}

        _, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            own_roles=own_roles,
            own_scopes=own_scopes,
            all_roles=own_roles + other_roles,
            all_scopes=own_scopes + other_scopes,
        )

        # across ALL build_scope_rules calls, no roles list contains an own role
        for c in bsr.call_args_list:
            assert own_role_names.isdisjoint({r.name for r in c.args[0]})
        # across ALL build_role_rules calls, no scopes list contains an own scope
        for c in brr.call_args_list:
            assert own_scope_names.isdisjoint({s.name for s in c.args[1]})


class TestEmptyUniverse:
    def test_no_other_entities_invokes_prb_with_empty_lists_and_returns_empty(self):
        own_role, own_scope = _role("weather.agent"), _scope("weather.forecast")

        result, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            own_roles=[own_role],
            own_scopes=[own_scope],
            all_roles=[own_role],  # only the service's own entities exist
            all_scopes=[own_scope],
        )

        assert bsr.call_count == 1
        assert bsr.call_args.args[0] == []  # empty other-roles universe
        assert brr.call_count == 1
        assert brr.call_args.args[1] == []  # empty other-scopes universe
        assert result == []


class TestErrors:
    def test_idp_unavailable_is_502_after_retries(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "2")
        with pytest.raises(HTTPException) as ei:
            _invoke(
                ServiceType.TOOL,
                own_roles=[],
                own_scopes=[],
                all_roles=[],
                all_scopes=[],
                get_service_exc=RuntimeError("HTTP 503"),
            )
        assert ei.value.status_code == 502

    def test_idp_get_roles_unavailable_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with pytest.raises(HTTPException) as ei:
            _invoke(
                ServiceType.TOOL,
                own_roles=[],
                own_scopes=[_scope("weather.forecast")],
                all_roles=[],
                all_scopes=[_scope("weather.forecast")],
                get_roles_exc=RuntimeError("HTTP 500"),
            )
        assert ei.value.status_code == 502

    def test_prb_exception_propagates_no_partial_apply(self):
        own_scope = _scope("weather.forecast")

        def _boom(roles, scope):
            raise RuntimeError("LLM/ChromaDB failure")

        with pytest.raises(RuntimeError, match="LLM/ChromaDB failure"):
            _invoke(
                ServiceType.TOOL,
                own_roles=[],
                own_scopes=[own_scope],
                all_roles=[_role("github.agent")],
                all_scopes=[own_scope],
                scope_rules=_boom,
            )
