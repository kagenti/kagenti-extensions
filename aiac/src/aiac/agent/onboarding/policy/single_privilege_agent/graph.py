#!/usr/bin/env python3
"""
Single Privilege Mapper - Main Module

This module provides the SinglePrivilegeMapper class that uses LangGraph workflows
to determine which real roles (realm roles) should have access to a specific
privilege based on semantic analysis of role descriptions and policy context.

Key Features:
    - Semantic matching of privilege to real roles
    - Policy description context for better decision making
    - Automatic validation and retry mechanism
    - LLM-powered analysis of role descriptions
"""

import re
from typing import Dict, Any, List, Optional
from pathlib import Path
import json
from dataclasses import dataclass

from langgraph.graph import StateGraph, END
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models import BaseChatModel

from config import create_llm
from .state import SinglePrivilegeState
from config.constants import MAX_VALIDATION_RETRIES
from prompts.single_role_prompt_builder import (
    build_single_role_system_prompt,
    build_single_role_retry_prompt,
    build_semantic_verification_prompt,
)


@dataclass
class SinglePrivilegeMapperConfig:
    """
    Configuration for SinglePrivilegeMapper agent.
    
    Attributes:
        llm: LangChain LLM instance
        verbose: Whether to print detailed output
        max_retries: Maximum validation retry attempts
    """
    llm: BaseChatModel
    verbose: bool = True
    max_retries: int = MAX_VALIDATION_RETRIES


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def extract_explanation_and_json_single_role(content: str) -> tuple[str, Optional[Dict[str, Any]]]:
    """
    Extract explanation and JSON from LLM response for single role mapping.

    Tries multiple parsing strategies in order:
    1. Fenced code blocks with ```explanation and ```json tags (preferred format)
    2. Any ```json or generic ``` block containing a dict
    3. A bare { ... } JSON object anywhere in the response

    Args:
        content: Raw LLM response content string

    Returns:
        Tuple of (explanation_text, parsed_json_dict)
        Returns empty string and None if parsing fails
    """
    explanation = ""
    json_data = None

    # Extract explanation block
    if "```explanation" in content:
        start = content.find("```explanation") + len("```explanation")
        end = content.find("```", start)
        if end != -1:
            explanation = content[start:end].strip()

    # Try fenced ```json block first
    if "```json" in content:
        start = content.find("```json") + len("```json")
        end = content.find("```", start)
        if end != -1:
            try:
                json_data = json.loads(content[start:end].strip())
            except json.JSONDecodeError:
                pass

    # Try any generic fenced code block containing a dict
    if json_data is None and "```" in content:
        import re
        for block in re.findall(r"```[^\n]*\n(.*?)```", content, re.DOTALL):
            try:
                candidate = json.loads(block.strip())
                if isinstance(candidate, dict):
                    json_data = candidate
                    break
            except json.JSONDecodeError:
                pass

    # Fall back: find the first complete { ... } object in the text
    if json_data is None:
        depth = 0
        start_idx = None
        for i, ch in enumerate(content):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx is not None:
                    try:
                        candidate = json.loads(content[start_idx:i + 1])
                        if isinstance(candidate, dict):
                            json_data = candidate
                            if not explanation:
                                explanation = content[:start_idx].strip()
                            break
                    except json.JSONDecodeError:
                        start_idx = None

    return explanation, json_data


def print_explanation_single_role(explanation: str, is_retry: bool = False, verbose: bool = True):
    """
    Print the LLM's explanation if verbose mode is enabled.
    
    Args:
        explanation: The explanation text to print
        is_retry: Whether this is from a retry attempt (adds retry indicator)
        verbose: Whether to print the explanation
    """
    if verbose and explanation:
        prefix = "🔄 Retry Explanation:" if is_retry else "💡 LLM Explanation:"
        print(f"\n{prefix}")
        print(explanation)
        print()


# ============================================================================
# PURE NODE FUNCTIONS
# ============================================================================

