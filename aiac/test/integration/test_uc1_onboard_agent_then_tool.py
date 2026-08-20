"""Rung 2 of the UC-1 onboarding ladder — onboard the **agent, then the tool**.

Issue ``testing/5.4.2-uc1-onboard-agent-then-tool.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``. Drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for the ``github-agent`` **first** and the
``github-tool`` **second**, then assert the **full** truth table at the end by driving **real HTTP
requests through AuthBridge** and reading the **real OPA plugin's** allow/deny (handoff 08; live loop
shape in ``k8s/opa-kind-runbook.md``).

This rung proves the key reconciliation property: onboarding the tool **after** the agent
retroactively completes the agent's outbound policy. When the agent is onboarded alone its outbound
gate is empty (rung 1); when the tool is then onboarded, its Service Policy Builder pairs the tool's
scopes against the existing role universe (agent role + user roles) and the PCE **routes** those
``(role, tool-scope)`` rules onto the **tool's** persistent ``ServicePolicyModel`` via
``compute_and_apply(override=False)``. Because the agent's role targets a tool scope, the agent is in
the affected set, so its ``AgentPolicyModel`` is **re-derived from the SPMs** and its outbound
``AuthorizationPolicy`` rule is (re)written with the full user→tool gate — which the live probes below
observe as real allow/deny decisions once ``bundle-service`` recomposes the bundle.

There is **no agent re-onboard** and **no intermediate validation** — only the end state is checked.
This is order 1 of the order-independence pair; rung 3 (tool then agent) asserts its final live
outbound matrix is **identical** to this rung's.

Reuses the shared harness (``uc1_onboard.py`` — config, Keycloak provisioning/cleanup, onboard
trigger, Part-B outbound-leg prep, bundle convergence poll, per-rung fixture flow) and
``scenario_uc1.py`` (the truth tables — the oracle). The deployed OPA plugin is the evaluator (no
``.rego`` dump, no ``opa`` binary). The **only** rung-2-specific content here is the oracle (the full
outbound gate, keyed on the **bare** runtime tool names) and the live assertions; the onboarding order
— ``[agent, tool]`` — is passed to the shared fixture flow.

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard agent → onboard tool → Part B →
poll bundle → drive real requests + assert → Keycloak cleanup**. Deployment + client registration are
**preconditions**, not test steps.

Run (needs a live rossoctl/Kind cluster with the AIAC stack + AuthBridge OPA pipeline wired in — see
``k8s/opa-kind-runbook.md`` / ``k8s/opa-kind-enable.sh`` — the demo workloads deployed +
registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``test/integration/.env`` sourced):

    .venv/bin/pytest test/integration/test_uc1_onboard_agent_then_tool.py -m integration -v

Without ``-m integration`` the suite is not collected; without a wired cluster / env it skips cleanly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

HERE = Path(__file__).resolve().parent  # test/integration/
REPO_ROOT = HERE.parents[1]  # -> aiac/
sys.path.insert(0, str(REPO_ROOT))  # so ``import test.integration.*`` resolves

from test.integration import scenario_uc1 as scn  # noqa: E402
from test.integration import uc1_onboard as uc1  # noqa: E402

TEST_REALM = uc1.TEST_REALM


# ======================================================================================
# Expected-verdict oracle (pure functions over the scenario_uc1 truth table)
# ======================================================================================
#
# Rung 2 asserts the **full** truth table. Inbound is the shared oracle (``uc1.expected_inbound`` —
# unaffected by tool onboarding). Outbound is now **non-empty**: onboarding the tool completed the
# agent's outbound user gate, so the expected user→tool grants are exactly
# ``scenario_uc1.OUTBOUND_SUBJECT_BARE`` (keyed on the **bare** runtime tool names AuthBridge sends).
# Because that gate is order-independent, ``uc1.expected_outbound_bare`` is the **shared** tool-onboarded
# oracle rung 3 must reproduce. Verdicts are computed here, never read from the plugin under test.

RUNG2_INBOUND = uc1.INBOUND_GRANT_SET
RUNG2_OUTBOUND_SUBJECT_BARE = scn.OUTBOUND_SUBJECT_BARE
expected_outbound = uc1.expected_outbound_bare


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 2's defining decisions
# ======================================================================================
#
# These need neither the cluster nor the env: they assert the rung-2 oracle itself (the intended
# policy the live decisions below are checked against). If these are wrong, every live assertion is
# meaningless — so they are the tracer bullet.


@pytest.mark.parametrize(
    "subject, allowed",
    [("dev-user", True), ("test-user", True), ("devops-user", False)],
)
def test_inbound_oracle(subject: str, allowed: bool) -> None:
    """Inbound: dev-user ✅, test-user ✅, devops-user ❌ (devops sources no agent scope) — unchanged
    from rung 1; tool onboarding does not touch the inbound gate."""
    assert uc1.expected_inbound(subject) is allowed


@pytest.mark.parametrize(
    "subject, tool_bare, allowed",
    [
        # dev-user (developer): source read/write + issues read; NOT issues write.
        ("dev-user", "source-read", True),
        ("dev-user", "source-write", True),
        ("dev-user", "issues-read", True),
        ("dev-user", "issues-write", False),
        # test-user (tester): issues read/write only.
        ("test-user", "source-read", False),
        ("test-user", "source-write", False),
        ("test-user", "issues-read", True),
        ("test-user", "issues-write", True),
        # devops-user (devops): no access to anything.
        ("devops-user", "source-read", False),
        ("devops-user", "source-write", False),
        ("devops-user", "issues-read", False),
        ("devops-user", "issues-write", False),
    ],
)
def test_outbound_oracle(subject: str, tool_bare: str, allowed: bool) -> None:
    """Rung 2's defining property: the full user→tool outbound gate over the **bare** tool names
    (developer: source rw + issues read; tester: issues rw; devops: nothing) — the gate tool
    onboarding completed on the agent."""
    assert expected_outbound(subject, tool_bare) is allowed


def test_rung2_grant_set_oracle() -> None:
    """Rung 2 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the outbound-subject
    (bare) grant set == the (non-empty) ``OUTBOUND_SUBJECT_BARE`` truth table."""
    assert RUNG2_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG2_OUTBOUND_SUBJECT_BARE == scn.OUTBOUND_SUBJECT_BARE
    assert RUNG2_OUTBOUND_SUBJECT_BARE, "rung 2's outbound gate must be non-empty (the tool was onboarded)"


# ======================================================================================
# Session fixture — cleanup → onboard agent → onboard tool → Part B → poll bundle → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Onboard the agent **then** the tool via the shared harness (order is this rung's identity —
    tool onboarding retroactively completes the agent's outbound gate), and yield the live probe
    context (``admin`` handle, ``agent_pod``, Keycloak URL/realm, ``tool_onboarded=True``). Keycloak
    cleanup + CR delete run before and after; the clients are left registered as before (spec § Per-rung
    flow). No agent re-onboard, no intermediate validation — only the end state is asserted below."""
    with uc1.onboarded_stack([scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD]) as ctx:
        yield ctx


