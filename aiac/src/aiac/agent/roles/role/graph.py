from langgraph.graph import END, StateGraph

from aiac.agent.shared.state import BaseAgentState
from aiac.agent.roles.role.nodes import (
    apply_mappings,
    fetch_pdp_state,
    format_response,
    propose_mappings,
    validate_mappings,
)


def _has_errors(state: BaseAgentState) -> str:
    return "abort" if state.get("validation_errors") else "continue"


def build_role_graph() -> StateGraph:
    graph = StateGraph(BaseAgentState)

    graph.add_node("fetch_pdp_state", fetch_pdp_state)
    graph.add_node("propose_mappings", propose_mappings)
    graph.add_node("validate_mappings", validate_mappings)
    graph.add_node("apply_mappings", apply_mappings)
    graph.add_node("format_response", format_response)

    graph.set_entry_point("fetch_pdp_state")
    graph.add_edge("fetch_pdp_state", "propose_mappings")
    graph.add_edge("propose_mappings", "validate_mappings")
    graph.add_conditional_edges(
        "validate_mappings",
        _has_errors,
        {"abort": "format_response", "continue": "apply_mappings"},
    )
    graph.add_edge("apply_mappings", "format_response")
    graph.add_edge("format_response", END)

    return graph


RoleGraph = build_role_graph().compile()
