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
from aiac.pdp.policy.models import Policy, Priviledge
from typing import Optional, Dict, Any
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
from utils.output_generators import generate_yaml_output


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
    verbose: bool,
    services_by_name: Dict[str, Service],
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
    mapper = SinglePrivilegeMapper(llm=llm, verbose=verbose)

    explanations = []
    # realm_role_name -> list of {"service": Service, "privilege": str} dicts
    realm_role_to_privileges: dict = {}

    for service_name, service_info in privileges_map.items():
        service_obj = services_by_name.get(service_name)
        for privilege in service_info["scopes"]:
            result = mapper.map_role(
                policy_description=state['description'],
                service_name=service_name,
                privilege=privilege,
                realm_roles=realm_roles,
            )

            roles_with_access = result.get('real_roles_with_access', [])
            if roles_with_access and result.get('explanation'):
                explanations.append(
                    f"{service_name}/{privilege['name']}: {result['explanation']}"
                )

            for realm_role_name in roles_with_access:
                realm_role_to_privileges.setdefault(realm_role_name, []).append(
                    {
                        'service': service_obj,
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
    Build a typed Policy model from extracted role mappings.

    Reads parsed_scopes (where each privilege dict carries a Service object
    under 'service') and produces a Policy with Priviledge objects grouped
    by privilege name so each Priviledge holds a list of Service objects.

    Args:
        state: PolicyState with 'parsed_scopes' field

    Returns:
        Updated PolicyState with 'policy_structure' set to a Policy instance
    """
    raw: Dict[str, list] = {}
    for role_info in state["parsed_scopes"]:
        role_name = role_info.get("role", "")
        privileges = role_info.get("privileges", [])
        priv_to_services: Dict[str, list] = {}
        for p in privileges:
            svc = p["service"]  # Service object stored by _parse_and_extract_scopes
            priv_to_services.setdefault(p["privilege"], []).append(svc)
        raw[role_name] = [
            Priviledge(name=priv_name, services=svcs)
            for priv_name, svcs in priv_to_services.items()
        ]

    policy = Policy(
        name=state["description"],
        policy=raw,
        explanation=state.get("explanation", ""),
    )
    return {**state, "policy_structure": policy}



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
    Validate the generated Policy model against structural rules.

    Converts the Policy back to a raw dict for validate_policy_structure,
    which expects {realm_role: [{"service": str, "privilege": str}]}.

    Args:
        state: PolicyState with 'policy_structure' as a Policy instance
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
    policy_obj: Optional[Policy] = state.get("policy_structure")

    if policy_obj is None:
        raw_policy: Dict[str, Any] = {}
    else:
        raw_policy = {
            realm_role: [
                {"service": svc.serviceId or svc.name or svc.id, "privilege": priv.name}
                for priv in privileges
                for svc in priv.services
            ]
            for realm_role, privileges in policy_obj.policy.items()
        }

    structural_errors = validate_policy_structure(
        raw_policy,
        realm_roles,
        service_names,
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
    service_names: list,
    services_by_name: Dict[str, Service],
):
    """
    Create and compile the policy builder graph.

    Args:
        config: PolicyBuilderConfig instance
        realm_roles: List of available realm roles
        privileges_map: Dict mapping service names to privileges
        service_names: List of service names
        services_by_name: Mapping of service name → Service object (threaded into
            _parse_and_extract_scopes so parsed_scopes carries Service objects)

    Returns:
        Compiled LangGraph workflow
    """

    def parse_and_extract_node(state: PolicyState) -> PolicyState:
        return _parse_and_extract_scopes(
            state, config.llm, realm_roles, privileges_map, config.verbose, services_by_name
        )
    
    def build_policy_node(state: PolicyState) -> PolicyState:
        return _build_policy(state)

    def validate_policy_node(state: PolicyState) -> PolicyState:
        return _validate_policy(
            state, config.llm, realm_roles, service_names,
            privileges_map, config.verbose, config.max_retries
        )

    def should_retry_node(state: PolicyState) -> str:
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
        realm: str = "demo",
        llm: Optional[BaseChatModel] = None,
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

        # Create LLM if not provided
        # LLM config is in the config directory relative to this file (llm.env)
        if llm is None:
            llm_env_path = Path(__file__).parent.parent / "config" / "llm.env"
            llm_instance = create_llm(env_path=llm_env_path, verbose=verbose)
        else:
            llm_instance = llm
        
        # Create configuration object
        self.config = PolicyBuilderConfig(
            llm=llm_instance,
            verbose=verbose,
            max_retries=max_retries
        )

        config_api = Configuration.for_realm(realm)
        
        roles_models = config_api.get_roles()
        print (f"Got {len(roles_models)} roles")
        self.realm_roles = [
            {"name": r.name, "description": r.description}
            for r in roles_models
            if r.description
        ]
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
            described_scopes = [
                {"name": scope.name, "description": scope.description}
                for scope in service.scopes
                if scope.description
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
            self.realm_roles,
            self.privileges_map,
            self.service_names,
            self.services_by_name,
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
    
    def generate_policy(self, description: str) -> Policy:
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
            "explanation": "",
            "parsed_scopes": [],
            "policy_structure": None,
            "yaml_output": "",
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True,
        }

        final_state = self.graph.invoke(initial_state)

        errors = final_state.get("errors", [])
        if errors:
            raise ValueError(f"Policy validation failed: {'; '.join(errors)}")

        self._last_policy_structure: Optional[Policy] = final_state["policy_structure"]

        return final_state["policy_structure"]
    
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

        policy = self._last_policy_structure
        return generate_yaml_output(
            {
                "policy": {
                    realm_role: [
                        {"service": svc.serviceId or svc.name or svc.id, "privilege": priv.name}
                        for priv in privileges
                        for svc in priv.services
                    ]
                    for realm_role, privileges in policy.policy.items()
                }
            },
            policy.name,
        )


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


