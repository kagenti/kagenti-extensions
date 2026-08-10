import pytest
from pydantic import ValidationError

from aiac.idp.configuration.models import (
    Role,
    RoleKind,
    Scope,
    Service,
    ServiceType,
    Subject,
)
from aiac.policy.model.models import (
    AgentPolicyModel,
    PolicyModel,
    PolicyRule,
    ServicePolicyModel,
)


def _role(id: str = "role-1", name: str = "admin") -> Role:
    return Role(id=id, name=name, composite=False)


def _scope(id: str = "scope-1", name: str = "read") -> Scope:
    return Scope(id=id, name=name)


def _service(id: str = "svc-1", service_id: str = "my-service") -> Service:
    return Service(id=id, serviceId=service_id, enabled=True)


def _subject(id: str = "sub-1", username: str = "alice") -> Subject:
    return Subject(id=id, username=username, enabled=True)


# --- RoleKind enum + Role.kind / Role.actorIds (SPM redesign) ---


def test_role_kind_values_mirror_service_type_style():
    assert RoleKind.USER == "User"
    assert RoleKind.AGENT == "Agent"


def test_role_accepts_kind_and_actor_ids():
    role = Role(
        id="r1",
        name="weather-reader",
        composite=False,
        kind=RoleKind.AGENT,
        actorIds=["weather-agent"],
    )
    assert role.kind == RoleKind.AGENT
    assert role.actorIds == ["weather-agent"]


def test_role_kind_and_actor_ids_round_trip():
    role = Role(
        id="r1",
        name="reader",
        composite=False,
        kind=RoleKind.USER,
        actorIds=["alice", "bob"],
    )
    restored = Role.model_validate(role.model_dump(mode="json"))
    assert restored.kind == RoleKind.USER
    assert restored.actorIds == ["alice", "bob"]


def test_role_rejects_malformed_kind():
    with pytest.raises(ValidationError):
        Role(id="r1", name="reader", composite=False, kind="Bogus")


def test_role_rejects_non_list_actor_ids():
    with pytest.raises(ValidationError):
        Role(
            id="r1",
            name="reader",
            composite=False,
            kind=RoleKind.USER,
            actorIds="alice",
        )


# --- Scope.serviceId (SPM routing key) ---


def test_scope_accepts_service_id():
    scope = Scope(id="s1", name="read", serviceId="github-tool")
    assert scope.serviceId == "github-tool"


def test_scope_service_id_round_trip():
    scope = Scope(id="s1", name="read", serviceId="github-tool")
    restored = Scope.model_validate(scope.model_dump(mode="json"))
    assert restored.serviceId == "github-tool"


# --- ServicePolicyModel (persistent source of truth) ---


def test_service_policy_model_constructs():
    role = _role()
    scope = _scope()
    spm = ServicePolicyModel(
        service_id="github-tool",
        service_type=ServiceType.TOOL,
        owned_roles=[role],
        owned_scopes=[scope],
        inbound_rules=[PolicyRule(role=role, scope=scope)],
    )
    assert spm.service_id == "github-tool"
    assert spm.service_type == ServiceType.TOOL
    assert spm.owned_roles == [role]
    assert spm.owned_scopes == [scope]
    assert spm.inbound_rules == [PolicyRule(role=role, scope=scope)]


def test_service_policy_model_round_trip_string_keys_only():
    role = _role()
    scope = _scope()
    spm = ServicePolicyModel(
        service_id="weather-agent",
        service_type=ServiceType.AGENT,
        owned_roles=[role],
        owned_scopes=[scope],
        inbound_rules=[PolicyRule(role=role, scope=scope)],
    )
    dumped = spm.model_dump(mode="json")
    assert all(isinstance(k, str) for k in dumped.keys())
    restored = ServicePolicyModel.model_validate(dumped)
    assert restored == spm


def test_service_policy_model_ignores_extra_fields():
    spm = ServicePolicyModel.model_validate(
        {
            "service_id": "svc",
            "service_type": "Tool",
            "owned_roles": [],
            "owned_scopes": [],
            "inbound_rules": [],
            "unknown_field": "ignored",
        }
    )
    assert not hasattr(spm, "unknown_field")


