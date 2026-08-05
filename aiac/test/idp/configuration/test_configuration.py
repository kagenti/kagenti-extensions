"""Unit tests for aiac.idp.configuration."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, RoleKind, Scope, Service, ServiceType, Subject

REALM = "rossoctl"
BASE = "http://127.0.0.1:7071"


@pytest.fixture(autouse=True)
def _single_attempt(monkeypatch):
    """Configuration now retries transient failures internally (``_request`` → ``run_upstream``).
    Pin the budget to a single attempt so error-path unit tests stay fast and single-call."""
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")


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
# aiac.managed marker surfaced on Role / Scope (naming convention)
# ---------------------------------------------------------------------------


class TestAiacManagedMarker:
    def test_role_with_marker_is_managed(self):
        role = Role.model_validate(
            {"id": "r1", "name": "source-helper", "composite": False,
             "attributes": {"aiac.managed": ["true"]}}
        )
        assert role.aiac_managed is True

    def test_role_without_marker_is_not_managed(self):
        role = Role.model_validate(
            {"id": "r1", "name": "default-roles-realm", "composite": False}
        )
        assert role.aiac_managed is False

    def test_scope_with_marker_is_managed(self):
        scope = Scope.model_validate(
            {"id": "s1", "name": "source-access", "attributes": {"aiac.managed": "true"}}
        )
        assert scope.aiac_managed is True

    def test_scope_without_marker_is_not_managed(self):
        scope = Scope.model_validate({"id": "s1", "name": "profile"})
        assert scope.aiac_managed is False


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
    # Call order: GET /subjects, GET /roles (+ per-composite /composites), GET /subjects/{id}/assignments — per subject.

    def test_returns_list_of_subject(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        assignments = {"realmMappings": [], "serviceMappings": {}}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok([]), _ok(assignments)],
        ) as m:
            result = Configuration.for_realm(REALM).get_subjects()
        assert isinstance(result[0], Subject)
        assert result[0].username == "alice"
        assert m.call_args_list[0] == ((f"{BASE}/subjects",), {"params": {"realm": REALM}})

    def test_roles_populated_from_keycloak_assignments(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        all_roles = [{"id": "r1", "name": "viewer", "composite": False}]
        assignments = {"realmMappings": [{"id": "r1", "name": "viewer"}], "serviceMappings": {}}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok(all_roles), _ok(assignments)],
        ):
            result = Configuration.for_realm(REALM).get_subjects()
        assert len(result[0].roles) == 1
        assert result[0].roles[0].name == "viewer"

    def test_unassigned_roles_not_included(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        all_roles = [
            {"id": "r1", "name": "viewer", "composite": False},
            {"id": "r2", "name": "admin", "composite": False},
        ]
        assignments = {"realmMappings": [{"id": "r1"}], "serviceMappings": {}}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok(all_roles), _ok(assignments)],
        ):
            result = Configuration.for_realm(REALM).get_subjects()
        assert len(result[0].roles) == 1
        assert result[0].roles[0].id == "r1"

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_subjects()

    def test_raises_when_assignments_call_fails(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok([]), _err(502)],
        ):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_subjects()

    def test_realm_forwarded_on_all_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        assignments = {"realmMappings": [], "serviceMappings": {}}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok([]), _ok(assignments)],
        ) as m:
            Configuration.for_realm(REALM).get_subjects()
        for c in m.call_args_list:
            assert c[1].get("params") == {"realm": REALM}


# ---------------------------------------------------------------------------
# get_roles
# ---------------------------------------------------------------------------


class TestGetRoles:
    def test_returns_list_of_role(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r1", "name": "admin", "composite": False}]
        with patch("aiac.idp.configuration.api.requests.get",
                   return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_roles()
        assert isinstance(result[0], Role)
        assert result[0].name == "admin"
        assert m.call_args_list[0][0][0] == f"{BASE}/roles"

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_roles()

    def test_non_composite_role_has_no_mappedScopes(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "viewer", "composite": False}]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(roles)):
            result = Configuration.for_realm(REALM).get_roles()
        assert not hasattr(result[0], "mappedScopes")
        assert result[0].childRoles == []

    def test_non_composite_role_skips_composites_and_scopes_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "viewer", "composite": False}]
        with patch("aiac.idp.configuration.api.requests.get",
                   return_value=_ok(roles)) as m:
            Configuration.for_realm(REALM).get_roles()
        urls = [c[0][0] for c in m.call_args_list]
        assert all("/composites" not in u for u in urls)
        assert all("/scopes" not in u for u in urls)

    def test_get_roles_never_calls_scopes_endpoint(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [
            {"id": "r1", "name": "admin", "composite": True},
            {"id": "r2", "name": "viewer", "composite": False},
        ]
        child_roles = [{"id": "r2", "name": "viewer", "composite": False}]
        with patch("aiac.idp.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok(child_roles)]) as m:
            Configuration.for_realm(REALM).get_roles()
        urls = [c[0][0] for c in m.call_args_list]
        assert all("/scopes" not in u for u in urls)

    def test_composite_role_populates_child_roles(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "admin", "composite": True}]
        child_roles = [{"id": "r2", "name": "viewer", "composite": False}]
        with patch("aiac.idp.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok(child_roles)]):
            result = Configuration.for_realm(REALM).get_roles()
        assert result[0].childRoles[0].name == "viewer"

    def test_composite_role_has_no_mappedScopes(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "admin", "composite": True}]
        child_roles = [{"id": "r2", "name": "viewer", "composite": False}]
        with patch("aiac.idp.configuration.api.requests.get",
                   side_effect=[_ok(roles), _ok(child_roles)]):
            result = Configuration.for_realm(REALM).get_roles()
        assert not hasattr(result[0], "mappedScopes")

    def test_raises_if_composites_call_fails(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        roles = [{"id": "r1", "name": "admin", "composite": True}]
        with patch("aiac.idp.configuration.api.requests.get",
                   side_effect=[_ok(roles), _err(502)]):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_roles()


# ---------------------------------------------------------------------------
# get_services
# ---------------------------------------------------------------------------


class TestGetServices:
    # Call order: GET /services, GET /roles (get_roles, + per-role secondary calls),
    #             GET /scopes (get_scopes), GET /services/{id}/roles, GET /services/{id}/scopes — per service.

    def test_returns_list_of_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "clientId": "my-app", "name": "my-app", "enabled": True}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok([]), _ok([]), _ok([]), _ok([])],
        ) as m:
            result = Configuration.for_realm(REALM).get_services()
        assert isinstance(result[0], Service)
        assert result[0].id == "c1"
        assert m.call_args_list[0] == (
            (f"{BASE}/services",),
            {"params": {"realm": REALM}},
        )

    def test_serviceId_populated_from_clientId(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "clientId": "mlflow", "enabled": True}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok([]), _ok([]), _ok([]), _ok([])],
        ):
            result = Configuration.for_realm(REALM).get_services()
        assert result[0].serviceId == "mlflow"

    def test_scope_descriptions_populated_from_get_scopes(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "clientId": "my-app", "name": "my-app", "enabled": True}]
        all_scopes = [{"id": "s1", "name": "read:data", "description": "Read access"}]
        service_scopes = [{"id": "s1", "name": "read:data"}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok([]), _ok(all_scopes), _ok([]), _ok(service_scopes)],
        ):
            result = Configuration.for_realm(REALM).get_services()
        assert result[0].scopes[0].description == "Read access"

    def test_role_details_populated_from_get_roles(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "c1", "clientId": "my-app", "name": "my-app", "enabled": True}]
        all_roles = [{"id": "r1", "name": "viewer", "composite": False}]
        service_roles = [{"id": "r1", "name": "viewer"}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[_ok(payload), _ok(all_roles), _ok([]), _ok(service_roles), _ok([])],
        ):
            result = Configuration.for_realm(REALM).get_services()
        assert result[0].roles[0].name == "viewer"

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_services()


# ---------------------------------------------------------------------------
# get_service
# ---------------------------------------------------------------------------
# Call order: GET /services/{id}, GET /roles (+ per-role /scopes calls),
#             GET /scopes, GET /services/{id}/roles, GET /services/{id}/scopes


class TestGetService:
    SERVICE_ID = "svc-001"

    def test_returns_single_enriched_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        raw = {"id": self.SERVICE_ID, "clientId": self.SERVICE_ID, "name": "my-svc", "enabled": True}
        all_roles = [{"id": "r1", "name": "viewer", "composite": False}]
        all_scopes = [{"id": "s1", "name": "read:data", "description": "Read access"}]
        service_roles = [{"id": "r1"}]
        service_scopes = [{"id": "s1"}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(raw),            # GET /services/svc-001
                _ok(all_roles),      # GET /roles (no /scopes per role)
                _ok(all_scopes),     # GET /scopes
                _ok(service_roles),  # GET /services/svc-001/roles
                _ok(service_scopes), # GET /services/svc-001/scopes
            ],
        ):
            result = Configuration.for_realm(REALM).get_service(self.SERVICE_ID)
        assert isinstance(result, Service)
        assert result.id == self.SERVICE_ID
        assert result.roles[0].name == "viewer"
        assert result.scopes[0].name == "read:data"

    def test_type_resolved_from_client_type_attribute(self, monkeypatch):
        # Typing comes from the client.type attribute (via Service._resolve_keycloak_fields),
        # never from the description — the description-keyword fallback has been removed.
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        raw = {
            "id": self.SERVICE_ID,
            "clientId": self.SERVICE_ID,
            "name": "my-agent",
            "description": "An Agent service",  # keyword present but ignored
            "enabled": True,
            "attributes": {"client.type": "Tool"},
        }
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(raw),  # GET /services/svc-001
                _ok([]),   # GET /roles
                _ok([]),   # GET /scopes
                _ok([]),   # GET /services/svc-001/roles
                _ok([]),   # GET /services/svc-001/scopes
            ],
        ):
            result = Configuration.for_realm(REALM).get_service(self.SERVICE_ID)
        assert result.type == "Tool"

    def test_type_not_inferred_from_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        raw = {
            "id": self.SERVICE_ID,
            "clientId": self.SERVICE_ID,
            "name": "my-agent",
            "description": "An Agent service",  # no attribute → type stays None
            "enabled": True,
        }
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(raw),  # GET /services/svc-001
                _ok([]),   # GET /roles
                _ok([]),   # GET /scopes
                _ok([]),   # GET /services/svc-001/roles
                _ok([]),   # GET /services/svc-001/scopes
            ],
        ):
            result = Configuration.for_realm(REALM).get_service(self.SERVICE_ID)
        assert result.type is None

    def test_raises_when_primary_fetch_returns_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err(404)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_service(self.SERVICE_ID)

    def test_raises_when_service_roles_call_fails(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        raw = {"id": self.SERVICE_ID, "name": "my-svc", "enabled": True}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(raw),  # GET /services/svc-001
                _ok([]),   # GET /roles
                _ok([]),   # GET /scopes
                _err(500), # GET /services/svc-001/roles → error
            ],
        ):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_service(self.SERVICE_ID)

    def test_raises_when_service_scopes_call_fails(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        raw = {"id": self.SERVICE_ID, "name": "my-svc", "enabled": True}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(raw),  # GET /services/svc-001
                _ok([]),   # GET /roles
                _ok([]),   # GET /scopes
                _ok([]),   # GET /services/svc-001/roles → ok
                _err(500), # GET /services/svc-001/scopes → error
            ],
        ):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_service(self.SERVICE_ID)

    def test_realm_forwarded_on_every_request(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        raw = {"id": self.SERVICE_ID, "clientId": self.SERVICE_ID, "name": "my-svc", "enabled": True}
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(raw), _ok([]), _ok([]), _ok([]), _ok([]),
            ],
        ) as m:
            Configuration.for_realm(REALM).get_service(self.SERVICE_ID)
        for c in m.call_args_list:
            assert c[1].get("params") == {"realm": REALM}


# ---------------------------------------------------------------------------
# mint_discovery_token — fetches a tool-audienced bearer token from the config service
# ---------------------------------------------------------------------------


class TestMintDiscoveryToken:
    def test_returns_access_token(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = {"access_token": "tok", "client_id": "github-tool", "audience": ["github-tool"]}
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).mint_discovery_token("svc-uuid")
        assert result == "tok"
        m.assert_called_once_with(
            f"{BASE}/services/svc-uuid/discovery-token", params={"realm": REALM}
        )

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err(502)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).mint_discovery_token("svc-uuid")


# ---------------------------------------------------------------------------
# set_service_type — writes the client.type attribute
# ---------------------------------------------------------------------------


class TestSetServiceType:
    def _make_service(self, **kwargs):
        defaults = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        return Service.model_validate({**defaults, **kwargs})

    def test_returns_updated_service_with_type(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        updated = {
            "id": "svc-uuid",
            "clientId": "svc-uuid",
            "name": "my-svc",
            "enabled": True,
            "attributes": {"client.type": "Agent"},
        }
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(updated, 200)):
            result = Configuration.for_realm(REALM).set_service_type(service, "Agent")
        assert isinstance(result, Service)
        assert result.type == "Agent"

    def test_posts_to_correct_url_with_type_body(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True,
                   "attributes": {"client.type": "Tool"}}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(updated, 200)) as m:
            Configuration.for_realm(REALM).set_service_type(service, "Tool")
        assert m.call_args[0][0] == f"{BASE}/services/svc-uuid/type"
        assert m.call_args[1].get("json") == {"type": "Tool"}

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True,
                   "attributes": {"client.type": "Agent"}}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(updated, 200)) as m:
            Configuration.for_realm(REALM).set_service_type(service, "Agent")
        assert m.call_args[1].get("params") == {"realm": REALM}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err(502)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).set_service_type(service, "Agent")

    def test_accepts_service_type_enum(self, monkeypatch):
        # ServiceType is a str enum; set_service_type unwraps it to the plain "Agent"/"Tool" value.
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True,
                   "attributes": {"client.type": "Agent"}}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(updated, 200)) as m:
            Configuration.for_realm(REALM).set_service_type(service, ServiceType.AGENT)
        assert m.call_args[1].get("json") == {"type": "Agent"}


# ---------------------------------------------------------------------------
# create_service_role / create_service_scope — idempotent create-or-get + map
# (tested by patching the Configuration methods they compose)
# ---------------------------------------------------------------------------


class TestCreateServiceRole:
    def _svc(self):
        return Service.model_validate({"id": "svc-1", "clientId": "svc-1", "enabled": True})

    def test_creates_role_when_absent_then_maps_to_service(self):
        cfg = Configuration.for_realm(REALM)
        role_def = SimpleNamespace(name="app.agent", description="Agent role")
        created = Role(id="r-1", name="app.agent", description="Agent role", composite=False)
        svc = self._svc()
        with (
            patch.object(cfg, "get_roles", return_value=[]),
            patch.object(cfg, "create_role", return_value=created) as create,
            patch.object(cfg, "get_service", return_value=svc),
            patch.object(cfg, "map_role_to_service", return_value=svc) as mapper,
        ):
            result = cfg.create_service_role("svc-1", role_def)
        create.assert_called_once_with("app.agent", "Agent role")
        mapper.assert_called_once_with(svc, created)
        assert result is created

    def test_reuses_existing_role_without_creating(self):
        cfg = Configuration.for_realm(REALM)
        role_def = SimpleNamespace(name="app.agent", description="Agent role")
        existing = Role(id="r-1", name="app.agent", description="Agent role", composite=False)
        svc = self._svc()
        with (
            patch.object(cfg, "get_roles", return_value=[existing]),
            patch.object(cfg, "create_role") as create,
            patch.object(cfg, "get_service", return_value=svc),
            patch.object(cfg, "map_role_to_service", return_value=svc) as mapper,
        ):
            result = cfg.create_service_role("svc-1", role_def)
        create.assert_not_called()
        mapper.assert_called_once_with(svc, existing)
        assert result is existing


class TestCreateServiceScope:
    def _svc(self):
        return Service.model_validate({"id": "svc-1", "clientId": "svc-1", "enabled": True})

    def test_creates_scope_when_absent_then_maps_to_service(self):
        cfg = Configuration.for_realm(REALM)
        scope_def = SimpleNamespace(name="app.read", description="Read tool")
        created = Scope(id="s-1", name="app.read", description="Read tool")
        svc = self._svc()
        with (
            patch.object(cfg, "get_scopes", return_value=[]),
            patch.object(cfg, "create_scope", return_value=created) as create,
            patch.object(cfg, "get_service", return_value=svc),
            patch.object(cfg, "map_scope_to_service", return_value=svc) as mapper,
        ):
            result = cfg.create_service_scope("svc-1", scope_def)
        create.assert_called_once_with("app.read", "Read tool")
        mapper.assert_called_once_with(svc, created)
        assert result is created

    def test_reuses_existing_scope_without_creating(self):
        cfg = Configuration.for_realm(REALM)
        scope_def = SimpleNamespace(name="app.read", description="Read tool")
        existing = Scope(id="s-1", name="app.read", description="Read tool")
        svc = self._svc()
        with (
            patch.object(cfg, "get_scopes", return_value=[existing]),
            patch.object(cfg, "create_scope") as create,
            patch.object(cfg, "get_service", return_value=svc),
            patch.object(cfg, "map_scope_to_service", return_value=svc) as mapper,
        ):
            result = cfg.create_service_scope("svc-1", scope_def)
        create.assert_not_called()
        mapper.assert_called_once_with(svc, existing)
        assert result is existing


# ---------------------------------------------------------------------------
# get_scopes
# ---------------------------------------------------------------------------


class TestGetScopes:
    def test_returns_list_of_scope(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "s1", "name": "email"}]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)) as m:
            result = Configuration.for_realm(REALM).get_scopes()
        assert isinstance(result[0], Scope)
        assert result[0].name == "email"
        m.assert_called_once_with(f"{BASE}/scopes", params={"realm": REALM})

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_scopes()


# ---------------------------------------------------------------------------
# create_scope
# ---------------------------------------------------------------------------


class TestCreateScope:
    def test_returns_scope_instance(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read:data", "description": "Read access"}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)):
            result = Configuration.for_realm(REALM).create_scope(
                scope_name="read:data", scope_description="Read access"
            )
        assert isinstance(result, Scope)
        assert result.name == "read:data"
        assert result.id == "sc1"

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "write"}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("write", "Write access")
        url = m.call_args[0][0]
        assert url == f"{BASE}/scopes"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("read", "desc")
        params = m.call_args[1].get("params", {})
        assert params == {"realm": REALM}

    def test_json_body_contains_name_and_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "sc1", "name": "read"}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_scope("read", "Read access")
        body = m.call_args[1].get("json", {})
        assert body == {"name": "read", "description": "Read access"}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_scope("read", "desc")

    def test_raises_on_409_conflict(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_scope("dupe", "desc")


# ---------------------------------------------------------------------------
# map_scope_to_service
# ---------------------------------------------------------------------------


class TestMapScopeToService:
    def _make_service(self, **kwargs):
        defaults = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        return Service.model_validate({**defaults, **kwargs})

    def _make_scope(self, **kwargs):
        defaults = {"id": "scope-id", "name": "read:data"}
        return Scope.model_validate({**defaults, **kwargs})

    def test_returns_updated_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        updated = {
            "id": "svc-uuid",
            "clientId": "svc-uuid",
            "name": "my-svc",
            "enabled": True,
            "scopes": [{"id": "scope-id", "name": "read:data"}],
        }
        post_resp = _ok({}, 201)
        get_resp = _ok(updated)
        with patch("aiac.idp.configuration.api.requests.post", return_value=post_resp), \
             patch("aiac.idp.configuration.api.requests.get", return_value=get_resp) as get_m:
            result = Configuration.for_realm(REALM).map_scope_to_service(service, scope)
        assert isinstance(result, Service)
        assert result.id == "svc-uuid"
        get_m.assert_called_once_with(f"{BASE}/services/svc-uuid", params={"realm": REALM})

    def test_issues_post_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.idp.configuration.api.requests.get", return_value=_ok(updated)):
            Configuration.for_realm(REALM).map_scope_to_service(service, scope)
        url = post_m.call_args[0][0]
        assert url == f"{BASE}/services/svc-uuid/scopes/scope-id"

    def test_raises_on_post_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).map_scope_to_service(service, scope)

    def test_realm_forwarded_on_both_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        scope = self._make_scope()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.idp.configuration.api.requests.get", return_value=_ok(updated)) as get_m:
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
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)):
            result = Configuration.for_realm(REALM).create_role("reader", "Read-only")
        assert isinstance(result, Role)
        assert result.name == "reader"
        assert result.id == "r1"

    def test_posts_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "composite": False}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_role("reader", "desc")
        url = m.call_args[0][0]
        assert url == f"{BASE}/roles"

    def test_forwards_realm_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "composite": False}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_role("reader", "desc")
        assert m.call_args[1].get("params") == {"realm": REALM}

    def test_json_body_contains_name_and_description(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        created = {"id": "r1", "name": "reader", "composite": False}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok(created, 201)) as m:
            Configuration.for_realm(REALM).create_role("reader", "Read-only")
        assert m.call_args[1].get("json") == {"name": "reader", "description": "Read-only"}

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err()):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_role("reader", "desc")

    def test_raises_on_409_conflict(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).create_role("dupe", "desc")


# ---------------------------------------------------------------------------
# map_role_to_service
# ---------------------------------------------------------------------------


class TestMapRoleToService:
    def _make_service(self, **kwargs):
        defaults = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        return Service.model_validate({**defaults, **kwargs})

    def _make_role(self, **kwargs):
        defaults = {"id": "role-id", "name": "reader", "composite": False}
        return Role.model_validate({**defaults, **kwargs})

    def test_returns_updated_service(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok({}, 201)), \
             patch("aiac.idp.configuration.api.requests.get", return_value=_ok(updated)) as get_m:
            result = Configuration.for_realm(REALM).map_role_to_service(service, role)
        assert isinstance(result, Service)
        get_m.assert_called_once_with(f"{BASE}/services/svc-uuid", params={"realm": REALM})

    def test_issues_post_to_correct_url(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.idp.configuration.api.requests.get", return_value=_ok(updated)):
            Configuration.for_realm(REALM).map_role_to_service(service, role)
        url = post_m.call_args[0][0]
        assert url == f"{BASE}/services/svc-uuid/roles/role-id"

    def test_raises_on_post_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        with patch("aiac.idp.configuration.api.requests.post", return_value=_err(409)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).map_role_to_service(service, role)

    def test_realm_forwarded_on_both_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        service = self._make_service()
        role = self._make_role()
        updated = {"id": "svc-uuid", "clientId": "svc-uuid", "name": "my-svc", "enabled": True}
        with patch("aiac.idp.configuration.api.requests.post", return_value=_ok({}, 201)) as post_m, \
             patch("aiac.idp.configuration.api.requests.get", return_value=_ok(updated)) as get_m:
            Configuration.for_realm(REALM).map_role_to_service(service, role)
        assert post_m.call_args[1].get("params") == {"realm": REALM}
        assert get_m.call_args[1].get("params") == {"realm": REALM}


# ---------------------------------------------------------------------------
# realm forwarded as ?realm= on all methods
# ---------------------------------------------------------------------------


class TestRealmParameter:
    @pytest.mark.parametrize("method,endpoint", [
        ("get_roles", "roles"),
        ("get_scopes", "scopes"),
    ])
    def test_realm_forwarded_as_query_param(self, method, endpoint, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])) as m:
            getattr(Configuration.for_realm(REALM), method)()
        m.assert_called_once_with(f"{BASE}/{endpoint}", params={"realm": REALM})

    def test_get_subjects_realm_forwarded_as_query_param(self, monkeypatch):
        from unittest.mock import call
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])) as m:
            Configuration.for_realm(REALM).get_subjects()
        assert call(f"{BASE}/subjects", params={"realm": REALM}) in m.call_args_list

    def test_get_services_realm_forwarded_as_query_param(self, monkeypatch):
        from unittest.mock import call
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])) as m:
            Configuration.for_realm(REALM).get_services()
        assert call(f"{BASE}/services", params={"realm": REALM}) in m.call_args_list


# ---------------------------------------------------------------------------
# Default URL fallback
# ---------------------------------------------------------------------------


def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AIAC_PDP_CONFIG_URL", raising=False)
    with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])) as m:
        Configuration.for_realm(REALM).get_subjects()
    assert m.call_args[0][0].startswith("http://127.0.0.1:7071")


# ---------------------------------------------------------------------------
# get_services_by_role
# ---------------------------------------------------------------------------


class TestGetServicesByRole:
    """``get_services_by_role`` filters ``get_services()`` client-side by role ``id``."""

    def _make_role(self, **kwargs):
        defaults = {"id": "role-uuid", "name": "viewer", "composite": False}
        return Role.model_validate({**defaults, **kwargs})

    def _make_service(self, sid, role_ids):
        return Service.model_validate({
            "id": sid,
            "clientId": sid,
            "name": sid,
            "enabled": True,
            "roles": [{"id": rid, "name": rid, "composite": False} for rid in role_ids],
        })

    def test_returns_only_services_whose_roles_contain_role_id(self):
        role = self._make_role(id="r1")
        services = [
            self._make_service("svc1", ["r1"]),
            self._make_service("svc2", ["r2"]),
            self._make_service("svc3", ["r1", "r2"]),
        ]
        with patch.object(Configuration, "get_services", return_value=services):
            result = Configuration.for_realm(REALM).get_services_by_role(role)
        assert [s.id for s in result] == ["svc1", "svc3"]
        assert all(isinstance(s, Service) for s in result)

    def test_returns_empty_list_for_realm_level_role(self):
        role = self._make_role(id="r-nobody")
        services = [self._make_service("svc1", ["r1"]), self._make_service("svc2", ["r2"])]
        with patch.object(Configuration, "get_services", return_value=services):
            result = Configuration.for_realm(REALM).get_services_by_role(role)
        assert result == []

    def test_raises_on_non_2xx(self):
        role = self._make_role()
        with patch.object(Configuration, "get_services", side_effect=RuntimeError("HTTP 500")):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_services_by_role(role)


# ---------------------------------------------------------------------------
# get_services_by_scope
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_subjects_by_role (8.15)
# ---------------------------------------------------------------------------


class TestGetSubjectsByRole:
    def _make_role(self, **kwargs):
        defaults = {"id": "role-uuid", "name": "viewer", "composite": False}
        return Role.model_validate({**defaults, **kwargs})

    def test_returns_list_of_subject(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role()
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
            result = Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert isinstance(result[0], Subject)
        assert result[0].id == "u1"

    def test_issues_get_with_role_id_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role(id="my-role-id")
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])) as m:
            Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert m.call_args[0][0] == f"{BASE}/subjects"
        assert m.call_args[1]["params"] == {"role_id": "my-role-id", "realm": REALM}

    def test_returns_empty_list_when_no_subjects(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role()
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])):
            result = Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert result == []

    def test_raises_on_non_2xx(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role()
        with patch("aiac.idp.configuration.api.requests.get", return_value=_err(500)):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_subjects_by_role(role)

    def test_realm_forwarded_as_query_param(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role()
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok([])) as m:
            Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert m.call_args[1]["params"]["realm"] == REALM

    def test_no_secondary_enrichment_calls(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role()
        payload = [{"id": "u1", "username": "alice", "enabled": True}]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)) as m:
            Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert m.call_count == 1

    # handoff 03 — actorIds consistency. The subject/username set this method
    # reports is the same set the IdP service uses to populate a user-kind role's
    # actorIds. Both come from the service, so they must agree and the library
    # must not recompute actorIds client-side.
    def test_subject_set_matches_user_role_actor_ids(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role(kind="User", actorIds=["alice", "bob"])
        payload = [
            {"id": "u1", "username": "alice", "enabled": True},
            {"id": "u2", "username": "bob", "enabled": True},
        ]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
            result = Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert {s.username for s in result} == set(role.actorIds)
        # The library surfaces the service's subject set as-is; it does not
        # mutate/recompute the role's actorIds.
        assert role.actorIds == ["alice", "bob"]

    def test_subject_fields_pass_through_unchanged(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        role = self._make_role()
        payload = [
            {
                "id": "u1",
                "username": "alice",
                "email": "alice@example.com",
                "firstName": "Alice",
                "enabled": True,
            }
        ]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
            result = Configuration.for_realm(REALM).get_subjects_by_role(role)
        assert result[0].email == "alice@example.com"
        assert result[0].firstName == "Alice"


# ---------------------------------------------------------------------------
# get_services_by_scope (unchanged)
# ---------------------------------------------------------------------------


class TestGetServicesByScope:
    """``get_services_by_scope`` filters ``get_services()`` client-side by scope ``id``."""

    def _make_scope(self, **kwargs):
        defaults = {"id": "scope-uuid", "name": "read:data"}
        return Scope.model_validate({**defaults, **kwargs})

    def _make_service(self, sid, scope_ids):
        return Service.model_validate({
            "id": sid,
            "clientId": sid,
            "name": sid,
            "enabled": True,
            "scopes": [{"id": scid, "name": scid} for scid in scope_ids],
        })

    def test_returns_only_services_whose_scopes_contain_scope_id(self):
        scope = self._make_scope(id="s1")
        services = [
            self._make_service("svc1", ["s1"]),
            self._make_service("svc2", ["s2"]),
            self._make_service("svc3", ["s1", "s2"]),
        ]
        with patch.object(Configuration, "get_services", return_value=services):
            result = Configuration.for_realm(REALM).get_services_by_scope(scope)
        assert [s.id for s in result] == ["svc1", "svc3"]
        assert all(isinstance(s, Service) for s in result)

    def test_returns_empty_list_when_no_service_exposes_scope(self):
        scope = self._make_scope(id="s-nobody")
        services = [self._make_service("svc1", ["s1"]), self._make_service("svc2", ["s2"])]
        with patch.object(Configuration, "get_services", return_value=services):
            result = Configuration.for_realm(REALM).get_services_by_scope(scope)
        assert result == []

    def test_raises_on_non_2xx(self):
        scope = self._make_scope()
        with patch.object(Configuration, "get_services", side_effect=RuntimeError("HTTP 500")):
            with pytest.raises(RuntimeError):
                Configuration.for_realm(REALM).get_services_by_scope(scope)


# ---------------------------------------------------------------------------
# handoff 03 — SPM/APM field pass-through through the library deserialization
#
# handoff 01 declares Role.kind / Role.actorIds / Scope.serviceId; handoff 02
# makes the IdP *service* populate them. The Configuration library is a thin
# pass-through (model_validate), so these fields must survive onto the returned
# models with no hand-rolled mapping dropping them and no client-side derivation.
# These tests stub the HTTP layer with a service response carrying the new
# fields and assert they round-trip through the real read paths.
# ---------------------------------------------------------------------------


class TestFieldPassThrough:
    def test_get_roles_surfaces_role_kind(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r1", "name": "agent-role", "composite": False, "kind": "Agent"}]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
            roles = Configuration.for_realm(REALM).get_roles()
        assert roles[0].kind == RoleKind.AGENT

    def test_get_roles_surfaces_actor_ids(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [
            {
                "id": "r1",
                "name": "viewer",
                "composite": False,
                "kind": "User",
                "actorIds": ["alice", "bob"],
            }
        ]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
            roles = Configuration.for_realm(REALM).get_roles()
        assert roles[0].actorIds == ["alice", "bob"]

    def test_get_scopes_surfaces_scope_service_id(self, monkeypatch):
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "s1", "name": "read:data", "serviceId": "mlflow"}]
        with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
            scopes = Configuration.for_realm(REALM).get_scopes()
        assert scopes[0].serviceId == "mlflow"

    def test_get_services_surfaces_new_fields_on_nested_role_and_scope(self, monkeypatch):
        # get_services() enriches each service's roles/scopes from the get_roles()
        # / get_scopes() maps, so the new fields must survive that join too.
        # Call order: GET /services, GET /roles, GET /scopes, GET /services/{id}/roles,
        # GET /services/{id}/scopes.
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        services = [{"id": "c1", "clientId": "mlflow", "name": "mlflow", "enabled": True}]
        all_roles = [
            {
                "id": "r1",
                "name": "mlflow-agent",
                "composite": False,
                "kind": "Agent",
                "actorIds": ["mlflow"],
            }
        ]
        all_scopes = [{"id": "s1", "name": "read:data", "serviceId": "mlflow"}]
        service_roles = [{"id": "r1", "name": "mlflow-agent"}]
        service_scopes = [{"id": "s1", "name": "read:data"}]
        with patch(
            "aiac.idp.configuration.api.requests.get",
            side_effect=[
                _ok(services),
                _ok(all_roles),
                _ok(all_scopes),
                _ok(service_roles),
                _ok(service_scopes),
            ],
        ):
            result = Configuration.for_realm(REALM).get_services()
        assert result[0].roles[0].kind == RoleKind.AGENT
        assert result[0].roles[0].actorIds == ["mlflow"]
        assert result[0].scopes[0].serviceId == "mlflow"

    def test_role_kind_taken_from_response_not_rederived(self, monkeypatch):
        # Role.kind is authoritative — the library must not re-derive it by
        # classifying a role against the service list. Point get_services at a
        # spy and assert get_roles never touches it.
        monkeypatch.setenv("AIAC_PDP_CONFIG_URL", BASE)
        payload = [{"id": "r1", "name": "agent-role", "composite": False, "kind": "Agent"}]
        spy = MagicMock(side_effect=AssertionError("get_services must not be called to set Role.kind"))
        with patch.object(Configuration, "get_services", spy):
            with patch("aiac.idp.configuration.api.requests.get", return_value=_ok(payload)):
                roles = Configuration.for_realm(REALM).get_roles()
        assert roles[0].kind == RoleKind.AGENT
        spy.assert_not_called()
