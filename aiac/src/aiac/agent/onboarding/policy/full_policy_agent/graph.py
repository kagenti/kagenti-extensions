#!/usr/bin/env python3
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
- output_generators.py: Output file generation utilities
- cli.py: Command-line interface

Key Features:
    - Natural language to YAML policy conversion
    - Automatic role mapping and validation
    - Call chain analysis and enforcement
    - Retry mechanism with semantic verification
"""

from aiac.pdp.library.configuration.models import Service
from typing import Dict, Any, Optional
from pathlib import Path
import os
import sys
from dataclasses import dataclass

from langgraph.graph import StateGraph, END
from langchain_core.language_models import BaseChatModel

from config import create_llm
from full_policy_agent.state import PolicyState
from config.constants import MAX_VALIDATION_RETRIES
from aiac.pdp.library.configuration.api import Configuration
from single_privilege_agent import SinglePrivilegeMapper
from utils.validators import validate_policy_structure
from utils.output_generators import (
    generate_yaml_output,
    generate_realm_roles_rego,
    generate_privileges_rego,
    generate_default_inbound_rego,
    generate_default_outbound_rego,
    generate_policy_rego,
)


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

def _parse_and_extract_scopes(
    state: PolicyState,
    llm: BaseChatModel,
    realm_roles: list,
    privileges_map: dict,
    verbose: bool
) -> PolicyState:
    """
    Map each privilege to realm roles using SingleRoleMapper, then aggregate
    results into the parsed_scopes format expected by _build_policy.

    For every privilege across all services, SingleRoleMapper determines which
    realm roles should have access. The per-role results are inverted so that
    parsed_scopes is a list of {role: realm_role, privileges: [...]}.

    Args:
        state: Current PolicyState with 'description' field
        llm: LLM instance for processing
        realm_roles: List of available realm roles [{'name': str, 'description': str}]
        privileges_map: Dict mapping service names to privileges
        verbose: Whether to print detailed output

    Returns:
        Updated PolicyState with parsed_scopes and explanation
    """
    mapper = SinglePrivilegeMapper(llm=llm, verbose=verbose)

    explanations = []
    # realm_role_name -> list of {"service": X, "privilege": Y} dicts
    realm_role_to_privileges: dict = {}

    for service_name, service_info in privileges_map.items():
        for privilege in service_info["roles"]:
            result = mapper.map_role(
                policy_description=state['description'],
                service_name=service_name,
                privilege=privilege,
                realm_roles=realm_roles,
            )

            if result.get('explanation'):
                explanations.append(
                    f"{service_id}/{privilege['name']}: {result['explanation']}"
                )

            for realm_role_name in result.get('real_roles_with_access', []):
                realm_role_to_privileges.setdefault(realm_role_name, []).append(
                    {
                        'service': service_id,
                        'privilege': privilege['name'],
                    }
                )

    parsed_scopes = [
        {'role': realm_role, 'privileges': priv_list}
        for realm_role, priv_list in realm_role_to_privileges.items()
    ]

    return {
        **state,
        "explanation": "\n\n".join(explanations) if explanations else "",
        "parsed_scopes": parsed_scopes,
        "messages": [],
        "errors": [],
        "retry_count": state.get("retry_count", 0),
        "validation_passed": True
    }


def _build_policy(state: PolicyState) -> PolicyState:
    """
    Build structured policy dictionary from extracted role mappings.
    
    This is the second node in the workflow.
    
    Args:
        state: PolicyState with 'parsed_scopes' field
        
    Returns:
        Updated PolicyState with 'policy_structure' field
    """
    policy = {}
    
    # Transform parsed scopes into policy structure
    for role_info in state["parsed_scopes"]:
        role_name = role_info.get("role", "")
        privileges = role_info.get("privileges", [])
        policy[role_name] = privileges
    
    # Wrap in policy structure
    policy_structure = {"policy": policy}
    
    return {
        **state,
        "policy_structure": policy_structure
    }



def _validate_policy(
    state: PolicyState,
    llm: BaseChatModel,
    realm_roles: list,
    service_names: list,
    privileges_map: dict,
    verbose: bool,
    max_retries: int
) -> PolicyState:
    """
    Validate the generated policy structure and verify it matches the description.

    This is the fourth and final node in the workflow. It performs structural
    validation and semantic verification.

    Args:
        state: PolicyState with 'policy_structure' and 'description'
        llm: LLM instance for semantic verification
        realm_roles: List of available realm roles
        service_names: List of service ids
        privileges_map: Dict mapping service names to privileges
        verbose: Whether to print detailed output
        max_retries: Maximum retry attempts

    Returns:
        Updated PolicyState with errors and validation_passed fields
    """
    retry_count = state.get("retry_count", 0)
    policy = state["policy_structure"].get("policy", {})
    
    # Perform structural validation
    structural_errors = validate_policy_structure(
        policy,
        realm_roles,
        service_names,
        privileges_map
    )
    
    # If there are structural errors and we can retry, trigger retry
    if structural_errors and retry_count < max_retries:
        return {
            **state,
            "errors": structural_errors,
            "validation_passed": False,
            "retry_count": retry_count + 1
        }
    
    # Return final result; semantic validation is handled per-privilege in SingleRoleMapper
    return {
        **state,
        "errors": structural_errors,
        "validation_passed": len(structural_errors) == 0,
        "retry_count": retry_count
    }


def _should_retry_validation(state: PolicyState, max_retries: int) -> str:
    """
    Determine if validation should retry by going back to parse_and_extract.
    
    This is a conditional edge function for the LangGraph state machine.
    
    Args:
        state: Current PolicyState containing validation results
        max_retries: Maximum retry attempts allowed
        
    Returns:
        "parse_and_extract" if validation failed and retries remain,
        otherwise END to terminate the workflow
    """
    validation_passed = state.get("validation_passed", False)
    retry_count = state.get("retry_count", 0)
    errors = state.get("errors", [])
    
    # If validation failed and we haven't exceeded max retries, retry from start
    if not validation_passed and retry_count < max_retries:
        print(f"\n⚠️  Validation failed (attempt {retry_count}/{max_retries}). Retrying from parse_and_extract...")
        if errors:
            print(f"\nValidation Errors (from this attempt):")
            for i, error in enumerate(errors, 1):
                print(f"  {i}. {error}")
            print()
        return "parse_and_extract"
    
    # Either validation passed or max retries exceeded
    return END


def create_policy_builder_graph(
    config: PolicyBuilderConfig,
    realm_roles: list,
    privileges_map: dict,
    service_names: list
):
    """
    Create and compile the policy builder graph.

    Following LangGraph patterns, this function separates graph construction
    from the agent class, making it easier to test and visualize.

    Args:
        config: PolicyBuilderConfig instance
        realm_roles: List of available realm roles
        privileges_map: Dict mapping service names to privileges
        service_names: List of service ids

    Returns:
        Compiled LangGraph workflow
    """

    # Define node functions as closures with access to config
    def parse_and_extract_node(state: PolicyState) -> PolicyState:
        """Parse natural language and extract privilege mappings."""
        return _parse_and_extract_scopes(
            state, config.llm, realm_roles, privileges_map, config.verbose
        )
    
    def build_policy_node(state: PolicyState) -> PolicyState:
        """Build structured policy from mappings."""
        return _build_policy(state)
    
    def validate_policy_node(state: PolicyState) -> PolicyState:
        """Validate structure and semantics."""
        return _validate_policy(
            state, config.llm, realm_roles, service_names,
            privileges_map, config.verbose, config.max_retries
        )
    
    def should_retry_node(state: PolicyState) -> str:
        """Determine if validation should retry."""
        return _should_retry_validation(state, config.max_retries)
    
    # Build the graph
    workflow = StateGraph(PolicyState)
    
    # Add nodes
    workflow.add_node("parse_and_extract", parse_and_extract_node)
    workflow.add_node("build_policy", build_policy_node)
    workflow.add_node("validate_policy", validate_policy_node)
    
    # Define edges
    workflow.set_entry_point("parse_and_extract")
    workflow.add_edge("parse_and_extract", "build_policy")
    workflow.add_edge("build_policy", "validate_policy")
    
    # Add conditional edge for retry logic
    workflow.add_conditional_edges(
        "validate_policy",
        should_retry_node,
        {
            "parse_and_extract": "parse_and_extract",
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
        1. parse_and_extract: Parse natural language and extract role mappings
        2. build_policy: Build structured policy from mappings
        3. validate_policy: Validate structure and semantics (with retry)
    
    Attributes:
        config: PolicyBuilderConfig instance
        realm_roles: List of available realm role names
        privileges_map: Dict mapping service names to their available privileges
        service_names: List of service ids
        graph: Compiled LangGraph state machine
    """
    
    def __init__(
        self,
        realm: str = "demo",
        config_path: Optional[Path] = None,
        llm: Optional[BaseChatModel] = None,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES
    ):
        """
        Initialize the policy builder with configuration and LLM.
        
        Args:
            realm: Realm name for fetching configuration data
            config_path: Path to config YAML. If None, AC_CONFIG_PATH env var is used.
            llm: Optional LangChain LLM instance. If not provided, creates a new
                 LLM instance using create_llm()
            verbose: If True, print LLM explanations and validation details
            max_retries: Maximum validation retry attempts
                    
        Raises:
            FileNotFoundError: If config_path doesn't exist
            yaml.YAMLError: If config file is invalid YAML
        """
        # Store realm for later use
        self.realm = realm

        # Create LLM if not provided
        # LLM config is in the config directory relative to this file (llm.env)
        if llm is None:
            llm_env_path = Path(__file__).parent.parent / "config" / "llm.env"
            llm_instance = create_llm(env_path=llm_env_path, verbose=verbose)
        else:
            llm_instance = llm
        
        if config_path is not None:
            os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_path)

        # Create configuration object
        self.config = PolicyBuilderConfig(
            llm=llm_instance,
            verbose=verbose,
            max_retries=max_retries
        )

        config_api = Configuration.for_realm(realm)
        
        roles_models = config_api.get_roles()
        self.realm_roles = [
            {"name": r.name, "description": r.description or ""}
            for r in roles_models
        ]
        services: list[Service] = config_api.get_services()
        self.privileges_map = {}
        self.service_names = []
        for service in services:
            # service_type is a property of the service, not of individual roles.
            # Service.roles contains the privileges/permissions for this service.
            if not service.description or not ("Demo" in service.description):
                continue
            service_name = service.name or service.id 
            print (f"Service {service_name} added: {service.description}")
            self.privileges_map[service.name] = {
                "service_type": service.type,
                "scopes": [
                    {"name": scope.name, "description": scope.description or ""}
                    for scope in service.scopes
                ],
            }
            self.service_names.append(service_name)

        # Build and compile the LangGraph state machine
        self.graph = create_policy_builder_graph(
            self.config,
            self.realm_roles,
            self.privileges_map,
            self.service_names
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
    
    def generate_policy(self, description: str) -> Dict[str, Any]:
        """
        Generate an access control policy from a natural language description.
        
        This is the main public API method. It executes the complete workflow.
        
        Args:
            description: Natural language description of the access control policy
                        
        Returns:
            Dictionary containing:
                - policy_structure (dict): Structured policy data
                - parsed_scopes (list): Raw role-to-privilege mappings from LLM
                - errors (list): Validation errors (empty if successful)
                - success (bool): True if generation succeeded without errors
                - retry_count (int): Number of validation retries that occurred
                        
        Example:
            >>> builder = PolicyBuilder(config_path=Path("config.yaml"))
            >>> result = builder.generate_policy("Admins have full access")
            >>> if result["success"]:
            ...     builder.save_policy("policy.yaml")
        """
        # Initialize the workflow state
        initial_state: PolicyState = {
            "description": description,
            "explanation": "",
            "parsed_scopes": [],
            "policy_structure": {},
            "yaml_output": "",
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True
        }
        
        # Execute the LangGraph workflow
        final_state = self.graph.invoke(initial_state)
        
        # Store the policy structure and description for later use
        self._last_policy_structure = final_state["policy_structure"]
        self._last_description = description
        
        # Extract and return results
        return {
            "policy_structure": final_state["policy_structure"],
            "parsed_scopes": final_state["parsed_scopes"],
            "explanation": final_state.get("explanation", ""),
            "errors": final_state["errors"],
            "success": len(final_state["errors"]) == 0,
            "retry_count": final_state.get("retry_count", 0)
        }
    
    def get_yaml_output(self) -> str:
        """
        Generate YAML output from the stored policy structure.
        
        This method generates YAML on demand from the stored policy structure.
        Must be called after generate_policy().
        
        Returns:
            Complete YAML policy file content with comments
            
        Raises:
            ValueError: If no policy has been generated yet
        """
        if not hasattr(self, '_last_policy_structure'):
            raise ValueError("No policy available. Generate a policy first using generate_policy().")
        
        description = getattr(self, '_last_description', "")
        return generate_yaml_output(self._last_policy_structure, description)

    def save_policy(self, filepath: str = "access_control_policy.yaml"):
        """
        Save the generated policy YAML to a file.
        
        Uses the last generated policy from instance state (_last_policy_structure).
        
        Args:
            filepath: Output file path (default: "access_control_policy.yaml")
            
        Raises:
            ValueError: If no policy has been generated yet
        """
        yaml_output = self.get_yaml_output()
        
        with open(filepath, 'w') as f:
            f.write(yaml_output)
        print(f"Access rules saved to {filepath}")
    
    def save_policy_rego(self, file_dir: str = "rego_policy"):
        """
        Save Rego files with realm roles, privileges maps, and generated policy from configuration.
        
        Uses the last generated policy from instance state (_last_policy_structure and _last_description).
        
        Creates multiple Rego files:
        - realm_roles.rego: Maps user names to lists of realm role names
        - generated_policy_<service>.rego: One access control policy file per service
        
        Args:
            file_dir: Directory to save Rego files (default: "rego_policy")
            
        Raises:
            ValueError: If no policy has been generated yet
        """
        # Get policy_structure and description from instance state
        if not hasattr(self, '_last_policy_structure'):
            raise ValueError("No policy structure available. Generate a policy first using generate_policy().")
        
        policy_structure = self._last_policy_structure
        description = getattr(self, '_last_description', "")
        
        # At this point, policy_structure is guaranteed to be a dict
        assert policy_structure is not None, "policy_structure should not be None after the check above"
        
        # Create directory if it doesn't exist
        dir_path = Path(file_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        
        user_to_roles = {}
        
        # Get all subjects and their role assignments
        config_api = Configuration.for_realm(self.realm)
        subjects = config_api.get_subjects()
        for subject in subjects:
            user_to_roles[subject.username] = [
                role.name for role in subject.roles
            ]
        
        # Fetch scopes (if available in config)
        scopes = []
        try:
            scopes = config_api.get_scopes()
        except Exception:
            # If no scopes in config, that's okay - we'll just have empty scope lists
            pass
        
        # Generate realm_roles.rego from users with their realm roles
        realm_roles_rego = generate_realm_roles_rego(user_to_roles)
        realm_roles_path = dir_path / "realm_roles.rego"
        with open(realm_roles_path, 'w') as f:
            f.write(realm_roles_rego)
        print(f"Realm roles Rego saved to {realm_roles_path}")
        
        # Generate privileges.rego from self.privileges_map with scopes
        # privileges_rego = generate_privileges_rego(self.privileges_map, scopes)
        # privileges_path = dir_path / "privileges.rego"
        # with open(privileges_path, 'w') as f:
        #     f.write(privileges_rego)
        # print(f"Privileges Rego saved to {privileges_path}")
        
        # Generate default.rego with deny-by-default behavior
        default_rego_inbound_path = dir_path / "default_inbound.rego"
        with open(default_rego_inbound_path, 'w') as f:
            f.write(generate_default_inbound_rego())
        print(f"Default Rego saved to {default_rego_inbound_path}")

        # Generate default.rego with deny-by-default behavior
        default_rego_outbound_path = dir_path / "default_outbound.rego"
        with open(default_rego_outbound_path, 'w') as f:
            f.write(generate_default_outbound_rego())
        print(f"Default Rego saved to {default_rego_outbound_path}")
        

        # Generate one policy rego file per service
        # First, collect all services that appear in the policy
        policy = policy_structure.get("policy", {})
        services_in_policy = set()
        for role_name, privileges in policy.items():
            for priv in privileges:
                service = priv.get("service", "")
                if service:
                    services_in_policy.add(service)
        
        # Build service_types from privileges_map — service_type is a property of
        # the service, not of individual privileges, so it is not stored in the policy.
        service_types = {
            svc_id: svc_info["service_type"]
            for svc_id, svc_info in self.privileges_map.items()
        }

        # Generate a separate rego file for each service
        for service_id in services_in_policy:
            policy_rego = generate_policy_rego(
                policy_structure, 
                service_id, 
                service_types,
                description
            )
            # Sanitize service name for filename (replace special chars with underscores)
            safe_service_id = service_id.replace('/', '_').replace('\\', '_').replace(' ', '_')
            policy_path = dir_path / f"generated_policy_{safe_service_id}.rego"
            with open(policy_path, 'w') as f:
                f.write(policy_rego)
            print(f"Generated policy Rego for service '{service_id}' saved to {policy_path}")


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


