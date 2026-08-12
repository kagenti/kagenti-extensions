"""Rung 1 of the UC-1 onboarding ladder — onboard the **agent only**.

The simplest rung (issue ``testing/5.4.1-uc1-onboard-agent-only.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``): drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for **only** the ``github-agent`` — the
``github-tool`` is deployed + registered but **not** onboarded — then assert the agent-side outcome
by driving **real HTTP requests through AuthBridge** and reading the **real OPA plugin's** allow/deny
(handoff 08; live loop shape in ``docs/opa-kind-runbook.md``). Proves agent discovery + inbound
policy generation stand alone, and that the outbound user gate is correctly **empty** (all deny) when
no tool has been onboarded.

Single live rossoctl/Kind cluster with the AuthBridge OPA pipeline wired into both legs. The shared
harness (config, Keycloak provisioning/cleanup, onboard trigger, Part-B outbound-leg prep, bundle
convergence poll, and the per-rung fixture flow) lives in ``uc1_onboard.py`` and is reused by every
rung; this module supplies only rung 1's oracle (verdicts computed from ``scenario_uc1.py``) and its
live assertions. There is no ``.rego`` dump and no ``opa`` binary anymore — the deployed plugin is the
evaluator.

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard agent → Part B → poll bundle →
drive real requests + assert → Keycloak cleanup**. Deployment + client registration are
**preconditions**, not test steps.

*Onboard + evaluate against the real plugin — no CrewAI flow is triggered* (the probes hit
``ping/nonexistent`` inbound and a bare ``tools/call`` outbound).

Run (needs a live rossoctl/Kind cluster with the AIAC stack + AuthBridge OPA pipeline wired in — see
``docs/opa-kind-runbook.md`` / ``../scripts/opa-kind-enable.sh`` — the demo workloads deployed +
registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``test/integration/.env`` sourced):

    .venv/bin/pytest test/integration/test_uc1_onboard_agent_only.py -m integration -v

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
# Rung 1 is the exception in the ladder: with no tool onboarded there is no tool scope in the
# universe, so the outbound **user gate is empty** (all deny) — regardless of
# ``scenario_uc1.OUTBOUND_SUBJECT_BARE`` (which is the rung-2/3 table). Inbound is unaffected
# (``uc1.expected_inbound`` — the shared inbound oracle). The request + expected outcome are keyed on
# the **bare** runtime tool names AuthBridge sends (``source-read``), never the prefixed provisioned
# names. Verdicts are computed here, never read from the plugin under test.

# Rung 1's expected grant sets (the oracle for the grant-set contract check). Inbound uses the
# prefixed provisioned truth; the outbound user gate is empty.
RUNG1_INBOUND = uc1.INBOUND_GRANT_SET
RUNG1_OUTBOUND_SUBJECT_BARE: set[tuple[str, str]] = set()  # ∅ — no tool onboarded


def expected_outbound(subject: str, tool_bare: str) -> bool:
    """Rung 1: the outbound user gate is entirely empty (no tool onboarded), so every
    ``(subject, tool_bare)`` is denied."""
    return (scn.USERS[subject], tool_bare) in RUNG1_OUTBOUND_SUBJECT_BARE


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 1's defining decisions
# ======================================================================================
#
# These need neither the cluster nor the env: they assert the rung-1 oracle itself (the intended
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
@pytest.mark.parametrize("tool_bare", scn.TOOL_REQUEST_NAMES)
def test_outbound_oracle_all_deny(subject: str, tool_bare: str) -> None:
    """Rung 1's defining property: the outbound user gate is empty, so every ``(subject, tool)`` is
    denied — no tool was onboarded, so no tool scope is in the universe."""
    assert expected_outbound(subject, tool_bare) is False


def test_rung1_grant_set_oracle() -> None:
    """Rung 1 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the
    outbound-subject gate is empty."""
    assert RUNG1_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG1_OUTBOUND_SUBJECT_BARE == set()


# ======================================================================================
# Session fixture — cleanup → onboard agent only → Part B → poll bundle → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Onboard **only** the agent (the tool is deployed but not onboarded) via the shared harness,
    and yield the live probe context (``admin`` handle, ``agent_pod``, Keycloak URL/realm,
    ``tool_onboarded=False``). Keycloak cleanup + CR delete run before and after; the clients are
    left registered as before (spec § Per-rung flow)."""
    with uc1.onboarded_stack([scn.AGENT_WORKLOAD]) as ctx:
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
    """Inbound gate — a real request through AuthBridge as ``subject`` is allowed iff their role may
    reach some discovered agent scope (dev-user ✅, test-user ✅, devops-user ❌). The real OPA plugin
    decides; AuthBridge's ``jwt-validation`` builds ``input.identity`` (no hand-built input)."""
    assert uc1.inbound_decision(onboarded, subject) == uc1.expected_inbound_decision(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("tool_bare", scn.TOOL_REQUEST_NAMES)
def test_outbound_all_deny(onboarded: dict, subject: str, tool_bare: str) -> None:
    """Outbound user gate denies every ``(subject, tool)`` — the gate is empty because no tool was
    onboarded. A real MCP ``tools/call`` for the bare tool through AuthBridge's forward proxy
    (token-exchange → OPA) is denied for every user/tool pair (real-plugin decision)."""
    assert uc1.outbound_decision(onboarded, subject, tool_bare) == "deny", f"{subject} / {tool_bare}"
