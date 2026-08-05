"""Rung 3 of the UC-1 onboarding ladder — onboard the **tool, then the agent**.

Issue ``testing/5.4.3-uc1-onboard-tool-then-agent.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``. Drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for the ``github-tool`` **first** and the
``github-agent`` **second**, then assert the **full** truth table at the end — and, crucially, that
this end state is **identical to rung 2's** (agent→tool).

This is the direct single-pass happy path: onboarding the tool first provisions the four
``github-tool.*`` scopes, and the ``(user role → tool scope)`` rules that pass produces are routed
**durably onto ``SPM(github-tool)``** (the tool gets an SPM, no APM; no agent APM is written yet —
no agent targets a tool scope at this point). When the agent is then onboarded, its Service Policy
Builder reads the universe (now including the tool scopes), the PCE routes the agent→tool rule to
``SPM(github-tool)``, marks the agent affected, and **derives** its APM from the SPMs — picking up
the durable user→tool rules already on ``SPM(github-tool)`` — so ``github_agent.outbound.rego`` is
emitted with the full user→tool gate in one pass.

This rung is the **live counterpart of the PCE's order-independence unit test (8.11)** and the exact
repro of the original order-dependence bug: under the old APM-only design, tool-then-agent **lost**
the ``user role → tool scope`` rule because no agent yet targeted the tool scope at tool onboarding.
The SPM redesign stores that rule durably on ``SPM(github-tool)`` and reconstructs it when the
agent's APM is derived. So this rung asserts, on top of the full truth table, that its final
``APM(github-agent)`` grant sets **equal rung 2's** (compared as order-independent ``(role, scope)``
sets, never a byte diff of the Rego). A divergence names the differing gate and is a **failure** — an
onboarding-order bug this rung exists to surface (spec § *Onboarding order is irrelevant*).

Reuses the shared harness (``uc1_onboard.py`` — config, Keycloak provisioning/cleanup, onboard
trigger, Rego capture, grant-set extraction, per-rung fixture flow), the shared tool-onboarded oracle
(``uc1.expected_outbound_with_tool`` / ``uc1.OUTBOUND_SUBJECT_GRANT_SET`` — the same gate rung 2
asserts), ``scenario_uc1.py`` (the truth tables — the oracle), and ``probe_uc1.rego`` (user-gate
probe). The **only** rung-3-specific content here is the onboarding order — ``[tool, agent]`` — and
the order-independence assertions against **rung 2's** published expectations.

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard tool → onboard agent → validate
end state → Keycloak cleanup**. Deployment + client registration are **preconditions**, not test
steps. *Onboard + evaluate — no A2A traffic, no live enforcement* (phase-1 out of scope).

Run (needs a live rossoctl/Kind cluster with the AIAC stack + OPA filesystem-stub writer, the demo
workloads deployed + registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``opa`` on PATH or
``$OPA_BIN``):

    .venv/bin/pytest test/integration/test_uc1_onboard_tool_then_agent.py -m integration -v

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
from test.integration import test_uc1_onboard_agent_then_tool as rung2  # noqa: E402
from test.integration import uc1_onboard as uc1  # noqa: E402
from test.integration.launcher import opa_eval  # noqa: E402

TEST_REALM = uc1.TEST_REALM
INBOUND_REGO = uc1.INBOUND_REGO
OUTBOUND_REGO = uc1.OUTBOUND_REGO
AGENT_SLUG = uc1.AGENT_SLUG


# ======================================================================================
# Expected-verdict oracle (the shared tool-onboarded oracle — identical to rung 2's)
# ======================================================================================
#
# Rung 3 asserts the **full** truth table, and it must be the **same** truth table as rung 2 (spec:
# *Onboarding order is irrelevant*). So the oracle is the shared tool-onboarded oracle in
# ``uc1_onboard`` — the same constants/helper rung 2 uses — and the order-independence check below
# compares this rung's live grant sets against **rung 2's published expectations** (``rung2.RUNG2_*``,
# which alias the same shared oracle). Verdicts are computed here, never read from the Rego under test.

RUNG3_INBOUND = uc1.INBOUND_GRANT_SET
RUNG3_OUTBOUND_SUBJECT = uc1.OUTBOUND_SUBJECT_GRANT_SET
expected_outbound = uc1.expected_outbound_with_tool

# Rung 2's published expectations — what this rung's end state must equal (order-independence).
RUNG2_INBOUND = rung2.RUNG2_INBOUND
RUNG2_OUTBOUND_SUBJECT = rung2.RUNG2_OUTBOUND_SUBJECT


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 3's defining decisions
# ======================================================================================
#
# These need neither the cluster nor ``opa``: they assert the rung-3 oracle itself (the intended
# policy the live decisions below are checked against), including that it is byte-for-byte the same
# oracle as rung 2's. If these are wrong, every live assertion is meaningless — so they are the
# tracer bullet.


@pytest.mark.parametrize(
    "subject, allowed",
    [("dev-user", True), ("test-user", True), ("devops-user", False)],
)
def test_inbound_oracle(subject: str, allowed: bool) -> None:
    """Inbound: dev-user ✅, test-user ✅, devops-user ❌ (devops sources no agent scope) — inbound is
    unaffected by onboarding order; identical to rungs 1 and 2."""
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
    """Rung 3's outbound gate is the full user→tool gate (developer: source rw + issues read; tester:
    issues rw; devops: nothing) — the same gate rung 2 produces, reached here in a single pass."""
    assert expected_outbound(subject, function_name) is allowed


def test_rung3_grant_set_oracle() -> None:
    """Rung 3 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the
    outbound-subject grant set == the (non-empty) ``OUTBOUND_SUBJECT_PAIRS`` truth table."""
    assert RUNG3_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG3_OUTBOUND_SUBJECT == set(scn.OUTBOUND_SUBJECT_PAIRS)
    assert RUNG3_OUTBOUND_SUBJECT, "rung 3's outbound gate must be non-empty (the tool was onboarded)"


def test_order_independence_oracle() -> None:
    """The order-independence property at the oracle level: rung 3's intended end state is **identical
    to rung 2's** (both the inbound and the outbound-subject grant sets). The live check below then
    proves the *generated* policy matches this — that onboarding order did not change the final policy."""
    assert RUNG3_INBOUND == RUNG2_INBOUND
    assert RUNG3_OUTBOUND_SUBJECT == RUNG2_OUTBOUND_SUBJECT


# ======================================================================================
# Session fixture — cleanup → onboard tool → onboard agent → capture rego → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Onboard the tool **then** the agent via the shared harness (order is this rung's identity —
    the tool's scopes already exist when the agent's Service Policy Builder reads the universe, so the
    agent's APM is derived with the full user→tool gate in one pass), and yield the live ``admin``
    handle + captured ``rego_dir`` (+ writer ``pod``). Keycloak cleanup runs before and after; the
    clients are left registered as before (spec § Per-rung flow)."""
    with uc1.onboarded_stack(
        [scn.TOOL_WORKLOAD, scn.AGENT_WORKLOAD], rego_subdir="rung3"
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
    """Inbound gate allows a user iff their role may reach some discovered agent scope
    (dev-user ✅, test-user ✅, devops-user ❌) — unaffected by onboarding order."""
    rego = onboarded["rego_dir"] / INBOUND_REGO
    allowed = opa_eval([rego], f"data.authz.{AGENT_SLUG}.inbound.allow", {"subject": subject})
    assert allowed == uc1.expected_inbound(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound(onboarded: dict, subject: str, function_name: str) -> None:
    """Outbound user gate (via ``probe_uc1.rego``) decides each ``(subject, function)`` per the full
    ``OUTBOUND_SUBJECT_PAIRS`` table — reconstructed from the durable ``SPM(github-tool)`` rules when
    the agent's APM was derived (exact-name match on full discovered scope names)."""
    rego = onboarded["rego_dir"] / OUTBOUND_REGO
    allowed = opa_eval(
        [rego, uc1.PROBE_UC1],
        "data.probe.outbound.allow",
        {"subject": subject, "function_name": function_name},
    )
    assert allowed == expected_outbound(subject, function_name), f"{subject} / {function_name}"


def test_only_agent_rego_present(onboarded: dict) -> None:
    """Exactly the two agent files on disk; explicitly **no** ``github_tool.*.rego`` even though the
    tool was onboarded first (the tool is a pure target — its rules live durably on ``SPM(github-tool)``
    and are reconstructed onto the agent's model; no rules are written for the tool alone). Checks the
    writer's own ``/rego``, not just the host copy."""
    written = uc1.writer_rego_files(onboarded["writer_pod"])
    assert not [f for f in written if "github_tool" in f], (
        f"unexpected tool Rego emitted: {written}"
    )
    for filename in (INBOUND_REGO, OUTBOUND_REGO):
        assert filename in written, f"missing {filename} in writer /rego: {written}"


def test_inbound_grant_set_matches_truth_table(onboarded: dict) -> None:
    """The inbound grant set re-derived from the Rego equals ``scenario_uc1``'s inbound truth table —
    catching verdict-neutral over/under-grants the coarse allow/deny oracle cannot see."""
    got = uc1.inbound_grants(onboarded["rego_dir"])
    assert got == RUNG3_INBOUND, f"inbound: missing={RUNG3_INBOUND - got} extra={got - RUNG3_INBOUND}"


def test_outbound_subject_grant_set_matches_truth_table(onboarded: dict) -> None:
    """The single-pass property, as a grant set: the outbound-subject grant set re-derived from the
    Rego equals the **non-empty** ``OUTBOUND_SUBJECT_PAIRS`` — the agent's APM was derived with the
    full user→tool gate because the tool's scopes (and their durable ``SPM(github-tool)`` rules)
    already existed."""
    got = uc1.outbound_subject_grants(onboarded["rego_dir"])
    assert got == RUNG3_OUTBOUND_SUBJECT, (
        f"outbound: missing={RUNG3_OUTBOUND_SUBJECT - got} extra={got - RUNG3_OUTBOUND_SUBJECT}"
    )
    assert got, "outbound gate must be non-empty after onboarding the tool (single-pass derivation)"


# ======================================================================================
# Order-independence — this rung's headline: tool→agent == agent→tool (vs rung 2)
# ======================================================================================
#
# The live counterpart of the PCE's order-independence unit test (8.11) and the exact repro of the
# original order-dependence bug. Compared at the **grant-set** level (semantic ``(role, scope)`` sets),
# never a byte diff of the Rego. A divergence names the differing gate and is a **failure** — an
# onboarding-order bug, not an accepted difference (spec § *Onboarding order is irrelevant*).


def test_inbound_grant_set_equals_rung2(onboarded: dict) -> None:
    """Onboarding-order-independence, inbound gate: rung 3's (tool→agent) inbound grant set equals
    rung 2's (agent→tool). Inbound never depended on order, so this is the easy half — but asserting
    it keeps the equivalence check total across both gates."""
    got = uc1.inbound_grants(onboarded["rego_dir"])
    assert got == RUNG2_INBOUND, (
        "onboarding order changed the INBOUND gate (bug): "
        f"missing={RUNG2_INBOUND - got} extra={got - RUNG2_INBOUND}"
    )


def test_outbound_subject_grant_set_equals_rung2(onboarded: dict) -> None:
    """Onboarding-order-independence, outbound user gate — this rung's raison d'être: rung 3's
    (tool→agent) outbound-subject grant set equals rung 2's (agent→tool). This is the exact cell the
    original order-dependence bug corrupted (tool-then-agent lost the user→tool rule); the SPM
    redesign makes both orders converge. A divergence here is an onboarding-order **bug**, not an
    accepted difference."""
    got = uc1.outbound_subject_grants(onboarded["rego_dir"])
    assert got == RUNG2_OUTBOUND_SUBJECT, (
        "onboarding order changed the OUTBOUND user gate (bug): "
        f"missing={RUNG2_OUTBOUND_SUBJECT - got} extra={got - RUNG2_OUTBOUND_SUBJECT}"
    )
    assert got, "outbound gate must be non-empty (the tool was onboarded)"
