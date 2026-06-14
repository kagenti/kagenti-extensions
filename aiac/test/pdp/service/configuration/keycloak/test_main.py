"""Unit tests for aiac/pdp/service/configuration/keycloak/main.py FastAPI application."""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from keycloak.exceptions import KeycloakError

from aiac.pdp.service.configuration.keycloak.main import app, get_admin

REALM = "kagenti"


def _make_client(admin_mock: MagicMock) -> TestClient:
    app.dependency_overrides[get_admin] = lambda realm=None: admin_mock
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /subjects
# ---------------------------------------------------------------------------


class TestGetSubjects:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_users.return_value = [{"id": "u1", "username": "alice"}]
        resp = _make_client(admin).get(f"/subjects?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "u1", "username": "alice"}]


# ---------------------------------------------------------------------------
# GET /roles
# ---------------------------------------------------------------------------


class TestGetRoles:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [{"id": "r1", "name": "admin"}]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "r1", "name": "admin"}]


# ---------------------------------------------------------------------------
# GET /services
# ---------------------------------------------------------------------------


class TestGetServices:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_clients.return_value = [{"id": "c1", "clientId": "my-app"}]
        resp = _make_client(admin).get(f"/services?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "c1", "clientId": "my-app"}]


# ---------------------------------------------------------------------------
# GET /scopes
# ---------------------------------------------------------------------------


class TestGetScopes:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_client_scopes.return_value = [{"id": "s1", "name": "email"}]
        resp = _make_client(admin).get(f"/scopes?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "s1", "name": "email"}]


# ---------------------------------------------------------------------------
# GET /subjects/{subject_id}/assignments
# ---------------------------------------------------------------------------


class TestGetSubjectAssignments:
    def test_returns_object_with_realm_and_service_mappings(self):
        admin = MagicMock()
        admin.get_all_roles_of_user.return_value = {
            "realmMappings": [{"id": "r1", "name": "admin"}],
            "clientMappings": {"account": {"id": "a1", "mappings": []}},
        }
        resp = _make_client(admin).get(f"/subjects/user-uuid/assignments?realm={REALM}")
        assert resp.status_code == 200
        body = resp.json()
        assert "realmMappings" in body
        assert "serviceMappings" in body


# ---------------------------------------------------------------------------
# GET /services/{service_id}/permissions
# ---------------------------------------------------------------------------


class TestGetServicePermissions:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_client_roles.return_value = [{"id": "cr1", "name": "view-clients"}]
        resp = _make_client(admin).get(f"/services/svc-uuid/permissions?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "cr1", "name": "view-clients"}]


# ---------------------------------------------------------------------------
# GET /roles/{role_name}/composites
# ---------------------------------------------------------------------------


class TestGetRoleComposites:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_composite_realm_roles_of_role.return_value = [{"id": "r2", "name": "viewer"}]
        resp = _make_client(admin).get(f"/roles/admin/composites?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "r2", "name": "viewer"}]
        admin.get_composite_realm_roles_of_role.assert_called_once_with(role_name="admin")


# ---------------------------------------------------------------------------
# Realm query parameter: optional, singleton at startup
# ---------------------------------------------------------------------------


class TestRealmQueryParam:
    def test_no_realm_returns_200(self):
        admin = MagicMock()
        admin.get_users.return_value = []
        app.dependency_overrides[get_admin] = lambda realm=None: admin
        resp = TestClient(app).get("/subjects")
        assert resp.status_code == 200

    def test_realm_param_creates_per_realm_admin(self):
        app.dependency_overrides.clear()
        admin_mock = MagicMock()
        admin_mock.get_users.return_value = []
        env = {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_REALM": "master",
            "KEYCLOAK_ADMIN_USERNAME": "admin",
            "KEYCLOAK_ADMIN_PASSWORD": "admin",
        }
        with patch.dict(os.environ, env), \
             patch("aiac.pdp.service.configuration.keycloak.main.KeycloakAdmin", return_value=admin_mock) as mock_cls:
            with TestClient(app) as client:
                resp = client.get(f"/subjects?realm={REALM}")
        assert resp.status_code == 200
        # First call is the startup singleton; second is per-realm
        calls = mock_cls.call_args_list
        per_realm = [c for c in calls if c.kwargs.get("realm_name") == REALM]
        assert len(per_realm) == 1

    def test_startup_creates_singleton_with_keycloak_realm(self):
        app.dependency_overrides.clear()
        admin_mock = MagicMock()
        admin_mock.get_users.return_value = []
        env = {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_REALM": "master",
            "KEYCLOAK_ADMIN_USERNAME": "admin",
            "KEYCLOAK_ADMIN_PASSWORD": "admin",
        }
        with patch.dict(os.environ, env), \
             patch("aiac.pdp.service.configuration.keycloak.main.KeycloakAdmin", return_value=admin_mock) as mock_cls:
            with TestClient(app) as client:
                pass  # lifespan runs
        startup_calls = [c for c in mock_cls.call_args_list if c.kwargs.get("realm_name") == "master"]
        assert len(startup_calls) == 1

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /health (readiness probe — pings Keycloak)
# ---------------------------------------------------------------------------


class TestHealth:
    def test_returns_200_when_keycloak_reachable(self):
        admin = MagicMock()
        resp = _make_client(admin).get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        admin.get_server_info.assert_called_once()

    def test_returns_503_when_keycloak_unreachable(self):
        admin = MagicMock()
        admin.get_server_info.side_effect = KeycloakError(
            error_message="connection refused", response_code=503
        )
        resp = _make_client(admin).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "error" in body

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /services/{service_id}/scopes
# ---------------------------------------------------------------------------


class TestCreateScope:
    def test_returns_201_with_scope_json(self):
        admin = MagicMock()
        admin.create_client_scope.return_value = "new-scope-id"
        admin.get_client_scope.return_value = {
            "id": "new-scope-id",
            "name": "read:data",
            "description": "Read access",
        }
        resp = _make_client(admin).post(
            f"/services/svc-uuid/scopes?realm={REALM}",
            json={"name": "read:data", "description": "Read access"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "new-scope-id"
        assert body["name"] == "read:data"

    def test_assigns_scope_as_default_to_service(self):
        admin = MagicMock()
        admin.create_client_scope.return_value = "scope-id-42"
        admin.get_client_scope.return_value = {"id": "scope-id-42", "name": "write"}
        _make_client(admin).post(
            f"/services/svc-abc/scopes?realm={REALM}",
            json={"name": "write", "description": "Write access"},
        )
        admin.add_default_default_client_scope.assert_called_once_with("svc-abc", "scope-id-42")

    def test_creates_scope_with_openid_connect_protocol(self):
        admin = MagicMock()
        admin.create_client_scope.return_value = "sid"
        admin.get_client_scope.return_value = {"id": "sid", "name": "read"}
        _make_client(admin).post(
            f"/services/svc/scopes?realm={REALM}",
            json={"name": "read", "description": "desc"},
        )
        call_payload = admin.create_client_scope.call_args[0][0]
        assert call_payload["protocol"] == "openid-connect"
        assert call_payload["name"] == "read"
        assert call_payload["description"] == "desc"

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.create_client_scope.side_effect = KeycloakError(
            error_message="backend failure", response_code=500
        )
        resp = _make_client(admin).post(
            f"/services/svc/scopes?realm={REALM}",
            json={"name": "read", "description": "desc"},
        )
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# KeycloakError → 502 on all endpoints
# ---------------------------------------------------------------------------


def _keycloak_error():
    return KeycloakError(error_message="connection refused", response_code=503)


class TestKeycloakErrorProduces502:
    def test_get_subjects(self):
        admin = MagicMock()
        admin.get_users.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/subjects?realm={REALM}").status_code == 502

    def test_get_roles(self):
        admin = MagicMock()
        admin.get_realm_roles.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/roles?realm={REALM}").status_code == 502

    def test_get_services(self):
        admin = MagicMock()
        admin.get_clients.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/services?realm={REALM}").status_code == 502

    def test_get_scopes(self):
        admin = MagicMock()
        admin.get_client_scopes.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/scopes?realm={REALM}").status_code == 502

    def test_get_subject_assignments(self):
        admin = MagicMock()
        admin.get_all_roles_of_user.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/subjects/u1/assignments?realm={REALM}").status_code == 502

    def test_get_service_permissions(self):
        admin = MagicMock()
        admin.get_client_roles.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/services/s1/permissions?realm={REALM}").status_code == 502

    def test_get_role_composites(self):
        admin = MagicMock()
        admin.get_composite_realm_roles_of_role.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/roles/admin/composites?realm={REALM}").status_code == 502

    def teardown_method(self):
        app.dependency_overrides.clear()
