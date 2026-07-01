"""Unit tests for aiac.pdp.service.policy.opa.rego (ID-only model)."""

from aiac.idp.configuration.models import Role, Scope
from aiac.pdp.service.policy.opa.rego import (
    generate_inbound_rego,
    generate_outbound_rego,
    slugify,
)
from aiac.policy.model.models import AgentPolicyModel, PolicyRule


def _role(name: str = "reader") -> Role:
    return Role(id=f"role-{name}", name=name, composite=False)


def _scope(name: str = "read") -> Scope:
    return Scope(id=f"scope-{name}", name=name)


def _model(
    agent_id: str = "weather-agent",
    agent_roles: list[Role] | None = None,
    agent_scopes: list[Scope] | None = None,
    subject_roles: dict[str, list[Role]] | None = None,
    source_roles: dict[str, list[Role]] | None = None,
    target_scopes: dict[str, list[Scope]] | None = None,
    inbound_rules: list[PolicyRule] | None = None,
    outbound_rules: list[PolicyRule] | None = None,
) -> AgentPolicyModel:
    return AgentPolicyModel(
        agent_id=agent_id,
        agent_roles=agent_roles or [],
        agent_scopes=agent_scopes or [],
        subject_roles=subject_roles or {},
        source_roles=source_roles or {},
        target_scopes=target_scopes or {},
        inbound_rules=inbound_rules or [],
        outbound_rules=outbound_rules or [],
    )


def _github_agent() -> AgentPolicyModel:
    """The worked example from the handoff."""
    developer = _role("developer")
    tester = _role("tester")
    source_helper = _role("source-helper")
    issues_helper = _role("issues-helper")
    source_access = _scope("source-access")
    issues_access = _scope("issues-access")
    source_read = _scope("source-read")
    source_write = _scope("source-write")
    issues_read = _scope("issues-read")
    issues_write = _scope("issues-write")
    return _model(
        agent_id="github-agent",
        agent_roles=[source_helper, issues_helper],
        agent_scopes=[source_access, issues_access],
        subject_roles={"dev-user": [developer], "test-user": [tester]},
        target_scopes={
            "github-tool": [source_read, source_write, issues_read, issues_write]
        },
        inbound_rules=[
            PolicyRule(role=developer, scope=source_access),
            PolicyRule(role=developer, scope=issues_access),
            PolicyRule(role=tester, scope=issues_access),
        ],
        outbound_rules=[
            PolicyRule(role=source_helper, scope=source_read),
            PolicyRule(role=source_helper, scope=source_write),
            PolicyRule(role=issues_helper, scope=issues_read),
            PolicyRule(role=issues_helper, scope=issues_write),
        ],
    )


# --- slugify ---


def test_slugify_hyphens_become_underscores():
    assert slugify("weather-agent") == "weather_agent"


def test_slugify_lowercases_without_hyphens():
    assert slugify("WeatherAgent") == "weatheragent"


# --- generate_inbound_rego ---


def test_inbound_has_package_header_with_slug():
    rego = generate_inbound_rego(_model(agent_id="weather-agent"))
    assert "package authz.weather_agent.inbound" in rego


def test_inbound_embeds_agent_scopes_list():
    model = _model(agent_scopes=[_scope("source-access"), _scope("issues-access")])
    rego = generate_inbound_rego(model)
    assert 'agent_scopes := ["source-access", "issues-access"]' in rego


def test_inbound_embeds_subject_roles_map():
    model = _model(subject_roles={"dev-user": [_role("developer"), _role("tester")]})
    rego = generate_inbound_rego(model)
    assert "subject_roles := {" in rego
    assert '"dev-user": ["developer", "tester"]' in rego


def test_inbound_embeds_source_roles_map():
    model = _model(source_roles={"github-tool": [_role("reader")]})
    rego = generate_inbound_rego(model)
    assert "source_roles := {" in rego
    assert '"github-tool": ["reader"]' in rego


def test_inbound_role_scopes_grouped_from_inbound_rules():
    model = _github_agent()
    rego = generate_inbound_rego(model)
    assert "role_scopes := {" in rego
    assert '"developer": ["source-access", "issues-access"]' in rego
    assert '"tester": ["issues-access"]' in rego


def test_inbound_has_subject_and_source_gates_and_allow():
    rego = generate_inbound_rego(_github_agent())
    assert "subject_ok if {" in rego
    assert "some role in subject_roles[input.subject]" in rego
    assert "some scope in role_scopes[role]" in rego
    assert "scope in agent_scopes" in rego
    assert "source_ok if { not input.source }" in rego
    assert "some role in source_roles[input.source]" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; source_ok }" in rego


def test_inbound_has_no_legacy_id_carrying_input():
    rego = generate_inbound_rego(_github_agent())
    assert "input.role" not in rego
    assert "input.scope" not in rego
    assert "source_roles[input.source]" in rego  # only inside source_ok gate
    assert "scope_targets" not in rego


def test_inbound_empty_model_renders_valid_empty_literals():
    rego = generate_inbound_rego(_model())
    assert "agent_scopes := []" in rego
    assert "subject_roles := {}" in rego
    assert "source_roles := {}" in rego
    assert "role_scopes := {}" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; source_ok }" in rego


# --- generate_outbound_rego ---


def test_outbound_has_package_header_with_slug():
    rego = generate_outbound_rego(_model(agent_id="weather-agent"))
    assert "package authz.weather_agent.outbound" in rego


def test_outbound_embeds_agent_roles_and_scopes_lists():
    rego = generate_outbound_rego(_github_agent())
    assert 'agent_roles := ["source-helper", "issues-helper"]' in rego
    assert 'agent_scopes := ["source-access", "issues-access"]' in rego


def test_outbound_agent_role_scopes_grouped_from_outbound_rules():
    rego = generate_outbound_rego(_github_agent())
    assert "agent_role_scopes := {" in rego
    assert '"source-helper": ["source-read", "source-write"]' in rego
    assert '"issues-helper": ["issues-read", "issues-write"]' in rego


def test_outbound_target_scopes_rendered_directly():
    rego = generate_outbound_rego(_github_agent())
    assert "target_scopes := {" in rego
    assert (
        '"github-tool": ["source-read", "source-write", "issues-read", "issues-write"]'
        in rego
    )
    assert "scope_targets" not in rego


def test_outbound_has_subject_and_target_gates_and_allow():
    rego = generate_outbound_rego(_github_agent())
    assert "subject_ok if {" in rego
    assert "target_ok if {" in rego
    assert "some role in agent_roles" in rego
    assert "some scope in agent_role_scopes[role]" in rego
    assert "scope in target_scopes[input.target]" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; target_ok }" in rego


def test_outbound_has_no_legacy_id_carrying_input():
    rego = generate_outbound_rego(_github_agent())
    assert "input.role" not in rego
    assert "input.scope" not in rego
    assert "scope_targets" not in rego


def test_outbound_empty_model_renders_valid_empty_literals():
    rego = generate_outbound_rego(_model())
    assert "agent_roles := []" in rego
    assert "agent_scopes := []" in rego
    assert "subject_roles := {}" in rego
    assert "role_scopes := {}" in rego
    assert "agent_role_scopes := {}" in rego
    assert "target_scopes := {}" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; target_ok }" in rego
