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
    RuleEffect,
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


# --- RuleEffect enum (ALLOW/DENY) ---


def test_rule_effect_values_mirror_service_type_style():
    assert RuleEffect.ALLOW == "Allow"
    assert RuleEffect.DENY == "Deny"


def test_rule_effect_serializes_as_string():
    rule = PolicyRule(role=_role(), scope=_scope(), effect=RuleEffect.DENY)
    assert rule.model_dump(mode="json")["effect"] == "Deny"


# --- Scope.serviceId (SPM routing key) ---


def test_scope_accepts_service_id():
    scope = Scope(id="s1", name="read", serviceId="github-tool")
    assert scope.serviceId == "github-tool"


def test_scope_service_id_round_trip():
    scope = Scope(id="s1", name="read", serviceId="github-tool")
    restored = Scope.model_validate(scope.model_dump(mode="json"))
    assert restored.serviceId == "github-tool"


# --- ServicePolicyModel (persistent source of truth) ---


def test_service_policy_model_constructs_with_split_rule_lists():
    role = _role()
    scope = _scope()
    allow = PolicyRule(role=role, scope=scope, effect=RuleEffect.ALLOW)
    deny = PolicyRule(role=role, scope=scope, effect=RuleEffect.DENY)
    spm = ServicePolicyModel(
        service_id="github-tool",
        service_type=ServiceType.TOOL,
        owned_roles=[role],
        owned_scopes=[scope],
        inbound_allow_rules=[allow],
        inbound_deny_rules=[deny],
    )
    assert spm.service_id == "github-tool"
    assert spm.service_type == ServiceType.TOOL
    assert spm.owned_roles == [role]
    assert spm.owned_scopes == [scope]
    assert spm.inbound_allow_rules == [allow]
    assert spm.inbound_deny_rules == [deny]


def test_service_policy_model_has_no_intermixed_inbound_rules_field():
    spm = ServicePolicyModel(
        service_id="svc",
        service_type=ServiceType.TOOL,
        owned_roles=[],
        owned_scopes=[],
    )
    # The single intermixed list is gone — allow/deny are explicitly separated.
    assert "inbound_rules" not in ServicePolicyModel.model_fields
    assert not hasattr(spm, "inbound_rules")


def test_service_policy_model_rule_lists_default_empty():
    spm = ServicePolicyModel(
        service_id="svc",
        service_type=ServiceType.TOOL,
        owned_roles=[],
        owned_scopes=[],
    )
    assert spm.inbound_allow_rules == []
    assert spm.inbound_deny_rules == []


def test_service_policy_model_round_trip_string_keys_only():
    role = _role()
    scope = _scope()
    spm = ServicePolicyModel(
        service_id="weather-agent",
        service_type=ServiceType.AGENT,
        owned_roles=[role],
        owned_scopes=[scope],
        inbound_allow_rules=[PolicyRule(role=role, scope=scope, effect=RuleEffect.ALLOW)],
        inbound_deny_rules=[PolicyRule(role=role, scope=scope, effect=RuleEffect.DENY)],
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
            "inbound_allow_rules": [],
            "inbound_deny_rules": [],
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


def test_policy_rule_effect_defaults_to_allow():
    rule = PolicyRule(role=_role(), scope=_scope())
    assert rule.effect == RuleEffect.ALLOW


def test_policy_rule_accepts_explicit_deny():
    rule = PolicyRule(role=_role(), scope=_scope(), effect=RuleEffect.DENY)
    assert rule.effect == RuleEffect.DENY


def test_same_role_scope_coexists_as_allow_and_deny():
    role = _role()
    scope = _scope()
    allow = PolicyRule(role=role, scope=scope, effect=RuleEffect.ALLOW)
    deny = PolicyRule(role=role, scope=scope, effect=RuleEffect.DENY)
    # Dedup identity is (role.id, scope.id, effect): differing only in effect keeps them distinct,
    # so both survive side by side in a rule list.
    assert allow != deny
    rules = [allow, deny]
    assert rules == [allow, deny]


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
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["source_roles"].keys()) == [svc.id]


def test_agent_policy_model_subject_roles_keyed_by_subject_id():
    subject = _subject()
    role = _role()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[],
        subject_roles={subject.id: [role]},
        source_roles={},
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["subject_roles"].keys()) == [subject.id]


def test_agent_policy_model_target_allow_scopes_keyed_by_target_id():
    scope = _scope()
    svc = _service()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[scope],
        subject_roles={},
        source_roles={},
        target_allow_scopes={svc.id: [scope]},
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["target_allow_scopes"].keys()) == [svc.id]


