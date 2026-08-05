"""Rung 2 of the UC-1 onboarding ladder — onboard the **agent, then the tool**.

Issue ``testing/5.4.2-uc1-onboard-agent-then-tool.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``. Drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for the ``github-agent`` **first** and the
``github-tool`` **second**, then assert the **full** truth table at the end.

This rung proves the key reconciliation property: onboarding the tool **after** the agent
retroactively completes the agent's outbound policy. When the agent is onboarded alone its outbound
gate is empty (rung 1); when the tool is then onboarded, its Service Policy Builder pairs the tool's
scopes against the existing role universe (agent role + user roles) and the PCE **routes** those
``(role, tool-scope)`` rules onto the **tool's** persistent ``ServicePolicyModel`` via
``compute_and_apply(override=False)``. Because the agent's role targets a tool scope, the agent is in
the affected set, so its ``AgentPolicyModel`` is **re-derived from the SPMs** and
``github_agent.outbound.rego`` is (re)written with the full user→tool gate.

There is **no agent re-onboard** and **no intermediate validation** — only the end state is checked.
This is order 1 of the order-independence pair; rung 3 (tool then agent) asserts its final
``APM(github-agent)`` grant sets are **identical** to this rung's.

Reuses the shared harness (``uc1_onboard.py`` — config, Keycloak provisioning/cleanup, onboard
trigger, Rego capture, grant-set extraction, per-rung fixture flow), ``scenario_uc1.py`` (the truth
tables — the oracle), and ``probe_uc1.rego`` (user-gate probe). The **only** rung-2-specific content
here is the oracle (the full outbound gate, from ``scenario_uc1.OUTBOUND_SUBJECT_PAIRS``) and the
live assertions; the onboarding order — ``[agent, tool]`` — is passed to the shared fixture flow.

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard agent → onboard tool → validate
end state → Keycloak cleanup**. Deployment + client registration are **preconditions**, not test
steps. *Onboard + evaluate — no A2A traffic, no live enforcement* (phase-1 out of scope).

Run (needs a live rossoctl/Kind cluster with the AIAC stack + OPA filesystem-stub writer, the demo
workloads deployed + registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``opa`` on PATH or
``$OPA_BIN``):

    .venv/bin/pytest test/integration/test_uc1_onboard_agent_then_tool.py -m integration -v

Without ``-m integration`` the suite is not collected; without ``opa`` it skips at runtime.
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
from test.integration.launcher import opa_eval  # noqa: E402

TEST_REALM = uc1.TEST_REALM
INBOUND_REGO = uc1.INBOUND_REGO
OUTBOUND_REGO = uc1.OUTBOUND_REGO
AGENT_SLUG = uc1.AGENT_SLUG


# ======================================================================================
# Expected-verdict oracle (pure functions over the scenario_uc1 truth table)
# ======================================================================================
#
# Rung 2 asserts the **full** truth table. Inbound is the shared oracle (``uc1.expected_inbound`` —
# unaffected by tool onboarding). Outbound is now **non-empty**: onboarding the tool completed the
# agent's outbound user gate, so the expected user→tool grant set is exactly
# ``scenario_uc1.OUTBOUND_SUBJECT_PAIRS``. Because that gate is order-independent, it is the **shared**
# tool-onboarded oracle in ``uc1_onboard`` (``uc1.expected_outbound_with_tool`` /
# ``uc1.OUTBOUND_SUBJECT_GRANT_SET``) — the exact set rung 3 must reproduce. Verdicts are computed
# here, never read from the Rego under test.

RUNG2_INBOUND = uc1.INBOUND_GRANT_SET
RUNG2_OUTBOUND_SUBJECT = uc1.OUTBOUND_SUBJECT_GRANT_SET
expected_outbound = uc1.expected_outbound_with_tool


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 2's defining decisions
# ======================================================================================
#
# These need neither the cluster nor ``opa``: they assert the rung-2 oracle itself (the intended
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
    "subject, function_name, allowed",
    [
        # dev-user (developer): source read/write + issues read; NOT issues write.
        ("dev-user", "github-tool.source-read", True),
        ("dev-user", "github-tool.source-write", True),
        ("dev-user", "github-tool.issues-read", True),
        ("dev-user", "github-tool.issues-write", False),
        # test-user (tester): issues read/write only.
        ("test-user", "github-tool.source-read", False),
        ("test-user", "github-tool.source-write", False),
        ("test-user", "github-tool.issues-read", True),
        ("test-user", "github-tool.issues-write", True),
        # devops-user (devops): no access to anything.
        ("devops-user", "github-tool.source-read", False),
        ("devops-user", "github-tool.source-write", False),
        ("devops-user", "github-tool.issues-read", False),
        ("devops-user", "github-tool.issues-write", False),
    ],
)
def test_outbound_oracle(subject: str, function_name: str, allowed: bool) -> None:
    """Rung 2's defining property: the full user→tool outbound gate (developer: source rw + issues
    read; tester: issues rw; devops: nothing) — the gate tool onboarding completed on the agent."""
    assert expected_outbound(subject, function_name) is allowed


def test_rung2_grant_set_oracle() -> None:
    """Rung 2 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the
    outbound-subject grant set == the (non-empty) ``OUTBOUND_SUBJECT_PAIRS`` truth table."""
    assert RUNG2_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG2_OUTBOUND_SUBJECT == set(scn.OUTBOUND_SUBJECT_PAIRS)
    assert RUNG2_OUTBOUND_SUBJECT, "rung 2's outbound gate must be non-empty (the tool was onboarded)"


# ======================================================================================
# Session fixture — cleanup → onboard agent → onboard tool → capture rego → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Onboard the agent **then** the tool via the shared harness (order is this rung's identity —
    tool onboarding retroactively completes the agent's outbound gate), and yield the live ``admin``
    handle + captured ``rego_dir`` (+ writer ``pod``). Keycloak cleanup runs before and after; the
    clients are left registered as before (spec § Per-rung flow). No agent re-onboard, no
    intermediate validation — only the end state is asserted below."""
    with uc1.onboarded_stack(
        [scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD], rego_subdir="rung2"
    ) as ctx:
        yield ctx