# --- PolicyRule construction ---


def test_policy_rule_with_typed_role_and_scope():
    role = _role()
    scope = _scope()
    rule = PolicyRule(role=role, scope=scope)
    assert rule.role == role
    assert rule.scope == scope


def test_policy_rule_rejects_plain_str_role():
    with pytest.raises(ValidationError):
        PolicyRule(role="admin", scope=_scope())


def test_policy_rule_rejects_plain_str_scope():
    with pytest.raises(ValidationError):
        PolicyRule(role=_role(), scope="read")


# --- AgentPolicyModel relationship maps keyed by string id ---


def test_agent_policy_model_source_roles_keyed_by_service_id():
    svc = _service()
    role = _role()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[role],
        agent_scopes=[],
        subject_roles={},
        source_roles={svc.id: [role]},
        target_scopes={},
        inbound_rules=[],
        outbound_rules=[],
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["source_roles"].keys()) == [svc.id]


def test_agent_policy_model_target_scopes_keyed_by_target_id():
    scope = _scope()
    svc = _service()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[scope],
        subject_roles={},
        source_roles={},
        target_scopes={svc.id: [scope]},
        inbound_rules=[],
        outbound_rules=[],
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["target_scopes"].keys()) == [svc.id]


def test_agent_policy_model_subject_roles_keyed_by_subject_id():
    subject = _subject()
    role = _role()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[],
        subject_roles={subject.id: [role]},
        source_roles={},
        target_scopes={},
        inbound_rules=[],
        outbound_rules=[],
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["subject_roles"].keys()) == [subject.id]


# --- outbound_subject_rules (user role -> tool scope) ---


def test_agent_policy_model_outbound_subject_rules_defaults_empty():
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[],
        subject_roles={},
        source_roles={},
        target_scopes={},
        inbound_rules=[],
        outbound_rules=[],
    )
    assert model.outbound_subject_rules == []


def test_agent_policy_model_outbound_subject_rules_round_trip():
    role = _role()
    scope = _scope()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[],
        subject_roles={},
        source_roles={},
        target_scopes={},
        inbound_rules=[],
        outbound_rules=[],
        outbound_subject_rules=[PolicyRule(role=role, scope=scope)],
    )
    restored = AgentPolicyModel.model_validate(model.model_dump(mode="json"))
    assert restored.outbound_subject_rules == [PolicyRule(role=role, scope=scope)]


# --- model_validate round-trip (JSON mode) ---


def test_agent_policy_model_round_trip():
    subject = _subject()
    role = _role()
    scope = _scope()
    svc = _service()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[role],
        agent_scopes=[scope],
        subject_roles={subject.id: [role]},
        source_roles={svc.id: [role]},
        target_scopes={svc.id: [scope]},
        inbound_rules=[PolicyRule(role=role, scope=scope)],
        outbound_rules=[],
    )
    dumped = model.model_dump(mode="json")
    restored = AgentPolicyModel.model_validate(dumped)
    assert restored == model


# --- extra='ignore' on all three model types ---


def test_policy_rule_ignores_extra_fields():
    role = _role()
    scope = _scope()
    rule = PolicyRule.model_validate({"role": role.model_dump(), "scope": scope.model_dump(), "unknown": "x"})
    assert not hasattr(rule, "unknown")


def test_agent_policy_model_ignores_extra_fields():
    model = AgentPolicyModel.model_validate(
        {
            "agent_id": "a",
            "agent_roles": [],
            "agent_scopes": [],
            "subject_roles": {},
            "source_roles": {},
            "target_scopes": {},
            "inbound_rules": [],
            "outbound_rules": [],
            "unknown_field": "ignored",
        }
    )
    assert not hasattr(model, "unknown_field")


def test_policy_model_ignores_extra_fields():
    model = PolicyModel.model_validate({"agents": [], "extra_key": "ignored"})
    assert not hasattr(model, "extra_key")


# --- Empty PolicyModel ---


def test_policy_model_empty():
    model = PolicyModel(agents=[])
    assert model.agents == []
