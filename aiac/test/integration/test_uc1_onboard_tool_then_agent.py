"""Rung 3 of the UC-1 onboarding ladder — onboard the **tool, then the agent**.

Issue ``testing/5.4.3-uc1-onboard-tool-then-agent.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``. Drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for the ``github-tool`` **first** and the
``github-agent`` **second**, then assert the **full** truth table at the end by driving **real HTTP
requests through AuthBridge** and reading the **real OPA plugin's** allow/deny (handoff 08) — and,
crucially, that this live end state is **identical to rung 2's** (agent→tool).

This is the direct single-pass happy path: onboarding the tool first provisions the four
``github-tool.*`` scopes, and the ``(user role → tool scope)`` rules that pass produces are routed
**durably onto ``SPM(github-tool)``** (the tool gets an SPM, no APM; no agent APM is written yet — no
agent targets a tool scope at this point). When the agent is then onboarded, its Service Policy Builder
reads the universe (now including the tool scopes), the PCE routes the agent→tool rule to
``SPM(github-tool)``, marks the agent affected, and **derives** its APM from the SPMs — picking up the
durable user→tool rules already on ``SPM(github-tool)`` — so the agent's outbound
``AuthorizationPolicy`` rule is emitted with the full user→tool gate in one pass.

This rung is the **live counterpart of the PCE's order-independence unit test (8.11)** and the exact
repro of the original order-dependence bug: under the old APM-only design, tool-then-agent **lost** the
``user role → tool scope`` rule because no agent yet targeted the tool scope at tool onboarding. The
SPM redesign stores that rule durably on ``SPM(github-tool)`` and reconstructs it when the agent's APM
is derived. So this rung's live outbound matrix must **equal rung 2's** — the live proof is both rungs
driving the *same* bare user→tool matrix through the real plugin and getting the same decisions. A
divergence is an onboarding-order **bug** this rung exists to surface (spec § *Onboarding order is
irrelevant*).

Reuses the shared harness (``uc1_onboard.py`` — config, Keycloak provisioning/cleanup, onboard
trigger, Part-B outbound-leg prep, bundle convergence poll, per-rung fixture flow), the shared
tool-onboarded oracle (``uc1.expected_outbound_bare`` — the same gate rung 2 asserts), and
``scenario_uc1.py`` (the truth tables — the oracle). The deployed OPA plugin is the evaluator (no
``.rego`` dump, no ``opa`` binary). The **only** rung-3-specific content here is the onboarding order —
``[tool, agent]`` — and the order-independence check against **rung 2's** published expectations.

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard tool → onboard agent → Part B →
poll bundle → drive real requests + assert → Keycloak cleanup**. Deployment + client registration are
**preconditions**, not test steps.

Run (needs a live rossoctl/Kind cluster with the AuthBridge OPA pipeline wired in — see
``docs/opa-kind-runbook.md`` / ``../scripts/opa-kind-enable.sh`` — the demo workloads deployed +
registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``test/integration/.env`` sourced):

    .venv/bin/pytest test/integration/test_uc1_onboard_tool_then_agent.py -m integration -v

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
from test.integration import test_uc1_onboard_agent_then_tool as rung2  # noqa: E402
from test.integration import uc1_onboard as uc1  # noqa: E402

TEST_REALM = uc1.TEST_REALM


# ======================================================================================
# Expected-verdict oracle (the shared tool-onboarded oracle — identical to rung 2's)
# ======================================================================================
#
# Rung 3 asserts the **full** truth table, and it must be the **same** truth table as rung 2 (spec:
# *Onboarding order is irrelevant*). So the oracle is the shared tool-onboarded oracle in
# ``uc1_onboard`` — the same helper/sets rung 2 uses, keyed on the **bare** runtime tool names — and
# the order-independence check below compares this rung's oracle against **rung 2's published
# expectations** (``rung2.RUNG2_*``). Verdicts are computed here, never read from the plugin under test.

RUNG3_INBOUND = uc1.INBOUND_GRANT_SET
RUNG3_OUTBOUND_SUBJECT_BARE = scn.OUTBOUND_SUBJECT_BARE
expected_outbound = uc1.expected_outbound_bare

# Rung 2's published expectations — what this rung's end state must equal (order-independence).
RUNG2_INBOUND = rung2.RUNG2_INBOUND
RUNG2_OUTBOUND_SUBJECT_BARE = rung2.RUNG2_OUTBOUND_SUBJECT_BARE


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 3's defining decisions
# ======================================================================================
#
# These need neither the cluster nor the env: they assert the rung-3 oracle itself (the intended
# policy the live decisions below are checked against), including that it is the same oracle as
# rung 2's. If these are wrong, every live assertion is meaningless — so they are the tracer bullet.


@pytest.mark.parametrize(
    "subject, allowed",
    [("dev-user", True), ("test-user", True), ("devops-user", False)],
)
def test_inbound_oracle(subject: str, allowed: bool) -> None:
    """Inbound: dev-user ✅, test-user ✅, devops-user ❌ (devops sources no agent scope) — inbound is
    unaffected by onboarding order; identical to rungs 1 and 2."""
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
    """Rung 3's outbound gate is the full user→tool gate over the **bare** tool names (developer:
    source rw + issues read; tester: issues rw; devops: nothing) — the same gate rung 2 produces,
    reached here in a single pass."""
    assert expected_outbound(subject, tool_bare) is allowed


def test_rung3_grant_set_oracle() -> None:
    """Rung 3 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the outbound-subject
    (bare) grant set == the (non-empty) ``OUTBOUND_SUBJECT_BARE`` truth table."""
    assert RUNG3_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG3_OUTBOUND_SUBJECT_BARE == scn.OUTBOUND_SUBJECT_BARE
    assert RUNG3_OUTBOUND_SUBJECT_BARE, "rung 3's outbound gate must be non-empty (the tool was onboarded)"


def test_order_independence_oracle() -> None:
    """The order-independence property at the oracle level: rung 3's intended end state is **identical
    to rung 2's** (both the inbound and the outbound-subject bare grant sets). The live matrix below
    then proves the *real plugin's decisions* match this in both orders — that onboarding order did not
    change the enforced policy."""
    assert RUNG3_INBOUND == RUNG2_INBOUND
    assert RUNG3_OUTBOUND_SUBJECT_BARE == RUNG2_OUTBOUND_SUBJECT_BARE


# ======================================================================================
# Session fixture — cleanup → onboard tool → onboard agent → Part B → poll bundle → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Onboard the tool **then** the agent via the shared harness (order is this rung's identity —
    the tool's scopes already exist when the agent's Service Policy Builder reads the universe, so the
    agent's APM is derived with the full user→tool gate in one pass), and yield the live probe context
    (``admin`` handle, ``agent_pod``, Keycloak URL/realm, ``tool_onboarded=True``). Keycloak cleanup +
    CR delete run before and after; the clients are left registered as before (spec § Per-rung flow)."""
    with uc1.onboarded_stack([scn.TOOL_WORKLOAD, scn.AGENT_WORKLOAD]) as ctx:
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
    """The tool was onboarded (first), so all four ``github-tool.*`` scopes exist with their MCP
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
    reach some discovered agent scope (dev-user ✅, test-user ✅, devops-user ❌) — unaffected by
    onboarding order. The real OPA plugin decides."""
    assert uc1.inbound_decision(onboarded, subject) == uc1.expected_inbound_decision(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("tool_bare", scn.TOOL_REQUEST_NAMES)
def test_outbound(onboarded: dict, subject: str, tool_bare: str) -> None:
    """Outbound user gate — a real MCP ``tools/call`` for the **bare** tool through AuthBridge's
    forward proxy (token-exchange → OPA) decides each ``(subject, tool)`` per the full
    ``OUTBOUND_SUBJECT_BARE`` table — reconstructed from the durable ``SPM(github-tool)`` rules when the
    agent's APM was derived. This is the same bare matrix rung 2 drives; **both rungs passing it is the
    live order-independence proof** (the exact cell the original order-dependence bug corrupted)."""
    assert uc1.outbound_decision(onboarded, subject, tool_bare) == uc1.expected_outbound_decision(
        subject, tool_bare
    ), f"{subject} / {tool_bare}"
