import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

from aiac.policy.model.models import AgentPolicyModel, PolicyModel

BASE_URL = "http://127.0.0.1:7074"

# Minimal valid fixtures
_AGENT_POLICY_DICT = {
    "agent_id": "agent-1",
    "agent_roles": [],
    "agent_scopes": [],
    "subject_roles": {},
    "source_roles": {},
    "target_scopes": {},
    "inbound_rules": [],
    "outbound_rules": [],
}
_POLICY_DICT = {"agents": [_AGENT_POLICY_DICT]}


def _mock_response(status_code: int, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# get_policy
# ---------------------------------------------------------------------------

class TestGetPolicy:
    def test_returns_policy_model(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, _POLICY_DICT)
            from aiac.policy.store.library.api import get_policy
            result = get_policy()
            mock_get.assert_called_once_with(f"{BASE_URL}/policy")
            assert isinstance(result, PolicyModel)

    def test_raises_on_error_response(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(500)
            from aiac.policy.store.library.api import get_policy
            with pytest.raises(RuntimeError):
                get_policy()


# ---------------------------------------------------------------------------
# get_agent_policy
# ---------------------------------------------------------------------------

class TestGetAgentPolicy:
    def test_returns_agent_policy_model(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, _AGENT_POLICY_DICT)
            from aiac.policy.store.library.api import get_agent_policy
            result = get_agent_policy("agent-1")
            mock_get.assert_called_once_with(f"{BASE_URL}/policy/agents/agent-1")
            assert isinstance(result, AgentPolicyModel)
            assert result.agent_id == "agent-1"

    def test_raises_on_error_response(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            from aiac.policy.store.library.api import get_agent_policy
            with pytest.raises(RuntimeError):
                get_agent_policy("missing-agent")


# ---------------------------------------------------------------------------
# apply_policy
# ---------------------------------------------------------------------------

class TestApplyPolicy:
    def test_posts_serialized_model(self):
        model = PolicyModel.model_validate(_POLICY_DICT)
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_response(200)
            from aiac.policy.store.library.api import apply_policy
            result = apply_policy(model)
            mock_post.assert_called_once_with(
                f"{BASE_URL}/policy", json=model.model_dump()
            )
            assert result is None

    def test_raises_on_error_response(self):
        model = PolicyModel.model_validate(_POLICY_DICT)
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_response(400)
            from aiac.policy.store.library.api import apply_policy
            with pytest.raises(RuntimeError):
                apply_policy(model)


# ---------------------------------------------------------------------------
# apply_agent_policy
# ---------------------------------------------------------------------------

class TestApplyAgentPolicy:
    def test_posts_serialized_agent_model(self):
        model = AgentPolicyModel.model_validate(_AGENT_POLICY_DICT)
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_response(200)
            from aiac.policy.store.library.api import apply_agent_policy
            result = apply_agent_policy("agent-1", model)
            mock_post.assert_called_once_with(
                f"{BASE_URL}/policy/agents/agent-1", json=model.model_dump()
            )
            assert result is None

    def test_raises_on_error_response(self):
        model = AgentPolicyModel.model_validate(_AGENT_POLICY_DICT)
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_response(500)
            from aiac.policy.store.library.api import apply_agent_policy
            with pytest.raises(RuntimeError):
                apply_agent_policy("agent-1", model)


# ---------------------------------------------------------------------------
# delete_agent_policy
# ---------------------------------------------------------------------------

class TestDeleteAgentPolicy:
    def test_deletes_agent_policy(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(204)
            from aiac.policy.store.library.api import delete_agent_policy
            result = delete_agent_policy("agent-1")
            mock_delete.assert_called_once_with(f"{BASE_URL}/policy/agents/agent-1")
            assert result is None

    def test_raises_on_error_response(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(404)
            from aiac.policy.store.library.api import delete_agent_policy
            with pytest.raises(RuntimeError):
                delete_agent_policy("missing-agent")


# ---------------------------------------------------------------------------
# delete_policy
# ---------------------------------------------------------------------------

class TestDeletePolicy:
    def test_deletes_policy(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(204)
            from aiac.policy.store.library.api import delete_policy
            result = delete_policy()
            mock_delete.assert_called_once_with(f"{BASE_URL}/policy")
            assert result is None

    def test_raises_on_error_response(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(500)
            from aiac.policy.store.library.api import delete_policy
            with pytest.raises(RuntimeError):
                delete_policy()


# ---------------------------------------------------------------------------
# URL fallback
# ---------------------------------------------------------------------------

class TestUrlFallback:
    def test_defaults_to_localhost_7074_when_env_unset(self):
        with patch.dict("os.environ", {}, clear=False):
            # Remove the env var if present
            import os
            os.environ.pop("AIAC_POLICY_STORE_URL", None)
            with patch("requests.get") as mock_get:
                mock_get.return_value = _mock_response(200, _POLICY_DICT)
                from aiac.policy.store.library.api import get_policy
                get_policy()
                call_url = mock_get.call_args[0][0]
                assert call_url == "http://127.0.0.1:7074/policy"
