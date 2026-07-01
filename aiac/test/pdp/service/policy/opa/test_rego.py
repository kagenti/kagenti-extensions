"""Unit tests for aiac.pdp.service.policy.opa.rego."""

from aiac.idp.configuration.models import Role, Scope
from aiac.policy.model.models import AgentPolicyModel, PolicyRule
from aiac.pdp.service.policy.opa.rego import (
    generate_inbound_rego,
    generate_outbound_rego,
    slugify,
)


def _role(name: str = "reader") -> Role:
    return Role(id=f"role-{name}", name=name, composite=False)


def _scope(name: str = "read") -> Scope:
    return Scope(id=f"scope-{name}", name=name)


def _model(
    agent_id: str = "weather-agent",
    source_roles: dict[str, list[Role]] | None = None,
    target_scopes: dict[str, list[Scope]] | None = None,
    inbound_rules: list[PolicyRule] | None = None,
    outbound_rules: list[PolicyRule] | None = None,
) -> AgentPolicyModel:
    return AgentPolicyModel(
        agent_id=agent_id,
        agent_roles=[],
        agent_scopes=[],
        subject_roles={},
        source_roles=source_roles or {},
        target_scopes=target_scopes or {},
        inbound_rules=inbound_rules or [],
        outbound_rules=outbound_rules or [],
    )


def test_slugify_hyphens_become_underscores():
    assert slugify("weather-agent") == "weather_agent"


def test_slugify_lowercases_without_hyphens():
    assert slugify("WeatherAgent") == "weatheragent"


# --- generate_inbound_rego ---


def test_inbound_has_package_header_with_slug():
    rego = generate_inbound_rego(_model(agent_id="weather-agent"))
    assert "package authz.weather_agent.inbound" in rego


def test_inbound_embeds_source_roles_map_with_role_names():
    model = _model(source_roles={"github-tool": [_role("reader"), _role("writer")]})
    rego = generate_inbound_rego(model)
    assert "source_roles := {" in rego
    assert '"github-tool": ["reader", "writer"]' in rego


def test_inbound_one_allow_block_per_rule():
    model = _model(
        inbound_rules=[
            PolicyRule(role=_role("reader"), scope=_scope("read")),
            PolicyRule(role=_role("writer"), scope=_scope("write")),
        ]
    )
    rego = generate_inbound_rego(model)
    assert rego.count("allow if {") == 2
    assert "some role in source_roles[input.source]" in rego
    assert 'role == "reader"' in rego
    assert 'input.scope == "read"' in rego
    assert 'role == "writer"' in rego
    assert 'input.scope == "write"' in rego


def test_inbound_empty_rules_produces_no_allow_blocks():
    rego = generate_inbound_rego(_model(inbound_rules=[]))
    assert "package authz.weather_agent.inbound" in rego
    assert "default allow := false" in rego
    assert "allow if {" not in rego


def test_inbound_renders_every_source_in_source_roles():
    model = _model(
        source_roles={
            "github-tool": [_role("reader")],
            "slack-tool": [_role("writer")],
        }
    )
    rego = generate_inbound_rego(model)
    assert '"github-tool": ["reader"]' in rego
    assert '"slack-tool": ["writer"]' in rego


# --- generate_outbound_rego ---


def test_outbound_has_package_header_with_slug():
    rego = generate_outbound_rego(_model(agent_id="weather-agent"))
    assert "package authz.weather_agent.outbound" in rego


def test_outbound_embeds_target_scopes_map_with_scope_names():
    model = _model(
        target_scopes={"github-tool": [_scope("issues.read"), _scope("issues.write")]}
    )
    rego = generate_outbound_rego(model)
    assert "target_scopes := {" in rego
    assert '"github-tool": ["issues.read", "issues.write"]' in rego


def test_outbound_one_allow_block_per_rule():
    model = _model(
        outbound_rules=[
            PolicyRule(role=_role("reader"), scope=_scope("issues.read")),
            PolicyRule(role=_role("writer"), scope=_scope("issues.write")),
        ]
    )
    rego = generate_outbound_rego(model)
    assert rego.count("allow if {") == 2
    assert 'input.role == "reader"' in rego
    assert 'input.scope == "issues.read"' in rego
    assert '"issues.read" in target_scopes[input.target]' in rego
    assert 'input.role == "writer"' in rego
    assert 'input.scope == "issues.write"' in rego
    assert '"issues.write" in target_scopes[input.target]' in rego


def test_outbound_empty_rules_produces_no_allow_blocks():
    rego = generate_outbound_rego(_model(outbound_rules=[]))
    assert "package authz.weather_agent.outbound" in rego
    assert "default allow := false" in rego
    assert "allow if {" not in rego
