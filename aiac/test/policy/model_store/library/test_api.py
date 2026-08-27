"""Unit tests for aiac/policy/model_store/library/api.py.

SPM-centric HTTP client. Mocks the ``requests`` layer — no live Policy Store. The store
persists ``ServicePolicyModel`` only; there are no per-agent or whole-collection functions.
"""

from unittest.mock import MagicMock, patch

import pytest

from aiac.idp.configuration.models import Role, Scope, ServiceType
from aiac.policy.model.models import PolicyRule, ServicePolicyModel
from aiac.policy.model_store.keying import encode_service_id
from aiac.policy.model_store.library.api import _HTTP_TIMEOUT

BASE_URL = "http://127.0.0.1:7074"


def _spm_dict(service_id: str = "svc-1", role_id: str = "role-1") -> dict:
    return ServicePolicyModel(
        service_id=service_id,
        service_type=ServiceType.AGENT,
        owned_roles=[],
        owned_scopes=[],
        inbound_allow_rules=[
            PolicyRule(
                role=Role(id=role_id, name="admin", composite=False),
                scope=Scope(id="scope-1", name="read", serviceId=service_id),
            )
        ],
    ).model_dump(mode="json")


def _mock_response(status_code: int, json_data=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


# ---------------------------------------------------------------------------
# get_service_policy  (by-id)
# ---------------------------------------------------------------------------


class TestGetServicePolicy:
    def test_by_id_hit_returns_spm(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, _spm_dict("svc-1"))
            from aiac.policy.model_store.library.api import get_service_policy

            result = get_service_policy("svc-1")
            mock_get.assert_called_once_with(
                f"{BASE_URL}/policy/services/{encode_service_id('svc-1')}", timeout=_HTTP_TIMEOUT
            )
            assert isinstance(result, ServicePolicyModel)
            assert result.service_id == "svc-1"

    def test_by_id_miss_returns_fresh_empty_spm_no_raise(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(404)
            from aiac.policy.model_store.library.api import get_service_policy

            result = get_service_policy("brand-new")
            assert isinstance(result, ServicePolicyModel)
            assert result.service_id == "brand-new"
            assert result.owned_roles == []
            assert result.owned_scopes == []
            assert result.inbound_allow_rules == []
            assert result.inbound_deny_rules == []

    def test_raises_on_other_error_response(self):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(500)
            from aiac.policy.model_store.library.api import get_service_policy

            with pytest.raises(RuntimeError):
                get_service_policy("svc-1")

    @pytest.mark.parametrize(
        "service_id",
        ["team1/github-agent", "spiffe://localtest.me/ns/team1/sa/github-agent"],
    )
    def test_slash_bearing_id_encoded_in_path(self, service_id):
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, _spm_dict(service_id))
            from aiac.policy.model_store.library.api import get_service_policy

            get_service_policy(service_id)
            called_url = mock_get.call_args[0][0]
            path_segment = called_url.rsplit("/", 1)[-1]
            assert "/" not in path_segment
            assert called_url == f"{BASE_URL}/policy/services/{encode_service_id(service_id)}"


# ---------------------------------------------------------------------------
# get_service_policy_by_scope
# ---------------------------------------------------------------------------


class TestGetServicePolicyByScope:
    def test_resolves_via_scope_service_id(self):
        scope = Scope(id="scope-1", name="read", serviceId="owning-svc")
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, _spm_dict("owning-svc"))
            from aiac.policy.model_store.library.api import get_service_policy_by_scope

            result = get_service_policy_by_scope(scope)
            mock_get.assert_called_once_with(
                f"{BASE_URL}/policy/services/{encode_service_id('owning-svc')}", timeout=_HTTP_TIMEOUT
            )
            assert result is not None
            assert result.service_id == "owning-svc"

    def test_returns_none_when_scope_has_no_owner(self):
        scope = Scope(id="scope-1", name="read")  # serviceId defaults to ""
        with patch("requests.get") as mock_get:
            from aiac.policy.model_store.library.api import get_service_policy_by_scope

            result = get_service_policy_by_scope(scope)
            assert result is None
            mock_get.assert_not_called()

    def test_raises_on_non_404_error(self):
        scope = Scope(id="scope-1", name="read", serviceId="owning-svc")
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(500)
            from aiac.policy.model_store.library.api import get_service_policy_by_scope

            with pytest.raises(RuntimeError):
                get_service_policy_by_scope(scope)


# ---------------------------------------------------------------------------
# get_service_policies_by_role
# ---------------------------------------------------------------------------


