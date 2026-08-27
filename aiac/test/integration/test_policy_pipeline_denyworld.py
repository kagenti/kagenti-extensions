"""Live full-deployment policy-pipeline test for a **DENY-bearing** policy under ``default_effect=ALLOW``.

The ALLOW-default sibling of ``test_policy_pipeline.py``. It onboards **Policy B** (permissive-default
prose that states only prohibitions + exclusive scoping — ``scenario_uc1_denyworld.POLICY_DENYWORLD``)
through the **same** real UC-1 pipeline, driving nothing mocked: it mounts Policy B's ``policy.md`` and
sets the derived ``AgentPolicyModel.default_effect`` to ``ALLOW`` (the #146 Controller hook the harness
applies), onboards both the ``github-agent`` and the ``github-tool`` through the in-cluster Controller
(``POST /apply/service/{id}``, upserting the ``AuthorizationPolicy`` CR), enables the outbound
token-exchange leg, waits for ``bundle-service`` + the AuthBridge OPA sidecars to recompose and reload
the bundle, then each test drives a **real HTTP request through AuthBridge** and asserts the **real OPA
plugin's** allow/deny against Policy B's §6 matrix (``scenario_uc1_denyworld.py``, the #148 oracle).

**Why ``default_effect=ALLOW`` — the whole point (handoff §2).** Under the shipped deny-by-default an
explicit ``DENY`` rule is *behaviorally invisible* at the enforced seam: an ungranted ``(role, tool)``
pair is already denied by the absence of an ALLOW, so ``test_policy_pipeline.py`` would still pass if
the entire DENY-generation machinery silently broke. Flip the default: under ``default=ALLOW`` an
unmentioned pair is **allowed**, so a ``DENY`` rule becomes the *only* thing that can deny a pair.
Dropping any explicit DENY anywhere in the chain (PRB → PCE → Rego → bundle → OPA) flips its cell from
❌ back to ✅ and **fails** this test — the load-bearing DENY-drop property the deny-by-default suite
cannot assert. The ``devops → issues-*`` = ✅ cells are the **default-flip tracer**: they are *deny*
under Policy A (``test_policy_pipeline.py:86-87``) and *allow* here purely by the permissive default, so
the test cannot false-green on a stack still running deny-by-default.

Policy B carries both ALLOW and DENY rules (the exclusivity idiom emits an ALLOW half), but under
``default=ALLOW`` the ALLOW halves are **inert** (everything not denied is already allowed) and the
prose constrains **user roles only** (no target/capability-gate denies), so the enforced §6 matrix is
driven **purely by the subject-side denies** — see the ``scenario_uc1_denyworld`` docstring.

The **fixture-independent oracle-contract tests** that pin the §6 matrix directly live in
``test_scenario_uc1_denyworld.py`` (#148, the unit lane — no cluster/Keycloak/LLM); this module reuses
that oracle's ``expected_*`` functions as its single source of expected verdicts rather than
re-pinning a second copy of the matrix. Policy A is **not** re-implemented here (it stays in
``test_policy_pipeline.py``).

Run (needs a live rossoctl/Kind cluster with the AuthBridge OPA pipeline wired into both legs — see
``k8s/opa-kind-runbook.md`` / ``k8s/opa-kind-enable.sh`` — the demo workloads deployed + registered,
a real LLM in-pod, **and #146's ``default_effect`` hook wired into ``onboarded_stack``**, with
``test/integration/.env`` sourced):

    .venv/bin/pytest test/integration/test_policy_pipeline_denyworld.py -m integration -v

Without ``-m integration`` the suite is not collected; without a wired cluster / env it skips cleanly,
and its teardown resets ``default_effect`` to ``Deny`` so a subsequent Policy-A run on the shared stack
is unaffected.
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
from test.integration import scenario_uc1_denyworld as scn_b  # noqa: E402
from test.integration import uc1_onboard as uc1  # noqa: E402


# The expected verdicts come straight from the #148 Policy-B oracle (``scenario_uc1_denyworld``), keyed
# on the **bare** runtime tool names AuthBridge sends (``source-read``). These thin adapters only turn
# the oracle's bool into the ``"allow"``/``"deny"`` decision string the live probes return — they add
# **no** matrix truth of their own, so the oracle stays the single source of truth (mirrors
# ``uc1.expected_inbound_decision`` / ``expected_outbound_decision`` for Policy A).


def _expected_inbound(subject: str) -> str:
    return "allow" if scn_b.expected_inbound_denyworld(subject) else "deny"


def _expected_outbound(subject: str, tool_bare: str) -> str:
    return "allow" if scn_b.expected_outbound_denyworld_bare(subject, tool_bare) else "deny"


# ======================================================================================
# Session fixture — one-time full-stack onboarding of Policy B under default_effect=ALLOW
# ======================================================================================


@pytest.fixture(scope="session")
def pipeline() -> dict:
    """Onboard the **full** stack (agent + tool) via the shared harness with **Policy B**'s prose and
    ``default_effect=ALLOW`` (the #146 hook), enable the outbound leg, and wait for the live pipeline to
    converge; yield the live probe context. Keycloak cleanup + CR delete run before and after, and the
    harness resets ``default_effect`` to ``Deny`` on teardown so the shared stack returns to the shipped
    default for the Policy-A suite. Skips cleanly if the pipeline is not wired or the env is unset.

    The convergence signal set is Policy-B-aware — its default-flip tracer (``outbound(devops-user,
    issues-read) == allow``) is *deny* under Policy A, so a stale Policy-A bundle can never satisfy it.
    (The tracer rides the **outbound** leg, not inbound: ``devops-user`` is *deny* inbound under **both**
    policies — no grant under A, an explicit source-prohibition deny under B — so an inbound devops
    signal would not distinguish the two defaults.) Each signal is polled to a **definitive** allow/deny
    (never ``error``), which also waits out the post-restart token-exchange 503 window:

      * ``inbound(dev-user) == allow`` — the agent is reachable (developer is unconstrained);
      * ``outbound(devops-user, issues-read) == allow`` — **the default-flip tracer**: this pair is
        *deny* under Policy A and *allow* here purely by the permissive default, so it proves *this*
        run's ``default=ALLOW`` CR is live, not a stale Policy-A bundle;
      * ``outbound(test-user, source-read) == deny`` — proves an explicit ``DENY`` is enforced (the
        tester source-DENY), not the permissive default leaking through.
    """
    signals = [
        uc1.ReadySignal("inbound", "dev-user", "allow"),
        uc1.ReadySignal("outbound", "devops-user", "allow", tool_bare="issues-read"),
        uc1.ReadySignal("outbound", "test-user", "deny", tool_bare="source-read"),
    ]
    with uc1.onboarded_stack(
        [scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD],
        policy_md=scn_b.POLICY_DENYWORLD,
        default_effect=uc1.DEFAULT_EFFECT_ALLOW,
        ready_signals=signals,
    ) as ctx:
        yield ctx


# ======================================================================================
# Live tests — the real OPA plugin's decisions over Policy B's §6 matrix
# ======================================================================================


@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(pipeline: dict, subject: str) -> None:
    """The enforced inbound gate — a real request through AuthBridge as ``subject``. Policy B's source
    prohibitions project onto the agent's ``source_operations`` scope, so under ``default=ALLOW`` the
    coarse deny-overrides inbound gate denies ``tester`` and ``devops`` the agent entirely while the
    unconstrained ``developer`` is allowed; ``tester`` inbound flips allow→deny vs. Policy A (a
    load-bearing observable DENY). The real OPA plugin decides; ``jwt-validation`` builds
    ``input.identity`` (no hand-built input)."""
    assert uc1.inbound_decision(pipeline, subject) == _expected_inbound(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("tool_bare", scn.TOOL_REQUEST_NAMES)
def test_outbound(pipeline: dict, subject: str, tool_bare: str) -> None:
    """The enforced outbound gate — a real MCP ``tools/call`` for the **bare** tool through AuthBridge's
    forward proxy (token-exchange → OPA). Under ``default=ALLOW`` a pair is allowed unless the
    subject gate explicitly denies it: every ❌ in Policy B's §6 matrix is a load-bearing explicit
    ``DENY`` (tester→source-*, devops→source-*), and the ``devops → issues-*`` ✅
    cells are the default-flip tracer. ``mcp-parser`` surfaces ``input.mcp.params.name`` (no hand-built
    input); a denial is a JSON-RPC error frame the harness classifies."""
    assert uc1.outbound_decision(pipeline, subject, tool_bare) == _expected_outbound(
        subject, tool_bare
    ), f"{subject} / {tool_bare}"


# ======================================================================================
# Negative controls — an unmatched tool name under default=ALLOW (resolved Rego semantics)
# ======================================================================================
#
# The mirror image of ``test_policy_pipeline.py:142-153``. Under the shipped ``default=DENY`` an
# unknown tool name matches no ALLOW gate and falls through to deny-by-default (``deny``). Under Policy
# B's ``default=ALLOW`` the outbound decision is ``default allow := true`` with ``allow := false if {
# subject_deny_ok }`` / ``{ target_deny_ok }``, and each DENY gate fires only when
# ``input.mcp.params.name`` is in that role's deny map (``rego.py`` ``_outbound_subject_gate`` /
# ``_decision_block``). An unrecognized name is in **no** deny map, so no DENY gate matches and the
# request falls through to the **permissive default = allow**. This is the resolved answer to the
# handoff §7.3 question ("does an unmatched tool fall to the permissive default or is it denied?"): it
# is **allowed**. The oracle agrees — ``expected_outbound_denyworld_bare`` denies only the four explicit
# subject-DENY pairs, so any name outside them is allow.


def test_outbound_unknown_tool_allowed_by_permissive_default(pipeline: dict) -> None:
    """An otherwise-allowed subject (dev-user) invoking a tool name in **no** map is **allowed** under
    ``default=ALLOW`` — it matches no explicit DENY gate, so it falls through to the permissive default
    (the mirror of the deny-by-default control, which denies it). Confirms the DENY gate matches
    ``input.mcp.params.name`` exactly and does not over-match an unrecognized name into a spurious
    deny."""
    assert uc1.outbound_decision(pipeline, "dev-user", "nonexistent-tool") == "allow"
    assert uc1.outbound_decision(pipeline, "dev-user", "nonexistent-tool") == _expected_outbound(
        "dev-user", "nonexistent-tool"
    )


def test_outbound_bogus_tool_shape_allowed_by_permissive_default(pipeline: dict) -> None:
    """A bogus, destructive-sounding tool name matching no deny scope is **allowed** under
    ``default=ALLOW`` (same permissive-default reasoning). Guards the inverse of the Policy-A control:
    here the risk is an over-broad DENY match spuriously denying an unrecognized name, and this pins
    that no such over-match happens."""
    assert uc1.outbound_decision(pipeline, "dev-user", "delete_everything") == "allow"
    assert uc1.outbound_decision(pipeline, "dev-user", "delete_everything") == _expected_outbound(
        "dev-user", "delete_everything"
    )
