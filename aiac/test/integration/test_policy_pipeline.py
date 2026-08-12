"""End-to-end policy-pipeline integration test — the **enforced** decision is the artifact under test.

Umbrella full-matrix e2e for the fixed ``github-agent`` scenario (spec:
``docs/specs/integration-test/policy-pipeline.md``; live loop shape: ``docs/opa-kind-runbook.md``). A
single session fixture drives the whole identity→policy→**enforcement** pipeline with nothing mocked
or dumped: it onboards **both** the ``github-agent`` and the ``github-tool`` through the real
in-cluster UC-1 Controller (``POST /apply/service/{id}``, upserting the ``AuthorizationPolicy`` CR),
enables the outbound token-exchange leg, waits for ``bundle-service`` + the AuthBridge OPA sidecars to
recompose and reload the bundle, then each test drives a **real HTTP request through AuthBridge** and
asserts the **real OPA plugin's** allow/deny against the scenario's role→access truth table
(``scenario_uc1.py``). A wrong LLM/PCE mapping fails the exact ``subject[ / tool]`` cell.

The evaluator is the **deployed plugin**, not a standalone OPA-CLI run over dumped ``.rego``: there is
no ``.rego`` dump and no ``opa`` binary here anymore (handoff 08). Both gates are exercised through AuthBridge's own parsers
— ``jwt-validation`` builds ``input.identity`` for the inbound gate; ``token-exchange`` + ``mcp-parser``
build the outbound ``input.identity`` + ``input.mcp.params.name`` (the **bare** tool name) — so the
test never hand-builds an input document.

Where this sits vs. the UC-1 ladder: rungs 1–3 (``test_uc1_onboard_*``) isolate onboarding-order
properties; this module is the **full happy-path matrix + negative controls** over the fully onboarded
stack. It shares the harness's live stack (the ``rossoctl`` realm + the deployed ``team1`` workloads)
rather than a throwaway realm — there is exactly one deployed pipeline to enforce against — so the
former two-policy-variant equivalence check (explicit vs. abstract) is deferred to the two-policy rung
``testing/5.4.4`` (only one ``policy.md`` is mounted on the live stack; see ``scenario_uc1`` docstring).

Run (needs a live rossoctl/Kind cluster with the AuthBridge OPA pipeline wired in — see
``docs/opa-kind-runbook.md`` / ``../scripts/opa-kind-enable.sh`` — the demo workloads deployed +
registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``test/integration/.env`` sourced):

    .venv/bin/pytest test/integration/test_policy_pipeline.py -m integration -v

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

# The inbound + outbound oracles are the shared, tool-onboarded oracle in ``uc1_onboard`` (the full
# stack is onboarded here). Outbound decisions are keyed on the **bare** runtime tool names AuthBridge
# sends (``source-read``), matching what the live plugin compares against.


# ======================================================================================
# Oracle contract tests — fixture-independent; pin the intended full matrix
# ======================================================================================
#
# These need neither the cluster nor the env: they assert the intended matrix itself (the tracer
# bullet). If these are wrong, every live assertion is meaningless.


@pytest.mark.parametrize(
    "subject, allowed",
    [("dev-user", True), ("test-user", True), ("devops-user", False)],
)
def test_inbound_oracle(subject: str, allowed: bool) -> None:
    """Inbound: dev-user ✅, test-user ✅, devops-user ❌ (devops sources no agent scope)."""
    assert uc1.expected_inbound(subject) is allowed


@pytest.mark.parametrize(
    "subject, tool_bare, allowed",
    [
        ("dev-user", "source-read", True),
        ("dev-user", "source-write", True),
        ("dev-user", "issues-read", True),
        ("dev-user", "issues-write", False),
        ("test-user", "source-read", False),
        ("test-user", "source-write", False),
        ("test-user", "issues-read", True),
        ("test-user", "issues-write", True),
        ("devops-user", "source-read", False),
        ("devops-user", "source-write", False),
        ("devops-user", "issues-read", False),
        ("devops-user", "issues-write", False),
    ],
)
def test_outbound_oracle(subject: str, tool_bare: str, allowed: bool) -> None:
    """The full user→tool outbound gate over the **bare** tool names (developer: source rw + issues
    read; tester: issues rw; devops: nothing)."""
    assert uc1.expected_outbound_bare(subject, tool_bare) is allowed


# ======================================================================================
# Session fixture — the one-time full-stack onboarding + bundle convergence
# ======================================================================================


@pytest.fixture(scope="session")
def pipeline() -> dict:
    """Onboard the **full** stack (agent + tool) via the shared harness, enable the outbound leg, and
    wait for the live pipeline to converge; yield the live probe context (``admin`` handle,
    ``agent_pod``, Keycloak URL/realm, ``tool_onboarded=True``). Keycloak cleanup + CR delete run
    before and after. Skips cleanly if the pipeline is not wired or the env is unset."""
    with uc1.onboarded_stack([scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD]) as ctx:
        yield ctx


# ======================================================================================
# Live tests — the real OPA plugin's decisions over the full matrix
# ======================================================================================


@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(pipeline: dict, subject: str) -> None:
    """The enforced inbound gate — a real request through AuthBridge as ``subject`` — allows a user
    iff their role may reach some agent scope. The real OPA plugin decides; ``jwt-validation`` builds
    ``input.identity`` (no hand-built input)."""
    assert uc1.inbound_decision(pipeline, subject) == uc1.expected_inbound_decision(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("tool_bare", scn.TOOL_REQUEST_NAMES)
def test_outbound(pipeline: dict, subject: str, tool_bare: str) -> None:
    """The enforced outbound gate — a real MCP ``tools/call`` for the **bare** tool through
    AuthBridge's forward proxy (token-exchange → OPA) — allows a subject's call iff both the subject
    and some agent role are entitled to that tool's scope. ``mcp-parser`` surfaces
    ``input.mcp.params.name`` (no hand-built input); a denial is a JSON-RPC error frame the harness
    classifies."""
    assert uc1.outbound_decision(pipeline, subject, tool_bare) == uc1.expected_outbound_decision(
        subject, tool_bare
    ), f"{subject} / {tool_bare}"


# ======================================================================================
# Negative controls — real requests that must be denied by the real plugin
# ======================================================================================


def test_outbound_unknown_tool_denied(pipeline: dict) -> None:
    """An otherwise-allowed subject (dev-user) invoking a tool name that is in **no** allowed scope is
    denied — the outbound gate matches ``input.mcp.params.name`` exactly, so an unknown tool falls
    through to deny-by-default (not an accidental allow)."""
    assert uc1.outbound_decision(pipeline, "dev-user", "nonexistent-tool") == "deny"


def test_outbound_bogus_tool_shape_denied(pipeline: dict) -> None:
    """A bogus, destructive-sounding tool name matching no discovered scope is denied — guards against
    an over-broad match letting an unrecognized operation through."""
    assert uc1.outbound_decision(pipeline, "dev-user", "delete_everything") == "deny"
