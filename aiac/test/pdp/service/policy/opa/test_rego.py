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
    outbound_subject_rules: list[PolicyRule] | None = None,
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
        outbound_subject_rules=outbound_subject_rules or [],
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
        outbound_subject_rules=[
            PolicyRule(role=developer, scope=source_read),
            PolicyRule(role=developer, scope=source_write),
            PolicyRule(role=developer, scope=issues_read),
            PolicyRule(role=tester, scope=issues_read),
            PolicyRule(role=tester, scope=issues_write),
        ],
    )


# --- slugify ---


def test_slugify_hyphens_become_underscores():
    assert slugify("weather-agent") == "weather_agent"


def test_slugify_lowercases_without_hyphens():
    assert slugify("WeatherAgent") == "weatheragent"


def test_slugify_strips_slashes_and_colons_from_spiffe_uri():
    slug = slugify("spiffe://localtest.me/ns/team1/sa/github-agent")
    assert "/" not in slug
    assert ":" not in slug
    assert "." not in slug


def test_slugify_spiffe_uri_extracts_namespace_and_name_short_id():
    """The slug must be predictable from just {namespace}/{name}, not the trust domain."""
    assert slugify("spiffe://localtest.me/ns/team1/sa/github-agent") == "team1_github_agent"
    assert (
        slugify("spiffe://other-trust-domain.example/ns/team1/sa/github-agent")
        == "team1_github_agent"
    )


def test_slugify_plain_ns_workload_clientid_matches_spiffe_slug():
    """Same {ns}/{workload} id, with or without SPIRE, must slugify identically."""
    assert slugify("team1/github-agent") == "team1_github_agent"


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


def test_outbound_embeds_agent_roles_list():
    rego = generate_outbound_rego(_github_agent())
    assert 'agent_roles := ["source-helper", "issues-helper"]' in rego
    # agent_scopes is the inbound audience gate; the outbound package must not emit it.
    assert "agent_scopes :=" not in rego


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


def test_outbound_subject_role_scopes_grouped_from_outbound_subject_rules():
    rego = generate_outbound_rego(_github_agent())
    assert "outbound_subject_role_scopes := {" in rego
    assert '"developer": ["source-read", "source-write", "issues-read"]' in rego
    assert '"tester": ["issues-read", "issues-write"]' in rego


def test_outbound_subject_ok_matches_target_scopes_not_agent_scopes():
    rego = generate_outbound_rego(_github_agent())
    # The outbound subject gate is user->tool: it reads outbound_subject_role_scopes
    # and matches against the tool's scopes, not the agent's own scopes.
    assert "subject_ok if {" in rego
    assert "some role in subject_roles[input.subject]" in rego
    assert "some scope in outbound_subject_role_scopes[role]" in rego
    assert "scope in target_scopes[input.target]" in rego
    # The inbound-flavoured subject gate must NOT appear in the outbound package.
    assert "some scope in role_scopes[role]" not in rego
    assert "scope in agent_scopes" not in rego


def test_outbound_does_not_embed_inbound_role_scopes():
    rego = generate_outbound_rego(_github_agent())
    # The inbound ``role_scopes`` map (from inbound_rules) must not leak into the
    # outbound package. Line-anchored so it does not match outbound_subject_role_scopes.
    assert "\nrole_scopes :=" not in rego
    assert not rego.startswith("role_scopes :=")


def test_outbound_has_target_gate_and_allow():
    rego = generate_outbound_rego(_github_agent())
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
    assert "subject_roles := {}" in rego
    assert "outbound_subject_role_scopes := {}" in rego
    assert "agent_role_scopes := {}" in rego
    assert "target_scopes := {}" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; target_ok }" in rego
