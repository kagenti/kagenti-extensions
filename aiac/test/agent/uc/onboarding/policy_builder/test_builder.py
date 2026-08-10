"""Unit tests for the Service Policy Builder sub-agent (UC1, issue 4.4).

The idp-library `Configuration` is mocked via the `_config` seam, and the PRB entry
points (`build_scope_rules` / `build_role_rules`) are patched on the builder module —
no live services, no LLM. The sub-agent is deterministic and applies nothing.

Candidates are sourced from `get_services()` / `get_subjects()` — the same worldview as
the Policy Computation Engine — and excluded/included by **ownership** (role id /
`scope.serviceId`), never by name. Both roles AND scopes come from `get_services()`, so
every scope carries its owning `serviceId` (the SPM routing key); the global `get_scopes()`
catalog is not a candidate source (it returns scopes with an empty `serviceId`). Fixtures
below always give non-focus services distinct `serviceId`s and mark AIAC-provisioned
roles/scopes with the `aiac.managed` attribute, so ownership-based routing is exercised for
real.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding.policy_builder import builder
from aiac.idp.configuration.models import RoleKind, Scope, Service, ServiceType, Subject
from aiac.idp.configuration.models import Role as RoleModel
from aiac.policy.model.models import PolicyRule

FOCUS_ID = "svc-focus"
OTHER_ID = "svc-other"
THIRD_ID = "svc-third"


def _role(name, *, role_id=None, composite=False, children=None, kind=RoleKind.USER, aiac_managed=True):
    return RoleModel(
        id=role_id or f"{name}-id",
        name=name,
        description=name,
        composite=composite,
        childRoles=children or [],
        attributes={"aiac.managed": ["true"]} if aiac_managed else {},
        kind=kind,
    )


def _scope(name, *, scope_id=None, service_id="", aiac_managed=True):
    return Scope(
        id=scope_id or f"{name}-id",
        name=name,
        description=name,
        attributes={"aiac.managed": "true"} if aiac_managed else {},
        serviceId=service_id,
    )


def _service(service_id, *, ref=None, roles=None, scopes=None, service_type=ServiceType.TOOL):
    # `service_id` is the internal client UUID (Service.id — the /apply/service/{id} route key);
    # `ref` is the human-readable clientId (Service.serviceId), which defaults to the UUID for the
    # tests that don't care but is set distinctly where the id-shape distinction matters.
    return Service(
        id=service_id,
        serviceId=ref or service_id,
        enabled=True,
        type=service_type,
        roles=roles or [],
        scopes=scopes or [],
    )


def _subject(username, *, roles=None, subject_id=None):
    return Subject(id=subject_id or f"{username}-id", username=username, enabled=True, roles=roles or [])


def _rule(role, scope):
    return PolicyRule(role=role, scope=scope)


def _invoke(
    service_type,
    *,
    services,
    all_scopes,
    subjects,
    service_id=FOCUS_ID,
    scope_rules=None,
    role_rules=None,
    get_services_exc=None,
    get_subjects_exc=None,
):
    """Run ServicePolicyBuilder.build with all IdP + PRB calls mocked.

    `services` / `subjects` back `get_services()` / `get_subjects()` respectively. Both
    candidate roles and scopes are sourced from `get_services()`; `all_scopes` is retained
    only to describe the global scope catalog in each fixture and is not consulted by the
    builder. `scope_rules` / `role_rules` are optional side_effect callables; default to
    returning an empty list so calls are counted without inventing rule content. `get_*_exc`
    injects an IdP-read failure on the corresponding call.
    """
    with (
        patch.object(builder, "_config") as cfg,
        patch.object(builder, "build_scope_rules") as bsr,
        patch.object(builder, "build_role_rules") as brr,
    ):
        conf = MagicMock()
        if get_services_exc is not None:
            conf.get_services.side_effect = get_services_exc
        else:
            conf.get_services.return_value = services
        if get_subjects_exc is not None:
            conf.get_subjects.side_effect = get_subjects_exc
        else:
            conf.get_subjects.return_value = subjects
        cfg.return_value = conf
        bsr.side_effect = scope_rules or (lambda roles, scope: [])
        brr.side_effect = role_rules or (lambda role, scopes: [])
        result = builder.ServicePolicyBuilder.build(service_id, service_type)
        return result, bsr, brr, conf


class TestTool:
    def test_single_scope_calls_build_scope_rules_once_and_merges(self):
        own_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        other_role = _role("github.agent", kind=RoleKind.AGENT)
        focus = _service(FOCUS_ID, scopes=[own_scope])
        other = _service(OTHER_ID, roles=[other_role])
        rule = _rule(other_role, own_scope)

        result, bsr, brr, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[own_scope],
            subjects=[],
            scope_rules=lambda roles, scope: [rule],
        )

        assert bsr.call_count == 1
        passed_roles, passed_scope = bsr.call_args.args
        assert passed_scope.name == "weather.forecast"
        assert [r.name for r in passed_roles] == ["github.agent"]
        brr.assert_not_called()
        assert result == [rule]

    def test_build_scope_rules_once_per_own_scope_and_results_merged(self):
        s1 = _scope("weather.forecast", service_id=FOCUS_ID)
        s2 = _scope("weather.history", service_id=FOCUS_ID)
        other_role = _role("github.agent", kind=RoleKind.AGENT)
        focus = _service(FOCUS_ID, scopes=[s1, s2])
        other = _service(OTHER_ID, roles=[other_role])
        r1, r2 = _rule(other_role, s1), _rule(other_role, s2)

        result, bsr, brr, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[s1, s2],
            subjects=[],
            scope_rules=lambda roles, scope: [r1] if scope.name == s1.name else [r2],
        )

        assert bsr.call_count == 2
        assert {c.args[1].name for c in bsr.call_args_list} == {s1.name, s2.name}
        brr.assert_not_called()
        assert result == [r1, r2]


class TestAgent:
    def test_scope_rules_per_own_scope_and_role_rules_per_own_role(self):
        own_role = _role("weather.agent")
        own_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        other_role = _role("github.agent", kind=RoleKind.AGENT)
        other_scope = _scope("github.issue", service_id=OTHER_ID)
        focus = _service(FOCUS_ID, roles=[own_role], scopes=[own_scope], service_type=ServiceType.AGENT)
        other = _service(OTHER_ID, roles=[other_role], scopes=[other_scope])
        scope_rule = _rule(other_role, own_scope)
        role_rule = _rule(own_role, other_scope)

        result, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            services=[focus, other],
            all_scopes=[own_scope, other_scope],
            subjects=[],
            scope_rules=lambda roles, scope: [scope_rule],
            role_rules=lambda role, scopes: [role_rule],
        )

        # scope side: once per own scope, roles list is the other-agent-role universe
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
        reader = _role("github.reader", role_id="reader-id", kind=RoleKind.AGENT)
        # composite whose closure includes reader, which also appears standalone
        admin = _role(
            "github.admin", role_id="admin-id", composite=True, children=[reader], kind=RoleKind.AGENT
        )
        own_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        focus = _service(FOCUS_ID, scopes=[own_scope])
        other = _service(OTHER_ID, roles=[admin, reader])

        _, bsr, _, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[own_scope],
            subjects=[],
        )

        passed_roles = bsr.call_args.args[0]
        # closure of admin (admin + reader) unioned with standalone reader, deduped by id
        assert [r.name for r in passed_roles] == ["github.admin", "github.reader"]
        assert [r.id for r in passed_roles] == ["admin-id", "reader-id"]

    def test_composite_own_agent_role_calls_build_role_rules_per_closure_member(self):
        sub = _role("weather.reader", role_id="wr-id")
        own_role = _role("weather.admin", role_id="wa-id", composite=True, children=[sub])
        other_scope = _scope("github.issue", service_id=OTHER_ID)
        focus = _service(FOCUS_ID, roles=[own_role], service_type=ServiceType.AGENT)
        other = _service(OTHER_ID, scopes=[other_scope])

        _, _, brr, _ = _invoke(
            ServiceType.AGENT,
            services=[focus, other],
            all_scopes=[other_scope],
            subjects=[],
        )

        assert brr.call_count == 2
        assert {c.args[0].name for c in brr.call_args_list} == {"weather.admin", "weather.reader"}


class TestSelfExclusion:
    """Exclusion is by ownership (role id / scope.serviceId), never by name — the other
    service's role/scope below intentionally shares a name with the focus's own, to prove
    that name is not what drives exclusion."""

    def test_own_role_and_scope_excluded_from_other_universe_even_when_name_matches(self):
        own_role = _role("shared.name", role_id="own-role-id")
        own_scope = _scope("shared.scope", scope_id="own-scope-id", service_id=FOCUS_ID)
        other_role = _role("shared.name", role_id="other-role-id", kind=RoleKind.AGENT)
        other_scope = _scope("shared.scope", scope_id="other-scope-id", service_id=OTHER_ID)
        focus = _service(FOCUS_ID, roles=[own_role], scopes=[own_scope], service_type=ServiceType.AGENT)
        other = _service(OTHER_ID, roles=[other_role], scopes=[other_scope])

        _, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            services=[focus, other],
            all_scopes=[own_scope, other_scope],
            subjects=[],
        )

        # own role never in the roles list handed to build_scope_rules — only the other
        # service's same-named-but-differently-owned role is present
        assert [r.id for r in bsr.call_args.args[0]] == ["other-role-id"]
        # own scope never in the scopes list handed to build_role_rules
        assert [s.id for s in brr.call_args.args[1]] == ["other-scope-id"]


class TestSelfMappingInvariant:
    def test_no_own_role_in_any_scope_call_and_no_own_scope_in_any_role_call(self):
        own_roles = [_role("weather.agent"), _role("weather.admin")]
        own_scopes = [
            _scope("weather.forecast", service_id=FOCUS_ID),
            _scope("weather.history", service_id=FOCUS_ID),
        ]
        other_roles = [_role("github.agent", kind=RoleKind.AGENT), _role("slack.bot", kind=RoleKind.AGENT)]
        other_scopes = [
            _scope("github.issue", service_id=OTHER_ID),
            _scope("slack.post", service_id=OTHER_ID),
        ]
        own_role_ids = {r.id for r in own_roles}
        own_scope_ids = {s.id for s in own_scopes}

        focus = _service(FOCUS_ID, roles=own_roles, scopes=own_scopes, service_type=ServiceType.AGENT)
        other = _service(OTHER_ID, roles=other_roles, scopes=other_scopes)

        _, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            services=[focus, other],
            all_scopes=own_scopes + other_scopes,
            subjects=[],
        )

        # across ALL build_scope_rules calls, no roles list contains an own role
        for c in bsr.call_args_list:
            assert own_role_ids.isdisjoint({r.id for r in c.args[0]})
        # across ALL build_role_rules calls, no scopes list contains an own scope
        for c in brr.call_args_list:
            assert own_scope_ids.isdisjoint({s.id for s in c.args[1]})


class TestOwnershipBeatsMembership:
    def test_role_owned_by_focus_and_held_by_user_is_excluded_from_candidates(self):
        own_role = _role("weather.admin", role_id="own-role-id")
        own_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        # a user also holds the focus's own role — ownership must still exclude it
        subject = _subject("alice", roles=[own_role])
        focus = _service(FOCUS_ID, roles=[own_role], scopes=[own_scope])
        other = _service(OTHER_ID)

        _, bsr, _, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[own_scope],
            subjects=[subject],
        )

        assert bsr.call_args.args[0] == []


class TestKindRouting:
    def test_other_agent_role_carries_agent_kind_and_user_role_carries_user_kind(self):
        own_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        other_role = _role("github.agent", kind=RoleKind.AGENT)
        user_role = _role("realm.viewer", kind=RoleKind.USER, aiac_managed=False)
        subject = _subject("alice", roles=[user_role])
        focus = _service(FOCUS_ID, scopes=[own_scope])
        other = _service(OTHER_ID, roles=[other_role])

        result, bsr, _, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[own_scope],
            subjects=[subject],
            scope_rules=lambda roles, scope: [_rule(r, scope) for r in roles],
        )

        by_name = {rule.role.name: rule.role.kind for rule in result}
        assert by_name["github.agent"] == RoleKind.AGENT
        assert by_name["realm.viewer"] == RoleKind.USER


class TestBuiltInScopeDropped:
    def test_non_aiac_managed_own_scope_never_reaches_build_scope_rules(self):
        managed_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        builtin_scope = _scope("profile", service_id=FOCUS_ID, aiac_managed=False)
        focus = _service(FOCUS_ID, scopes=[managed_scope, builtin_scope])
        other = _service(OTHER_ID)

        _, bsr, _, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[managed_scope, builtin_scope],
            subjects=[],
        )

        assert bsr.call_count == 1
        assert bsr.call_args.args[1].name == "weather.forecast"


class TestMissingFocus:
    def test_service_id_not_in_catalog_raises_http_exception_not_stopiteration(self):
        other = _service(OTHER_ID)

        with pytest.raises(HTTPException) as ei:
            _invoke(
                ServiceType.TOOL,
                services=[other],
                all_scopes=[],
                subjects=[],
                service_id="svc-does-not-exist",
            )

        assert ei.value.status_code == 404


class TestFocusResolvedByInternalId:
    """The trigger id handed to build() is the Keycloak internal client UUID (Service.id), not the
    human-readable clientId (Service.serviceId) — the /apply/service/{id} route is keyed on the UUID
    because a clientId can be a slash-bearing SPIFFE URI. Regression for the id-shape mismatch that
    made every onboard 404 (focus matched on serviceId instead of id)."""

    UUID = "f5592be1-uuid"
    CLIENT_ID = "spiffe://localtest.me/ns/team1/sa/github-agent"

    def test_focus_resolved_by_uuid_when_serviceid_differs(self):
        own_scope = _scope("weather.forecast", service_id=self.UUID)
        focus = _service(self.UUID, ref=self.CLIENT_ID, scopes=[own_scope])
        other = _service(OTHER_ID, ref="svc-other-client")

        result, bsr, _, _ = _invoke(
            ServiceType.TOOL,
            services=[focus, other],
            all_scopes=[own_scope],
            subjects=[],
            service_id=self.UUID,  # the route/trigger id is the UUID, not the clientId
        )

        # resolved (no 404); the focus's own scope reached build_scope_rules
        assert bsr.call_count == 1
        assert bsr.call_args.args[1].name == "weather.forecast"

    def test_passing_the_clientid_does_not_resolve_focus(self):
        focus = _service(self.UUID, ref=self.CLIENT_ID)
        other = _service(OTHER_ID, ref="svc-other-client")

        with pytest.raises(HTTPException) as ei:
            _invoke(
                ServiceType.TOOL,
                services=[focus, other],
                all_scopes=[],
                subjects=[],
                service_id=self.CLIENT_ID,  # clientId is NOT the route key → not found
            )

        assert ei.value.status_code == 404


class TestEmptyUniverse:
    def test_no_other_services_or_subjects_yields_no_rules_without_error(self):
        focus = _service(FOCUS_ID)

        result, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            services=[focus],
            all_scopes=[],
            subjects=[],
        )

        bsr.assert_not_called()
        brr.assert_not_called()
        assert result == []

    def test_own_entities_only_invokes_prb_with_empty_lists(self):
        own_role, own_scope = _role("weather.agent"), _scope("weather.forecast", service_id=FOCUS_ID)
        focus = _service(FOCUS_ID, roles=[own_role], scopes=[own_scope], service_type=ServiceType.AGENT)

        result, bsr, brr, _ = _invoke(
            ServiceType.AGENT,
            services=[focus],  # no other services, no subjects
            all_scopes=[own_scope],
            subjects=[],
        )

        assert bsr.call_count == 1
        assert bsr.call_args.args[0] == []  # empty candidate-roles universe
        assert brr.call_count == 1
        assert brr.call_args.args[1] == []  # empty other-scopes universe
        assert result == []


class TestErrors:
    def test_get_services_unavailable_is_502(self):
        with pytest.raises(HTTPException) as ei:
            _invoke(
                ServiceType.TOOL,
                services=[],
                all_scopes=[],
                subjects=[],
                get_services_exc=RuntimeError("HTTP 503"),
            )
        assert ei.value.status_code == 502

    def test_get_subjects_unavailable_is_502(self):
        focus = _service(FOCUS_ID)
        with pytest.raises(HTTPException) as ei:
            _invoke(
                ServiceType.TOOL,
                services=[focus],
                all_scopes=[],
                subjects=[],
                get_subjects_exc=RuntimeError("HTTP 500"),
            )
        assert ei.value.status_code == 502

    def test_prb_exception_propagates_no_partial_apply(self):
        own_scope = _scope("weather.forecast", service_id=FOCUS_ID)
        other_role = _role("github.agent", kind=RoleKind.AGENT)
        focus = _service(FOCUS_ID, scopes=[own_scope])
        other = _service(OTHER_ID, roles=[other_role])

        def _boom(roles, scope):
            raise RuntimeError("LLM/ChromaDB failure")

        with pytest.raises(RuntimeError, match="LLM/ChromaDB failure"):
            _invoke(
                ServiceType.TOOL,
                services=[focus, other],
                all_scopes=[own_scope],
                subjects=[],
                scope_rules=_boom,
            )
