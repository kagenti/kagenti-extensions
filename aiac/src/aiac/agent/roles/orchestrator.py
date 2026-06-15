from aiac.agent.roles.role.graph import RoleGraph
from aiac.agent.shared.state import BaseAgentState


def dispatch(state: BaseAgentState) -> BaseAgentState:
    """Dispatch a role/{id} trigger to the Role sub-agent and return the result."""
    return RoleGraph.invoke(state)
