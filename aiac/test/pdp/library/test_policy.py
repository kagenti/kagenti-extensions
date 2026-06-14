"""Unit tests for aiac.pdp.library.policy."""

import pytest
from unittest.mock import MagicMock, patch

from aiac.pdp.library.models import Scope
from aiac.pdp.library.policy import Policy

REALM = "kagenti"
BASE = "http://127.0.0.1:7072"


def _ok(json_data=None, status=201):
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
# Factory method
# ---------------------------------------------------------------------------


class TestForRealm:
    def test_returns_policy_bound_to_realm(self):
        p = Policy.for_realm(REALM)
        assert isinstance(p, Policy)
        assert p.realm == REALM

    def test_direct_init_sets_realm(self):
        p = Policy(REALM)
        assert p.realm == REALM


# ---------------------------------------------------------------------------
# create_scope
# ---------------------------------------------------------------------------


class TestCreateScope:
    def test_returns_scope_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        created = {"id": "sc1", "name": "read:data", "description": "Read access"}
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created)) as m:
            result = Policy.for_realm(REALM).create_scope(
                service_id="svc-uuid", scope_name="read:data", description="Read access"
            )
        assert isinstance(result, Scope)
        assert result.name == "read:data"
        assert result.id == "sc1"

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        created = {"id": "sc1", "name": "write"}
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created)) as m:
            Policy.for_realm(REALM).create_scope("svc-abc", "write", "Write access")
        url = m.call_args[0][0]
        assert url == f"{BASE}/services/svc-abc/scopes"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created)) as m:
            Policy.for_realm(REALM).create_scope("svc-uuid", "read", "desc")
        params = m.call_args[1].get("params", {})
        assert params == {"realm": REALM}

    def test_json_body_contains_name_and_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created)) as m:
            Policy.for_realm(REALM).create_scope("svc-uuid", "read", "Read access")
        body = m.call_args[1].get("json", {})
        assert body == {"name": "read", "description": "Read access"}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_POLICY_URL", BASE)
        with patch("aiac.pdp.library.policy.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Policy.for_realm(REALM).create_scope("svc-uuid", "read", "desc")


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_POLICY_URL", raising=False)
    created = {"id": "sc1", "name": "read"}
    with patch("aiac.pdp.library.policy.requests.post", return_value=_ok(created)) as m:
        Policy.for_realm(REALM).create_scope("svc-uuid", "read", "desc")
    assert m.call_args[0][0].startswith("http://127.0.0.1:7072")