def test_agent_policy_model_target_deny_scopes_keyed_by_target_id():
    scope = _scope()
    svc = _service()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[scope],
        subject_roles={},
        source_roles={},
        target_deny_scopes={svc.id: [scope]},
    )
    dumped = model.model_dump(mode="json")
    assert list(dumped["target_deny_scopes"].keys()) == [svc.id]


# --- 8 entity×effect rule lists + split target maps ---

_EIGHT_RULE_LISTS = [
    "inbound_subject_allow_rules",
    "inbound_subject_deny_rules",
    "inbound_source_allow_rules",
    "inbound_source_deny_rules",
    "outbound_target_allow_rules",
    "outbound_target_deny_rules",
    "outbound_subject_allow_rules",
    "outbound_subject_deny_rules",
]


def test_agent_policy_model_eight_lists_and_target_maps_default_empty():
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[],
        subject_roles={},
        source_roles={},
    )
    for field in _EIGHT_RULE_LISTS:
        assert getattr(model, field) == [], f"{field} should default to []"
    assert model.target_allow_scopes == {}
    assert model.target_deny_scopes == {}


def test_agent_policy_model_has_no_legacy_rule_fields():
    for legacy in ("inbound_rules", "outbound_rules", "outbound_subject_rules", "target_scopes"):
        assert legacy not in AgentPolicyModel.model_fields, f"{legacy} should be removed"


# --- model_validate round-trip (JSON mode) ---


def test_agent_policy_model_round_trip():
    subject = _subject()
    role = _role()
    scope = _scope()
    svc = _service()
    allow = PolicyRule(role=role, scope=scope, effect=RuleEffect.ALLOW)
    deny = PolicyRule(role=role, scope=scope, effect=RuleEffect.DENY)
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[role],
        agent_scopes=[scope],
        subject_roles={subject.id: [role]},
        source_roles={svc.id: [role]},
        target_allow_scopes={svc.id: [scope]},
        target_deny_scopes={svc.id: [scope]},
        inbound_subject_allow_rules=[allow],
        inbound_subject_deny_rules=[deny],
        inbound_source_allow_rules=[allow],
        inbound_source_deny_rules=[deny],
        outbound_target_allow_rules=[allow],
        outbound_target_deny_rules=[deny],
        outbound_subject_allow_rules=[allow],
        outbound_subject_deny_rules=[deny],
    )
    dumped = model.model_dump(mode="json")
    restored = AgentPolicyModel.model_validate(dumped)
    assert restored == model
    # Typed PolicyRule / Scope values survive the round-trip in the split lists and target maps.
    assert restored.outbound_target_deny_rules[0].effect == RuleEffect.DENY
    assert restored.target_allow_scopes[svc.id] == [scope]


# --- effect-agnostic identity maps must include deny-only roles ---


def test_deny_only_subject_role_still_registered_in_subject_roles():
    # A role that appears ONLY in a DENY edge must still be registered in the effect-agnostic
    # subject_roles map, or the Rego deny lookup cannot resolve it and the prohibition never fires.
    subject = _subject()
    deny_role = _role(id="deny-role", name="developer")
    agent_scope = _scope(id="agent-scope", name="invoke")
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[agent_scope],
        source_roles={},
        subject_roles={subject.id: [deny_role]},  # deny-only role, still listed
        inbound_subject_allow_rules=[],
        inbound_subject_deny_rules=[PolicyRule(role=deny_role, scope=agent_scope, effect=RuleEffect.DENY)],
    )
    assert deny_role in model.subject_roles[subject.id]
    restored = AgentPolicyModel.model_validate(model.model_dump(mode="json"))
    assert restored.subject_roles[subject.id] == [deny_role]


def test_deny_only_source_role_still_registered_in_source_roles():
    svc = _service()
    deny_role = _role(id="deny-src", name="caller")
    agent_scope = _scope(id="agent-scope", name="invoke")
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[agent_scope],
        source_roles={svc.id: [deny_role]},  # deny-only source role, still listed
        subject_roles={},
        inbound_source_allow_rules=[],
        inbound_source_deny_rules=[PolicyRule(role=deny_role, scope=agent_scope, effect=RuleEffect.DENY)],
    )
    assert deny_role in model.source_roles[svc.id]
    restored = AgentPolicyModel.model_validate(model.model_dump(mode="json"))
    assert restored.source_roles[svc.id] == [deny_role]


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
