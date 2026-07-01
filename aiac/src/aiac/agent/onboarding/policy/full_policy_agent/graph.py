"""
Policy Builder - Main Module

This module provides the main PolicyBuilder class that orchestrates the
AI-powered generation of Keycloak access control policies from natural
language descriptions using LangGraph workflows.

Refactored to follow official LangGraph patterns:
- Separation of graph definition from business logic
- Pure node functions for better testability
- Proper type hints and annotations
- Configuration as a separate concern
- Support for graph visualization

The PolicyBuilder has been refactored into multiple modules for better
organization and maintainability:
- state.py: State definitions
- config_utils.py: Configuration loading and parsing
- constants.py: Constants
- prompt_builder.py: LLM prompt construction
- parsers.py: Response parsing utilities
- validators.py: Policy validation logic
- cli.py: Command-line interface

Key Features:
    - Natural language to YAML policy conversion
    - Automatic role mapping and validation
    - Call chain analysis and enforcement
    - Retry mechanism with semantic verification
"""

from aiac.idp.configuration.models import Role, Service
from aiac.pdp.policy.models import PolicyObjectModel, Rule
from typing import Optional, Dict
import sys
from dataclasses import dataclass

from langgraph.graph import StateGraph, END
from langchain_core.language_models import BaseChatModel

from full_policy_agent.state import PolicyState
from config.constants import MAX_VALIDATION_RETRIES
from aiac.idp.configuration.api import Configuration
from single_privilege_agent import SinglePrivilegeMapper
from utils.validators import validate_policy_structure
from aiac.pdp.policy.builders.yaml import _generate_yaml_output


@dataclass
class PolicyBuilderConfig:
    """
    Configuration for PolicyBuilder agent.

    Following LangGraph best practices, configuration is separated from
    the agent logic for better testability and flexibility.

    Attributes:
        llm: LangChain LLM instance
        verbose: Whether to print detailed output
        max_retries: Maximum validation retry attempts
    """
    llm: BaseChatModel
    verbose: bool = True
    max_retries: int = MAX_VALIDATION_RETRIES


# ============================================================================
# PURE NODE FUNCTIONS (Following LangGraph Best Practices)
# ============================================================================
# These functions are pure and stateless, making them easier to test and reason about

def _build_policy(
    state: PolicyState,
    llm: BaseChatModel,
    roles: list[Role],
    privileges_map: dict,
    verbose: bool
    ) -> PolicyState:
    """
    Map each privilege to realm roles using SingleRoleMapper, then aggregate
    results into the parsed_scopes format expected by _build_policy.

    For every privilege across all services, SingleRoleMapper determines which
    realm roles should have access. The per-role results are inverted so that
    parsed_scopes is a list of {role: realm_role, privileges: [...]}.

    The 'service' key in each privilege dict holds a Service object (not a string)
    so that _build_policy can construct a typed Policy directly.

    Args:
        state: Current PolicyState with 'description' field
        llm: LLM instance for processing
        realm_roles: List of available realm roles [{'name': str, 'description': str}]
        privileges_map: Dict mapping service names to privileges
        verbose: Whether to print detailed output
        services_by_name: Mapping of service name → Service object

    Returns:
        Updated PolicyState with parsed_scopes and explanation
    """

    explanations = []
    # realm_role_name -> list of {"service": Service, "privilege": str} dicts
    policy_rules: list[Rule] = []

    for _ , service_info in privileges_map.items():
        for privilege in service_info["scopes"]:
            mapper = SinglePrivilegeMapper(
                llm=llm, 
                verbose=verbose, 
                roles=roles, 
                privilege=privilege)

            result = mapper.generate_policy(description=state['description'])

            explanations.append(
                f"**{privilege.name}**: {result.explanation}"
            )

            policy_rules.extend(result.rules)
    policy = PolicyObjectModel(
        rules=policy_rules, 
        explanation = "\n\n".join(explanations) if explanations else ""
        )

    return PolicyState(
        description=state["description"],
        policy=policy,
        messages=[],
        errors=[],
        retry_count=state.get("retry_count", 0),
        validation_passed=True,
    )

