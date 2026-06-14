from aiac.pdp.library.models import Subject, Role, Service, Scope


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

    def test_roles_populated(self):
        s = Subject.model_validate(
            {
                "id": "u1",
                "username": "alice",
                "enabled": True,
                "roles": [{"id": "r1", "name": "admin", "composite": False}],
            }
        )
        assert len(s.roles) == 1
        assert s.roles[0].name == "admin"

    def test_roles_default_empty(self):
        s = Subject.model_validate({"id": "u1", "username": "alice", "enabled": True})
        assert s.roles == []

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
            }
        )
        assert r.id == "r1"
        assert r.name == "admin"
        assert r.description == "Administrator role"
        assert r.composite is False

    def test_optional_description_absent(self):
        r = Role.model_validate({"id": "r2", "name": "viewer", "composite": True})
        assert r.description is None

    def test_child_roles_populated(self):
        r = Role.model_validate(
            {
                "id": "r1",
                "name": "admin",
                "composite": True,
                "childRoles": [{"id": "r2", "name": "viewer", "composite": False}],
            }
        )
        assert len(r.childRoles) == 1
        assert r.childRoles[0].name == "viewer"

    def test_child_roles_default_empty(self):
        r = Role.model_validate({"id": "r1", "name": "admin", "composite": False})
        assert r.childRoles == []

    def test_mapped_scopes_populated(self):
        r = Role.model_validate(
            {
                "id": "r1",
                "name": "admin",
                "composite": False,
                "mappedScopes": [{"id": "s1", "name": "email"}],
            }
        )
        assert len(r.mappedScopes) == 1
        assert r.mappedScopes[0].name == "email"

    def test_mapped_scopes_default_empty(self):
        r = Role.model_validate({"id": "r1", "name": "admin", "composite": False})
        assert r.mappedScopes == []

    def test_no_clientRole_field(self):
        r = Role.model_validate(
            {"id": "r1", "name": "admin", "composite": False, "clientRole": True}
        )
        assert not hasattr(r, "clientRole")

    def test_extra_fields_ignored(self):
        r = Role.model_validate(
            {
                "id": "r3",
                "name": "editor",
                "composite": False,
                "containerId": "master",
            }
        )
        assert not hasattr(r, "containerId")


class TestService:
    def test_full_payload(self):
        s = Service.model_validate(
            {
                "id": "c1",
                "name": "My Application",
                "description": "Does things",
                "enabled": True,
                "type": "Agent",
            }
        )
        assert s.id == "c1"
        assert s.name == "My Application"
        assert s.description == "Does things"
        assert s.enabled is True
        assert s.type == "Agent"

    def test_type_tool(self):
        s = Service.model_validate({"id": "c1", "name": "tool-svc", "enabled": True, "type": "Tool"})
        assert s.type == "Tool"

    def test_type_none_when_absent(self):
        s = Service.model_validate({"id": "c2", "name": "bare", "enabled": True})
        assert s.type is None

    def test_optional_fields_absent(self):
        s = Service.model_validate({"id": "c2", "enabled": False})
        assert s.name is None
        assert s.description is None
        assert s.type is None

    def test_description_present(self):
        s = Service.model_validate({"id": "c1", "enabled": True, "description": "a desc"})
        assert s.description == "a desc"

    def test_description_absent_is_none(self):
        s = Service.model_validate({"id": "c1", "enabled": True})
        assert s.description is None

    def test_roles_populated(self):
        s = Service.model_validate(
            {
                "id": "c1",
                "enabled": True,
                "roles": [{"id": "r1", "name": "admin", "composite": False}],
            }
        )
        assert len(s.roles) == 1
        assert s.roles[0].name == "admin"

    def test_roles_default_empty(self):
        s = Service.model_validate({"id": "c1", "enabled": True})
        assert s.roles == []

    def test_scopes_populated(self):
        s = Service.model_validate(
            {
                "id": "c1",
                "enabled": True,
                "scopes": [{"id": "s1", "name": "email"}],
            }
        )
        assert len(s.scopes) == 1
        assert s.scopes[0].name == "email"

    def test_scopes_default_empty(self):
        s = Service.model_validate({"id": "c1", "enabled": True})
        assert s.scopes == []

    def test_no_clientId_field(self):
        s = Service.model_validate(
            {"id": "c1", "enabled": True, "clientId": "my-app"}
        )
        assert not hasattr(s, "clientId")

    def test_no_protocol_field(self):
        s = Service.model_validate(
            {"id": "c1", "enabled": True, "protocol": "openid-connect"}
        )
        assert not hasattr(s, "protocol")

    def test_no_publicClient_field(self):
        s = Service.model_validate(
            {"id": "c1", "enabled": True, "publicClient": False}
        )
        assert not hasattr(s, "publicClient")

    def test_extra_fields_ignored(self):
        s = Service.model_validate(
            {"id": "c3", "enabled": True, "surplusField": "ignored"}
        )
        assert not hasattr(s, "surplusField")


class TestScope:
    def test_full_payload(self):
        s = Scope.model_validate({"id": "s1", "name": "email", "description": "Email scope"})
        assert s.id == "s1"
        assert s.name == "email"
        assert s.description == "Email scope"

    def test_optional_description_absent(self):
        s = Scope.model_validate({"id": "s2", "name": "profile"})
        assert s.description is None

    def test_no_protocol_field(self):
        s = Scope.model_validate({"id": "s1", "name": "email", "protocol": "openid-connect"})
        assert not hasattr(s, "protocol")

    def test_extra_fields_ignored(self):
        s = Scope.model_validate({"id": "s3", "name": "roles", "unknownAttr": "dropped"})
        assert not hasattr(s, "unknownAttr")
