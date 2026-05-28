"""Integration tests against a live aiac-keycloak-service (port-forwarded to localhost:7070).

Run with:
    AC_SERVICE_URL=http://localhost:7070 pytest test/test_library_api_integration.py -v
or simply ensure .env contains AC_SERVICE_URL=http://localhost:7070 and run pytest.

Skip automatically when the service is unreachable.
"""

import pytest
import requests as _requests

from aiac.keycloak.library import api
from aiac.keycloak.library.models import (
    Client,
    ClientRole,
    ClientScope,
    RealmRole,
    RoleMappings,
    User,
)

REALM = "kagenti"


def _service_reachable() -> bool:
    try:
        _requests.get("http://localhost:7070/users", timeout=2)
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _service_reachable(), reason="aiac-keycloak-service not reachable on localhost:7070")


@live
def test_get_users_returns_users():
    users = api.get_users(realm=REALM)
    assert len(users) > 0
    assert all(isinstance(u, User) for u in users)
    usernames = {u.username for u in users}
    assert "admin" in usernames


@live
def test_get_realm_roles_returns_roles():
    roles = api.get_realm_roles(realm=REALM)
    assert len(roles) > 0
    assert all(isinstance(r, RealmRole) for r in roles)


@live
def test_get_clients_returns_clients():
    clients = api.get_clients(realm=REALM)
    assert len(clients) > 0
    assert all(isinstance(c, Client) for c in clients)
    client_ids = {c.clientId for c in clients}
    assert "account" in client_ids


@live
def test_get_client_scopes_returns_scopes():
    scopes = api.get_client_scopes(realm=REALM)
    assert len(scopes) > 0
    assert all(isinstance(s, ClientScope) for s in scopes)


@live
def test_get_user_role_mappings_returns_role_mappings():
    users = api.get_users(realm=REALM)
    user_id = users[0].id
    mappings = api.get_user_role_mappings(user_id, realm=REALM)
    assert isinstance(mappings, RoleMappings)


@live
def test_get_client_roles_returns_roles():
    clients = api.get_clients(realm=REALM)
    client = next(c for c in clients if c.clientId == "account")
    roles = api.get_client_roles(client.id, realm=REALM)
    assert len(roles) > 0
    assert all(isinstance(r, ClientRole) for r in roles)


@live
def test_assign_and_revoke_client_roles_roundtrip():
    users = api.get_users(realm=REALM)
    user = next(u for u in users if u.username == "alice")

    clients = api.get_clients(realm=REALM)
    client = next(c for c in clients if c.clientId == "account")
    roles = api.get_client_roles(client.id, realm=REALM)

    role = roles[0]

    api.assign_client_roles(user.id, client.id, [role], realm=REALM)
    mappings_after = api.get_user_role_mappings(user.id, realm=REALM)
    client_role_names = {
        r["name"]
        for mapping in mappings_after.clientMappings.values()
        for r in mapping.get("mappings", [])
    }
    assert role.name in client_role_names

    api.revoke_client_roles(user.id, client.id, [role], realm=REALM)
    mappings_revoked = api.get_user_role_mappings(user.id, realm=REALM)
    client_role_names_after = {
        r["name"]
        for mapping in mappings_revoked.clientMappings.values()
        for r in mapping.get("mappings", [])
    }
    assert role.name not in client_role_names_after
