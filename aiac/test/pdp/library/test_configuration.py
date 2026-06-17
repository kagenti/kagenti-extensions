"""Unit tests for aiac.pdp.library.configuration."""

import pytest
from unittest.mock import MagicMock, patch

from aiac.pdp.library.configuration.models import Subject, Role, Service, Scope
from aiac.pdp.library.configuration.api import Configuration

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
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_subjects()
        assert isinstance(result[0], Subject)
        assert result[0].username == "alice"
        m.assert_called_once_with(f"{BASE}/subjects", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_subjects()


# ---------------------------------------------------------------------------
# get_roles
# ---------------------------------------------------------------------------


class TestGetRoles:
    def test_returns_list_of_role(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r1", "name": "admin", "composite": False}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(payload), _ok([])]) as m:
            result = Configuration.for_realm(REALM).get_roles()
        assert isinstance(result[0], Role)
        assert result[0].name == "admin"
        assert m.call_args_list[0][0][0] == f"{BASE}/roles"

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_roles()

    def test_non_composite_role_populates_mapped_scopes(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "viewer", "composite": False}]
        scopes = [{"id": "s1", "name": "read:data"}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok(scopes)]):
            result = Configuration.for_realm(REALM).get_roles()
        assert result[0].mappedScopes[0].name == "read:data"
        assert result[0].childRoles == []

    def test_non_composite_role_skips_composites_call(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "viewer", "composite": False}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok([])]) as m:
            Configuration.for_realm(REALM).get_roles()
        urls = [c[0][0] for c in m.call_args_list]
        assert all("/composites" not in u for u in urls)

    def test_composite_role_populates_child_roles(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "admin", "composite": True}]
        child_roles = [{"id": "r2", "name": "viewer", "composite": False}]
        scopes = [{"id": "s1", "name": "profile"}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok(child_roles), _ok(scopes)]):
            result = Configuration.for_realm(REALM).get_roles()
        assert result[0].childRoles[0].name == "viewer"

    def test_composite_role_populates_mapped_scopes(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "admin", "composite": True}]
        child_roles = [{"id": "r2", "name": "viewer", "composite": False}]
        scopes = [{"id": "s1", "name": "profile"}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok(child_roles), _ok(scopes)]):
            result = Configuration.for_realm(REALM).get_roles()
        assert result[0].mappedScopes[0].name == "profile"

    def test_raises_if_scopes_call_fails(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "viewer", "composite": False}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(roles), _err(502)]):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_roles()

    def test_raises_if_composites_call_fails(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "admin", "composite": True}]
        with patch("aiac.pdp.library.configuration.api.requests.get",
                   side_effect=[_ok(roles), _err(502)]):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_roles()


# ---------------------------------------------------------------------------
# get_services
# ---------------------------------------------------------------------------


