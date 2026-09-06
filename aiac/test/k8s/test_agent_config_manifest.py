"""Manifest-content tests for the agent's ``aiac-agent-config`` ConfigMap.

The Policy Rules Builder LLM seam reads its own dedicated retry cadence from the
environment (``graph._llm_retry_config()`` — ``LLM_MAX_RETRIES`` / ``LLM_RETRY_BACKOFF_MIN`` /
``LLM_RETRY_BACKOFF_MAX``). These knobs must therefore be surfaced on the agent's ConfigMap
so an operator can tune them per environment. They are deliberately SEPARATE from the shared
transport-layer knob ``UPSTREAM_MAX_RETRIES`` (``aiac.shared.upstream.max_retries``, which
still governs the IdP/MCP/K8s seams).

These tests parse the manifest YAML directly and assert on its ``data`` dict — they do not
require (or talk to) Kubernetes.
"""

from pathlib import Path

import yaml

_MANIFEST = Path(__file__).resolve().parents[2] / "k8s" / "agent-deployment.yaml"


def _agent_config_data() -> dict[str, str]:
    """Return the ``data`` map of the ``aiac-agent-config`` ConfigMap from the manifest."""
    docs = list(yaml.safe_load_all(_MANIFEST.read_text()))
    for doc in docs:
        if (
            doc
            and doc.get("kind") == "ConfigMap"
            and doc.get("metadata", {}).get("name") == "aiac-agent-config"
        ):
            return doc.get("data", {})
    raise AssertionError("aiac-agent-config ConfigMap not found in agent-deployment.yaml")


def test_llm_retry_knobs_present_with_defaults():
    """The three dedicated PRB LLM retry knobs are surfaced with the issue's stated defaults."""
    data = _agent_config_data()
    assert data.get("LLM_MAX_RETRIES") == "3"
    assert data.get("LLM_RETRY_BACKOFF_MIN") == "1"
    assert data.get("LLM_RETRY_BACKOFF_MAX") == "30"


def test_upstream_max_retries_remains_and_is_distinct_from_llm_knobs():
    """The transport-layer knob is not removed and is a separate key from the LLM knobs."""
    data = _agent_config_data()
    # UPSTREAM_MAX_RETRIES stays (transport seams: IdP / MCP / K8s) and keeps its default.
    assert data.get("UPSTREAM_MAX_RETRIES") == "3"
    # It is distinct from the dedicated LLM knobs — not conflated with any of them.
    assert "UPSTREAM_MAX_RETRIES" not in {
        "LLM_MAX_RETRIES",
        "LLM_RETRY_BACKOFF_MIN",
        "LLM_RETRY_BACKOFF_MAX",
    }
