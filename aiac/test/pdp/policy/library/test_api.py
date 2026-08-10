"""Unit tests for aiac.pdp.policy.library.api.

The PDP Policy Writer HTTP boundary is mocked; no live service is required.
"""

from unittest.mock import MagicMock, patch

import pytest

from aiac.policy.model.models import AgentPolicyModel, PolicyModel

BASE = "http://127.0.0.1:7072"

# Minimal valid model fixtures (all eight AgentPolicyModel fields present).
_AGENT_POLICY_DICT = {
    "agent_id": "weather-agent",
    "agent_roles": [],
    "agent_scopes": [],
    "subject_roles": {},
    "source_roles": {},
    "target_scopes": {},
    "inbound_rules": [],
    "outbound_rules": [],
}
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
