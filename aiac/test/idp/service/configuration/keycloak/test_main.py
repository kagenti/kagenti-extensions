"""Unit tests for aiac/idp/service/configuration/keycloak/main.py FastAPI application."""

import base64
import json
import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from keycloak.exceptions import KeycloakError

from aiac.idp.service.configuration.keycloak.main import _cache, app, get_admin

REALM = "rossoctl"


def _make_client(admin_mock: MagicMock) -> TestClient:
    app.dependency_overrides[get_admin] = lambda realm=None: admin_mock
    return TestClient(app)


def _make_jwt(payload: dict) -> str:
    """Encode a payload into a `header.payload.sig` JWT shape (unsigned — the endpoint only
    base64-decodes the payload to read iss/aud; it never verifies the signature)."""
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"h.{seg}.s"


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
        # Realm roles are user roles (clientRole == false) -> kind=User (handoff 02).
        assert resp.json() == [{"id": "r1", "name": "admin", "kind": "User"}]

    def test_requests_full_representation_for_attributes(self):
        # The aiac.managed marker lives in role attributes, which Keycloak's brief
        # representation omits — the endpoint must ask for the full representation.
        admin = MagicMock()
        admin.get_realm_roles.return_value = []
        _make_client(admin).get(f"/roles?realm={REALM}")
        admin.get_realm_roles.assert_called_once_with(brief_representation=False)

    def test_populates_user_kind_on_all_realm_roles(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [
            {"id": "r1", "name": "reader"},
            {"id": "r2", "name": "default-roles-rossoctl"},
        ]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        # default-roles-rossoctl is the Keycloak default composite for this realm -> excluded.
        assert [r["kind"] for r in resp.json()] == ["User"]

    def test_excludes_default_roles_composite_for_realm(self):
        # The default composite (default-roles-{realm}) is the sole path to Keycloak's
        # built-ins (offline_access, uma_authorization, view-profile, account roles) --
        # it must never appear in the response, and its members must never be scanned.
        admin = MagicMock()
        admin.get_realm_roles.return_value = [
            {"id": "r1", "name": "reader"},
            {"id": "r2", "name": f"default-roles-{REALM}"},
        ]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        names = [r["name"] for r in resp.json()]
        assert f"default-roles-{REALM}" not in names
        assert names == ["reader"]
        admin.get_realm_role_members.assert_not_called()

    def test_aiac_managed_role_gets_member_usernames_as_actor_ids(self):
        # For a user (realm) role, actorIds = the member usernames — resolved via the same
        # get_realm_role_members call that GET /subjects?role_id= uses (SPM/APM alignment).
        admin = MagicMock()
        admin.get_realm_roles.return_value = [
            {"id": "r1", "name": "invoicing", "attributes": {"aiac.managed": ["true"]}},
        ]
        admin.get_realm_role_members.return_value = [
            {"id": "u1", "username": "alice"},
            {"id": "u2", "username": "bob"},
        ]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        role = resp.json()[0]
        assert role["kind"] == "User"
        assert role["actorIds"] == ["alice", "bob"]
        admin.get_realm_role_members.assert_called_once_with("invoicing")

    def test_non_managed_role_skips_member_query(self):
        # Built-ins / non-AIAC roles are not enriched with actorIds (no member scan).
        admin = MagicMock()
        admin.get_realm_roles.return_value = [{"id": "r1", "name": "admin"}]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        assert "actorIds" not in resp.json()[0]
        admin.get_realm_role_members.assert_not_called()


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
# GET /services/{service_id}/roles
# ---------------------------------------------------------------------------


class TestListServiceRoles:
    def test_sources_client_roles_and_service_account_realm_roles(self):
        # The endpoint returns both client roles (kind=Agent via clientRole=true) and
        # aiac-managed realm roles assigned to the service account (kind=Agent via the
        # provisioning path used by the Configuration library).
        admin = MagicMock()
        admin.get_client_roles.return_value = [{"id": "cr1", "name": "invoke", "clientRole": True}]
        admin.get_client.return_value = {"id": "svc-uuid", "clientId": "github-agent"}
        sa_user = {"id": "sa-uid"}
        admin.get_client_service_account_user.return_value = sa_user
        admin.get_realm_roles_of_user.return_value = []
        resp = _make_client(admin).get(f"/services/svc-uuid/roles?realm={REALM}")
        assert resp.status_code == 200
        admin.get_client_roles.assert_called_once_with("svc-uuid")
        admin.get_client_service_account_user.assert_called_once_with("svc-uuid")
        admin.get_realm_roles_of_user.assert_called_once_with(sa_user["id"])

    def test_populates_agent_kind_and_owner_actor_ids(self):
        # clientRole == true -> kind=Agent; actorIds = the owning client's serviceId,
        # resolved from the role's containerId -> client.
        admin = MagicMock()
        admin.get_client_roles.return_value = [
            {"id": "cr1", "name": "invoke", "clientRole": True, "containerId": "svc-uuid"},
        ]
        admin.get_client.return_value = {"id": "svc-uuid", "clientId": "github-agent"}
        admin.get_client_service_account_user.return_value = {"id": "sa-uid"}
        admin.get_realm_roles_of_user.return_value = []
        resp = _make_client(admin).get(f"/services/svc-uuid/roles?realm={REALM}")
        assert resp.status_code == 200
        role = resp.json()[0]
        assert role["kind"] == "Agent"
        assert role["actorIds"] == ["github-agent"]

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.get_client_roles.side_effect = KeycloakError(
            error_message="not found", response_code=404
        )
        resp = _make_client(admin).get(f"/services/svc-uuid/roles?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_returns_empty_list_when_client_has_no_client_roles(self):
        admin = MagicMock()
        admin.get_client_roles.side_effect = KeycloakError(
            error_message="Client not found", response_code=400
        )
        resp = _make_client(admin).get(f"/services/svc-uuid/roles?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == []

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Assumption 1 (no cross-kind role) — fail loud (handoff 02)
# ---------------------------------------------------------------------------


class TestCrossKindEnforcement:
    def test_role_held_by_users_and_service_accounts_returns_409(self):
        # A role held by *both* human users and agent service accounts cannot be represented
        # by a single actorIds list — fail loud rather than silently picking a side.
        admin = MagicMock()
        admin.get_realm_roles.return_value = [
            {"id": "r1", "name": "shared", "attributes": {"aiac.managed": ["true"]}},
        ]
        admin.get_realm_role_members.return_value = [
            {"id": "u1", "username": "alice"},
            {"id": "sa", "username": "service-account-github-agent"},
        ]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        assert resp.status_code == 409
        assert "error" in resp.json()

    def test_role_held_only_by_users_is_ok(self):
        admin = MagicMock()
        admin.get_realm_roles.return_value = [
            {"id": "r1", "name": "readers", "attributes": {"aiac.managed": ["true"]}},
        ]
        admin.get_realm_role_members.return_value = [
            {"id": "u1", "username": "alice"},
            {"id": "u2", "username": "bob"},
        ]
        resp = _make_client(admin).get(f"/roles?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json()[0]["actorIds"] == ["alice", "bob"]

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /services/{service_id}/scopes
# ---------------------------------------------------------------------------


class TestListServiceScopes:
    def test_returns_json_array_with_owner_service_id(self):
        # Scope.serviceId = the owning client (the service exposing the scope), resolved to the
        # client's serviceId (clientId) — the single owner for a per-service scope listing.
        admin = MagicMock()
        admin.get_client_default_client_scopes.return_value = [
            {"id": "sc1", "name": "profile"},
            {"id": "sc2", "name": "email"},
        ]
        admin.get_client.return_value = {"id": "svc-uuid", "clientId": "github-agent"}
        resp = _make_client(admin).get(f"/services/svc-uuid/scopes?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [
            {"id": "sc1", "name": "profile", "serviceId": "github-agent"},
            {"id": "sc2", "name": "email", "serviceId": "github-agent"},
        ]
        admin.get_client.assert_called_once_with("svc-uuid")

    def test_verifies_get_client_default_client_scopes_called(self):
        admin = MagicMock()
        admin.get_client_default_client_scopes.return_value = []
        _make_client(admin).get(f"/services/svc-uuid/scopes?realm={REALM}")
        admin.get_client_default_client_scopes.assert_called_once_with("svc-uuid")

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.get_client_default_client_scopes.side_effect = KeycloakError(
            error_message="not found", response_code=404
        )
        resp = _make_client(admin).get(f"/services/svc-uuid/scopes?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Assumption 2 (single scope owner) — fail loud (handoff 02)
# ---------------------------------------------------------------------------


class TestSingleScopeOwnerEnforcement:
    _MANAGED = {"id": "aud-1", "name": "svc-a-aud", "attributes": {"aiac.managed": "true"}}

    def test_aiac_managed_scope_with_multiple_owners_returns_409(self):
        # An AIAC-managed client scope must have exactly one owning client; if more than one
        # client exposes it as a default scope, a single Scope.serviceId cannot represent it.
        admin = MagicMock()
        admin.get_client_default_client_scopes.return_value = [self._MANAGED]
        admin.get_client.return_value = {"id": "svc-a", "clientId": "svc-a"}
        admin.get_clients.return_value = [{"id": "svc-a"}, {"id": "svc-b"}]
        resp = _make_client(admin).get(f"/services/svc-a/scopes?realm={REALM}")
        assert resp.status_code == 409
        assert "error" in resp.json()

    def test_aiac_managed_scope_with_single_owner_is_ok(self):
        admin = MagicMock()
        admin.get_client.return_value = {"id": "svc-a", "clientId": "svc-a"}
        admin.get_clients.return_value = [{"id": "svc-a"}, {"id": "svc-b"}]

        def _defaults(client_id):
            return [self._MANAGED] if client_id == "svc-a" else []

        admin.get_client_default_client_scopes.side_effect = _defaults
        resp = _make_client(admin).get(f"/services/svc-a/scopes?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json()[0]["serviceId"] == "svc-a"

    def test_non_managed_scope_skips_owner_scan(self):
        # Built-in / non-AIAC scopes are not subject to the single-owner invariant (Keycloak
        # ships them assigned to many clients) — no cross-client scan runs for them.
        admin = MagicMock()
        admin.get_client_default_client_scopes.return_value = [{"id": "sc1", "name": "profile"}]
        admin.get_client.return_value = {"id": "svc-a", "clientId": "svc-a"}
        resp = _make_client(admin).get(f"/services/svc-a/scopes?realm={REALM}")
        assert resp.status_code == 200
        admin.get_clients.assert_not_called()

    def teardown_method(self):
        app.dependency_overrides.clear()


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
# Realm query parameter: required, lazy per-realm cache
# ---------------------------------------------------------------------------


class TestRealmQueryParam:
    def test_missing_realm_returns_422(self):
        app.dependency_overrides.clear()
        resp = TestClient(app).get("/subjects")
        assert resp.status_code == 422

    def test_realm_param_creates_admin_with_admin_realm(self):
        _cache.clear()
        app.dependency_overrides.clear()
        admin_mock = MagicMock()
        admin_mock.get_users.return_value = []
        env = {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_ADMIN_REALM": "master",
            "KEYCLOAK_ADMIN_USERNAME": "admin",
            "KEYCLOAK_ADMIN_PASSWORD": "admin",
        }
        with patch.dict(os.environ, env), \
             patch("aiac.idp.service.configuration.keycloak.main.KeycloakAdmin", return_value=admin_mock) as mock_cls:
            with TestClient(app) as client:
                resp = client.get(f"/subjects?realm={REALM}")
        assert resp.status_code == 200
        mock_cls.assert_called_once_with(
            server_url="http://keycloak:8080/",
            realm_name=REALM,
            user_realm_name="master",
            username="admin",
            password="admin",
        )

    def test_second_request_same_realm_hits_cache(self):
        _cache.clear()
        app.dependency_overrides.clear()
        admin_mock = MagicMock()
        admin_mock.get_users.return_value = []
        env = {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_ADMIN_REALM": "master",
            "KEYCLOAK_ADMIN_USERNAME": "admin",
            "KEYCLOAK_ADMIN_PASSWORD": "admin",
        }
        with patch.dict(os.environ, env), \
             patch("aiac.idp.service.configuration.keycloak.main.KeycloakAdmin", return_value=admin_mock) as mock_cls:
            with TestClient(app) as client:
                client.get(f"/subjects?realm={REALM}")
                client.get(f"/subjects?realm={REALM}")
        assert mock_cls.call_count == 1

    def teardown_method(self):
        app.dependency_overrides.clear()
        _cache.clear()


# ---------------------------------------------------------------------------
# GET /health (readiness probe — pings Keycloak)
# ---------------------------------------------------------------------------


_HEALTH_ENV = {"KEYCLOAK_ADMIN_REALM": "master"}
_HEALTH_TARGET = "aiac.idp.service.configuration.keycloak.main._get_or_create_admin"


class TestHealth:
    def test_returns_200_when_keycloak_reachable(self):
        admin = MagicMock()
        with patch(_HEALTH_TARGET, return_value=admin), patch.dict(os.environ, _HEALTH_ENV):
            resp = TestClient(app).get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        admin.get_server_info.assert_called_once()

    def test_returns_503_when_keycloak_unreachable(self):
        admin = MagicMock()
        admin.get_server_info.side_effect = KeycloakError(
            error_message="connection refused", response_code=503
        )
        with patch(_HEALTH_TARGET, return_value=admin), patch.dict(os.environ, _HEALTH_ENV):
            resp = TestClient(app).get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unavailable"
        assert "error" in body

    def test_uses_admin_realm_env_var(self):
        admin = MagicMock()
        with patch(_HEALTH_TARGET, return_value=admin) as mock_factory, \
             patch.dict(os.environ, {"KEYCLOAK_ADMIN_REALM": "master"}):
            TestClient(app).get("/health")
        mock_factory.assert_called_once_with("master")


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
        admin.add_client_default_client_scope.assert_called_once_with("svc-abc", "scope-id-42", {})

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
        assert call_payload["attributes"] == {"aiac.managed": "true"}

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
# GET /services/{service_id}
# ---------------------------------------------------------------------------


class TestGetService:
    def test_returns_200_with_client_json(self):
        admin = MagicMock()
        admin.get_client.return_value = {"id": "svc-uuid", "clientId": "my-app"}
        resp = _make_client(admin).get(f"/services/svc-uuid?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json()["id"] == "svc-uuid"
        admin.get_client.assert_called_once_with("svc-uuid")

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.get_client.side_effect = KeycloakError(error_message="not found", response_code=404)
        resp = _make_client(admin).get(f"/services/svc-uuid?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /services/{service_id}/discovery-token
# ---------------------------------------------------------------------------


class TestMintDiscoveryToken:
    ISS = "http://keycloak.localtest.me:8080/realms/rossoctl"

    def _wire(self, admin, monkeypatch, *, aud, iss=None, mappers=None, secret="sek"):
        monkeypatch.setenv("KEYCLOAK_URL", "http://kc-internal:8080")
        monkeypatch.delenv("AIAC_KEYCLOAK_ISSUER", raising=False)
        admin.get_client.return_value = {
            "id": "svc-uuid", "clientId": "github-tool", "secret": secret
        }
        admin.get_mappers_from_client.return_value = mappers if mappers is not None else []
        oid = MagicMock()
        oid.token.return_value = {"access_token": _make_jwt({"aud": aud, "iss": iss or self.ISS})}
        return oid

    def test_returns_200_with_token_and_resolves_client_id(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(admin, monkeypatch, aud=["github-tool"])
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            resp = _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["access_token"]
        assert body["client_id"] == "github-tool"
        assert "github-tool" in body["audience"]
        admin.get_client.assert_called_once_with("svc-uuid")
        oid.token.assert_called_once_with(grant_type="client_credentials")

    def test_adds_audience_mapper_when_absent(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(admin, monkeypatch, aud=["github-tool"], mappers=[])
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        admin.add_mapper_to_client.assert_called_once()

    def test_idempotent_when_mapper_present(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(
            admin, monkeypatch, aud=["github-tool"],
            mappers=[{"name": "aiac-discovery-audience"}],
        )
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        admin.add_mapper_to_client.assert_not_called()

    def test_does_not_regenerate_secret(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(admin, monkeypatch, aud=["github-tool"])
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        admin.generate_client_secrets.assert_not_called()

    def test_502_when_aud_missing_client_id(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(admin, monkeypatch, aud=["account"])
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            resp = _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        assert resp.status_code == 502
        assert "does not contain" in resp.json()["error"]

    def test_502_when_no_secret(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(admin, monkeypatch, aud=["github-tool"], secret=None)
        admin.get_client_secrets.return_value = {}  # no secret available via the secrets endpoint
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            resp = _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        assert resp.status_code == 502
        assert "no readable secret" in resp.json()["error"]

    def test_hard_iss_assertion_when_env_set(self, monkeypatch):
        admin = MagicMock()
        oid = self._wire(
            admin, monkeypatch, aud=["github-tool"], iss="http://kc-internal:8080/realms/rossoctl"
        )
        monkeypatch.setenv("AIAC_KEYCLOAK_ISSUER", self.ISS)
        with patch(
            "aiac.idp.service.configuration.keycloak.main.KeycloakOpenID", return_value=oid
        ):
            resp = _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        assert resp.status_code == 502
        assert "iss" in resp.json()["error"]

    def test_502_on_keycloak_error(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_URL", "http://kc-internal:8080")
        admin = MagicMock()
        admin.get_client.side_effect = KeycloakError(error_message="not found", response_code=404)
        resp = _make_client(admin).get(f"/services/svc-uuid/discovery-token?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /services/{service_id}/type
# ---------------------------------------------------------------------------


class TestSetServiceType:
    def test_returns_200_with_updated_client(self):
        admin = MagicMock()
        admin.get_client.side_effect = [
            {"id": "svc-uuid", "clientId": "my-app", "attributes": {}},
            {"id": "svc-uuid", "clientId": "my-app", "attributes": {"client.type": "Agent"}},
        ]
        resp = _make_client(admin).post(
            f"/services/svc-uuid/type?realm={REALM}", json={"type": "Agent"}
        )
        assert resp.status_code == 200
        assert resp.json()["attributes"] == {"client.type": "Agent"}

    def test_sets_client_type_attribute_via_update_client(self):
        admin = MagicMock()
        admin.get_client.return_value = {"id": "svc-uuid", "attributes": {"existing": "keep"}}
        _make_client(admin).post(f"/services/svc-uuid/type?realm={REALM}", json={"type": "Tool"})
        # existing attributes preserved; client.type merged in (not clobbered)
        admin.update_client.assert_called_once_with(
            "svc-uuid", {"attributes": {"existing": "keep", "client.type": "Tool"}}
        )

    def test_stores_capitalized_plain_string_value(self):
        admin = MagicMock()
        admin.get_client.return_value = {"id": "svc-uuid", "attributes": {}}
        _make_client(admin).post(f"/services/svc-uuid/type?realm={REALM}", json={"type": "Agent"})
        payload = admin.update_client.call_args[0][1]
        assert payload["attributes"]["client.type"] == "Agent"  # plain string, not a list

    def test_rejects_invalid_type_with_422(self):
        admin = MagicMock()
        resp = _make_client(admin).post(
            f"/services/svc-uuid/type?realm={REALM}", json={"type": "agent"}
        )
        assert resp.status_code == 422

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.get_client.side_effect = KeycloakError(error_message="not found", response_code=404)
        resp = _make_client(admin).post(
            f"/services/svc-uuid/type?realm={REALM}", json={"type": "Agent"}
        )
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /scopes
# ---------------------------------------------------------------------------


class TestCreateScopeEndpoint:
    def test_returns_201_with_scope_json(self):
        admin = MagicMock()
        admin.create_client_scope.return_value = "new-scope-id"
        admin.get_client_scope.return_value = {
            "id": "new-scope-id",
            "name": "read:data",
            "description": "Read access",
        }
        resp = _make_client(admin).post(
            f"/scopes?realm={REALM}",
            json={"name": "read:data", "description": "Read access"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "new-scope-id"
        assert body["name"] == "read:data"

    def test_creates_scope_with_openid_connect_protocol(self):
        admin = MagicMock()
        admin.create_client_scope.return_value = "sid"
        admin.get_client_scope.return_value = {"id": "sid", "name": "read"}
        _make_client(admin).post(f"/scopes?realm={REALM}", json={"name": "read", "description": "desc"})
        payload = admin.create_client_scope.call_args[0][0]
        assert payload["protocol"] == "openid-connect"
        assert payload["name"] == "read"
        assert payload["description"] == "desc"
        assert payload["attributes"] == {"aiac.managed": "true"}

    def test_returns_409_on_duplicate_name(self):
        admin = MagicMock()
        admin.create_client_scope.side_effect = KeycloakError(
            error_message="Conflict", response_code=409
        )
        resp = _make_client(admin).post(f"/scopes?realm={REALM}", json={"name": "dupe", "description": ""})
        assert resp.status_code == 409

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.create_client_scope.side_effect = KeycloakError(
            error_message="backend failure", response_code=500
        )
        resp = _make_client(admin).post(f"/scopes?realm={REALM}", json={"name": "read", "description": "desc"})
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_realm_override_uses_per_realm_admin(self):
        _cache.clear()
        app.dependency_overrides.clear()
        admin_mock = MagicMock()
        admin_mock.create_client_scope.return_value = "s1"
        admin_mock.get_client_scope.return_value = {"id": "s1", "name": "x"}
        env = {
            "KEYCLOAK_URL": "http://keycloak:8080/",
            "KEYCLOAK_ADMIN_REALM": "master",
            "KEYCLOAK_ADMIN_USERNAME": "admin",
            "KEYCLOAK_ADMIN_PASSWORD": "admin",
        }
        with patch.dict(os.environ, env), \
             patch("aiac.idp.service.configuration.keycloak.main.KeycloakAdmin", return_value=admin_mock):
            with TestClient(app) as client:
                resp = client.post("/scopes?realm=other", json={"name": "x", "description": ""})
        assert resp.status_code == 201

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /services/{service_id}/scopes/{scope_id}
# ---------------------------------------------------------------------------


class TestAssignScopeToService:
    def test_returns_201_on_success(self):
        admin = MagicMock()
        resp = _make_client(admin).post(f"/services/svc-uuid/scopes/scope-id?realm={REALM}")
        assert resp.status_code == 201
        admin.add_client_default_client_scope.assert_called_once_with("svc-uuid", "scope-id", {})

    def test_returns_409_when_already_assigned(self):
        admin = MagicMock()
        admin.add_client_default_client_scope.side_effect = KeycloakError(
            error_message="Conflict", response_code=409
        )
        resp = _make_client(admin).post(f"/services/svc-uuid/scopes/scope-id?realm={REALM}")
        assert resp.status_code == 409

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.add_client_default_client_scope.side_effect = KeycloakError(
            error_message="failure", response_code=500
        )
        resp = _make_client(admin).post(f"/services/svc-uuid/scopes/scope-id?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /roles
# ---------------------------------------------------------------------------


class TestCreateRoleEndpoint:
    def test_returns_201_with_role_json(self):
        admin = MagicMock()
        admin.create_realm_role.return_value = "new-role-id"
        admin.get_realm_role.return_value = {
            "id": "new-role-id",
            "name": "reader",
            "description": "Read-only",
        }
        resp = _make_client(admin).post(
            f"/roles?realm={REALM}",
            json={"name": "reader", "description": "Read-only"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "new-role-id"
        assert body["name"] == "reader"

    def test_creates_role_with_correct_payload(self):
        admin = MagicMock()
        admin.create_realm_role.return_value = "rid"
        admin.get_realm_role.return_value = {"id": "rid", "name": "reader"}
        _make_client(admin).post(f"/roles?realm={REALM}", json={"name": "reader", "description": "desc"})
        payload = admin.create_realm_role.call_args[0][0]
        assert payload == {
            "name": "reader",
            "description": "desc",
            "attributes": {"aiac.managed": ["true"]},
        }

    def test_returns_409_on_duplicate_name(self):
        admin = MagicMock()
        admin.create_realm_role.side_effect = KeycloakError(
            error_message="Conflict", response_code=409
        )
        resp = _make_client(admin).post(f"/roles?realm={REALM}", json={"name": "dupe", "description": ""})
        assert resp.status_code == 409

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.create_realm_role.side_effect = KeycloakError(
            error_message="backend failure", response_code=500
        )
        resp = _make_client(admin).post(f"/roles?realm={REALM}", json={"name": "reader", "description": "desc"})
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /services/{service_id}/roles/{role_id}
# ---------------------------------------------------------------------------


class TestAssignRoleToService:
    def test_returns_201_on_success(self):
        admin = MagicMock()
        admin.get_client_service_account_user.return_value = {"id": "sa-user-id"}
        admin.get_realm_role_by_id.return_value = {"id": "role-id", "name": "src-helper"}
        resp = _make_client(admin).post(f"/services/svc-uuid/roles/role-id?realm={REALM}")
        assert resp.status_code == 201
        admin.get_client_service_account_user.assert_called_once_with("svc-uuid")
        admin.get_realm_role_by_id.assert_called_once_with("role-id")
        admin.assign_realm_roles.assert_called_once_with(
            "sa-user-id", [{"id": "role-id", "name": "src-helper"}]
        )

    def test_returns_409_when_already_assigned(self):
        admin = MagicMock()
        admin.get_client_service_account_user.return_value = {"id": "sa-user-id"}
        admin.assign_realm_roles.side_effect = KeycloakError(
            error_message="Conflict", response_code=409
        )
        resp = _make_client(admin).post(f"/services/svc-uuid/roles/role-id?realm={REALM}")
        assert resp.status_code == 409

    def test_returns_502_on_keycloak_error(self):
        admin = MagicMock()
        admin.get_client_service_account_user.return_value = {"id": "sa-user-id"}
        admin.assign_realm_roles.side_effect = KeycloakError(
            error_message="failure", response_code=500
        )
        resp = _make_client(admin).post(f"/services/svc-uuid/roles/role-id?realm={REALM}")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def teardown_method(self):
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /subjects?role_id= (filtered variant, 2.14)
# ---------------------------------------------------------------------------


class TestGetSubjectsByRole:
    def test_returns_enriched_subjects_for_role(self):
        admin = MagicMock()
        admin.get_realm_role_by_id.return_value = {"id": "rid", "name": "viewer"}
        admin.get_realm_role_members.return_value = [
            {"id": "u1", "username": "alice"},
            {"id": "u2", "username": "bob"},
        ]
        admin.get_all_roles_of_user.side_effect = [
            {"realmMappings": [{"id": "rid", "name": "viewer"}], "clientMappings": {}},
            {"realmMappings": [{"id": "rid", "name": "viewer"}], "clientMappings": {}},
        ]
        resp = _make_client(admin).get(f"/subjects?realm={REALM}&role_id=rid")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        admin.get_realm_role_by_id.assert_called_once_with("rid")
        admin.get_realm_role_members.assert_called_once_with("viewer")
        assert admin.get_all_roles_of_user.call_count == 2

    def test_returns_empty_list_when_no_members(self):
        admin = MagicMock()
        admin.get_realm_role_by_id.return_value = {"id": "rid", "name": "viewer"}
        admin.get_realm_role_members.return_value = []
        resp = _make_client(admin).get(f"/subjects?realm={REALM}&role_id=rid")
        assert resp.status_code == 200
        assert resp.json() == []
        admin.get_all_roles_of_user.assert_not_called()

    def test_returns_502_on_keycloak_error_in_get_role(self):
        admin = MagicMock()
        admin.get_realm_role_by_id.side_effect = KeycloakError(
            error_message="not found", response_code=404
        )
        resp = _make_client(admin).get(f"/subjects?realm={REALM}&role_id=rid")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_returns_502_on_keycloak_error_in_get_members(self):
        admin = MagicMock()
        admin.get_realm_role_by_id.return_value = {"id": "rid", "name": "viewer"}
        admin.get_realm_role_members.side_effect = KeycloakError(
            error_message="error", response_code=500
        )
        resp = _make_client(admin).get(f"/subjects?realm={REALM}&role_id=rid")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_returns_502_on_keycloak_error_during_enrichment(self):
        admin = MagicMock()
        admin.get_realm_role_by_id.return_value = {"id": "rid", "name": "viewer"}
        admin.get_realm_role_members.return_value = [{"id": "u1", "username": "alice"}]
        admin.get_all_roles_of_user.side_effect = KeycloakError(
            error_message="error", response_code=500
        )
        resp = _make_client(admin).get(f"/subjects?realm={REALM}&role_id=rid")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_enrichment_shape_includes_realm_mappings(self):
        admin = MagicMock()
        admin.get_realm_role_by_id.return_value = {"id": "rid", "name": "viewer"}
        admin.get_realm_role_members.return_value = [{"id": "u1", "username": "alice"}]
        admin.get_all_roles_of_user.return_value = {
            "realmMappings": [{"id": "rid", "name": "viewer"}],
            "clientMappings": {"account": {"mappings": []}},
        }
        resp = _make_client(admin).get(f"/subjects?realm={REALM}&role_id=rid")
        assert resp.status_code == 200
        body = resp.json()
        assert "realmMappings" in body[0]
        assert body[0]["realmMappings"] == [{"id": "rid", "name": "viewer"}]

    def test_actor_ids_align_with_subjects_by_role(self):
        # SPM/APM alignment (1.12 / 2.14): the member usernames GET /subjects?role_id= returns
        # for a user (realm) role are exactly the Role.actorIds GET /roles populates for that
        # kind=User role — both resolve via admin.get_realm_role_members.
        admin = MagicMock()
        admin.get_realm_role_by_id.return_value = {"id": "rid", "name": "invoicing"}
        members = [{"id": "u1", "username": "alice"}, {"id": "u2", "username": "bob"}]
        admin.get_realm_role_members.return_value = members
        admin.get_all_roles_of_user.return_value = {"realmMappings": [], "clientMappings": {}}
        admin.get_realm_roles.return_value = [
            {"id": "rid", "name": "invoicing", "attributes": {"aiac.managed": ["true"]}},
        ]
        client = _make_client(admin)

        subjects = client.get(f"/subjects?realm={REALM}&role_id=rid").json()
        roles = client.get(f"/roles?realm={REALM}").json()

        subject_usernames = [s["username"] for s in subjects]
        actor_ids = roles[0]["actorIds"]
        assert subject_usernames == actor_ids == ["alice", "bob"]

    def test_missing_realm_returns_422(self):
        app.dependency_overrides.clear()
        resp = TestClient(app).get("/subjects?role_id=rid")
        assert resp.status_code == 422

    def test_unfiltered_still_works(self):
        admin = MagicMock()
        admin.get_users.return_value = [{"id": "u1", "username": "alice"}]
        resp = _make_client(admin).get(f"/subjects?realm={REALM}")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "u1", "username": "alice"}]
        admin.get_users.assert_called_once()

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
        assert _make_client(admin).get(f"/services/s1/roles?realm={REALM}").status_code == 502

    def test_get_service_scopes(self):
        admin = MagicMock()
        admin.get_client_default_client_scopes.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/services/s1/scopes?realm={REALM}").status_code == 502

    def test_get_role_composites(self):
        admin = MagicMock()
        admin.get_composite_realm_roles_of_role.side_effect = _keycloak_error()
        assert _make_client(admin).get(f"/roles/admin/composites?realm={REALM}").status_code == 502

    def test_mint_discovery_token(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_URL", "http://kc-internal:8080")
        admin = MagicMock()
        admin.get_client.side_effect = _keycloak_error()
        assert (
            _make_client(admin).get(f"/services/s1/discovery-token?realm={REALM}").status_code
            == 502
        )

    def teardown_method(self):
        app.dependency_overrides.clear()
