from unittest.mock import patch, MagicMock

import pytest

from aiac.keycloak.library.models import (
    User,
    RealmRole,
    Client,
    ClientScope,
    ClientRole,
    RoleMappings,
)

BASE = "http://test-service"
REALM = "kagenti"


@pytest.fixture(autouse=True)
def service_url(monkeypatch):
    monkeypatch.setenv("AC_SERVICE_URL", BASE)


def _ok(payload):
    m = MagicMock()
    m.ok = True
    m.json.return_value = payload
    return m


def _err(status=400):
    m = MagicMock()
    m.ok = False
    m.status_code = status
    m.text = "bad request"
    return m


# ---------------------------------------------------------------------------
# Cycle 1: get_users
# ---------------------------------------------------------------------------

def test_get_users_returns_typed_list():
    payload = [{"id": "u1", "username": "alice", "email": "a@x.com", "enabled": True}]
    with patch("requests.get", return_value=_ok(payload)) as mock_get:
        from aiac.keycloak.library import api
        result = api.get_users(realm=REALM)
    mock_get.assert_called_once_with(f"{BASE}/users", params={"realm": REALM})
    assert result == [User(id="u1", username="alice", email="a@x.com", enabled=True)]


# ---------------------------------------------------------------------------
# Cycle 2: get_realm_roles
# ---------------------------------------------------------------------------

def test_get_realm_roles_returns_typed_list():
    payload = [{"id": "r1", "name": "admin", "composite": False, "clientRole": False}]
    with patch("requests.get", return_value=_ok(payload)) as mock_get:
        from aiac.keycloak.library import api
        result = api.get_realm_roles(realm=REALM)
    mock_get.assert_called_once_with(f"{BASE}/realm-roles", params={"realm": REALM})
    assert result == [RealmRole(id="r1", name="admin", composite=False, clientRole=False)]


# ---------------------------------------------------------------------------
# Cycle 3: get_clients
# ---------------------------------------------------------------------------

def test_get_clients_returns_typed_list():
    payload = [{"id": "c1", "clientId": "my-client", "enabled": True, "publicClient": False}]
    with patch("requests.get", return_value=_ok(payload)) as mock_get:
        from aiac.keycloak.library import api
        result = api.get_clients(realm=REALM)
    mock_get.assert_called_once_with(f"{BASE}/clients", params={"realm": REALM})
    assert result == [Client(id="c1", clientId="my-client", enabled=True, publicClient=False)]


# ---------------------------------------------------------------------------
# Cycle 4: get_client_scopes
# ---------------------------------------------------------------------------

def test_get_client_scopes_returns_typed_list():
    payload = [{"id": "s1", "name": "email", "protocol": "openid-connect"}]
    with patch("requests.get", return_value=_ok(payload)) as mock_get:
        from aiac.keycloak.library import api
        result = api.get_client_scopes(realm=REALM)
    mock_get.assert_called_once_with(f"{BASE}/client-scopes", params={"realm": REALM})
    assert result == [ClientScope(id="s1", name="email", protocol="openid-connect")]


# ---------------------------------------------------------------------------
# Cycle 5: get_user_role_mappings
# ---------------------------------------------------------------------------

def test_get_user_role_mappings_returns_role_mappings():
    payload = {
        "realmMappings": [{"id": "r1", "name": "admin", "composite": False, "clientRole": False}],
        "clientMappings": {},
    }
    with patch("requests.get", return_value=_ok(payload)) as mock_get:
        from aiac.keycloak.library import api
        result = api.get_user_role_mappings("user-123", realm=REALM)
    mock_get.assert_called_once_with(
        f"{BASE}/users/user-123/role-mappings", params={"realm": REALM}
    )
    assert isinstance(result, RoleMappings)
    assert len(result.realmMappings) == 1
    assert result.realmMappings[0].name == "admin"


