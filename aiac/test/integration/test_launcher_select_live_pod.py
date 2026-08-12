"""Unit tests for ``launcher.select_live_pod`` — the pure pod-selection behind issue #139.

Not an integration test (no ``pytest.mark.integration``): it exercises the selection logic against
synthetic ``kubectl get pods`` items, so it runs in the normal ``-m "not integration"`` suite with no
cluster. This pins the race that stalled ``test_uc1_onboard_agent_then_tool``: during a rolling
restart the outgoing pod lingers ``Terminating`` next to the new Ready one, and the old
``items[0]`` selection could pin that doomed pod — every later ``kubectl exec`` then failed
``NotFound`` -> classified ``"error"`` -> 300s convergence timeout.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # -> aiac/
sys.path.insert(0, str(REPO_ROOT))

from test.integration.launcher import select_live_pod  # noqa: E402


def _pod(name: str, *, created: str, phase: str = "Running", ready: bool = True, terminating: bool = False) -> dict:
    meta: dict = {"name": name, "creationTimestamp": created}
    if terminating:
        meta["deletionTimestamp"] = "2026-01-01T00:00:05Z"
    return {
        "metadata": meta,
        "status": {
            "phase": phase,
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def test_skips_terminating_pod_during_rolling_restart() -> None:
    """The exact #139 shape: an old pod still Ready but Terminating, alongside the new Ready pod.
    The terminating pod must never be chosen even though it is Ready and listed first/older."""
    old = _pod("github-agent-OLD", created="2026-01-01T00:00:00Z", ready=True, terminating=True)
    new = _pod("github-agent-NEW", created="2026-01-01T00:00:03Z", ready=True)
    assert select_live_pod([old, new]) == "github-agent-NEW"
    assert select_live_pod([new, old]) == "github-agent-NEW"  # order-independent


def test_prefers_ready_over_not_ready() -> None:
    """A newer not-yet-Ready pod must not shadow an older Ready one (avoid exec'ing a booting pod)."""
    ready_old = _pod("agent-READY", created="2026-01-01T00:00:00Z", ready=True)
    starting_new = _pod("agent-STARTING", created="2026-01-01T00:00:09Z", phase="Pending", ready=False)
    assert select_live_pod([ready_old, starting_new]) == "agent-READY"


def test_picks_newest_among_ready() -> None:
    a = _pod("agent-A", created="2026-01-01T00:00:00Z", ready=True)
    b = _pod("agent-B", created="2026-01-01T00:00:07Z", ready=True)
    assert select_live_pod([a, b]) == "agent-B"


def test_falls_back_to_newest_nonterminating_when_none_ready() -> None:
    """Mid-startup: nothing Ready yet — still return a live (non-terminating) pod, the newest."""
    p1 = _pod("agent-1", created="2026-01-01T00:00:00Z", phase="Pending", ready=False)
    p2 = _pod("agent-2", created="2026-01-01T00:00:04Z", phase="Pending", ready=False)
    assert select_live_pod([p1, p2]) == "agent-2"


def test_none_when_only_terminating_or_empty() -> None:
    assert select_live_pod([]) is None
    dying = _pod("agent-dying", created="2026-01-01T00:00:00Z", ready=True, terminating=True)
    assert select_live_pod([dying]) is None