class TestGetServices:
    def test_returns_list_of_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "name": "my-app", "enabled": True}]
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_services()
        assert isinstance(result[0], Service)
        assert result[0].id == "c1"
        m.assert_called_once_with(f"{BASE}/services", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_services()


# ---------------------------------------------------------------------------
# get_scopes
# ---------------------------------------------------------------------------


class TestGetScopes:
    def test_returns_list_of_scope(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "s1", "name": "email"}]
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_scopes()
        assert isinstance(result[0], Scope)
        assert result[0].name == "email"
        m.assert_called_once_with(f"{BASE}/scopes", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_scopes()


# ---------------------------------------------------------------------------
# create_scope
# ---------------------------------------------------------------------------


class TestCreateScope:
    def test_returns_scope_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read:data", "description": "Read access"}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            result = Configuration.for_realm(REALM).create_scope(
                scope_name="read:data", scope_description="Read access"
            )
        assert isinstance(result, Scope)
        assert result.name == "read:data"
        assert result.id == "sc1"

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "write"}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("write", "Write access")
        url = m.call_args[0][0]
        assert url == f"{BASE}/scopes"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("read", "desc")
        params = m.call_args[1].get("params", {})
        assert params == {"realm": REALM}

    def test_json_body_contains_name_and_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("read", "Read access")
        body = m.call_args[1].get("json", {})
        assert body == {"name": "read", "description": "Read access"}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_scope("read", "desc")

    def test_raises_on_409_conflict(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_scope("dupe", "desc")


# ---------------------------------------------------------------------------
# map_scope_to_service
# ---------------------------------------------------------------------------


class TestMapScopeToService:
    def _make_service(self, **kwargs):
        defaults = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        return Service.model_validate({**defaults, **kwargs})

    def _make_scope(self, **kwargs):
        defaults = {"id": "scope-id", "name": "read:data"}
        return Scope.model_validate({**defaults, **kwargs})

    def test_returns_updated_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        updated = {"id": "svc-uuid", "name": "my-svc", "enabled": True, "scopes": [{"id": "scope-id", "name": "read:data"}]}
        post_resp = _ok({}, 201)
        get_resp = _ok(updated)
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=post_resp), \
             patch("aiac.pdp.library.configuration.api.requests.get", return_value=get_resp) as get_m:
            result = Configuration.for_realm(REALM).map_scope_to_service(service, scope)
        assert isinstance(result, Service)
        assert result.id == "svc-uuid"
        get_m.assert_called_once_with(f"{BASE}/services/svc-uuid", params={"realm": REALM})

    def test_issues_post_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        updated = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(updated)):
            Configuration.for_realm(REALM).map_scope_to_service(service, scope)
        url = post_m.call_args[0][0]
        assert url == f"{BASE}/services/svc-uuid/scopes/scope-id"

    def test_raises_on_post_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).map_scope_to_service(service, scope)

    def test_realm_forwarded_on_both_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        updated = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(updated)) as get_m:
            Configuration.for_realm(REALM).map_scope_to_service(service, scope)
        assert post_m.call_args[1].get("params") == {"realm": REALM}
        assert get_m.call_args[1].get("params") == {"realm": REALM}


# ---------------------------------------------------------------------------
# create_role
# ---------------------------------------------------------------------------


class TestCreateRole:
    def test_returns_role_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "description": "Read-only", "composite": False}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            result = Configuration.for_realm(REALM).create_role("reader", "Read-only")
        assert isinstance(result, Role)
        assert result.name == "reader"
        assert result.id == "r1"

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "composite": False}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_role("reader", "desc")
        url = m.call_args[0][0]
        assert url == f"{BASE}/roles"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "composite": False}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_role("reader", "desc")
        assert m.call_args[1].get("params") == {"realm": REALM}

    def test_json_body_contains_name_and_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "composite": False}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_role("reader", "Read-only")
        assert m.call_args[1].get("json") == {"name": "reader", "description": "Read-only"}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_role("reader", "desc")

    def test_raises_on_409_conflict(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_role("dupe", "desc")


# ---------------------------------------------------------------------------
# map_role_to_service
# ---------------------------------------------------------------------------


class TestMapRoleToService:
    def _make_service(self, **kwargs):
        defaults = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        return Service.model_validate({**defaults, **kwargs})

    def _make_role(self, **kwargs):
        defaults = {"id": "role-id", "name": "reader", "composite": False}
        return Role.model_validate({**defaults, **kwargs})

    def test_returns_updated_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        updated = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok({}, 201)), \
             patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(updated)) as get_m:
            result = Configuration.for_realm(REALM).map_role_to_service(service, role)
        assert isinstance(result, Service)
        get_m.assert_called_once_with(f"{BASE}/services/svc-uuid", params={"realm": REALM})

    def test_issues_post_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        updated = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(updated)):
            Configuration.for_realm(REALM).map_role_to_service(service, role)
        url = post_m.call_args[0][0]
        assert url == f"{BASE}/services/svc-uuid/roles/role-id"

    def test_raises_on_post_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).map_role_to_service(service, role)

    def test_realm_forwarded_on_both_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        updated = {"id": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.pdp.library.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok(updated)) as get_m:
            Configuration.for_realm(REALM).map_role_to_service(service, role)
        assert post_m.call_args[1].get("params") == {"realm": REALM}
        assert get_m.call_args[1].get("params") == {"realm": REALM}


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
        with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok([])) as m:
            getattr(Configuration.for_realm(REALM), method)()
        m.assert_called_once_with(f"{BASE}/{endpoint}", params={"realm": REALM})


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_CONFIG_URL", raising=False)
    with patch("aiac.pdp.library.configuration.api.requests.get", return_value=_ok([])) as m:
        Configuration.for_realm(REALM).get_subjects()
    assert m.call_args[0][0].startswith("http://127.0.0.1:7071")