def _validate_policy(
    state: PolicyState,
    realm_roles: list,
    privileges_map: dict,
    max_retries: int
) -> PolicyState:
    """
    Validate the generated Policy model against structural rules.

    Converts the Policy back to a raw dict for validate_policy_structure,
    which expects {realm_role: [{"service": str, "privilege": str}]}.

    Args:
        state: PolicyState with 'policy' as a Policy instance
        llm: LLM instance (reserved for future semantic verification)
        realm_roles: List of available realm roles
        service_names: List of service names
        privileges_map: Dict mapping service names to privileges
        verbose: Whether to print detailed output
        max_retries: Maximum retry attempts

    Returns:
        Updated PolicyState with errors and validation_passed fields
    """
    retry_count = state.get("retry_count", 0)
    policy_obj: Optional[PolicyObjectModel] = state.get("policy")

    structural_errors = validate_policy_structure(
        policy_obj,
        realm_roles,
        privileges_map
    )

    if structural_errors and retry_count < max_retries:
        return {
            **state,
            "errors": structural_errors,
            "validation_passed": False,
            "retry_count": retry_count + 1
        }

    return {
        **state,
        "errors": structural_errors,
        "validation_passed": len(structural_errors) == 0,
        "retry_count": retry_count
    }


def _should_retry_validation(state: PolicyState, max_retries: int) -> str:
    """
    Determine if validation should retry by going back to build_policy_node.
    
    This is a conditional edge function for the LangGraph state machine.
    
    Args:
        state: Current PolicyState containing validation results
        max_retries: Maximum retry attempts allowed
        
    Returns:
        "build_policy_node" if validation failed and retries remain,
        otherwise END to terminate the workflow
    """
    validation_passed = state.get("validation_passed", False)
    retry_count = state.get("retry_count", 0)
    errors = state.get("errors", [])
    
    # If validation failed and we haven't exceeded max retries, retry from start
    if not validation_passed and retry_count < max_retries:
        print(f"\n⚠️  Validation failed (attempt {retry_count}/{max_retries}). Retrying from build_policy_node...")
        if errors:
            print(f"\nValidation Errors (from this attempt):")
            for i, error in enumerate(errors, 1):
                print(f"  {i}. {error}")
            print()
        return "build_policy"
    
    # Either validation passed or max retries exceeded
    return END


def create_policy_builder_graph(
    config: PolicyBuilderConfig,
    roles: list[Role],
    privileges_map: dict,
):
    """
    Create and compile the policy builder graph.

    Args:
        config: PolicyBuilderConfig instance
        roles: List of available realm roles
        privileges_map: Dict mapping service names to privileges

    Returns:
        Compiled LangGraph workflow
    """

    def build_policy_node(state: PolicyState) -> PolicyState:
        return _build_policy(
            state, config.llm, roles, privileges_map, config.verbose
        )
    
    def validate_policy_node(state: PolicyState) -> PolicyState:
        return _validate_policy(
            state, roles, 
            privileges_map, config.max_retries
        )

    def should_retry_node(state: PolicyState) -> str:
        return _should_retry_validation(state, config.max_retries)
    
    # Build the graph
    workflow = StateGraph(PolicyState)
    
    # Add nodes
    workflow.add_node("build_policy", build_policy_node)
    workflow.add_node("validate_policy", validate_policy_node)
    
    # Define edges
    workflow.set_entry_point("build_policy")
    workflow.add_edge("build_policy", "validate_policy")
    
    # Add conditional edge for retry logic
    workflow.add_conditional_edges(
        "validate_policy",
        should_retry_node,
        {
            "build_policy": "build_policy",
            END: END
        }
    )
    
    return workflow.compile()