# ---------------------------------------------------------------------------
# Cycle 6: get_client_roles
# ---------------------------------------------------------------------------

def test_get_client_roles_returns_typed_list():
    payload = [{"id": "cr1", "name": "viewer", "composite": False, "clientRole": True}]
    with patch("requests.get", return_value=_ok(payload)) as mock_get:
        from aiac.keycloak.library import api
        result = api.get_client_roles("client-456", realm=REALM)
    mock_get.assert_called_once_with(f"{BASE}/clients/client-456/roles", params={"realm": REALM})
    assert result == [ClientRole(id="cr1", name="viewer", composite=False, clientRole=True)]


# ---------------------------------------------------------------------------
# Cycle 7: assign_client_roles
# ---------------------------------------------------------------------------

def test_assign_client_roles_posts_and_returns_none():
    roles = [ClientRole(id="cr1", name="viewer", composite=False, clientRole=True)]
    with patch("requests.post", return_value=_ok(None)) as mock_post:
        from aiac.keycloak.library import api
        result = api.assign_client_roles("user-1", "client-1", roles, realm=REALM)
    expected_url = f"{BASE}/users/user-1/role-mappings/clients/client-1"
    expected_body = [{"id": "cr1", "name": "viewer", "description": None, "composite": False, "clientRole": True}]
    mock_post.assert_called_once_with(expected_url, json=expected_body, params={"realm": REALM})
    assert result is None


# ---------------------------------------------------------------------------
# Cycle 8: revoke_client_roles
# ---------------------------------------------------------------------------

def test_revoke_client_roles_deletes_and_returns_none():
    roles = [ClientRole(id="cr1", name="viewer", composite=False, clientRole=True)]
    with patch("requests.delete", return_value=_ok(None)) as mock_delete:
        from aiac.keycloak.library import api
        result = api.revoke_client_roles("user-1", "client-1", roles, realm=REALM)
    expected_url = f"{BASE}/users/user-1/role-mappings/clients/client-1"
    expected_body = [{"id": "cr1", "name": "viewer", "description": None, "composite": False, "clientRole": True}]
    mock_delete.assert_called_once_with(expected_url, json=expected_body, params={"realm": REALM})
    assert result is None


# ---------------------------------------------------------------------------
# Cycle 9: non-2xx raises RuntimeError
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,patch_target,kwargs", [
    ("get_users",             "requests.get",    {"realm": REALM}),
    ("get_realm_roles",       "requests.get",    {"realm": REALM}),
    ("get_clients",           "requests.get",    {"realm": REALM}),
    ("get_client_scopes",     "requests.get",    {"realm": REALM}),
    ("get_user_role_mappings","requests.get",    {"user_id": "u1", "realm": REALM}),
    ("get_client_roles",      "requests.get",    {"client_id": "c1", "realm": REALM}),
    ("assign_client_roles",   "requests.post",   {"user_id": "u1", "client_id": "c1", "roles": [], "realm": REALM}),
    ("revoke_client_roles",   "requests.delete", {"user_id": "u1", "client_id": "c1", "roles": [], "realm": REALM}),
])
def test_non_2xx_raises_runtime_error(fn, patch_target, kwargs):
    from aiac.keycloak.library import api
    with patch(patch_target, return_value=_err(503)):
        with pytest.raises(RuntimeError):
            getattr(api, fn)(**kwargs)


# ---------------------------------------------------------------------------
# Cycle 10: AC_SERVICE_URL fallback to http://127.0.0.1:7070
# ---------------------------------------------------------------------------

def test_default_base_url_used_when_env_unset(monkeypatch):
    monkeypatch.delenv("AC_SERVICE_URL", raising=False)
    with patch("requests.get", return_value=_ok([])) as mock_get:
        from aiac.keycloak.library import api
        api.get_users(realm=REALM)
    mock_get.assert_called_once_with("http://127.0.0.1:7070/users", params={"realm": REALM})