# ======================================================================================
# Live tests — Keycloak entities + real-plugin decisions (verdicts computed from scenario_uc1)
# ======================================================================================


def test_agent_role_and_scopes_provisioned(onboarded: dict) -> None:
    """Keycloak holds the agent's per-skill operator roles + the two AgentCard scopes, all with
    their descriptions."""
    admin = onboarded["admin"]
    admin.change_current_realm(TEST_REALM)

    for name, description in scn.AGENT_ROLES.items():
        role = admin.get_realm_role(name)
        assert role and role.get("name") == name, f"missing realm role {name!r}"
        assert (role.get("description") or "") == description, (
            f"agent role {name!r} description mismatch: {role.get('description')!r} != {description!r}"
        )

    scopes = {s["name"]: (s.get("description") or "") for s in admin.get_client_scopes()}
    for name, description in scn.AGENT_SCOPES.items():
        assert name in scopes, f"missing agent scope {name!r}"
        assert scopes[name] == description, (
            f"agent scope {name!r} description mismatch: {scopes[name]!r} != {description!r}"
        )


def test_tool_scopes_provisioned(onboarded: dict) -> None:
    """The tool was onboarded, so all four ``github-tool.*`` scopes exist with their MCP
    ``tools/list`` descriptions (the discovered tool boundary)."""
    admin = onboarded["admin"]
    admin.change_current_realm(TEST_REALM)

    scopes = {s["name"]: (s.get("description") or "") for s in admin.get_client_scopes()}
    for name, description in scn.TOOL_SCOPES.items():
        assert name in scopes, f"missing tool scope {name!r}"
        assert scopes[name] == description, (
            f"tool scope {name!r} description mismatch: {scopes[name]!r} != {description!r}"
        )


@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(onboarded: dict, subject: str) -> None:
    """Inbound gate — a real request through AuthBridge as ``subject`` is allowed iff their role may
    reach some discovered agent scope (dev-user ✅, test-user ✅, devops-user ❌) — unchanged by the
    tool onboarding. The real OPA plugin decides."""
    assert uc1.inbound_decision(onboarded, subject) == uc1.expected_inbound_decision(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("tool_bare", scn.TOOL_REQUEST_NAMES)
def test_outbound(onboarded: dict, subject: str, tool_bare: str) -> None:
    """Outbound user gate — a real MCP ``tools/call`` for the **bare** tool through AuthBridge's
    forward proxy (token-exchange → OPA) decides each ``(subject, tool)`` per the full
    ``OUTBOUND_SUBJECT_BARE`` table — the gate tool onboarding completed on the agent. AuthBridge's
    ``mcp-parser`` surfaces ``input.mcp.params.name`` (no hand-built input); the real OPA plugin
    renders a denial as a JSON-RPC error frame the harness classifies."""
    assert uc1.outbound_decision(onboarded, subject, tool_bare) == uc1.expected_outbound_decision(
        subject, tool_bare
    ), f"{subject} / {tool_bare}"
