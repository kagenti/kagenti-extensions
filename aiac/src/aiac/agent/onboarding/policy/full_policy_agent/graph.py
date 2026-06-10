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
- cli.py: Command-line interface

Key Features:
    - Natural language to YAML policy conversion
    - Automatic role mapping and validation
    - Call chain analysis and enforcement
    - Retry mechanism with semantic verification
"""

from typing import Dict, Any, Optional
from pathlib import Path
import os
import sys
import yaml
from dataclasses import dataclass

from langgraph.graph import StateGraph, END
from langchain_core.language_models import BaseChatModel

from config import create_llm
from full_policy_agent.state import PolicyState
from config.constants import MAX_VALIDATION_RETRIES
from aiac.pdp.library.read_api_from_config import (
    get_roles,
    get_services,
    get_service_permissions,
    get_subjects,
    get_subject_assignments,
    get_scopes,
)
from single_privilege_agent import SinglePrivilegeMapper
from utils.validators import validate_policy_structure


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

    for service_name, privileges in privileges_map.items():
        for privilege in privileges:
            result = mapper.map_role(
                policy_description=state['description'],
                service_name=service_name,
                privilege=privilege,
                realm_roles=realm_roles,
            )

            if result.get('explanation'):
                explanations.append(
                    f"{service_name}/{privilege['name']}: {result['explanation']}"
                )

            for realm_role_name in result.get('real_roles_with_access', []):
                realm_role_to_privileges.setdefault(realm_role_name, []).append(
                    {'service': service_name, 'privilege': privilege['name']}
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
        service_names: List of service names
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
        service_names: List of service names

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
        service_names: List of service names
        graph: Compiled LangGraph state machine
    """
    
    def __init__(
        self,
        realm: str = "",
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

        roles_models = get_roles(realm=realm)
        self.realm_roles = [
            {"name": r.name, "description": r.description or ""}
            for r in roles_models
        ]
        services = get_services(realm=realm)
        self.privileges_map = {}
        self.service_names = []
        for service in services:
            permissions = get_service_permissions(service.id, realm=realm)
            self.privileges_map[service.clientId] = [
                {"name": permission.name, "description": permission.description or ""}
                for permission in permissions
            ]
            self.service_names.append(service.clientId)

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
        
        # Create header comments
        header = """# Access Control Policy
# Maps user roles (realm roles) to specific privileges
# Format: user_role_name -> list of privilege mappings
# Each entry specifies: service (service name) and privilege (privilege name from that service)

"""
        
        # Add original policy description as comment
        if hasattr(self, '_last_description') and self._last_description:
            description_lines = self._last_description.strip().split('\n')
            header += "# Original Policy Description:\n"
            for line in description_lines:
                header += f"#   {line.strip()}\n"
            header += "#\n"
        
        # Generate YAML from policy structure
        yaml_content = yaml.dump(
            self._last_policy_structure,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True
        )
        
        # Add footer
        footer = "\n# Generated by PolicyBuilder using LangGraph\n"
        
        # Combine all parts
        return header + yaml_content + footer

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
        
        Creates three Rego files:
        - realm_roles.rego: Maps realm role names to lists of user names
        - privileges.rego: Maps service/client IDs to their available privileges and scopes
        - generated_policy.rego: Access control policy rules based on the policy structure
        
        Args:
            file_dir: Directory to save Rego files (default: "file_dir")
            
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
        
        # Build role_to_users mapping using get_subject_assignments
        role_to_users = {}
        
        # Get all subjects and their role assignments
        subjects = get_subjects(realm=self.realm)
        for subject in subjects:
            assignments = get_subject_assignments(subject.id, realm=self.realm)
            # Get realm role mappings for this subject
            for role in assignments.realmMappings:
                role_name = role.name
                if role_name not in role_to_users:
                    role_to_users[role_name] = []
                role_to_users[role_name].append(subject.username)
        
        # Fetch scopes (if available in config)
        scopes = []
        try:
            scopes = get_scopes(realm=self.realm)
        except Exception:
            # If no scopes in config, that's okay - we'll just have empty scope lists
            pass
        
        # Generate realm_roles.rego from self.realm_roles with users
        realm_roles_rego = self._generate_realm_roles_rego(self.realm_roles, role_to_users)
        realm_roles_path = dir_path / "realm_roles.rego"
        with open(realm_roles_path, 'w') as f:
            f.write(realm_roles_rego)
        print(f"Realm roles Rego saved to {realm_roles_path}")
        
        # Generate privileges.rego from self.privileges_map with scopes
        privileges_rego = self._generate_privileges_rego(self.privileges_map, scopes)
        privileges_path = dir_path / "privileges.rego"
        with open(privileges_path, 'w') as f:
            f.write(privileges_rego)
        print(f"Privileges Rego saved to {privileges_path}")
        
        # Generate generated_policy.rego from policy_structure
        policy_rego = self._generate_policy_rego(policy_structure, description)
        policy_path = dir_path / "generated_policy.rego"
        with open(policy_path, 'w') as f:
            f.write(policy_rego)
        print(f"Generated policy Rego saved to {policy_path}")
    
    def _generate_realm_roles_rego(self, realm_roles: list, role_to_users: dict) -> str:
        """
        Generate Rego content for realm roles mapping.
        
        Args:
            realm_roles: List of realm role dictionaries with 'name' and 'description'
            role_to_users: Dictionary mapping role names to lists of usernames
            
        Returns:
            Rego file content as string
        """
        rego_content = """package authz.realm_roles

# Realm Roles Mapping
# Maps realm role names to lists of user names

"""
        
        for role in realm_roles:
            role_name = role.get("name", "")
            
            # Get users for this role
            users = role_to_users.get(role_name, [])
            
            # Generate simple mapping: role_name -> [user1, user2, ...]
            rego_content += f'realm_role["{role_name}"] := [\n'
            for user in users:
                # Escape quotes in username
                user_escaped = user.replace('"', '\\"')
                rego_content += f'    "{user_escaped}",\n'
            rego_content += "]\n\n"
        
        return rego_content
    
    def _generate_privileges_rego(self, privileges_map: Dict[str, list], scopes: list) -> str:
        """
        Generate Rego content for privileges mapping by service/client.
        
        Args:
            privileges_map: Dictionary mapping service/client IDs to their privilege lists
                           Each privilege is a dict with 'name' and 'description'
            scopes: List of Scope objects with 'name' and 'description'
            
        Returns:
            Rego file content as string
        """
        rego_content = """package authz.privileges

# Service Privileges Mapping
# Maps service/client IDs to their available privileges

"""
        
        # Create a list of scope names for inclusion in privileges
        scope_names = [scope.name for scope in scopes]
        
        for service_id, privileges in privileges_map.items():
            rego_content += f'service["{service_id}"] := [\n'
            for priv in privileges:
                priv_name = priv.get("name", "")
                priv_desc = priv.get("description", "")
                # Escape quotes in description
                priv_desc = priv_desc.replace('"', '\\"')
                
                rego_content += f'    {{\n'
                rego_content += f'        "name": "{priv_name}",\n'
                rego_content += f'        "description": "{priv_desc}",\n'
                rego_content += f'        "scopes": [\n'
                # Add all available scopes to each privilege
                for scope_name in scope_names:
                    scope_name_escaped = scope_name.replace('"', '\\"')
                    rego_content += f'            "{scope_name_escaped}",\n'
                rego_content += '        ]\n'
                rego_content += f'    }},\n'
            rego_content += "]\n\n"
        
        return rego_content
    
    def _generate_policy_rego(self, policy_structure: dict, description: str = "") -> str:
        """
        Generate Rego content for the access control policy.
        
        Converts the policy structure (role -> privileges) into Rego allow rules.
        Each rule checks if the user has the required role and matches the service/privilege.
        
        Args:
            policy_structure: Dictionary with 'policy' key containing role-to-privileges mapping
            description: Original policy description for comments
            
        Returns:
            Rego file content as string
        """
        rego_content = """package authbridge.inbound.request

import data.authz.realm_roles.realm_role

# Access Control Policy
# Maps user roles (realm roles) to specific privileges
# Format: user_role_name -> list of privilege mappings
# Each entry specifies: service (service name) and privilege (privilege name from that service)

"""
        
        # Add original policy description as comment
        if description:
            description_lines = description.strip().split('\n')
            rego_content += "# Original Policy Description:\n"
            for line in description_lines:
                rego_content += f"#   {line.strip()}\n"
            rego_content += "#\n\n"
        
        rego_content += "default allow := false\n\n"
        
        # Extract policy from structure
        policy = policy_structure.get("policy", {})
        
        # Generate allow rules for each role and their privileges
        for role_name, privileges in policy.items():
            for priv in privileges:
                service = priv.get("service", "")
                privilege = priv.get("privilege", "")
                
                # Escape quotes in service and privilege names
                service_escaped = service.replace('"', '\\"')
                privilege_escaped = privilege.replace('"', '\\"')
                
                rego_content += f'allow if {{\n'
                rego_content += f'  realm_role["{role_name}"][_] == input.identity.subject\n'
                rego_content += f'  input.identity.client_id == "{service_escaped}"\n'
                rego_content += f'  input.identity.scopes[_] == "{privilege_escaped}"\n'
                rego_content += "}\n\n"
       
        return rego_content


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

# Made with Bob

