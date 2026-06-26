"""
Single Role Scope Mapper

Maps a single realm role to the privileges/scopes it should hold.
Uses a LangGraph workflow that analyzes, validates, and semantically verifies
the mapping before returning a PolicyObjectModel.

Workflow:
    1. analyze_role_scopes          — LLM determines which privileges the role should hold.
    2. validate_role_scopes         — structural validation with retry.
    3. verify_semantic_scope_mapping — LLM cross-checks the assignment against the policy.
"""

import sys
from typing import Any

from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from aiac.pdp.library.configuration.models import Role, Scope
from aiac.pdp.policy.models import Rule
from .state import SingleRoleState
from base_mapper import (
    BaseSingleMapper,
    MapperConfig,
    extract_explanation_and_json,
    print_explanation,
    validate_mapping_items,
    verify_semantic_mapping,
    should_route_after_structural_validation,
    should_retry_after_semantic,
)
from config.constants import MAX_VALIDATION_RETRIES
from prompts.single_prompt_role_builder import (
    build_single_role_to_scopes_system_prompt,
    build_single_role_to_scopes_retry_prompt,
    build_single_role_to_scopes_verification_prompt,
)


# ============================================================================
# PURE NODE FUNCTIONS
# ============================================================================

def _analyze_role_scopes(
    state: SingleRoleState,
    llm: BaseChatModel,
    verbose: bool,
) -> dict[str, Any]:
    """
    Analyze which privileges should be granted to the role.

    First node in the workflow. Sends the role, available privileges,
    and policy context to the LLM for semantic analysis.
    """
    system_prompt = build_single_role_to_scopes_system_prompt(
        state["role"],
        state["privileges"],
        state.get("policy_description", ""),
    )

    user_prompt = (
        f"Analyze which privileges should be granted to role '{state['role'].name}'."
    )
    if state.get("policy_description"):
        user_prompt += f"\n\nPolicy Context:\n{state['policy_description']}"

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    response = llm.invoke(messages)
    content = (
        response.content if isinstance(response.content, str) else str(response.content)
    )
    explanation, parsed_data = extract_explanation_and_json(content)

    if not parsed_data:
        retry_prompt = build_single_role_to_scopes_retry_prompt(
            state["role"],
            state["privileges"],
        )
        retry_messages = [*messages, response, HumanMessage(content=retry_prompt)]
        retry_response = llm.invoke(retry_messages)
        retry_content = (
            retry_response.content
            if isinstance(retry_response.content, str)
            else str(retry_response.content)
        )
        explanation, parsed_data = extract_explanation_and_json(retry_content)

        if not parsed_data:
            raise ValueError(
                f"Failed to parse valid JSON from LLM response after retry.\n"
                f"Last response: {retry_content[:500]}..."
            )

    granted_privileges = (
        parsed_data.get("granted_privileges", []) if isinstance(parsed_data, dict) else []
    )

    if granted_privileges:
        print_explanation(explanation, verbose=verbose)

    return {
        **state,
        "explanation": explanation,
        "granted_privileges": [p for p in state["privileges"] if p.name in granted_privileges],
        "messages": [*state.get("messages", []), response],
        "errors": [],
        "retry_count": state.get("retry_count", 0),
        "validation_passed": True,
    }


# ============================================================================
# GRAPH CONSTRUCTION
# ============================================================================

