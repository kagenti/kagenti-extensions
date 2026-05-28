"""Unit tests for aiac/service/main.py FastAPI application."""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from keycloak.exceptions import KeycloakError

from aiac.keycloak.service.main import app, get_admin

REALM = "kagenti"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(admin_mock: MagicMock) -> TestClient:
    app.dependency_overrides[get_admin] = lambda realm: admin_mock
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /users
# ---------------------------------------------------------------------------


class TestGetUsers:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_users.return_value = [{"id": "u1", "username": "alice"}]
        client = _make_client(admin)

        resp = client.get(f"/users?realm={REALM}")

        assert resp.status_code == 200
        assert resp.json() == [{"id": "u1", "username": "alice"}]


class TestGetRealmRoles:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [{"id": "r1", "name": "admin"}]
        client = _make_client(admin)

        resp = client.get(f"/realm-roles?realm={REALM}")

        assert resp.status_code == 200
        assert resp.json() == [{"id": "r1", "name": "admin"}]


class TestGetClients:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_clients.return_value = [{"id": "c1", "clientId": "my-app"}]
        client = _make_client(admin)

        resp = client.get(f"/clients?realm={REALM}")

        assert resp.status_code == 200
        assert resp.json() == [{"id": "c1", "clientId": "my-app"}]


class TestGetClientScopes:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_client_scopes.return_value = [{"id": "s1", "name": "email"}]
        client = _make_client(admin)

        resp = client.get(f"/client-scopes?realm={REALM}")

        assert resp.status_code == 200
        assert resp.json() == [{"id": "s1", "name": "email"}]


class TestGetUserRoleMappings:
    def test_returns_json_object_with_realm_and_client_mappings(self):
        admin = MagicMock()
        admin.get_all_roles_of_user.return_value = {
            "realmMappings": [{"id": "r1", "name": "admin"}],
            "clientMappings": {"account": {"id": "a1", "mappings": []}},
        }
        client = _make_client(admin)

        resp = client.get(f"/users/user-uuid/role-mappings?realm={REALM}")

        assert resp.status_code == 200
        body = resp.json()
        assert "realmMappings" in body
        assert "clientMappings" in body


class TestGetClientRoles:
    def test_returns_json_array(self):
        admin = MagicMock()
        admin.get_client_roles.return_value = [{"id": "cr1", "name": "view-clients"}]
        client = _make_client(admin)

        resp = client.get(f"/clients/client-uuid/roles?realm={REALM}")

        assert resp.status_code == 200
        assert resp.json() == [{"id": "cr1", "name": "view-clients"}]


class TestAssignClientRoleMappings:
    def test_post_returns_204(self):
        admin = MagicMock()
        client = _make_client(admin)

        resp = client.post(
            f"/users/user-uuid/role-mappings/clients/client-uuid?realm={REALM}",
            json=[{"id": "cr1", "name": "view-clients"}],
        )

        assert resp.status_code == 204

    def test_delete_returns_204(self):
        admin = MagicMock()
        client = _make_client(admin)

        resp = client.request(
            "DELETE",
            f"/users/user-uuid/role-mappings/clients/client-uuid?realm={REALM}",
            json=[{"id": "cr1", "name": "view-clients"}],
        )

        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Realm query parameter: KeycloakAdmin instantiated with the supplied realm
# ---------------------------------------------------------------------------


class TestRealmQueryParam:
    def test_realm_param_creates_per_realm_admin(self):
        """Every request instantiates a KeycloakAdmin bound to the supplied realm."""
        app.dependency_overrides.clear()

        admin_mock = MagicMock()
        admin_mock.get_users.return_value = [{"id": "u1", "username": "alice"}]

        env = {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_ADMIN_USERNAME": "admin",
            "KEYCLOAK_ADMIN_PASSWORD": "admin",
        }
        with patch.dict(os.environ, env), \
             patch("aiac.keycloak.service.main.KeycloakAdmin", return_value=admin_mock) as mock_cls:
            client = TestClient(app)
            resp = client.get(f"/users?realm={REALM}")

        assert resp.status_code == 200
        mock_cls.assert_called_once_with(
            server_url="http://keycloak:8080/",
            realm_name=REALM,
            username="admin",
            password="admin",
        )

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# KeycloakError → 502 on all endpoints
# ---------------------------------------------------------------------------


def _keycloak_error():
    return KeycloakError(error_message="connection refused", response_code=503)


class TestKeycloakErrorProduces502:
    def test_get_users(self):
        admin = MagicMock()
        admin.get_users.side_effect = _keycloak_error()
        resp = _make_client(admin).get(f"/users?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_get_realm_roles(self):
        admin = MagicMock()
        admin.get_realm_roles.side_effect = _keycloak_error()
        resp = _make_client(admin).get(f"/realm-roles?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_get_clients(self):
        admin = MagicMock()
        admin.get_clients.side_effect = _keycloak_error()
        resp = _make_client(admin).get(f"/clients?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_get_client_scopes(self):
        admin = MagicMock()
        admin.get_client_scopes.side_effect = _keycloak_error()
        resp = _make_client(admin).get(f"/client-scopes?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_get_user_role_mappings(self):
        admin = MagicMock()
        admin.get_all_roles_of_user.side_effect = _keycloak_error()
        resp = _make_client(admin).get(f"/users/user-uuid/role-mappings?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_get_client_roles(self):
        admin = MagicMock()
        admin.get_client_roles.side_effect = _keycloak_error()
        resp = _make_client(admin).get(f"/clients/client-uuid/roles?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_post_client_role_mappings(self):
        admin = MagicMock()
        admin.assign_client_role.side_effect = _keycloak_error()
        resp = _make_client(admin).post(
            f"/users/user-uuid/role-mappings/clients/client-uuid?realm={REALM}",
            json=[{"id": "cr1", "name": "view-clients"}],
        )
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_delete_client_role_mappings(self):
        admin = MagicMock()
        admin.delete_client_roles_of_user.side_effect = _keycloak_error()
        resp = _make_client(admin).request(
            "DELETE",
            f"/users/user-uuid/role-mappings/clients/client-uuid?realm={REALM}",
            json=[{"id": "cr1", "name": "view-clients"}],
        )
        assert resp.status_code == 502
        assert "error" in resp.json()