class PolicyBuilder:
    """
    AI-powered access control policy builder using LangGraph.
    
    Refactored to follow official LangGraph patterns:
    - Configuration separated from logic
    - Graph construction delegated to factory function
    - Pure node functions for better testability
    - Support for graph visualization
    
    This class orchestrates a multi-stage workflow to convert natural language
    policy descriptions into structured YAML access control policies.
    
    Workflow Stages:
        1. build_policy: Parse natural language and extract role mappings, and build structured policy from mappings
        2. validate_policy: Validate structure and semantics (with retry)
    
    Attributes:
        config: PolicyBuilderConfig instance
        realm_roles: List of available realm role names
        privileges_map: Dict mapping service names to their available privileges
        service_names: List of service names
        graph: Compiled LangGraph state machine
    """
    
    def __init__(
        self,
        llm: BaseChatModel,
        realm: str = "demo",
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES
    ):
        """
        Initialize the policy builder with configuration and LLM.
        
        Args:
            realm: Realm name for fetching configuration data
            llm: Optional LangChain LLM instance. If not provided, creates a new
                 LLM instance using create_llm()
            verbose: If True, print LLM explanations and validation details
            max_retries: Maximum validation retry attempts
                    
        Raises:
            yaml.YAMLError: If config file is invalid YAML
        """
        # Store realm for later use
        self.realm = realm

        # Create configuration object
        self.config = PolicyBuilderConfig(
            llm=llm,
            verbose=verbose,
            max_retries=max_retries
        )

        config_api = Configuration.for_realm(realm)
        subjects = config_api.get_subjects()
        all_roles = [r for sublist in [subject.roles for subject in subjects] for r in sublist]
        role_map: dict[str,Role] = {}
        for r in all_roles:
            role_map[r.name] = r
        roles = list(role_map.values())
        print (f"Got {len(roles)} roles")
        self.roles = [r for r in roles if r.description]
        services = config_api.get_services()
        print (f"Got {len(services)} services")
        self.privileges_map = {}
        self.service_names = []
        self.services_by_name: Dict[str, Service] = {}
        for service in services:
            # service_type is a property of the service, not of individual roles.
            # Service.roles contains the privileges/permissions for this service.
            if not service.description or not ("Demo" in service.description):
                continue
            service_name = service.name or service.id
            print (f"Service {service_name} added: <{service.description}> <{service.type}>")
            described_scopes = [ scope for scope in service.scopes if scope.description
            ]
            if not described_scopes:
                continue
            self.privileges_map[service_name] = {
                "service_type": service.type,
                "scopes": described_scopes,
            }
            self.service_names.append(service_name)
            self.services_by_name[service_name] = service

        self.graph = create_policy_builder_graph(
            self.config,
            self.roles,
            self.privileges_map
        )
    
    # ========================================================================
    # GRAPH VISUALIZATION AND INSPECTION
    # ========================================================================
    
    def get_graph(self):
        """
        Get the compiled graph for visualization or inspection.
        
        Following LangGraph patterns, this allows external tools to
        visualize or analyze the graph structure.
        
        Returns:
            Compiled LangGraph workflow
        """
        return self.graph
    
    # ========================================================================
    # PUBLIC API METHODS
    # ========================================================================
    
    def generate_policy(self, description: str) -> PolicyObjectModel:
        """
        Generate an access control policy from a natural language description.

        This is the main public API method. It executes the complete workflow.

        Args:
            description: Natural language description of the access control policy

        Returns:
            Policy model instance with the generated policy and explanation.

        Raises:
            ValueError: If validation fails after all retries.

        Example:
            >>> policy = builder.generate_policy("Admins have full access")
        """
        initial_state: PolicyState = {
            "description": description,
            "policy": None,
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True,
        }

        final_state = self.graph.invoke(initial_state)

        errors = final_state.get("errors", [])
        if errors:
            raise ValueError(f"Policy validation failed: {'; '.join(errors)}")

        self._last_policy_structure: Optional[PolicyObjectModel] = final_state["policy"]

        return final_state["policy"]
    
    def get_yaml_output(self) -> str:
        """
        Generate YAML output from the stored Policy model.

        Must be called after generate_policy().

        Returns:
            Complete YAML policy file content with comments

        Raises:
            ValueError: If no policy has been generated yet
        """
        if not hasattr(self, '_last_policy_structure') or self._last_policy_structure is None:
            raise ValueError("No policy available. Generate a policy first using generate_policy().")

        return _generate_yaml_output(self._last_policy_structure)


# ============================================================================
# BACKWARD COMPATIBILITY
# ============================================================================
# For backward compatibility, keep the main() function here but delegate to CLI
if __name__ == "__main__":
    # This file should not be run directly anymore
    # Use main.py in the parent directory instead
    print("Please use main.py to run the policy builder:")
    print("  python main.py <policy_file.txt> <config.yaml> <output_file.yaml>")
    sys.exit(1)


