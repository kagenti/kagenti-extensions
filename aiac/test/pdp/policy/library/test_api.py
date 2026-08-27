"""Unit tests for aiac.pdp.policy.library.api.

The PDP Policy Writer HTTP boundary is mocked; no live service is required.

``aiac.pdp.policy.library`` is a *pass-through* over the canonical policy models:
``api.py`` imports ``AgentPolicyModel`` / ``PolicyModel`` straight from
``aiac.policy.model.models`` (there is no separate library ``models`` module). So the
model-shape + round-trip assertions here exercise the same canonical ALLOW/DENY models the
library serializes over the wire, and the HTTP-client transport stays a pass-through
``model_dump()`` (no ``?realm=``).
"""

from unittest.mock import MagicMock, patch

import pytest

from aiac.idp.configuration.models import Role, RoleKind, Scope
from aiac.policy.model.models import (
    AgentPolicyModel,
    PolicyModel,
    PolicyRule,
    RuleEffect,
)

BASE = "http://127.0.0.1:7072"


# ---------------------------------------------------------------------------
# New-shape fixtures (ALLOW/DENY model)
#
# Built from real Role/Scope objects so the fixture dicts are exactly what the
# canonical models serialize to — the HTTP-client body assertions then compare a
# genuine ``model_dump()`` round-trip, and a DENY tuple is present in the fixture.
# ---------------------------------------------------------------------------


def _role(id: str = "role-1", name: str = "reader") -> Role:
    return Role(id=id, name=name, composite=False, kind=RoleKind.AGENT, actorIds=["weather-agent"])


def _scope(id: str = "scope-1", name: str = "read") -> Scope:
    return Scope(id=id, name=name, serviceId="weather-tool")


def _agent_policy_model() -> AgentPolicyModel:
    """A representative agent policy exercising the new ALLOW/DENY shape.

    Populates identity maps, both split target-scope maps, and at least one ALLOW rule and one
    DENY rule across the 8 entity×effect rule lists, so a ``model_dump()`` round-trip is lossless
    for both effects.
    """
    role = _role()
    deny_role = _role(id="role-2", name="blocked")
    scope = _scope()
    allow = PolicyRule(role=role, scope=scope, effect=RuleEffect.ALLOW)
    deny = PolicyRule(role=deny_role, scope=scope, effect=RuleEffect.DENY)
    return AgentPolicyModel(
        agent_id="weather-agent",
        agent_roles=[role],
        agent_scopes=[scope],
        # Effect-agnostic identity maps must include the deny-only role.
        source_roles={"caller-agent": [role]},
        subject_roles={"alice": [role, deny_role]},
        # Split outbound target maps.
        target_allow_scopes={"weather-tool": [scope]},
        target_deny_scopes={"secret-tool": [scope]},
        # 8 entity×effect rule lists (ALLOW + DENY populated).
        inbound_subject_allow_rules=[allow],
        inbound_subject_deny_rules=[deny],
        inbound_source_allow_rules=[allow],
        inbound_source_deny_rules=[deny],
        outbound_target_allow_rules=[allow],
        outbound_target_deny_rules=[deny],
        outbound_subject_allow_rules=[allow],
        outbound_subject_deny_rules=[deny],
    )


_AGENT_MODEL = _agent_policy_model()
_AGENT_POLICY_DICT = _AGENT_MODEL.model_dump()
_POLICY_DICT = {"agents": [_AGENT_POLICY_DICT]}