# ======================================================================================
# Live tests — Keycloak entities + opa-eval decisions (verdicts computed from scenario_uc1)
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
    """Inbound gate allows a user iff their role may reach some discovered agent scope
    (dev-user ✅, test-user ✅, devops-user ❌) — unchanged by the tool onboarding."""
    rego = onboarded["rego_dir"] / INBOUND_REGO
    allowed = opa_eval([rego], f"data.authz.{AGENT_SLUG}.inbound.allow", {"subject": subject})
    assert allowed == uc1.expected_inbound(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound(onboarded: dict, subject: str, function_name: str) -> None:
    """Outbound user gate (via ``probe_uc1.rego``) decides each ``(subject, function)`` per the full
    ``OUTBOUND_SUBJECT_PAIRS`` table — the gate tool onboarding completed on the agent (exact-name
    match on full discovered scope names)."""
    rego = onboarded["rego_dir"] / OUTBOUND_REGO
    allowed = opa_eval(
        [rego, uc1.PROBE_UC1],
        "data.probe.outbound.allow",
        {"subject": subject, "function_name": function_name},
    )
    assert allowed == expected_outbound(subject, function_name), f"{subject} / {function_name}"


def test_only_agent_rego_present(onboarded: dict) -> None:
    """Exactly the two agent files on disk; explicitly **no** ``github_tool.*.rego`` even though the
    tool was onboarded (the tool is a pure target — its rules route onto the agent's model, and no
    rules are written for the tool alone). Checks the writer's own ``/rego``, not just the host copy."""
    written = uc1.writer_rego_files(onboarded["writer_pod"])
    assert not [f for f in written if "github_tool" in f], (
        f"unexpected tool Rego emitted: {written}"
    )  # substring, not startswith: catches the namespace-prefixed ``team1_github_tool.*.rego`` too
    for filename in (INBOUND_REGO, OUTBOUND_REGO):
        assert filename in written, f"missing {filename} in writer /rego: {written}"


def test_inbound_grant_set_matches_truth_table(onboarded: dict) -> None:
    """The inbound grant set re-derived from the Rego equals ``scenario_uc1``'s inbound truth table —
    catching verdict-neutral over/under-grants the coarse allow/deny oracle cannot see."""
    got = uc1.inbound_grants(onboarded["rego_dir"])
    assert got == RUNG2_INBOUND, f"inbound: missing={RUNG2_INBOUND - got} extra={got - RUNG2_INBOUND}"


def test_outbound_subject_grant_set_matches_truth_table(onboarded: dict) -> None:
    """The reconciliation property, as a grant set: the outbound-subject grant set re-derived from
    the Rego equals the **non-empty** ``OUTBOUND_SUBJECT_PAIRS`` — tool onboarding completed the
    agent's outbound gate (empty in rung 1, full here). This is what rung 3 asserts identical."""
    got = uc1.outbound_subject_grants(onboarded["rego_dir"])
    assert got == RUNG2_OUTBOUND_SUBJECT, (
        f"outbound: missing={RUNG2_OUTBOUND_SUBJECT - got} extra={got - RUNG2_OUTBOUND_SUBJECT}"
    )
    assert got, "outbound gate must be non-empty after onboarding the tool (the reconciliation property)"