def create_single_role_mapper_graph(config: MapperConfig):
    """Build and compile the single role scope mapper graph."""

    def analyze_role_scopes_node(state: SingleRoleState) -> dict[str, Any]:
        return _analyze_role_scopes(state, config.llm, config.verbose)

    def validate_role_scopes_node(state: SingleRoleState) -> dict[str, Any]:
        return validate_mapping_items(
            state, config.verbose, config.max_retries,
            items_key="granted_privileges",
            reference_key="privileges",
            item_type_label="privilege",
        )

    def verify_semantic_scope_mapping_node(state: SingleRoleState) -> dict[str, Any]:
        return verify_semantic_mapping(
            state=state,
            llm=config.llm,
            verbose=config.verbose,
            max_retries=config.max_retries,
            subject_name=state["role"].name,
            verification_prompt=build_single_role_to_scopes_verification_prompt(
                policy_description=state.get("policy_description", ""),
                role=state["role"],
                privileges=state["privileges"],
                granted_privileges=state.get("granted_privileges", []),
            ),
            mapped_items=state.get("granted_privileges", []),
        )

    def should_route_after_structure_node(state: SingleRoleState) -> str:
        return should_route_after_structural_validation(
            validation_passed=state.get("validation_passed", False),
            retry_count=state.get("retry_count", 0),
            max_retries=config.max_retries,
            analyze_node="analyze_role_scopes",
            verify_node="verify_semantic_scope_mapping",
        )

    def should_retry_after_semantic_node(state: SingleRoleState) -> str:
        return should_retry_after_semantic(
            validation_passed=state.get("validation_passed", False),
            retry_count=state.get("retry_count", 0),
            max_retries=config.max_retries,
            analyze_node="analyze_role_scopes",
        )

    workflow = StateGraph(SingleRoleState)

    workflow.add_node("analyze_role_scopes", analyze_role_scopes_node)
    workflow.add_node("validate_role_scopes", validate_role_scopes_node)
    workflow.add_node("verify_semantic_scope_mapping", verify_semantic_scope_mapping_node)

    workflow.set_entry_point("analyze_role_scopes")
    workflow.add_edge("analyze_role_scopes", "validate_role_scopes")

    workflow.add_conditional_edges(
        "validate_role_scopes",
        should_route_after_structure_node,
        {
            "analyze_role_scopes": "analyze_role_scopes",
            "verify_semantic_scope_mapping": "verify_semantic_scope_mapping",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "verify_semantic_scope_mapping",
        should_retry_after_semantic_node,
        {
            "analyze_role_scopes": "analyze_role_scopes",
            END: END,
        },
    )

    return workflow.compile()


# ============================================================================
# MAIN CLASS
# ============================================================================

class SingleRoleMapper(BaseSingleMapper):
    """
    AI-powered mapper for determining which privileges a realm role should hold.

    Given a natural language policy description and a realm role, produces a
    PolicyObjectModel with the realm-role → privilege mappings for that role.

    Workflow:
        1. analyze_role_scopes          — LLM determines which privileges the role gets
        2. validate_role_scopes         — structural validation with retry
        3. verify_semantic_scope_mapping — LLM cross-checks the assignment
    """

    def __init__(
        self,
        role: Role,
        privileges: list[Scope],
        llm: BaseChatModel,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ):
        self.role: Role = role
        self.privileges: list[Scope] = privileges
        super().__init__(llm=llm, verbose=verbose, max_retries=max_retries)

    def _create_graph(self, config: MapperConfig) -> CompiledStateGraph:
        return create_single_role_mapper_graph(config)

    def _run(self, policy_description: str) -> dict[str, Any]:
        initial_state: SingleRoleState = {
            "policy_description": policy_description,
            "role": self.role,
            "privileges": self.privileges,
            "explanation": "",
            "granted_privileges": [],
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True,
        }

        final_state = self.graph.invoke(initial_state)

        return {
            "policy_description": policy_description,
            "role": self.role,
            "granted_privileges": final_state["granted_privileges"],
            "explanation": final_state["explanation"],
            "errors": final_state["errors"],
            "success": len(final_state["errors"]) == 0,
            "retry_count": final_state.get("retry_count", 0),
        }

    def _build_rules(self, result: dict[str, Any]) -> list[Rule]:
        return [Rule(role=self.role, scope=priv) for priv in result.get("granted_privileges", [])]

    def map_scopes(self, policy_description: str) -> dict[str, Any]:
        """
        Determine which privileges a realm role should be granted.

        Returns a dict with granted_privileges, explanation, errors, success,
        and retry_count.
        """
        return self._run(policy_description=policy_description)


if __name__ == "__main__":
    print("Use SingleRoleMapper programmatically or via the CLI.")
    sys.exit(1)