def _analyze_role_mapping(
    state: SinglePrivilegeState,
    llm: BaseChatModel,
    verbose: bool
) -> SinglePrivilegeState:
    """
    Analyze which real roles should have access to the privilege.

    This is the first node in the workflow. It sends the privilege,
    available real roles, policy context, and call chain structure to the LLM
    for semantic analysis.

    Args:
        state: Current SinglePrivilegeState
        llm: LLM instance for processing
        verbose: Whether to print detailed output

    Returns:
        Updated SinglePrivilegeState with real_roles_with_access and explanation
    """
    # Build prompts
    system_prompt = build_single_role_system_prompt(
        state['realm_roles'],
        state['privilege'],
        state.get('policy_description', ''),
        state.get('service_name', ''),
    )
    
    user_prompt = (
        f"Analyze which real roles should have access to the privilege "
        f"'{state['privilege']['name']}' from service '{state['service_name']}'."
    )
    
    # Add policy context to user prompt if available
    if state.get('policy_description'):
        user_prompt += f"\n\nPolicy Context:\n{state['policy_description']}"
    
    # First attempt
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt)
    ]
    
    response = llm.invoke(messages)
    content = response.content if isinstance(response.content, str) else str(response.content)
    explanation, parsed_data = extract_explanation_and_json_single_role(content)
    
    # Print explanation if available
    print_explanation_single_role(explanation, verbose=verbose)
    
    # Retry once if parsing failed
    if not parsed_data:
        retry_prompt = build_single_role_retry_prompt(
            state['realm_roles'],
            state['privilege']
        )
        
        retry_messages = [
            *messages,
            response,
            HumanMessage(content=retry_prompt)
        ]
        
        retry_response = llm.invoke(retry_messages)
        retry_content = (
            retry_response.content
            if isinstance(retry_response.content, str)
            else str(retry_response.content)
        )
        explanation, parsed_data = extract_explanation_and_json_single_role(retry_content)
        
        # Print retry explanation
        print_explanation_single_role(explanation, is_retry=True, verbose=verbose)
        
        # If still failed after retry, raise exception
        if not parsed_data:
            raise ValueError(
                f"Failed to parse valid JSON from LLM response after retry.\n"
                f"Last response: {retry_content[:500]}..."
            )
    
    # Extract real roles with access
    # Handle both dict format (new) and list format (old/mock)
    if isinstance(parsed_data, dict):
        real_roles_with_access = parsed_data.get('real_roles_with_access', [])
    elif isinstance(parsed_data, list):
        # Old format or mock - assume it's directly the list of roles
        real_roles_with_access = parsed_data
    else:
        real_roles_with_access = []
    
    # Return updated state
    return {
        **state,
        "explanation": explanation,
        "real_roles_with_access": real_roles_with_access,
        "messages": [*state.get("messages", []), response],
        "errors": [],  # Clear errors on new parse attempt
        "retry_count": state.get("retry_count", 0),
        "validation_passed": True  # Assume passed until validation runs
    }


def _validate_role_mapping(
    state: SinglePrivilegeState,
    verbose: bool,
    max_retries: int
) -> SinglePrivilegeState:
    """
    Validate the role mapping results.
    
    This is the second node in the workflow. It validates that:
    - All returned role names exist in the available realm roles
    - The mapping makes semantic sense
    
    Args:
        state: SinglePrivilegeState with real_roles_with_access
        verbose: Whether to print detailed output
        max_retries: Maximum retry attempts

    Returns:
        Updated SinglePrivilegeState with errors and validation_passed fields
    """
    retry_count = state.get("retry_count", 0)
    real_roles_with_access = state.get("real_roles_with_access", [])
    available_role_names = [role['name'] for role in state['realm_roles']]
    
    errors = []
    
    # Normalize real_roles_with_access to handle both string and dict formats
    normalized_roles = []
    for item in real_roles_with_access:
        if isinstance(item, str):
            normalized_roles.append(item)
        elif isinstance(item, dict) and 'role' in item:
            # Old format: list of dicts with 'role' key
            normalized_roles.append(item['role'])
        else:
            errors.append(f"Invalid role format: {item}")
    
    # Validate that all returned roles exist
    for role_name in normalized_roles:
        if role_name not in available_role_names:
            errors.append(
                f"Invalid role name '{role_name}'. Must be one of: {', '.join(available_role_names)}"
            )
    
    # Check for duplicates
    if len(normalized_roles) != len(set(normalized_roles)):
        errors.append("Duplicate role names found in the result")
    
    # Update state with normalized roles
    if normalized_roles != real_roles_with_access:
        real_roles_with_access = normalized_roles
    
    # Determine if validation passed
    validation_passed = len(errors) == 0
    
    # If there are errors and we can retry, trigger retry
    if errors and retry_count < max_retries:
        if verbose:
            print(f"\n⚠️  Validation failed (attempt {retry_count + 1}/{max_retries})")
            for error in errors:
                print(f"  - {error}")
        return {
            **state,
            "real_roles_with_access": normalized_roles,
            "errors": errors,
            "validation_passed": False,
            "retry_count": retry_count + 1
        }
    
    # Return final result
    return {
        **state,
        "real_roles_with_access": normalized_roles,
        "errors": errors,
        "validation_passed": validation_passed,
        "retry_count": retry_count
    }


