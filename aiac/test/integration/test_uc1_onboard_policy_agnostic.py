"""Unit tests for the policy-agnostic parametrization of the ``uc1_onboard`` harness (issue #149).

Not an integration test (no ``pytest.mark.integration``): it exercises the *pure* seams the #149
change adds — the ``ReadySignal`` convergence-probe descriptor, the Policy-A default signal set, and
the behavior-preserving defaults of ``ensure_agent_policy`` / ``onboarded_stack`` — with no cluster,
no Keycloak, and no LLM. It runs in the normal ``-m "not integration"`` suite.

The point of these tests is the #149 acceptance property: **every new parameter defaults to today's
Policy-A behavior**, so the existing rung callers (which pass only a positional ``workloads`` list)
stay byte-for-byte unchanged, while a second policy can drive the same harness under a different
default effect and its own convergence probe.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # -> aiac/
sys.path.insert(0, str(REPO_ROOT))

from test.integration import scenario_uc1 as scn  # noqa: E402
from test.integration import uc1_onboard as uc1  # noqa: E402

# --- ReadySignal: the parametrized convergence probe --------------------------------------------


def test_ready_signal_inbound_dispatches_to_inbound_decision(monkeypatch) -> None:
    """An ``inbound`` signal routes through ``inbound_decision(ctx, subject)`` and returns its verdict."""
    calls: list[tuple] = []
    monkeypatch.setattr(uc1, "inbound_decision", lambda ctx, user: calls.append(("in", user)) or "allow")
    sig = uc1.ReadySignal("inbound", "dev-user", "allow")
    assert sig.decide({"marker": 1}) == "allow"
    assert calls == [("in", "dev-user")]


def test_ready_signal_outbound_dispatches_with_bare_tool(monkeypatch) -> None:
    """An ``outbound`` signal routes through ``outbound_decision(ctx, subject, tool_bare)``."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        uc1, "outbound_decision", lambda ctx, user, tool: calls.append(("out", user, tool)) or "deny"
    )
    sig = uc1.ReadySignal("outbound", "tester-user", "deny", tool_bare="source-read")
    assert sig.decide({}) == "deny"
    assert calls == [("out", "tester-user", "source-read")]


def test_ready_signal_labels_are_human_readable() -> None:
    """Labels feed the raw-diagnostics message, so they must name the probe unambiguously."""
    assert uc1.ReadySignal("inbound", "dev-user", "allow").label() == "inbound(dev-user)"
    assert (
        uc1.ReadySignal("outbound", "devops-user", "allow", tool_bare="issues-read").label()
        == "outbound(devops-user,issues-read)"
    )


def test_outbound_signal_requires_a_bare_tool() -> None:
    """An outbound signal with no ``tool_bare`` is a programming error — fail loudly at construction."""
    import pytest

    with pytest.raises(ValueError):
        uc1.ReadySignal("outbound", "dev-user", "allow")


# --- The Policy-A default signal set (preserved behavior) ---------------------------------------


def test_default_ready_signals_match_todays_policy_a_probe_tool_onboarded() -> None:
    """With a tool onboarded (rungs 2 & 3), the default signals are exactly today's hardcoded probe:
    dev-user inbound allow, devops-user inbound deny, dev-user outbound source-read allow."""
    signals = uc1._default_ready_signals(tool_onboarded=True)
    assert [(s.kind, s.subject, s.expected, s.tool_bare) for s in signals] == [
        ("inbound", "dev-user", "allow", None),
        ("inbound", "devops-user", "deny", None),
        ("outbound", "dev-user", "allow", "source-read"),
    ]


def test_default_ready_signals_flip_source_read_for_the_agent_only_rung() -> None:
    """Rung 1 (agent only, empty outbound gate): dev-user outbound source-read converges to ``deny``,
    exactly as today's ``expected_source_read = "allow" if tool_onboarded else "deny"``."""
    signals = uc1._default_ready_signals(tool_onboarded=False)
    outbound = [s for s in signals if s.kind == "outbound"]
    assert len(outbound) == 1
    assert outbound[0].expected == "deny"
    assert outbound[0].tool_bare == "source-read"


# --- Behavior-preserving defaults on the parametrized entry points ------------------------------


def test_ensure_agent_policy_defaults_to_policy_a_abstract() -> None:
    """``ensure_agent_policy`` gains ``policy_md`` but defaults to Policy A's abstract, so existing
    (positional-namespace-only) callers mount the same policy as before."""
    params = inspect.signature(uc1.ensure_agent_policy).parameters
    assert "policy_md" in params
    assert params["policy_md"].default == scn.POLICY_ABSTRACT


def test_onboarded_stack_new_params_default_to_policy_a_behavior() -> None:
    """``onboarded_stack`` gains ``policy_md`` / ``default_effect`` / ``ready_signals`` but every new
    parameter defaults to today's Policy-A behavior, so the rung callers stay unchanged."""
    params = inspect.signature(uc1.onboarded_stack).parameters
    assert params["policy_md"].default == scn.POLICY_ABSTRACT
    assert params["default_effect"].default == uc1.DEFAULT_EFFECT_DENY
    assert params["ready_signals"].default is None
    # The shipped default effect is DENY (deny-by-default least-privilege) — the value the harness
    # never patches onto the stack, keeping Policy-A runs from touching the Controller env.
    assert uc1.DEFAULT_EFFECT_DENY == "Deny"
    assert uc1.DEFAULT_EFFECT_ALLOW == "Allow"
