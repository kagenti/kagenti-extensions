"""Unit tests for aiac.pdp.library.configuration."""

import pytest
from unittest.mock import MagicMock, patch

from aiac.pdp.library.models import Subject, Role, Assignments, Service, Scope, Permission
from aiac.pdp.library import configuration

REALM = "kagenti"
BASE = "http://127.0.0.1:7071"

_ALL_FUNCTIONS = [
    ("get_subjects", (REALM,), "get"),
    ("get_roles", (REALM,), "get"),
    ("get_services", (REALM,), "get"),
    ("get_scopes", (REALM,), "get"),
    ("get_subject_assignments", ("subject-uuid", REALM), "get"),
    ("get_service_permissions", ("svc-uuid", REALM), "get"),
    ("get_role_composites", ("admin", REALM), "get"),
]


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
# Success paths — typed returns
# ---------------------------------------------------------------------------


class TestGetSubjects:
    def test_returns_list_of_subject(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)) as m:
            result = configuration.get_subjects(REALM)
        assert isinstance(result[0], Subject)
        assert result[0].username == "alice"
        m.assert_called_once_with(f"{BASE}/subjects", params={"realm": REALM})


class TestGetRoles:
    def test_returns_list_of_role(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r1", "name": "admin", "composite": False, "clientRole": False}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)):
            result = configuration.get_roles(REALM)
        assert isinstance(result[0], Role)
        assert result[0].name == "admin"


class TestGetServices:
    def test_returns_list_of_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "clientId": "my-app", "enabled": True, "publicClient": False}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)):
            result = configuration.get_services(REALM)
        assert isinstance(result[0], Service)
        assert result[0].clientId == "my-app"


class TestGetScopes:
    def test_returns_list_of_scope(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "s1", "name": "email"}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)):
            result = configuration.get_scopes(REALM)
        assert isinstance(result[0], Scope)
        assert result[0].name == "email"


class TestGetSubjectAssignments:
    def test_returns_assignments(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = {
            "realmMappings": [{"id": "r1", "name": "admin", "composite": False, "clientRole": False}],
            "serviceMappings": {},
        }
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)):
            result = configuration.get_subject_assignments("subject-uuid", REALM)
        assert isinstance(result, Assignments)
        assert result.realmMappings[0].name == "admin"


class TestGetServicePermissions:
    def test_returns_list_of_permission(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "cr1", "name": "view-data", "composite": False, "clientRole": True}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)):
            result = configuration.get_service_permissions("svc-uuid", REALM)
        assert isinstance(result[0], Permission)
        assert result[0].name == "view-data"


class TestGetRoleComposites:
    def test_returns_list_of_permission(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r2", "name": "viewer", "composite": False, "clientRole": False}]
        with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)):
            result = configuration.get_role_composites("admin", REALM)
        assert isinstance(result[0], Permission)
        assert result[0].name == "viewer"


# ---------------------------------------------------------------------------
# Non-2xx → RuntimeError for all functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn_name,args,method", _ALL_FUNCTIONS)
def test_non_2xx_raises_runtime_error(fn_name, args, method, monkeypatch):
    monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
    with patch(f"aiac.pdp.library.configuration.requests.{method}", return_value=_err()):
        with pytest.raises(RuntimeError):
            getattr(configuration, fn_name)(*args)


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_CONFIG_URL", raising=False)
    payload = [{"id": "u1", "username": "alice", "enabled": True}]
    with patch("aiac.pdp.library.configuration.requests.get", return_value=_ok(payload)) as m:
        configuration.get_subjects(REALM)
    assert m.call_args[0][0].startswith("http://127.0.0.1:7071")