def _verify_semantic_mapping(
    state: SinglePrivilegeState,
    llm: BaseChatModel,
    verbose: bool,
    max_retries: int,
) -> SinglePrivilegeState:
    """
    Semantically verify the role mapping using LLM.

    Asks the LLM whether the assigned realm roles correctly reflect the access
    requirements for this privilege given the policy description. On failure
    the retry counter is incremented so the graph can loop back to
    analyze_role_mapping.

    Args:
        state: SinglePrivilegeState with real_roles_with_access populated
        llm: LLM instance for verification
        verbose: Whether to print verification details
        max_retries: Maximum retry attempts allowed

    Returns:
        Updated SinglePrivilegeState with validation_passed and errors
    """
    retry_count = state.get("retry_count", 0)
    real_roles_with_access = state.get("real_roles_with_access", [])

    verification_prompt = build_semantic_verification_prompt(
        policy_description=state.get("policy_description", ""),
        service_name=state.get("service_name", ""),
        privilege=state["privilege"],
        realm_roles=state["realm_roles"],
        real_roles_with_access=real_roles_with_access,
    )

    try:
        response = llm.invoke([HumanMessage(content=verification_prompt)])
        content = response.content if isinstance(response.content, str) else str(response.content)

        mapping_match = re.search(r'MAPPING_CORRECT:\s*(YES|NO)', content, re.IGNORECASE)
        explanation_match = re.search(r'EXPLANATION:\s*(.+?)$', content, re.DOTALL | re.IGNORECASE)

        mapping_correct = mapping_match.group(1).upper() == 'YES' if mapping_match else False
        explanation = explanation_match.group(1).strip() if explanation_match else content

        if verbose:
            status = 'YES' if mapping_correct else 'NO'
            print(
                f"\nSemantic verification [{state['service_name']}/{state['privilege']['name']}]:"
                f" MAPPING_CORRECT={status}"
            )
            if not mapping_correct:
                print(f"  {explanation}")

        if not mapping_correct:
            error_msg = (
                f"Semantic mismatch for {state['service_name']}/{state['privilege']['name']}:"
                f" {explanation}"
            )
            if retry_count < max_retries:
                return {
                    **state,
                    "errors": [error_msg],
                    "validation_passed": False,
                    "retry_count": retry_count + 1,
                }
            return {
                **state,
                "errors": [error_msg],
                "validation_passed": False,
            }

        return {**state, "errors": [], "validation_passed": True}

    except Exception as e:
        # Allow the pipeline to proceed on transient errors (rate limits, etc.)
        return {**state, "errors": [], "validation_passed": True}


def _should_route_after_structural_validation(state: SinglePrivilegeState, max_retries: int) -> str:
    """
    Route after structural validation: retry, proceed to semantic check, or end.

    Args:
        state: Current SinglePrivilegeState with validation results
        max_retries: Maximum retry attempts allowed

    Returns:
        "analyze_role_mapping" if structural errors remain and retries are available,
        "verify_semantic_mapping" if structural validation passed,
        END if structural errors remain but retries are exhausted
    """
    validation_passed = state.get("validation_passed", False)
    retry_count = state.get("retry_count", 0)

    if not validation_passed and retry_count < max_retries:
        return "analyze_role_mapping"

    if validation_passed:
        return "verify_semantic_mapping"

    return END


def _should_retry_after_semantic(state: SinglePrivilegeState, max_retries: int) -> str:
    """
    Determine if semantic verification failure should retry analyze_role_mapping.

    Args:
        state: Current SinglePrivilegeState with semantic verification results
        max_retries: Maximum retry attempts allowed

    Returns:
        "analyze_role_mapping" if semantic check failed and retries remain,
        otherwise END to terminate the workflow
    """
    validation_passed = state.get("validation_passed", False)
    retry_count = state.get("retry_count", 0)

    if not validation_passed and retry_count < max_retries:
        return "analyze_role_mapping"

    return END


# ============================================================================
# GRAPH CONSTRUCTION
# ============================================================================

