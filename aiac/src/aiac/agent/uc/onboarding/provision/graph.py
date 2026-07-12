"""Compiled StateGraph for the Service Provision sub-agent (UC1).

    START -> classify_service -> [analyze_agent | analyze_tool] -> provision_service -> END

The conditional edge routes on `service_type` (set by `classify_service`). All nodes are
non-LLM; no LLM is built here.
"""

from langgraph.graph import END, START, StateGraph

from aiac.idp.configuration.models import ServiceType

from .nodes import analyze_agent, analyze_tool, classify_service, provision_service
from .state import OnboardingProvisionState


def _route(state: OnboardingProvisionState) -> str:
    return "analyze_agent" if state.service_type is ServiceType.AGENT else "analyze_tool"


def build_provision_graph():
    g = StateGraph(OnboardingProvisionState)
    g.add_node("classify_service", classify_service)
    g.add_node("analyze_agent", analyze_agent)
    g.add_node("analyze_tool", analyze_tool)
    g.add_node("provision_service", provision_service)

    g.add_edge(START, "classify_service")
    g.add_conditional_edges(
        "classify_service",
        _route,
        {"analyze_agent": "analyze_agent", "analyze_tool": "analyze_tool"},
    )
    g.add_edge("analyze_agent", "provision_service")
    g.add_edge("analyze_tool", "provision_service")
    g.add_edge("provision_service", END)
    return g.compile()
