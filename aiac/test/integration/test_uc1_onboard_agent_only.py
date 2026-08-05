"""Rung 1 of the UC-1 onboarding ladder — onboard the **agent only**.

The simplest rung (issue ``testing/5.4.1-uc1-onboard-agent-only.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``): drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for **only** the ``github-agent`` — the
``github-tool`` is deployed + registered but **not** onboarded — then assert the agent-side outcome.
Proves agent discovery + inbound policy generation stand alone, and that the outbound user gate is
correctly **empty** when no tool has been onboarded.

Single AIAC stack, OPA filesystem-stub writer, single abstract ``policy.md``. The shared harness
(config, Keycloak provisioning/cleanup, onboard trigger, Rego capture, grant-set extraction, and the
per-rung fixture flow) lives in ``uc1_onboard.py`` and is reused by every rung; this module supplies
only rung 1's oracle (the expected verdicts, computed from ``scenario_uc1.py``) and its live
assertions. It also reuses ``scenario_uc1.py`` (truth tables — the oracle) and ``probe_uc1.rego``
(user-gate probe).

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard agent → validate end state →
Keycloak cleanup**. Deployment + client registration are **preconditions**, not test steps.

*Onboard + evaluate — no A2A traffic, no live enforcement* (phase-1 out of scope).

Run (needs a live rossoctl/Kind cluster with the AIAC stack + OPA filesystem-stub writer, the demo
workloads deployed + registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``opa`` on PATH or
``$OPA_BIN``):

    .venv/bin/pytest test/integration/test_uc1_onboard_agent_only.py -m integration -v

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
# Rung 1 is the exception in the ladder: with no tool onboarded there are no tool scopes in the
# universe, so the outbound **user gate is empty** (all deny) and the outbound-subject grant set is
# ``∅`` — regardless of ``scenario_uc1.OUTBOUND_SUBJECT_PAIRS`` (which is the rung-2/3 table). Inbound
# is unaffected (``uc1.expected_inbound`` — the shared inbound oracle). These encode rung 1's
# contract; verdicts are computed here, never read from the Rego under test.

# Rung 1's expected grant sets (the oracle for the semantic-equivalence check).
RUNG1_INBOUND = uc1.INBOUND_GRANT_SET
RUNG1_OUTBOUND_SUBJECT: set[tuple[str, str]] = set()  # ∅ — no tool onboarded


def expected_outbound(subject: str, function_name: str) -> bool:
    """Rung 1: the outbound user gate is entirely empty (no tool onboarded), so every
    ``(subject, function_name)`` is denied."""
    return (scn.USERS[subject], function_name) in RUNG1_OUTBOUND_SUBJECT


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 1's defining decisions
# ======================================================================================
#
# These need neither the cluster nor ``opa``: they assert the rung-1 oracle itself (the intended
# policy the live decisions below are checked against). If these are wrong, every live assertion is
# meaningless — so they are the tracer bullet.


@pytest.mark.parametrize(
    "subject, allowed",
    [("dev-user", True), ("test-user", True), ("devops-user", False)],
)
def test_inbound_oracle(subject: str, allowed: bool) -> None:
    """Inbound: dev-user ✅, test-user ✅, devops-user ❌ (devops sources no agent scope)."""
    assert uc1.expected_inbound(subject) is allowed


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound_oracle_all_deny(subject: str, function_name: str) -> None:
    """Rung 1's defining property: the outbound user gate is empty, so every ``(subject, function)``
    is denied — no tool was onboarded, so no tool scope is in the universe."""
    assert expected_outbound(subject, function_name) is False


def test_rung1_grant_set_oracle() -> None:
    """Rung 1 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the
    outbound-subject grant set is empty."""
    assert RUNG1_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG1_OUTBOUND_SUBJECT == set()


# ======================================================================================
# Session fixture — cleanup → onboard agent only → capture rego → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Onboard **only** the agent (the tool is deployed but not onboarded) via the shared harness,
    and yield the live ``admin`` handle + captured ``rego_dir`` (+ writer ``pod``). Keycloak cleanup
    runs before and after; the clients are left registered as before (spec § Per-rung flow)."""
    with uc1.onboarded_stack([scn.AGENT_WORKLOAD], rego_subdir="rung1") as ctx:
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


def test_no_tool_scopes_provisioned(onboarded: dict) -> None:
    """The tool was not onboarded, so no ``github-tool.*`` scope exists (UC-1-provisioned scopes are
    prefixed ``github-tool.``; the operator's ``*-aud`` audience scopes are not and don't count)."""
    admin = onboarded["admin"]
    admin.change_current_realm(TEST_REALM)
    tool_scopes = [
        s["name"] for s in admin.get_client_scopes()
        if s.get("name", "").startswith(f"{scn.TOOL_WORKLOAD}.")
    ]
    assert not tool_scopes, f"unexpected tool scopes provisioned: {tool_scopes}"


@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(onboarded: dict, subject: str) -> None:
    """Inbound gate allows a user iff their role may reach some discovered agent scope
    (dev-user ✅, test-user ✅, devops-user ❌)."""
    rego = onboarded["rego_dir"] / INBOUND_REGO
    allowed = opa_eval([rego], f"data.authz.{AGENT_SLUG}.inbound.allow", {"subject": subject})
    assert allowed == uc1.expected_inbound(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound_all_deny(onboarded: dict, subject: str, function_name: str) -> None:
    """Outbound user gate (via ``probe_uc1.rego``) denies every ``(subject, function)`` — the gate is
    empty because no tool was onboarded (exact-name match on full discovered scope names)."""
    rego = onboarded["rego_dir"] / OUTBOUND_REGO
    allowed = opa_eval(
        [rego, uc1.PROBE_UC1],
        "data.probe.outbound.allow",
        {"subject": subject, "function_name": function_name},
    )
    assert allowed == expected_outbound(subject, function_name), f"{subject} / {function_name}"


def test_only_agent_rego_present(onboarded: dict) -> None:
    """Exactly the two agent files on disk; explicitly no ``github_tool.*.rego`` (the tool is a pure
    target — no rules written for it, and it was not onboarded). Checks the writer's own ``/rego``,
    not just what was copied to the host."""
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
    assert got == RUNG1_INBOUND, f"inbound: missing={RUNG1_INBOUND - got} extra={got - RUNG1_INBOUND}"


def test_outbound_subject_grant_set_empty(onboarded: dict) -> None:
    """The outbound-subject grant set re-derived from the Rego is ``∅`` — no tool onboarded, so the
    user gate is empty."""
    got = uc1.outbound_subject_grants(onboarded["rego_dir"])
    assert got == RUNG1_OUTBOUND_SUBJECT, f"expected empty outbound gate, got {got}"
