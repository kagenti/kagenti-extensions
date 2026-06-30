import pytest
from pydantic import ValidationError

from aiac.idp.configuration.models import Role, Scope, Service, Subject
from aiac.policy.model.models import AgentPolicyModel, PolicyModel, PolicyRule


def _role(id: str = "role-1", name: str = "admin") -> Role:
    return Role(id=id, name=name, composite=False)


def _scope(id: str = "scope-1", name: str = "read") -> Scope:
    return Scope(id=id, name=name)


def _service(id: str = "svc-1", service_id: str = "my-service") -> Service:
    return Service(id=id, serviceId=service_id, enabled=True)


def _subject(id: str = "sub-1", username: str = "alice") -> Subject:
    return Subject(id=id, username=username, enabled=True)


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


# --- AgentPolicyModel serialization with model dict keys ---


def test_agent_policy_model_service_keys_in_source_roles():
    svc = _service()
    role = _role()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[role],
        agent_scopes=[],
        subject_roles={},
        source_roles={svc: [role]},
        scope_targets={},
        inbound_rules=[],
        outbound_rules=[],
    )
    dumped = model.model_dump()
    assert isinstance(dumped, dict)


def test_agent_policy_model_scope_keys_in_scope_targets():
    scope = _scope()
    svc = _service()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[scope],
        subject_roles={},
        source_roles={},
        scope_targets={scope: [svc]},
        inbound_rules=[],
        outbound_rules=[],
    )
    dumped = model.model_dump()
    assert isinstance(dumped, dict)


def test_agent_policy_model_subject_keys_in_subject_roles():
    subject = _subject()
    role = _role()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[],
        agent_scopes=[],
        subject_roles={subject: [role]},
        source_roles={},
        scope_targets={},
        inbound_rules=[],
        outbound_rules=[],
    )
    dumped = model.model_dump()
    assert isinstance(dumped, dict)


# --- model_validate round-trip ---


def test_agent_policy_model_round_trip():
    subject = _subject()
    role = _role()
    scope = _scope()
    svc = _service()
    model = AgentPolicyModel(
        agent_id="agent-1",
        agent_roles=[role],
        agent_scopes=[scope],
        subject_roles={subject: [role]},
        source_roles={svc: [role]},
        scope_targets={scope: [svc]},
        inbound_rules=[PolicyRule(role=role, scope=scope)],
        outbound_rules=[],
    )
    dumped = model.model_dump()
    restored = AgentPolicyModel.model_validate(dumped)
    assert restored == model


# --- Hash / equality: Role ---


def test_role_same_id_equal_and_hash_equal():
    r1 = _role(id="r1")
    r2 = _role(id="r1", name="different-name")
    assert r1 == r2
    assert hash(r1) == hash(r2)
    d = {r1: "value"}
    assert d[r2] == "value"


def test_role_different_id_not_equal():
    r1 = _role(id="r1")
    r2 = _role(id="r2")
    assert r1 != r2
    d = {r1: "v1", r2: "v2"}
    assert len(d) == 2


# --- Hash / equality: Scope ---


def test_scope_same_id_equal_and_hash_equal():
    s1 = _scope(id="s1")
    s2 = _scope(id="s1", name="other")
    assert s1 == s2
    assert hash(s1) == hash(s2)
    d = {s1: "value"}
    assert d[s2] == "value"


def test_scope_different_id_not_equal():
    s1 = _scope(id="s1")
    s2 = _scope(id="s2")
    assert s1 != s2
    d = {s1: "v1", s2: "v2"}
    assert len(d) == 2


# --- Hash / equality: Service ---


def test_service_same_id_equal_and_hash_equal():
    svc1 = _service(id="svc-1")
    svc2 = _service(id="svc-1", service_id="other-id")
    assert svc1 == svc2
    assert hash(svc1) == hash(svc2)
    d = {svc1: "value"}
    assert d[svc2] == "value"


def test_service_different_id_not_equal():
    svc1 = _service(id="svc-1")
    svc2 = _service(id="svc-2")
    assert svc1 != svc2
    d = {svc1: "v1", svc2: "v2"}
    assert len(d) == 2


# --- Hash / equality: Subject ---


def test_subject_same_id_equal_and_hash_equal():
    sub1 = _subject(id="sub-1")
    sub2 = _subject(id="sub-1", username="bob")
    assert sub1 == sub2
    assert hash(sub1) == hash(sub2)
    d = {sub1: "value"}
    assert d[sub2] == "value"


def test_subject_different_id_not_equal():
    sub1 = _subject(id="sub-1")
    sub2 = _subject(id="sub-2")
    assert sub1 != sub2
    d = {sub1: "v1", sub2: "v2"}
    assert len(d) == 2


# --- extra='ignore' on all three model types ---


def test_policy_rule_ignores_extra_fields():
    role = _role()
    scope = _scope()
    rule = PolicyRule.model_validate(
        {"role": role.model_dump(), "scope": scope.model_dump(), "unknown": "x"}
    )
    assert not hasattr(rule, "unknown")


def test_agent_policy_model_ignores_extra_fields():
    model = AgentPolicyModel.model_validate(
        {
            "agent_id": "a",
            "agent_roles": [],
            "agent_scopes": [],
            "subject_roles": {},
            "source_roles": {},
            "scope_targets": {},
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
