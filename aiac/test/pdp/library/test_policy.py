"""Unit tests for aiac.pdp.library.policy."""

import pytest
from unittest.mock import MagicMock, patch

from aiac.pdp.library.models import Permission, Scope
from aiac.pdp.library import policy

REALM = "kagenti"
BASE = "http://127.0.0.1:7072"

_WRITE_FUNCTIONS = [
    ("add_role_composites", ("admin", [], REALM), "post"),
    ("remove_role_composites", ("admin", [], REALM), "delete"),
    ("clear_all_composites", (REALM,), "delete"),
    ("create_service_permission", ("svc-uuid", "view-data", "desc", REALM), "post"),
    ("create_service_scope", ("svc-uuid", "read:data", "desc", REALM), "post"),
]


def _ok(json_data=None, status=204):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = status
    resp.json.return_value = json_data or {}
    return resp


def _err(status=500):
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status
    resp.text = "internal error"
    return resp


# ---------------------------------------------------------------------------
# add_role_composites
# ---------------------------------------------------------------------------


class TestAddRoleComposites:
    def test_posts_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        perms = [Permission(id="r1", name="viewer", composite=False, clientRole=False)]
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok()) as m:
            result = policy.add_role_composites("admin", perms, REALM)
        assert result is None
        m.assert_called_once()
        url, kwargs = m.call_args[0][0], m.call_args[1]
        assert "/roles/admin/composites" in url
        assert "realm" in kwargs.get("params", {})


# ---------------------------------------------------------------------------
# remove_role_composites
# ---------------------------------------------------------------------------


class TestRemoveRoleComposites:
    def test_deletes_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        perms = [Permission(id="r1", name="viewer", composite=False, clientRole=False)]
        with patch("aiac.pdp.library.policy.requests.delete", return_value=_ok()) as m:
            result = policy.remove_role_composites("admin", perms, REALM)
        assert result is None
        m.assert_called_once()
        url = m.call_args[0][0]
        assert "/roles/admin/composites" in url


# ---------------------------------------------------------------------------
# clear_all_composites
# ---------------------------------------------------------------------------


class TestClearAllComposites:
    def test_deletes_and_returns_none(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        with patch("aiac.pdp.library.policy.requests.delete", return_value=_ok()) as m:
            result = policy.clear_all_composites(REALM)
        assert result is None
        url = m.call_args[0][0]
        assert url.endswith("/composites")


# ---------------------------------------------------------------------------
# create_service_permission
# ---------------------------------------------------------------------------


class TestCreateServicePermission:
    def test_returns_permission_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        created = {"id": "cr1", "name": "view-data", "composite": False, "clientRole": True}
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created, 201)):
            result = policy.create_service_permission("svc-uuid", "view-data", "desc", REALM)
        assert isinstance(result, Permission)
        assert result.name == "view-data"


# ---------------------------------------------------------------------------
# create_service_scope
# ---------------------------------------------------------------------------


class TestCreateServiceScope:
    def test_returns_scope_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        created = {"id": "sc1", "name": "read:data"}
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created, 201)):
            result = policy.create_service_scope("svc-uuid", "read:data", "desc", REALM)
        assert isinstance(result, Scope)
        assert result.name == "read:data"


# ---------------------------------------------------------------------------
# Non-2xx → RuntimeError for all functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn_name,args,method", _WRITE_FUNCTIONS)
def test_non_2xx_raises_runtime_error(fn_name, args, method, monkeypatch):
    monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
    with patch(f"aiac.pdp.library.policy.requests.{method}", return_value=_err()):
        with pytest.raises(RuntimeError):
            getattr(policy, fn_name)(*args)


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_POLICY_URL", raising=False)
    with patch("aiac.pdp.library.policy.requests.delete", return_value=_ok()) as m:
        policy.clear_all_composites(REALM)
    assert m.call_args[0][0].startswith("http://127.0.0.1:7072")
