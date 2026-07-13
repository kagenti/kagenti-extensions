"""
Single Privilege Mapper

Maps a single privilege to the realm roles that should have access to it.
Uses a LangGraph workflow that analyzes, validates, and semantically verifies
the mapping before returning a PolicyObjectModel.

Workflow:
    1. analyze_priviledge_roles  — LLM determines which roles get access.
    2. validate_priviledge_roles — structural validation with retry.
    3. verify_semantic_mapping — LLM cross-checks the assignment against the policy.
"""

import sys
from typing import Any

from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from aiac.policy.model.models import PolicyRule, Role, Scope
from .state import SinglePrivilegeState
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
from prompts.single_role_prompt_builder import (
    build_single_privilege_to_roles_system_prompt,
    build_single_privilege_to_roles_retry_prompt,
    build_single_privilege_to_roles_verification_prompt,
)


# ============================================================================
# PURE NODE FUNCTIONS
# ============================================================================

def _analyze_priviledge_roles(
    state: SinglePrivilegeState,
    llm: BaseChatModel,
    verbose: bool,
) -> dict[str, Any]:
    """
    Analyze which roles should have access to the privilege.

    First node in the workflow. Sends the privilege, available realm roles,
    and policy context to the LLM for semantic analysis.
    """
    system_prompt = build_single_privilege_to_roles_system_prompt(
        state["privilege"],
        state["roles"],
        state.get("policy_description", ""),
    )

    user_prompt = (
        f"Analyze which roles should have access to the privilege '{state['privilege'].name}'."
    )
    if state.get("policy_description"):
        user_prompt += f"\n\nPolicy Context:\n{state['policy_description']}"

    prior_errors = state.get("errors", [])
    if prior_errors:
        user_prompt += (
            "\n\nPREVIOUS ATTEMPT FEEDBACK — your last mapping was rejected. "
            "Read the feedback carefully and correct the mapping:\n"
            + "\n".join(f"  - {e}" for e in prior_errors)
        )

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
        retry_prompt = build_single_privilege_to_roles_retry_prompt(
            state["privilege"],
            state["roles"],
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

    roles_with_access = (
        parsed_data.get("roles_with_access", []) if isinstance(parsed_data, dict) else []
    )

    if roles_with_access:
        print_explanation(explanation, verbose=verbose)

    return {
        **state,
        "explanation": explanation,
        "roles_with_access": [r for r in state["roles"] if r.name in roles_with_access],
        "messages": [*state.get("messages", []), response],
        "errors": [],
        "retry_count": state.get("retry_count", 0),
        "validation_passed": True,
    }


# ============================================================================
# GRAPH CONSTRUCTION
# ============================================================================

def create_single_privilege_mapper_graph(config: MapperConfig):
    """Build and compile the single privilege mapper graph."""

    def analyze_role_mapping_node(state: SinglePrivilegeState) -> dict[str, Any]:
        return _analyze_priviledge_roles(state, config.llm, config.verbose)

    def validate_role_mapping_node(state: SinglePrivilegeState) -> dict[str, Any]:
        return validate_mapping_items(
            state, config.verbose, config.max_retries,
            items_key="roles_with_access",
            reference_key="roles",
            item_type_label="role",
        )

    def verify_semantic_mapping_node(state: SinglePrivilegeState) -> dict[str, Any]:
        return verify_semantic_mapping(
            state=state,
            llm=config.llm,
            verbose=config.verbose,
            max_retries=config.max_retries,
            subject_name=state["privilege"].name,
            verification_prompt=build_single_privilege_to_roles_verification_prompt(
                policy_description=state.get("policy_description", ""),
                privilege=state["privilege"],
                roles=state["roles"],
                roles_with_access=state.get("roles_with_access", []),
            ),
            mapped_items=state.get("roles_with_access", []),
        )

    def should_route_after_structure_node(state: SinglePrivilegeState) -> str:
        return should_route_after_structural_validation(
            validation_passed=state.get("validation_passed", False),
            retry_count=state.get("retry_count", 0),
            max_retries=config.max_retries,
            analyze_node="analyze_role_mapping",
            verify_node="verify_semantic_mapping",
        )

    def should_retry_after_semantic_node(state: SinglePrivilegeState) -> str:
        return should_retry_after_semantic(
            validation_passed=state.get("validation_passed", False),
            retry_count=state.get("retry_count", 0),
            max_retries=config.max_retries,
            analyze_node="analyze_role_mapping",
        )

    workflow = StateGraph(SinglePrivilegeState)

    workflow.add_node("analyze_role_mapping", analyze_role_mapping_node)
    workflow.add_node("validate_role_mapping", validate_role_mapping_node)
    workflow.add_node("verify_semantic_mapping", verify_semantic_mapping_node)

    workflow.set_entry_point("analyze_role_mapping")
    workflow.add_edge("analyze_role_mapping", "validate_role_mapping")

    workflow.add_conditional_edges(
        "validate_role_mapping",
        should_route_after_structure_node,
        {
            "analyze_role_mapping": "analyze_role_mapping",
            "verify_semantic_mapping": "verify_semantic_mapping",
            END: END,
        },
    )

    workflow.add_conditional_edges(
        "verify_semantic_mapping",
        should_retry_after_semantic_node,
        {
            "analyze_role_mapping": "analyze_role_mapping",
            END: END,
        },
    )

    return workflow.compile()


# ============================================================================
# MAIN CLASS
# ============================================================================

class SinglePrivilegeMapper(BaseSingleMapper):
    """
    AI-powered mapper for determining which roles should have access to a
    single privilege.
s
    Given a natural language policy description and a privilege, produces a
    PolicyObjectModel with the realm-role → privilege mapping for that privilege.

    Workflow:
        1. analyze_role_mapping  — LLM determines which roles get access
        2. validate_role_mapping — structural validation with retry
        3. verify_semantic_mapping — LLM cross-checks the assignment
    """

    def __init__(
        self,
        privilege: Scope,
        roles: list[Role],
        llm: BaseChatModel,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ):
        self.privilege: Scope = privilege
        self.roles: list[Role] = roles
        super().__init__(llm=llm, verbose=verbose, max_retries=max_retries)

    def _create_graph(self, config: MapperConfig) -> CompiledStateGraph[SinglePrivilegeState, None, SinglePrivilegeState, SinglePrivilegeState]:
        return create_single_privilege_mapper_graph(config)

    def _run(self, policy_description: str) -> dict[str, Any]:
        initial_state: SinglePrivilegeState = {
            "policy_description": policy_description,
            "privilege": self.privilege,
            "roles": self.roles,
            "explanation": "",
            "roles_with_access": [],
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True,
        }

        final_state = self.graph.invoke(initial_state)

        return {
            "policy_description": policy_description,
            "privilege": self.privilege,
            "roles_with_access": final_state["roles_with_access"],
            "explanation": final_state["explanation"],
            "errors": final_state["errors"],
            "success": len(final_state["errors"]) == 0,
            "retry_count": final_state.get("retry_count", 0),
        }

    def _build_rules(self, result: dict[str, Any]) -> list[PolicyRule]:
        return [PolicyRule(role=role, scope=self.privilege) for role in result.get("roles_with_access", [])]

    def map_roles(self, policy_description: str) -> dict[str, Any]:
        """
        Determine which roles should have access to the privilege.

        Returns a dict with roles_with_access, explanation, errors, success,
        and retry_count.
        """
        return self._run(policy_description=policy_description)

if __name__ == "__main__":
    print("Use SinglePrivilegeMapper programmatically or via the CLI.")
    sys.exit(1)
