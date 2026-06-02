from aiac.pdp.library.models import Subject, Role, Assignments, Service, Scope, Permission


class TestSubject:
    def test_full_payload(self):
        s = Subject.model_validate(
            {
                "id": "u1",
                "username": "alice",
                "email": "alice@example.com",
                "firstName": "Alice",
                "lastName": "Smith",
                "enabled": True,
            }
        )
        assert s.id == "u1"
        assert s.username == "alice"
        assert s.email == "alice@example.com"
        assert s.firstName == "Alice"
        assert s.lastName == "Smith"
        assert s.enabled is True

    def test_optional_fields_absent(self):
        s = Subject.model_validate({"id": "u2", "username": "bob", "enabled": False})
        assert s.email is None
        assert s.firstName is None
        assert s.lastName is None

    def test_extra_fields_ignored(self):
        s = Subject.model_validate(
            {"id": "u3", "username": "carol", "enabled": True, "unknownField": "garbage"}
        )
        assert not hasattr(s, "unknownField")


class TestRole:
    def test_full_payload(self):
        r = Role.model_validate(
            {
                "id": "r1",
                "name": "admin",
                "description": "Administrator role",
                "composite": False,
                "clientRole": False,
            }
        )
        assert r.id == "r1"
        assert r.name == "admin"
        assert r.description == "Administrator role"
        assert r.composite is False
        assert r.clientRole is False

    def test_optional_description_absent(self):
        r = Role.model_validate(
            {"id": "r2", "name": "viewer", "composite": True, "clientRole": True}
        )
        assert r.description is None

    def test_extra_fields_ignored(self):
        r = Role.model_validate(
            {
                "id": "r3",
                "name": "editor",
                "composite": False,
                "clientRole": False,
                "containerId": "master",
            }
        )
        assert not hasattr(r, "containerId")


class TestAssignments:
    def test_full_payload(self):
        a = Assignments.model_validate(
            {
                "realmMappings": [
                    {"id": "r1", "name": "admin", "composite": False, "clientRole": False}
                ],
                "serviceMappings": {
                    "account": {"id": "a1", "client": "account", "mappings": []}
                },
            }
        )
        assert len(a.realmMappings) == 1
        assert a.realmMappings[0].name == "admin"
        assert "account" in a.serviceMappings

    def test_defaults_to_empty(self):
        a = Assignments.model_validate({})
        assert a.realmMappings == []
        assert a.serviceMappings == {}

    def test_extra_fields_ignored(self):
        a = Assignments.model_validate({"realmMappings": [], "unknownField": "dropped"})
        assert not hasattr(a, "unknownField")


class TestService:
    def test_full_payload(self):
        s = Service.model_validate(
            {
                "id": "c1",
                "clientId": "my-app",
                "name": "My Application",
                "description": "Does things",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
            }
        )
        assert s.id == "c1"
        assert s.clientId == "my-app"
        assert s.name == "My Application"
        assert s.description == "Does things"
        assert s.enabled is True
        assert s.protocol == "openid-connect"
        assert s.publicClient is False

    def test_optional_fields_absent(self):
        s = Service.model_validate(
            {"id": "c2", "clientId": "bare-client", "enabled": False, "publicClient": True}
        )
        assert s.name is None
        assert s.description is None
        assert s.protocol is None

    def test_extra_fields_ignored(self):
        s = Service.model_validate(
            {
                "id": "c3",
                "clientId": "extra-client",
                "enabled": True,
                "publicClient": False,
                "surplusField": "ignored",
            }
        )
        assert not hasattr(s, "surplusField")


class TestScope:
    def test_full_payload(self):
        s = Scope.model_validate(
            {
                "id": "s1",
                "name": "email",
                "description": "Email scope",
                "protocol": "openid-connect",
            }
        )
        assert s.id == "s1"
        assert s.name == "email"
        assert s.description == "Email scope"
        assert s.protocol == "openid-connect"

    def test_optional_fields_absent(self):
        s = Scope.model_validate({"id": "s2", "name": "profile"})
        assert s.description is None
        assert s.protocol is None

    def test_extra_fields_ignored(self):
        s = Scope.model_validate({"id": "s3", "name": "roles", "unknownAttr": "dropped"})
        assert not hasattr(s, "unknownAttr")


class TestPermission:
    def test_full_payload(self):
        p = Permission.model_validate(
            {
                "id": "cr1",
                "name": "view-clients",
                "description": "View clients role",
                "composite": False,
                "clientRole": True,
            }
        )
        assert p.id == "cr1"
        assert p.name == "view-clients"
        assert p.description == "View clients role"
        assert p.composite is False
        assert p.clientRole is True

    def test_optional_description_absent(self):
        p = Permission.model_validate(
            {"id": "cr2", "name": "manage-clients", "composite": True, "clientRole": True}
        )
        assert p.description is None

    def test_extra_fields_ignored(self):
        p = Permission.model_validate(
            {
                "id": "cr3",
                "name": "query-clients",
                "composite": False,
                "clientRole": True,
                "containerId": "master",
            }
        )
        assert not hasattr(p, "containerId")
