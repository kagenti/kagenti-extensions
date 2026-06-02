"""Unit tests for aiac/pdp/service/policy/keycloak/main.py FastAPI application."""

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from keycloak.exceptions import KeycloakError

from aiac.pdp.service.policy.keycloak.main import app, get_admin

REALM = "kagenti"


def _make_client(admin_mock: MagicMock) -> TestClient:
    app.dependency_overrides[get_admin] = lambda realm=None: admin_mock
    return TestClient(app)


def _keycloak_error():
    return KeycloakError(error_message="connection refused", response_code=503)


# ---------------------------------------------------------------------------
# POST /roles/{role_name}/composites → 204
# ---------------------------------------------------------------------------


class TestAddRoleComposites:
    def test_returns_204(self):
        admin = MagicMock()
        resp = _make_client(admin).post(
            f"/roles/admin/composites?realm={REALM}",
            json=[{"id": "r1", "name": "viewer"}],
        )
        assert resp.status_code == 204
        admin.add_composite_realm_roles_to_role.assert_called_once()

    def test_keycloak_error_returns_502(self):
        admin = MagicMock()
        admin.add_composite_realm_roles_to_role.side_effect = _keycloak_error()
        resp = _make_client(admin).post(
            f"/roles/admin/composites?realm={REALM}",
            json=[{"id": "r1", "name": "viewer"}],
        )
        assert resp.status_code == 502

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /roles/{role_name}/composites → 204
# ---------------------------------------------------------------------------


class TestRemoveRoleComposites:
    def test_returns_204(self):
        admin = MagicMock()
        resp = _make_client(admin).request(
            "DELETE",
            f"/roles/admin/composites?realm={REALM}",
            json=[{"id": "r1", "name": "viewer"}],
        )
        assert resp.status_code == 204
        admin.remove_composite_realm_roles_to_role.assert_called_once()

    def test_keycloak_error_returns_502(self):
        admin = MagicMock()
        admin.remove_composite_realm_roles_to_role.side_effect = _keycloak_error()
        resp = _make_client(admin).request(
            "DELETE",
            f"/roles/admin/composites?realm={REALM}",
            json=[{"id": "r1", "name": "viewer"}],
        )
        assert resp.status_code == 502

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# DELETE /composites → 204
# ---------------------------------------------------------------------------


class TestClearAllComposites:
    def test_returns_204(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [
            {"id": "r1", "name": "admin"},
            {"id": "r2", "name": "viewer"},
        ]
        admin.get_composite_realm_roles_of_role.return_value = [{"id": "r2", "name": "viewer"}]
        resp = _make_client(admin).request("DELETE", f"/composites?realm={REALM}")
        assert resp.status_code == 204

    def test_iterates_all_roles_and_removes_composites(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [{"id": "r1", "name": "admin"}]
        admin.get_composite_realm_roles_of_role.return_value = [{"id": "r2", "name": "viewer"}]
        _make_client(admin).request("DELETE", f"/composites?realm={REALM}")
        admin.remove_composite_realm_roles_to_role.assert_called_once()

    def test_skips_role_with_no_composites(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [{"id": "r1", "name": "admin"}]
        admin.get_composite_realm_roles_of_role.return_value = []
        _make_client(admin).request("DELETE", f"/composites?realm={REALM}")
        admin.remove_composite_realm_roles_to_role.assert_not_called()

    def test_keycloak_error_returns_502(self):
        admin = MagicMock()
        admin.get_realm_roles.side_effect = _keycloak_error()
        resp = _make_client(admin).request("DELETE", f"/composites?realm={REALM}")
        assert resp.status_code == 502

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /services/{service_id}/permissions → 201 + JSON
# ---------------------------------------------------------------------------


class TestCreateServicePermission:
    def test_returns_201_with_created_role(self):
        admin = MagicMock()
        admin.create_client_role.return_value = {"id": "cr1", "name": "view-data"}
        resp = _make_client(admin).post(
            f"/services/svc-uuid/permissions?realm={REALM}",
            json={"name": "view-data", "description": "View data permission"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "view-data"

    def test_keycloak_error_returns_502(self):
        admin = MagicMock()
        admin.create_client_role.side_effect = _keycloak_error()
        resp = _make_client(admin).post(
            f"/services/svc-uuid/permissions?realm={REALM}",
            json={"name": "view-data", "description": ""},
        )
        assert resp.status_code == 502

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /services/{service_id}/scopes → 201 + JSON
# ---------------------------------------------------------------------------


class TestCreateServiceScope:
    def test_returns_201_with_created_scope(self):
        admin = MagicMock()
        admin.create_client_scope.return_value = {"id": "sc1", "name": "read:data"}
        resp = _make_client(admin).post(
            f"/services/svc-uuid/scopes?realm={REALM}",
            json={"name": "read:data", "description": "Read data scope"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "read:data"
        admin.add_default_default_client_scope.assert_called_once()

    def test_keycloak_error_returns_502(self):
        admin = MagicMock()
        admin.create_client_scope.side_effect = _keycloak_error()
        resp = _make_client(admin).post(
            f"/services/svc-uuid/scopes?realm={REALM}",
            json={"name": "read:data", "description": ""},
        )
        assert resp.status_code == 502

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Realm query parameter: optional
# ---------------------------------------------------------------------------


class TestRealmQueryParam:
    def test_no_realm_returns_200(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = []
        admin.get_composite_realm_roles_of_role.return_value = []
        app.dependency_overrides[get_admin] = lambda realm=None: admin
        resp = TestClient(app).request("DELETE", "/composites")
        assert resp.status_code == 204

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
