"""Unit tests for the Service Provision `analyze_agent` node (UC1, issue 4.3).

Kubernetes access (AgentCard CRs) is mocked via the `_custom_objects` seam.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.uc.onboarding.provision import kube, nodes
from aiac.agent.uc.onboarding.provision.state import OnboardingProvisionState, Trigger

NS = "team-a"
WORKLOAD = "weather"


def _state():
    return OnboardingProvisionState(
        trigger=Trigger(entity_id="svc-123"), namespace=NS, workload_name=WORKLOAD
    )


def _card(name=WORKLOAD, skills=None):
    """An AgentCard CR as the operator syncs it: the fetched A2A card lives under ``status.card``,
    and each skill carries a machine ``id`` (a stable identifier) plus a display ``name``."""
    return {"metadata": {"name": name}, "status": {"card": {"skills": skills or []}}}


def _run(items=None, list_exc=None):
    with patch.object(kube, "_custom_objects") as co:
        client = MagicMock()
        if list_exc is not None:
            client.list_namespaced_custom_object.side_effect = list_exc
        else:
            client.list_namespaced_custom_object.return_value = {"items": items or []}
        co.return_value = client
        return nodes.analyze_agent(_state())


class TestAnalyzeAgentFound:
    def test_one_agent_role_and_one_scope_per_skill(self):
        # Scope names come from each skill's machine ``id`` — not its display ``name`` (which may
        # contain spaces) — so they are stable identifiers usable as Keycloak scope names.
        skills = [
            {"id": "forecast", "name": "Weather forecast operations", "description": "Get forecast"},
            {"id": "history", "name": "Historical data operations", "description": "Historical data"},
        ]
        provision = _run(items=[_card(skills=skills)])["service_provision"]

        assert [r.name for r in provision.roles] == [f"{WORKLOAD}.agent"]
        assert provision.roles[0].description == "Agent role"
        assert [s.name for s in provision.scopes] == [f"{WORKLOAD}.forecast", f"{WORKLOAD}.history"]
        assert provision.scopes[0].description == "Get forecast"
        assert "derived from AgentCard: 2 skills" == provision.reasoning


    def test_agentcard_matched_by_targetref_not_metadata_name(self):
        # The operator names the card after the Deployment (e.g. "<workload>-deployment-card") and
        # points spec.targetRef at the workload; provision must link the card by targetRef, not by
        # metadata.name == workload.
        card = {
            "metadata": {"name": f"{WORKLOAD}-deployment-card"},
            "spec": {"targetRef": {"kind": "Deployment", "name": WORKLOAD}},
            "status": {"card": {"skills": [{"id": "forecast", "name": "F", "description": "d"}]}},
        }
        provision = _run(items=[card])["service_provision"]

        assert [s.name for s in provision.scopes] == [f"{WORKLOAD}.forecast"]
        assert "derived from AgentCard: 1 skills" == provision.reasoning


class TestAnalyzeAgentLegacyFallback:
    def test_no_agentcard_yields_default_access_scope_and_partial_reasoning(self):
        provision = _run(items=[])["service_provision"]

        assert [r.name for r in provision.roles] == [f"{WORKLOAD}.agent"]
        assert [s.name for s in provision.scopes] == [f"{WORKLOAD}.access"]
        assert provision.scopes[0].description == "Default access scope"
        assert "partial: no AgentCard found" in provision.reasoning

    def test_agentcard_present_but_no_name_match_is_legacy_fallback(self):
        provision = _run(
            items=[_card(name="other-agent", skills=[{"id": "x", "name": "X", "description": "y"}])]
        )["service_provision"]
        assert [s.name for s in provision.scopes] == [f"{WORKLOAD}.access"]
        assert "partial: no AgentCard found" in provision.reasoning

    def test_agentcard_present_but_no_synced_skills_is_fallback(self):
        # The CR exists but its ``status.card`` has not synced any skills yet (e.g. the operator has
        # not fetched the A2A card): fall back to a default access scope rather than provision none.
        provision = _run(items=[_card(skills=[])])["service_provision"]
        assert [s.name for s in provision.scopes] == [f"{WORKLOAD}.access"]
        assert "no synced skills" in provision.reasoning


class TestAnalyzeAgent502:
    def test_k8s_agentcards_list_failure_is_502(self, monkeypatch):
        monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "1")
        with pytest.raises(HTTPException) as ei:
            _run(list_exc=RuntimeError("apiserver down"))
        assert ei.value.status_code == 502