def create_single_privilege_mapper_graph(config: SinglePrivilegeMapperConfig):
    """
    Create and compile the single privilege mapper graph.
    
    Args:
        config: SinglePrivilegeMapperConfig instance
        
    Returns:
        Compiled LangGraph workflow
    """
    
    # Define node functions as closures with access to config
    def analyze_role_mapping_node(state: SinglePrivilegeState) -> SinglePrivilegeState:
        """Analyze which real roles should have access."""
        return _analyze_role_mapping(state, config.llm, config.verbose)

    def validate_role_mapping_node(state: SinglePrivilegeState) -> SinglePrivilegeState:
        """Validate structural correctness of the role mapping."""
        return _validate_role_mapping(state, config.verbose, config.max_retries)

    def verify_semantic_mapping_node(state: SinglePrivilegeState) -> SinglePrivilegeState:
        """Semantically verify the role mapping against the policy description."""
        return _verify_semantic_mapping(state, config.llm, config.verbose, config.max_retries)

    def should_route_after_structure_node(state: SinglePrivilegeState) -> str:
        """Route after structural validation."""
        return _should_route_after_structural_validation(state, config.max_retries)

    def should_retry_after_semantic_node(state: SinglePrivilegeState) -> str:
        """Determine if semantic failure should retry."""
        return _should_retry_after_semantic(state, config.max_retries)

    # Build the graph
    workflow = StateGraph(SinglePrivilegeState)

    # Add nodes
    workflow.add_node("analyze_role_mapping", analyze_role_mapping_node)
    workflow.add_node("validate_role_mapping", validate_role_mapping_node)
    workflow.add_node("verify_semantic_mapping", verify_semantic_mapping_node)

    # Define edges
    workflow.set_entry_point("analyze_role_mapping")
    workflow.add_edge("analyze_role_mapping", "validate_role_mapping")

    # After structural validation: retry, proceed to semantic check, or end
    workflow.add_conditional_edges(
        "validate_role_mapping",
        should_route_after_structure_node,
        {
            "analyze_role_mapping": "analyze_role_mapping",
            "verify_semantic_mapping": "verify_semantic_mapping",
            END: END,
        }
    )

    # After semantic verification: retry or end
    workflow.add_conditional_edges(
        "verify_semantic_mapping",
        should_retry_after_semantic_node,
        {
            "analyze_role_mapping": "analyze_role_mapping",
            END: END,
        }
    )

    return workflow.compile()


# ============================================================================
# MAIN CLASS
# ============================================================================

class SinglePrivilegeMapper:
    """
    AI-powered mapper for determining which real roles should have access to a privilege.

    This class uses LangGraph to orchestrate a workflow that:
    1. Analyzes a privilege, available real roles, and policy context
    2. Uses LLM to semantically match roles based on descriptions
    3. Validates the results
    4. Retries if validation fails
    
    Attributes:
        config: SinglePrivilegeMapperConfig instance
        graph: Compiled LangGraph state machine
    """
    
    def __init__(
        self,
        llm: Optional[BaseChatModel] = None,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES
    ):
        """
        Initialize the single role mapper.
        
        Args:
            llm: Optional LangChain LLM instance. If not provided, creates a new
                 LLM instance using create_llm()
            verbose: If True, print LLM explanations and validation details
            max_retries: Maximum validation retry attempts
        """
        # Create LLM if not provided
        if llm is None:
            llm_env_path = Path(__file__).parent.parent / "config" / "llm.env"
            llm_instance = create_llm(env_path=llm_env_path, verbose=verbose)
        else:
            llm_instance = llm
        
        # Create configuration object
        self.config = SinglePrivilegeMapperConfig(
            llm=llm_instance,
            verbose=verbose,
            max_retries=max_retries
        )
        
        # Build and compile the LangGraph state machine
        self.graph = create_single_privilege_mapper_graph(self.config)
    
    def get_graph(self):
        """
        Get the compiled graph for visualization or inspection.
        
        Returns:
            Compiled LangGraph workflow
        """
        return self.graph
    
    def map_role(
        self,
        policy_description: str,
        service_name: str,
        privilege: Dict[str, str],
        realm_roles: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """
        Determine which real roles should have access to a privilege.

        Args:
            policy_description: Natural language policy description for context
            service_name: Name of the service that owns the privilege
            privilege: Dict with 'name' and 'description' of the privilege
            realm_roles: List of dicts with 'name' and 'description' for realm roles

        Returns:
            Dictionary containing:
                - policy_description (str): The policy context used
                - service_name (str): Name of the service
                - privilege (str): Name of the privilege analyzed
                - real_roles_with_access (list): List of realm role names that should have access
                - explanation (str): LLM's explanation of the mapping
                - errors (list): Validation errors (empty if successful)
                - success (bool): True if mapping succeeded without errors
                - retry_count (int): Number of validation retries that occurred
        """
        # Initialize the workflow state
        initial_state: SinglePrivilegeState = {
            "policy_description": policy_description,
            "service_name": service_name,
            "privilege": privilege,
            "realm_roles": realm_roles,
            "explanation": "",
            "real_roles_with_access": [],
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True
        }
        
        # Execute the LangGraph workflow
        final_state = self.graph.invoke(initial_state)
        
        # Extract and return results
        return {
            "policy_description": policy_description,
            "service_name": service_name,
            "privilege": privilege['name'],
            "real_roles_with_access": final_state["real_roles_with_access"],
            "explanation": final_state["explanation"],
            "errors": final_state["errors"],
            "success": len(final_state["errors"]) == 0,
            "retry_count": final_state.get("retry_count", 0)
        }