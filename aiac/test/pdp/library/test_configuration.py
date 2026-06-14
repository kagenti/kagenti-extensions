"""Unit tests for aiac.pdp.library.configuration."""

import pytest
from unittest.mock import MagicMock, patch

from aiac.pdp.library.models import Subject, Role, Service, Scope
from aiac.pdp.library.configuration import Configuration

REALM = "kagenti"
BASE = "http://127.0.0.1:7071"


def _ok(json_data, status=200):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = status
    resp.json.return_value = json_data
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
    def test_returns_configuration_bound_to_realm(self):
        cfg = Configuration.for_realm(REALM)
        assert isinstance(cfg, Configuration)
        assert cfg.realm == REALM

    def test_direct_init_sets_realm(self):
        cfg = Configuration(REALM)
        assert cfg.realm == REALM


# ---------------------------------------------------------------------------
# get_subjects
# ---------------------------------------------------------------------------


class TestGetSubjects:
    def test_returns_list_of_subject(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_subjects()
        assert isinstance(result[0], Subject)
        assert result[0].username == "alice"
        m.assert_called_once_with(f"{BASE}/subjects", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_subjects()


# ---------------------------------------------------------------------------
# get_roles
# ---------------------------------------------------------------------------


class TestGetRoles:
    def test_returns_list_of_role(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r1", "name": "admin", "composite": False}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_roles()
        assert isinstance(result[0], Role)
        assert result[0].name == "admin"
        m.assert_called_once_with(f"{BASE}/roles", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_roles()


# ---------------------------------------------------------------------------
# get_services
# ---------------------------------------------------------------------------


class TestGetServices:
    def test_returns_list_of_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "name": "my-app", "enabled": True}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_services()
        assert isinstance(result[0], Service)
        assert result[0].id == "c1"
        m.assert_called_once_with(f"{BASE}/services", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_services()


# ---------------------------------------------------------------------------
# get_scopes
# ---------------------------------------------------------------------------


class TestGetScopes:
    def test_returns_list_of_scope(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "s1", "name": "email"}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_scopes()
        assert isinstance(result[0], Scope)
        assert result[0].name == "email"
        m.assert_called_once_with(f"{BASE}/scopes", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_scopes()


# ---------------------------------------------------------------------------
# create_scope
# ---------------------------------------------------------------------------


class TestCreateScope:
    def test_returns_scope_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read:data", "description": "Read access"}
        with patch("aiac.pdp.library.configuration.requests.post", return_value=_ok(created, 201)) as m:
            result = Configuration.for_realm(REALM).create_scope(
                service_id="svc-uuid", scope_name="read:data", description="Read access"
            )
        assert isinstance(result, Scope)
        assert result.name == "read:data"
        assert result.id == "sc1"

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "write"}
        with patch("aiac.pdp.library.configuration.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("svc-abc", "write", "Write access")
        url = m.call_args[0][0]
        assert url == f"{BASE}/services/svc-abc/scopes"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.pdp.library.configuration.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("svc-uuid", "read", "desc")
        params = m.call_args[1].get("params", {})
        assert params == {"realm": REALM}

    def test_json_body_contains_name_and_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.pdp.library.configuration.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("svc-uuid", "read", "Read access")
        body = m.call_args[1].get("json", {})
        assert body == {"name": "read", "description": "Read access"}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_scope("svc-uuid", "read", "desc")


# ---------------------------------------------------------------------------
# realm forwarded as ?realm= on all methods
# ---------------------------------------------------------------------------


class TestRealmParameter:
    @pytest.mark.parametrize("method,endpoint", [
        ("get_subjects", "subjects"),
        ("get_roles", "roles"),
        ("get_services", "services"),
        ("get_scopes", "scopes"),
    ])
    def test_realm_forwarded_as_query_param(self, method, endpoint, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok([])) as m:
            getattr(Configuration.for_realm(REALM), method)()
        m.assert_called_once_with(f"{BASE}/{endpoint}", params={"realm": REALM})


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_CONFIG_URL", raising=False)
    with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok([])) as m:
        Configuration.for_realm(REALM).get_subjects()
    assert m.call_args[0][0].startswith("http://127.0.0.1:7071")
