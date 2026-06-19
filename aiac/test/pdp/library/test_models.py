from aiac.pdp.library.configuration.models import Subject, Role, Service, Scope


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

    def test_serviceId_populated_from_clientId(self):
        s = Service.model_validate(
            {"id": "c1", "enabled": True, "clientId": "my-app"}
        )
        assert s.serviceId == "my-app"

    def test_serviceId_none_when_clientId_absent(self):
        s = Service.model_validate({"id": "c1", "enabled": True})
        assert s.serviceId is None

    def test_no_clientId_field(self):
        s = Service.model_validate(
            {"id": "c1", "enabled": True, "clientId": "my-app"}
        )
        assert not hasattr(s, "clientId")
        assert s.serviceId == "my-app"

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


class TestServiceNameResolution:
    def test_placeholder_name_replaced_by_clientId(self):
        s = Service.model_validate(
            {
                "id": "abc123",
                "clientId": "account",
                "name": "${client_account}",
                "enabled": True,
            }
        )
        assert s.name == "account"

    def test_absent_name_resolved_from_clientId(self):
        s = Service.model_validate(
            {
                "id": "abc456",
                "clientId": "mlflow",
                "enabled": True,
            }
        )
        assert s.name == "mlflow"

    def test_valid_display_name_not_replaced_by_clientId(self):
        s = Service.model_validate(
            {
                "id": "abc789",
                "clientId": "github-tool",
                "name": "GitHub Tool",
                "enabled": True,
            }
        )
        assert s.name == "GitHub Tool"


class TestServiceTypeResolution:
    def test_type_agent_from_kagenti_attribute(self):
        s = Service.model_validate(
            {
                "id": "c1",
                "clientId": "some-agent",
                "enabled": True,
                "attributes": {"kagenti.service.type": "Agent"},
            }
        )
        assert s.type == "Agent"

    def test_type_tool_from_kagenti_attribute(self):
        s = Service.model_validate(
            {
                "id": "c2",
                "clientId": "github-tool",
                "enabled": True,
                "attributes": {"kagenti.service.type": "Tool"},
            }
        )
        assert s.type == "Tool"

    def test_type_agent_from_spiffe_clientId(self):
        s = Service.model_validate(
            {
                "id": "c3",
                "clientId": "spiffe://cluster.local/ns/team1/sa/git-issue-agent",
                "enabled": True,
            }
        )
        assert s.type == "Agent"

    def test_explicit_type_not_overridden_by_validator(self):
        s = Service.model_validate(
            {
                "id": "c4",
                "clientId": "spiffe://cluster.local/ns/team1/sa/some-agent",
                "enabled": True,
                "type": "Tool",
            }
        )
        assert s.type == "Tool"

    def test_unknown_kagenti_attribute_value_gives_none(self):
        s = Service.model_validate(
            {
                "id": "c5",
                "clientId": "mlflow",
                "enabled": True,
                "attributes": {"kagenti.service.type": "Unknown"},
            }
        )
        assert s.type is None


class TestKeycloakRealWorldPayloads:
    """Each model parsed against a realistic Keycloak API payload including extra noise fields."""

    def test_subject_keycloak_user(self):
        s = Subject.model_validate(
            {
                "id": "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f90",
                "username": "alice",
                "email": "alice@kagenti.org",
                "firstName": "Alice",
                "lastName": "Kagenti",
                "enabled": True,
                "emailVerified": True,
                "createdTimestamp": 1700000000,
                "roles": [
                    {
                        "id": "r-admin-uuid",
                        "name": "kagenti-admin",
                        "composite": False,
                        "clientRole": False,
                    }
                ],
            }
        )
        assert s.id == "d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f90"
        assert s.username == "alice"
        assert s.email == "alice@kagenti.org"
        assert s.firstName == "Alice"
        assert s.lastName == "Kagenti"
        assert s.enabled is True
        assert len(s.roles) == 1
        assert s.roles[0].name == "kagenti-admin"

    def test_role_keycloak_composite_with_child_and_scope(self):
        r = Role.model_validate(
            {
                "id": "r-dev-lead-uuid",
                "name": "dev-lead",
                "description": "Developer lead with admin and viewer permissions",
                "composite": True,
                "clientRole": False,
                "containerId": "kagenti",
                "childRoles": [
                    {
                        "id": "r-dev-uuid",
                        "name": "developer",
                        "composite": False,
                        "clientRole": False,
                    }
                ],
                "mappedScopes": [
                    {"id": "sc-read-uuid", "name": "read"},
                ],
            }
        )
        assert r.id == "r-dev-lead-uuid"
        assert r.name == "dev-lead"
        assert r.description == "Developer lead with admin and viewer permissions"
        assert r.composite is True
        assert len(r.childRoles) == 1
        assert r.childRoles[0].id == "r-dev-uuid"
        assert r.childRoles[0].name == "developer"
        assert r.childRoles[0].composite is False
        assert len(r.mappedScopes) == 1
        assert r.mappedScopes[0].name == "read"

    def test_service_keycloak_system_client_account(self):
        """Keycloak system 'account' client: placeholder name resolved, no kagenti type."""
        s = Service.model_validate(
            {
                "id": "account-client-uuid",
                "clientId": "account",
                "name": "${client_account}",
                "description": "${client_account_description}",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "attributes": {},
            }
        )
        assert s.id == "account-client-uuid"
        assert s.serviceId == "account"
        assert s.name == "account"
        assert s.description == "${client_account_description}"
        assert s.enabled is True
        assert s.type is None
        assert s.roles == []
        assert s.scopes == []

    def test_scope_keycloak_email_scope(self):
        """Standard OpenID Connect 'email' client scope as Keycloak returns it."""
        s = Scope.model_validate(
            {
                "id": "sc-email-uuid",
                "name": "email",
                "description": "OpenID Connect built-in scope: email",
                "protocol": "openid-connect",
                "attributes": {
                    "consent.screen.text": "${emailScopeConsentText}",
                    "display.on.consent.screen": "true",
                    "include.in.token.scope": "true",
                },
            }
        )
        assert s.id == "sc-email-uuid"
        assert s.name == "email"
        assert s.description == "OpenID Connect built-in scope: email"


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