class TestGetServicePoliciesByRole:
    def test_by_role_hit_returns_matching_spm(self):
        role = Role(id="user-role", name="reader", composite=False)
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, [_spm_dict("svc-a", "user-role")])
            from aiac.policy.model_store.library.api import get_service_policies_by_role

            result = get_service_policies_by_role(role)
            mock_get.assert_called_once_with(
                f"{BASE_URL}/policy/services", params={"role": "user-role"}, timeout=_HTTP_TIMEOUT
            )
            assert [s.service_id for s in result] == ["svc-a"]

    def test_by_role_multiple_returns_all(self):
        role = Role(id="shared", name="reader", composite=False)
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, [_spm_dict("svc-a", "shared"), _spm_dict("svc-b", "shared")])
            from aiac.policy.model_store.library.api import get_service_policies_by_role

            result = get_service_policies_by_role(role)
            assert sorted(s.service_id for s in result) == ["svc-a", "svc-b"]

    def test_by_role_miss_returns_empty_list(self):
        role = Role(id="nobody", name="reader", composite=False)
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(200, [])
            from aiac.policy.model_store.library.api import get_service_policies_by_role

            assert get_service_policies_by_role(role) == []

    def test_raises_on_error_response(self):
        role = Role(id="user-role", name="reader", composite=False)
        with patch("requests.get") as mock_get:
            mock_get.return_value = _mock_response(500)
            from aiac.policy.model_store.library.api import get_service_policies_by_role

            with pytest.raises(RuntimeError):
                get_service_policies_by_role(role)


# ---------------------------------------------------------------------------
# apply_service_policy  (upsert)
# ---------------------------------------------------------------------------


class TestApplyServicePolicy:
    def test_posts_serialized_spm_upsert(self):
        spm = ServicePolicyModel.model_validate(_spm_dict("svc-1"))
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_response(204)
            from aiac.policy.model_store.library.api import apply_service_policy

            result = apply_service_policy("svc-1", spm)
            mock_post.assert_called_once_with(
                f"{BASE_URL}/policy/services/{encode_service_id('svc-1')}",
                json=spm.model_dump(),
                timeout=_HTTP_TIMEOUT,
            )
            assert result is None

    def test_raises_on_error_response(self):
        spm = ServicePolicyModel.model_validate(_spm_dict("svc-1"))
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_response(502)
            from aiac.policy.model_store.library.api import apply_service_policy

            with pytest.raises(RuntimeError):
                apply_service_policy("svc-1", spm)


# ---------------------------------------------------------------------------
# delete_service_policy
# ---------------------------------------------------------------------------


class TestDeleteServicePolicy:
    def test_deletes_service_policy(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(204)
            from aiac.policy.model_store.library.api import delete_service_policy

            result = delete_service_policy("svc-1")
            mock_delete.assert_called_once_with(
                f"{BASE_URL}/policy/services/{encode_service_id('svc-1')}", timeout=_HTTP_TIMEOUT
            )
            assert result is None

    def test_raises_on_error_response(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(502)
            from aiac.policy.model_store.library.api import delete_service_policy

            with pytest.raises(RuntimeError):
                delete_service_policy("svc-1")


# ---------------------------------------------------------------------------
# clear_service_policies  (collection-root DELETE)
# ---------------------------------------------------------------------------


class TestClearServicePolicies:
    def test_deletes_collection_root_no_id_segment(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(204)
            from aiac.policy.model_store.library.api import clear_service_policies

            result = clear_service_policies()
            mock_delete.assert_called_once_with(f"{BASE_URL}/policy/services", timeout=_HTTP_TIMEOUT)
            assert result is None

    def test_raises_on_error_response(self):
        with patch("requests.delete") as mock_delete:
            mock_delete.return_value = _mock_response(502)
            from aiac.policy.model_store.library.api import clear_service_policies

            with pytest.raises(RuntimeError):
                clear_service_policies()


# ---------------------------------------------------------------------------
# Removed surface: no per-agent / whole-collection functions
# ---------------------------------------------------------------------------


class TestRemovedFunctions:
    def test_no_agent_or_collection_functions(self):
        import aiac.policy.model_store.library.api as api

        for removed in (
            "get_policy",
            "apply_policy",
            "delete_policy",
            "get_agent_policy",
            "apply_agent_policy",
            "delete_agent_policy",
        ):
            assert not hasattr(api, removed), f"{removed} should be removed"


# ---------------------------------------------------------------------------
# URL fallback
# ---------------------------------------------------------------------------


class TestUrlFallback:
    def test_defaults_to_localhost_7074_when_env_unset(self):
        import os

        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("AIAC_POLICY_MODEL_STORE_URL", None)
            with patch("requests.get") as mock_get:
                mock_get.return_value = _mock_response(200, _spm_dict("svc-1"))
                from aiac.policy.model_store.library.api import get_service_policy

                get_service_policy("svc-1")
                call_url = mock_get.call_args[0][0]
                assert call_url == f"http://127.0.0.1:7074/policy/services/{encode_service_id('svc-1')}"
