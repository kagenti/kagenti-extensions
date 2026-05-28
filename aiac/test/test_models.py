import pytest
from aiac.keycloak.library.models import User, RealmRole, RoleMappings, ClientRole, Client, ClientScope


class TestRoleMappings:
    def test_full_payload(self):
        rm = RoleMappings.model_validate(
            {
                "realmMappings": [
                    {"id": "r1", "name": "admin", "composite": False, "clientRole": False}
                ],
                "clientMappings": {
                    "account": {"id": "a1", "client": "account", "mappings": []}
                },
            }
        )
        assert len(rm.realmMappings) == 1
        assert rm.realmMappings[0].name == "admin"
        assert "account" in rm.clientMappings

    def test_defaults_to_empty(self):
        rm = RoleMappings.model_validate({})
        assert rm.realmMappings == []
        assert rm.clientMappings == {}

    def test_extra_fields_ignored(self):
        rm = RoleMappings.model_validate({"realmMappings": [], "unknownField": "dropped"})
        assert not hasattr(rm, "unknownField")


class TestUser:
    def test_full_payload(self):
        user = User.model_validate(
            {
                "id": "u1",
                "username": "alice",
                "email": "alice@example.com",
                "firstName": "Alice",
                "lastName": "Smith",
                "enabled": True,
            }
        )
        assert user.id == "u1"
        assert user.username == "alice"
        assert user.email == "alice@example.com"
        assert user.firstName == "Alice"
        assert user.lastName == "Smith"
        assert user.enabled is True

    def test_optional_fields_absent(self):
        user = User.model_validate({"id": "u2", "username": "bob", "enabled": False})
        assert user.email is None
        assert user.firstName is None
        assert user.lastName is None

    def test_extra_fields_ignored(self):
        user = User.model_validate(
            {"id": "u3", "username": "carol", "enabled": True, "unknownField": "garbage"}
        )
        assert not hasattr(user, "unknownField")


class TestRealmRole:
    def test_full_payload(self):
        role = RealmRole.model_validate(
            {
                "id": "r1",
                "name": "admin",
                "description": "Administrator role",
                "composite": False,
                "clientRole": False,
            }
        )
        assert role.id == "r1"
        assert role.name == "admin"
        assert role.description == "Administrator role"
        assert role.composite is False
        assert role.clientRole is False

    def test_optional_fields_absent(self):
        role = RealmRole.model_validate(
            {"id": "r2", "name": "viewer", "composite": True, "clientRole": True}
        )
        assert role.description is None

    def test_extra_fields_ignored(self):
        role = RealmRole.model_validate(
            {
                "id": "r3",
                "name": "editor",
                "composite": False,
                "clientRole": False,
                "containerId": "master",
            }
        )
        assert not hasattr(role, "containerId")


class TestClient:
    def test_full_payload(self):
        client = Client.model_validate(
            {
                "id": "c1",
                "clientId": "my-app",
                "name": "My Application",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
            }
        )
        assert client.id == "c1"
        assert client.clientId == "my-app"
        assert client.name == "My Application"
        assert client.enabled is True
        assert client.protocol == "openid-connect"
        assert client.publicClient is False

    def test_optional_fields_absent(self):
        client = Client.model_validate(
            {"id": "c2", "clientId": "bare-client", "enabled": False, "publicClient": True}
        )
        assert client.name is None
        assert client.protocol is None

    def test_extra_fields_ignored(self):
        client = Client.model_validate(
            {
                "id": "c3",
                "clientId": "extra-client",
                "enabled": True,
                "publicClient": False,
                "surplusField": "ignored",
            }
        )
        assert not hasattr(client, "surplusField")


class TestClientScope:
    def test_full_payload(self):
        scope = ClientScope.model_validate(
            {
                "id": "s1",
                "name": "email",
                "description": "Email scope",
                "protocol": "openid-connect",
            }
        )
        assert scope.id == "s1"
        assert scope.name == "email"
        assert scope.description == "Email scope"
        assert scope.protocol == "openid-connect"

    def test_optional_fields_absent(self):
        scope = ClientScope.model_validate({"id": "s2", "name": "profile"})
        assert scope.description is None
        assert scope.protocol is None

    def test_extra_fields_ignored(self):
        scope = ClientScope.model_validate(
            {"id": "s3", "name": "roles", "unknownAttr": "dropped"}
        )
        assert not hasattr(scope, "unknownAttr")


class TestClientRole:
    def test_full_payload(self):
        role = ClientRole.model_validate(
            {
                "id": "cr1",
                "name": "view-clients",
                "description": "View clients role",
                "composite": False,
                "clientRole": True,
            }
        )
        assert role.id == "cr1"
        assert role.name == "view-clients"
        assert role.description == "View clients role"
        assert role.composite is False
        assert role.clientRole is True

    def test_optional_fields_absent(self):
        role = ClientRole.model_validate(
            {"id": "cr2", "name": "manage-clients", "composite": True, "clientRole": True}
        )
        assert role.description is None

    def test_extra_fields_ignored(self):
        role = ClientRole.model_validate(
            {
                "id": "cr3",
                "name": "query-clients",
                "composite": False,
                "clientRole": True,
                "containerId": "master",
            }
        )
        assert not hasattr(role, "containerId")