def _ok(status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = True
    return resp


def _err(status: int = 500) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = False
    resp.text = "internal error"
    return resp


# ---------------------------------------------------------------------------
# Model shape — ALLOW/DENY (field-set assertions)
# ---------------------------------------------------------------------------


class TestModelShape:
    def test_policy_rule_carries_role_scope_effect(self):
        fields = set(PolicyRule.model_fields)
        assert {"role", "scope", "effect"} <= fields

    def test_policy_rule_effect_defaults_to_allow(self):
        rule = PolicyRule(role=_role(), scope=_scope())
        assert rule.effect == RuleEffect.ALLOW

    def test_agent_policy_model_has_split_target_maps(self):
        fields = set(AgentPolicyModel.model_fields)
        assert {"target_allow_scopes", "target_deny_scopes"} <= fields
        # The pre-ALLOW/DENY single map is gone.
        assert "target_scopes" not in fields

    def test_agent_policy_model_has_eight_split_rule_lists(self):
        fields = set(AgentPolicyModel.model_fields)
        assert {
            "inbound_subject_allow_rules",
            "inbound_subject_deny_rules",
            "inbound_source_allow_rules",
            "inbound_source_deny_rules",
            "outbound_target_allow_rules",
            "outbound_target_deny_rules",
            "outbound_subject_allow_rules",
            "outbound_subject_deny_rules",
        } <= fields
        # The pre-ALLOW/DENY intermixed lists are gone.
        assert "inbound_rules" not in fields
        assert "outbound_rules" not in fields

    def test_agent_policy_model_keeps_effect_agnostic_identity_maps(self):
        fields = set(AgentPolicyModel.model_fields)
        assert {"agent_roles", "agent_scopes", "source_roles", "subject_roles"} <= fields


# ---------------------------------------------------------------------------
# Round-trip — lossless, including a DENY tuple
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_agent_policy_model_round_trips_losslessly(self):
        restored = AgentPolicyModel.model_validate(_AGENT_MODEL.model_dump(mode="json"))
        assert restored == _AGENT_MODEL

    def test_deny_tuple_survives_round_trip(self):
        restored = AgentPolicyModel.model_validate(_AGENT_MODEL.model_dump(mode="json"))
        deny_rule = restored.outbound_target_deny_rules[0]
        assert deny_rule.effect == RuleEffect.DENY
        assert deny_rule.role.name == "blocked"
        assert deny_rule.scope.name == "read"
        # DENY-only role still resolvable via the effect-agnostic subject map.
        assert any(r.name == "blocked" for r in restored.subject_roles["alice"])
        # Split target maps survive with the right sides.
        assert "weather-tool" in restored.target_allow_scopes
        assert "secret-tool" in restored.target_deny_scopes

    def test_policy_model_round_trips_losslessly(self):
        model = PolicyModel(agents=[_agent_policy_model()])
        restored = PolicyModel.model_validate(model.model_dump(mode="json"))
        assert restored == model
        assert restored.agents[0].outbound_subject_deny_rules[0].effect == RuleEffect.DENY


# ---------------------------------------------------------------------------
# apply_policy
# ---------------------------------------------------------------------------


class TestApplyPolicy:
    def test_posts_serialized_policy_model(self):
        model = PolicyModel.model_validate(_POLICY_DICT)
        with patch("aiac.pdp.policy.library.api.requests.post", return_value=_ok()) as m:
            from aiac.pdp.policy.library.api import apply_policy

            result = apply_policy(model)
        assert result is None
        assert m.call_args[0][0] == f"{BASE}/policy"
        assert m.call_args.kwargs["json"] == model.model_dump()
        assert m.call_args.kwargs.get("params") is None

    def test_raises_on_non_2xx(self):
        model = PolicyModel.model_validate(_POLICY_DICT)
        with patch("aiac.pdp.policy.library.api.requests.post", return_value=_err()):
            from aiac.pdp.policy.library.api import apply_policy

            with pytest.raises(RuntimeError):
                apply_policy(model)


# ---------------------------------------------------------------------------
# apply_agent_policy
# ---------------------------------------------------------------------------


class TestApplyAgentPolicy:
    def test_posts_serialized_agent_model_to_agent_path(self):
        model = AgentPolicyModel.model_validate(_AGENT_POLICY_DICT)
        with patch("aiac.pdp.policy.library.api.requests.post", return_value=_ok()) as m:
            from aiac.pdp.policy.library.api import apply_agent_policy

            result = apply_agent_policy("weather-agent", model)
        assert result is None
        assert m.call_args[0][0] == f"{BASE}/policy/agents/weather-agent"
        assert m.call_args.kwargs["json"] == model.model_dump()
        assert m.call_args.kwargs.get("params") is None

    def test_raises_on_non_2xx(self):
        model = AgentPolicyModel.model_validate(_AGENT_POLICY_DICT)
        with patch("aiac.pdp.policy.library.api.requests.post", return_value=_err()):
            from aiac.pdp.policy.library.api import apply_agent_policy

            with pytest.raises(RuntimeError):
                apply_agent_policy("weather-agent", model)


# ---------------------------------------------------------------------------
# delete_agent_policy
# ---------------------------------------------------------------------------


class TestDeleteAgentPolicy:
    def test_deletes_agent_path(self):
        with patch(
            "aiac.pdp.policy.library.api.requests.delete", return_value=_ok(204)
        ) as m:
            from aiac.pdp.policy.library.api import delete_agent_policy

            result = delete_agent_policy("weather-agent")
        assert result is None
        assert m.call_args[0][0] == f"{BASE}/policy/agents/weather-agent"
        assert m.call_args.kwargs.get("params") is None

    def test_raises_on_non_2xx(self):
        with patch(
            "aiac.pdp.policy.library.api.requests.delete", return_value=_err(404)
        ):
            from aiac.pdp.policy.library.api import delete_agent_policy

            with pytest.raises(RuntimeError):
                delete_agent_policy("missing-agent")


# ---------------------------------------------------------------------------
# delete_policy
# ---------------------------------------------------------------------------


class TestDeletePolicy:
    def test_deletes_policy_path(self):
        with patch(
            "aiac.pdp.policy.library.api.requests.delete", return_value=_ok(204)
        ) as m:
            from aiac.pdp.policy.library.api import delete_policy

            result = delete_policy()
        assert result is None
        assert m.call_args[0][0] == f"{BASE}/policy"
        assert m.call_args.kwargs.get("params") is None

    def test_raises_on_non_2xx(self):
        with patch(
            "aiac.pdp.policy.library.api.requests.delete", return_value=_err(500)
        ):
            from aiac.pdp.policy.library.api import delete_policy

            with pytest.raises(RuntimeError):
                delete_policy()


# ---------------------------------------------------------------------------
# AIAC_PDP_POLICY_URL fallback
# ---------------------------------------------------------------------------


class TestUrlFallback:
    def test_defaults_to_localhost_7072_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("AIAC_PDP_POLICY_URL", raising=False)
        model = PolicyModel.model_validate(_POLICY_DICT)
        with patch("aiac.pdp.policy.library.api.requests.post", return_value=_ok()) as m:
            from aiac.pdp.policy.library.api import apply_policy

            apply_policy(model)
        assert m.call_args[0][0] == "http://127.0.0.1:7072/policy"


# ---------------------------------------------------------------------------
# No realm query parameter on any request
# ---------------------------------------------------------------------------


class TestNoRealmParam:
    def test_none_of_the_four_functions_append_realm(self):
        policy = PolicyModel.model_validate(_POLICY_DICT)
        agent = AgentPolicyModel.model_validate(_AGENT_POLICY_DICT)
        with patch(
            "aiac.pdp.policy.library.api.requests.post", return_value=_ok()
        ) as post, patch(
            "aiac.pdp.policy.library.api.requests.delete", return_value=_ok(204)
        ) as delete:
            from aiac.pdp.policy.library.api import (
                apply_agent_policy,
                apply_policy,
                delete_agent_policy,
                delete_policy,
            )

            apply_policy(policy)
            apply_agent_policy("weather-agent", agent)
            delete_agent_policy("weather-agent")
            delete_policy()

        for call in list(post.call_args_list) + list(delete.call_args_list):
            assert call.kwargs.get("params") is None
            assert "realm" not in call.args[0]
