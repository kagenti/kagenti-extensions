"""Unit tests for aiac.pdp.service.policy.opa.rego.

Targets the post-rework generator: fixed package names
(``authbridge.client.{inbound,outbound}.request`` + ``import rego.v1``), the
nested ``input.identity`` / ``input.mcp`` shape, the ``rossoctl`` platform
bypass, and outbound de-prefixing (provisioned ``<owner>.<tool>`` scope names
collapse to the bare ``input.mcp.params.name`` the live plugin sends).
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from aiac.idp.configuration.models import Role, Scope
from aiac.pdp.service.policy.opa.rego import (
    generate_inbound_rego,
    generate_outbound_rego,
    identity_ref,
)
from aiac.policy.model.models import AgentPolicyModel, PolicyRule

# Full SPIFFE id of the github-tool workload that owns the outbound scopes.
GH_TOOL = "spiffe://localtest.me/ns/team1/sa/github-tool"
# The agent whose policy we render.
GH_AGENT = "spiffe://localtest.me/ns/team1/sa/github-agent"


def _role(name: str = "reader") -> Role:
    return Role(id=f"role-{name}", name=name, composite=False)


def _scope(name: str = "read", service_id: str = "") -> Scope:
    return Scope(id=f"scope-{name}", name=name, serviceId=service_id)


def _model(
    agent_id: str = "team1/weather-agent",
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
    """The worked example.

    Inbound agent scopes are prefixed by the *agent* (``github-agent.*``) and are
    **not** de-prefixed — inbound compares scopes internally, never against the
    invoked tool name. Outbound tool scopes are prefixed by the *tool*
    (``github-tool.*``) and carry ``serviceId`` so they de-prefix to the bare tool
    names the live plugin puts in ``input.mcp.params.name``.
    """
    developer = _role("developer")
    tester = _role("tester")
    source_helper = _role("source-helper")
    issues_helper = _role("issues-helper")
    # Inbound audience scopes — agent-owned, prefixed, NOT de-prefixed.
    source_access = _scope("github-agent.source_operations")
    issues_access = _scope("github-agent.issues_operations")
    # Outbound tool scopes — tool-owned, prefixed AND carrying serviceId.
    source_read = _scope("github-tool.source-read", GH_TOOL)
    source_write = _scope("github-tool.source-write", GH_TOOL)
    issues_read = _scope("github-tool.issues-read", GH_TOOL)
    issues_write = _scope("github-tool.issues-write", GH_TOOL)
    return _model(
        agent_id=GH_AGENT,
        agent_roles=[source_helper, issues_helper],
        agent_scopes=[source_access, issues_access],
        subject_roles={"dev-user": [developer], "test-user": [tester]},
        source_roles={"github-tool": [_role("reader")]},
        # target_scopes keyed by the FULL tool service id.
        target_scopes={
            GH_TOOL: [source_read, source_write, issues_read, issues_write]
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


# --- identity_ref ---


def test_identity_ref_spiffe():
    assert identity_ref("spiffe://localtest.me/ns/team1/sa/github-agent") == (
        "team1",
        "github-agent",
    )


def test_identity_ref_trust_domain_irrelevant():
    assert identity_ref("spiffe://other.example/ns/team1/sa/github-agent") == (
        "team1",
        "github-agent",
    )


def test_identity_ref_plain_ns_name():
    assert identity_ref("team1/github-agent") == ("team1", "github-agent")


def test_identity_ref_no_namespace_raises():
    with pytest.raises(ValueError):
        identity_ref("github-agent")


def test_identity_ref_invalid_label_raises():
    with pytest.raises(ValueError):
        identity_ref("Team1/GitHub-Agent")  # uppercase -> not a DNS-1123 label


# --- generate_inbound_rego ---


def test_inbound_has_fixed_package_header():
    rego = generate_inbound_rego(_model())
    assert "package authbridge.client.inbound.request" in rego
    assert "import rego.v1" in rego


def test_inbound_embeds_agent_scopes_list_full_names():
    # Inbound audience scopes stay FULL (prefixed) — they are compared internally
    # against role_scopes, never against the bare invoked tool name.
    model = _model(
        agent_scopes=[
            _scope("github-agent.source_operations"),
            _scope("github-agent.issues_operations"),
        ]
    )
    rego = generate_inbound_rego(model)
    assert (
        'agent_scopes := ["github-agent.source_operations", '
        '"github-agent.issues_operations"]' in rego
    )


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


def test_inbound_role_scopes_grouped_from_inbound_rules_full_names():
    rego = generate_inbound_rego(_github_agent())
    assert "role_scopes := {" in rego
    assert (
        '"developer": ["github-agent.source_operations", '
        '"github-agent.issues_operations"]' in rego
    )
    assert '"tester": ["github-agent.issues_operations"]' in rego


def test_inbound_subject_gate_uses_identity_fields():
    rego = generate_inbound_rego(_github_agent())
    assert "subject_ok if {" in rego
    assert "some role in subject_roles[input.identity.subject]" in rego
    assert "some scope in role_scopes[role]" in rego
    assert "scope in agent_scopes" in rego


def test_inbound_platform_bypass_default_rossoctl():
    rego = generate_inbound_rego(_github_agent())
    assert "source_ok if { not input.identity.client_id }" in rego
    assert 'source_ok if { input.identity.client_id == "rossoctl" }' in rego
    assert "some role in source_roles[input.identity.client_id]" in rego


def test_inbound_platform_bypass_multiple_clients():
    rego = generate_inbound_rego(
        _github_agent(), platform_clients=("rossoctl", "argocd")
    )
    assert 'source_ok if { input.identity.client_id == "rossoctl" }' in rego
    assert 'source_ok if { input.identity.client_id == "argocd" }' in rego


def test_inbound_has_default_deny_and_allow():
    rego = generate_inbound_rego(_github_agent())
    assert "default allow := false" in rego
    assert "allow if { subject_ok; source_ok }" in rego


def test_inbound_uses_only_nested_identity_input():
    rego = generate_inbound_rego(_github_agent())
    # Only the nested identity fields appear (no flat legacy input keys).
    assert "input.identity.subject" in rego
    assert "input.identity.client_id" in rego


def test_inbound_empty_model_renders_valid_empty_literals():
    rego = generate_inbound_rego(_model())
    assert "agent_scopes := []" in rego
    assert "subject_roles := {}" in rego
    assert "source_roles := {}" in rego
    assert "role_scopes := {}" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; source_ok }" in rego


# --- generate_outbound_rego ---


def test_outbound_has_fixed_package_header():
    rego = generate_outbound_rego(_model())
    assert "package authbridge.client.outbound.request" in rego
    assert "import rego.v1" in rego


def test_outbound_embeds_agent_roles_list():
    rego = generate_outbound_rego(_github_agent())
    assert 'agent_roles := ["source-helper", "issues-helper"]' in rego
    # agent_scopes is the inbound audience gate; the outbound package must not emit it.
    assert "agent_scopes :=" not in rego


def test_outbound_subject_role_scopes_are_deprefixed():
    rego = generate_outbound_rego(_github_agent())
    assert "subject_role_scopes := {" in rego
    # bare tool names, owner prefix stripped
    assert '"developer": ["source-read", "source-write", "issues-read"]' in rego
    assert '"tester": ["issues-read", "issues-write"]' in rego


def test_outbound_agent_role_scopes_are_deprefixed():
    rego = generate_outbound_rego(_github_agent())
    assert "agent_role_scopes := {" in rego
    assert '"source-helper": ["source-read", "source-write"]' in rego
    assert '"issues-helper": ["issues-read", "issues-write"]' in rego


def test_outbound_target_scopes_full_key_bare_values():
    rego = generate_outbound_rego(_github_agent())
    assert "target_scopes := {" in rego
    # key stays the FULL service id; values de-prefix to bare tool names.
    assert (
        '"spiffe://localtest.me/ns/team1/sa/github-tool": '
        '["source-read", "source-write", "issues-read", "issues-write"]' in rego
    )


def test_outbound_no_prefixed_scope_leaks():
    rego = generate_outbound_rego(_github_agent())
    # Nothing prefixed leaks into the outbound package's scope values.
    assert "github-tool.source-read" not in rego
    assert "github-tool.issues-write" not in rego


def test_outbound_gates_use_nested_identity_and_mcp_input():
    rego = generate_outbound_rego(_github_agent())
    assert "subject_ok if {" in rego
    assert "some role in subject_roles[input.identity.subject]" in rego
    assert "input.mcp.params.name in subject_role_scopes[role]" in rego
    assert "target_ok if {" in rego
    assert "input.mcp.params.name in target_scopes[input.identity.service_id]" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; target_ok }" in rego


def test_outbound_does_not_embed_inbound_subject_gate():
    rego = generate_outbound_rego(_github_agent())
    # The inbound-flavoured subject gate must NOT appear in the outbound package.
    assert "some scope in role_scopes[role]" not in rego
    assert "scope in agent_scopes" not in rego
    # The inbound ``role_scopes`` map must not leak (line-anchored so it does not
    # match subject_role_scopes / agent_role_scopes).
    assert "\nrole_scopes :=" not in rego
    assert not rego.startswith("role_scopes :=")


def test_outbound_deprefix_fallbacks_survive_unchanged():
    """A scope with no owner (empty serviceId) and one whose name lacks the
    ``<owner>.`` prefix both survive unchanged — no crash, no partial strip."""
    orphan = _scope("orphan", "")  # no serviceId -> survives as "orphan"
    already_bare = _scope("already-bare", GH_TOOL)  # owner is github-tool, no prefix
    prefixed = _scope("github-tool.source-read", GH_TOOL)  # -> source-read
    model = _model(
        agent_id="team1/github-agent",
        target_scopes={GH_TOOL: [prefixed, already_bare, orphan]},
    )
    rego = generate_outbound_rego(model)
    assert (
        '"spiffe://localtest.me/ns/team1/sa/github-tool": '
        '["source-read", "already-bare", "orphan"]' in rego
    )


def test_outbound_empty_model_renders_valid_empty_literals():
    rego = generate_outbound_rego(_model())
    assert "agent_roles := []" in rego
    assert "subject_roles := {}" in rego
    assert "subject_role_scopes := {}" in rego
    assert "agent_role_scopes := {}" in rego
    assert "target_scopes := {}" in rego
    assert "default allow := false" in rego
    assert "allow if { subject_ok; target_ok }" in rego


# --- per-scope AND intersection semantics ---


def _outbound_and_model() -> AgentPolicyModel:
    """Pins the per-scope-AND intersection with de-prefixing in play.

    The target (keyed by its full SPIFFE id) admits bare {scope-b, scope-c}
    (capability gate); the user's role admits bare {scope-a, scope-c} (subject
    gate). Only scope-c is in both, so only scope-c is allowed — scope-a
    (user-only) and scope-b (target-only) are denied. All provisioned scope names
    are ``github-tool.*``-prefixed to also exercise de-prefixing.
    """
    user = _role("u-role")
    operator = _role("op-role")
    a = _scope("github-tool.scope-a", GH_TOOL)
    b = _scope("github-tool.scope-b", GH_TOOL)
    c = _scope("github-tool.scope-c", GH_TOOL)
    return _model(
        agent_id="team1/github-agent",
        agent_roles=[operator],
        subject_roles={"user1": [user]},
        # capability gate: the target admits bare {scope-b, scope-c}.
        target_scopes={GH_TOOL: [b, c]},
        # subject gate: the user's role admits bare {scope-a, scope-c}.
        outbound_subject_rules=[
            PolicyRule(role=user, scope=a),
            PolicyRule(role=user, scope=c),
        ],
        # informational agent_role_scopes (not referenced by allow).
        outbound_rules=[
            PolicyRule(role=operator, scope=b),
            PolicyRule(role=operator, scope=c),
        ],
    )


def test_outbound_per_scope_and_structural():
    """Structural: the two gates read the same ``input.mcp.params.name`` from
    disjoint maps — the subject gate grants {scope-a, scope-c}, the capability
    gate grants {scope-b, scope-c} — so allow is their per-scope intersection."""
    rego = generate_outbound_rego(_outbound_and_model())
    assert '"u-role": ["scope-a", "scope-c"]' in rego  # subject gate
    assert (
        '"spiffe://localtest.me/ns/team1/sa/github-tool": ["scope-b", "scope-c"]'
        in rego
    )  # capability gate
    assert "input.mcp.params.name in subject_role_scopes[role]" in rego
    assert "input.mcp.params.name in target_scopes[input.identity.service_id]" in rego
    assert "allow if { subject_ok; target_ok }" in rego


@pytest.mark.skipif(not shutil.which("opa"), reason="opa binary not on PATH")
@pytest.mark.parametrize(
    "tool_name, allowed",
    [
        ("scope-c", True),  # in BOTH gates -> allowed
        ("scope-a", False),  # user-only (not in the target's capability gate) -> denied
        ("scope-b", False),  # target-only (not in the user's subject gate) -> denied
    ],
)
def test_outbound_per_scope_and_denies_mismatch(tool_name: str, allowed: bool):
    """Generator-sanity: evaluate the generated ``allow`` with ``opa eval`` against
    the nested ``input.identity`` / ``input.mcp`` doc the live plugin sends. Only
    the scope in both gates (scope-c) is allowed; the user-only (scope-a) and
    target-only (scope-b) scopes are denied.

    This is a generator-sanity check only (valid Rego + plausible allow/deny) —
    the authoritative allow/deny lives in the e2e integration suite (handoff 08).
    """
    rego = generate_outbound_rego(_outbound_and_model())
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "outbound.rego"
        path.write_text(rego)
        cmd = [
            shutil.which("opa"),
            "eval",
            "-f",
            "json",
            "-d",
            str(path),
            "--stdin-input",
            "data.authbridge.client.outbound.request.allow",
        ]
        doc = {
            "identity": {"subject": "user1", "service_id": GH_TOOL},
            "mcp": {"params": {"name": tool_name}},
        }
        out = subprocess.run(
            cmd,
            input=json.dumps(doc),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        result = json.loads(out)["result"][0]["expressions"][0]["value"]
    assert result is allowed, f"tool_name={tool_name!r}"
