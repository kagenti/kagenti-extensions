"""Unit tests for aiac.agent.roles.orchestrator (issue 4.11)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from aiac.agent.shared.state import BaseAgentState, TriggerContext

REALM = "kagenti"
ROLE_ID = "role-uuid-1"


def _make_state(trigger_type: str = f"role/{ROLE_ID}", entity_id: str = ROLE_ID) -> BaseAgentState:
    return {
        "trigger": {"trigger_type": trigger_type, "entity_id": entity_id},
        "realm": REALM,
        "policy_chunks": [],
        "domain_knowledge_chunks": [],
        "pdp_snapshot": None,
        "proposed_diff": None,
        "validation_errors": [],
        "added": [],
        "removed": [],
        "summary": "",
    }


# ---------------------------------------------------------------------------
# dispatch scenarios
# ---------------------------------------------------------------------------


class TestRoleOrchestrator:
    def test_role_trigger_invokes_role_graph(self):
        from aiac.agent.roles.orchestrator import dispatch

        state = _make_state()
        expected_result = {**state, "summary": "done", "provisioned": None}

        with patch("aiac.agent.roles.orchestrator.RoleGraph") as MockGraph:
            MockGraph.invoke.return_value = expected_result
            result = dispatch(state)

        MockGraph.invoke.assert_called_once_with(state)
        assert result == expected_result

    def test_sub_agent_response_returned_unchanged(self):
        from aiac.agent.roles.orchestrator import dispatch

        state = _make_state()
        raw_result = {**state, "summary": "applied 2 additions", "provisioned": None, "added": [], "removed": []}

        with patch("aiac.agent.roles.orchestrator.RoleGraph") as MockGraph:
            MockGraph.invoke.return_value = raw_result
            result = dispatch(state)

        assert result is raw_result

    def test_sub_agent_http_error_propagated(self):
        from aiac.agent.roles.orchestrator import dispatch

        state = _make_state()

        with patch("aiac.agent.roles.orchestrator.RoleGraph") as MockGraph:
            MockGraph.invoke.side_effect = HTTPException(status_code=502, detail="PDP down")

            with pytest.raises(HTTPException) as exc_info:
                dispatch(state)

        assert exc_info.value.status_code == 502

    def test_sub_agent_generic_error_propagated(self):
        from aiac.agent.roles.orchestrator import dispatch

        state = _make_state()

        with patch("aiac.agent.roles.orchestrator.RoleGraph") as MockGraph:
            MockGraph.invoke.side_effect = RuntimeError("unexpected failure")

            with pytest.raises(RuntimeError):
                dispatch(state)
