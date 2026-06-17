#!/usr/bin/env python3
"""
Service Policy Agent

Generates a partial access control policy that contains only the rules
relevant for a single specified Keycloak service.  Inputs are a natural
language policy description and a service ID; output is a YAML policy
with realm-role → service-role mappings scoped to that service.

Workflow:
    1. filter_and_extract  — run SingleRoleMapper for every role of the
                             given service and aggregate the results.
    2. build_policy        — assemble the {policy: {realm_role: [...]}} dict.
    3. generate_yaml       — render YAML with header comments.
    4. validate_policy     — structural validation with retry.
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
from service_policy_agent.state import ServicePolicyState
from config.constants import MAX_VALIDATION_RETRIES
from aiac.pdp.library.configuration.api import Configuration
from single_privilege_agent import SinglePrivilegeMapper
from utils.validators import validate_policy_structure


@dataclass
class ServicePolicyBuilderConfig:
    """
    Configuration for the ServicePolicyBuilder agent.

    Attributes:
        llm: LangChain LLM instance
        verbose: Whether to print detailed output
        max_retries: Maximum validation retry attempts
    """
    llm: BaseChatModel
    verbose: bool = True
    max_retries: int = MAX_VALIDATION_RETRIES


# ============================================================================
# PURE NODE FUNCTIONS
# ============================================================================

def _filter_and_extract_scopes(
    state: ServicePolicyState,
    llm: BaseChatModel,
    realm_roles: list,
    service_type: str,
    privileges: list,
    verbose: bool,
) -> ServicePolicyState:
    """
    Run SingleRoleMapper for every privilege of the target service and invert the
    results into the {role to privileges} structure used by _build_policy.

    Args:
        state: Current ServicePolicyState (needs 'description' and 'service_id')
        llm: LLM instance
        realm_roles: All available realm roles [{'name': str, 'description': str}]
        service_type: Service type (e.g. 'Tool', 'Agent') — property of the service, not each privilege
        privileges: Privileges belonging to the target service [{'name': str, 'description': str}]
        verbose: Whether to print detailed output

    Returns:
        Updated ServicePolicyState with parsed_scopes and explanation
    """
    service_id = state["service_id"]
    mapper = SinglePrivilegeMapper(llm=llm, verbose=verbose)

    explanations: list[str] = []
    realm_role_to_privileges: dict = {}

    for privilege in privileges:
        result = mapper.map_role(
            policy_description=state["description"],
            service_name=service_id,
            privilege=privilege,
            realm_roles=realm_roles,
        )

        if result.get("explanation"):
            explanations.append(f"{service_id}/{privilege['name']}: {result['explanation']}")

        for realm_role_name in result.get("real_roles_with_access", []):
            realm_role_to_privileges.setdefault(realm_role_name, []).append(
                {
                    "service": service_id,
                    "privilege": privilege["name"],
                }
            )

    parsed_scopes = [
        {"role": realm_role, "privileges": priv_list}
        for realm_role, priv_list in realm_role_to_privileges.items()
    ]

    return {
        **state,
        "explanation": "\n\n".join(explanations) if explanations else "",
        "parsed_scopes": parsed_scopes,
        "messages": [],
        "errors": [],
        "retry_count": state.get("retry_count", 0),
        "validation_passed": True,
    }


def _build_policy(state: ServicePolicyState) -> ServicePolicyState:
    """
    Assemble the structured policy dict from parsed_scopes.

    Returns:
        Updated ServicePolicyState with policy_structure
    """
    policy: dict = {}
    for entry in state["parsed_scopes"]:
        policy[entry["role"]] = entry["privileges"]

    return {**state, "policy_structure": {"policy": policy}}


def _generate_yaml(state: ServicePolicyState) -> ServicePolicyState:
    """
    Render the policy structure as a YAML string with explanatory comments.

    Returns:
        Updated ServicePolicyState with yaml_output
    """
    service_id = state.get("service_id", "")
    header = (
        "# Partial Access Control Policy\n"
        f"# Scoped to service: {service_id}\n"
        "# Maps realm roles to the privileges they may access.\n\n"
    )

    if state.get("description"):
        header += "# Original Policy Description:\n"
        for line in state["description"].strip().splitlines():
            header += f"#   {line.strip()}\n"
        header += "#\n"

    if state.get("explanation"):
        header += "# LLM Mapping Explanation:\n"
        for line in state["explanation"].strip().splitlines():
            header += f"#   {line.strip()}\n"
        header += "\n"

    yaml_content = yaml.dump(
        state["policy_structure"],
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    footer = "\n# Generated by ServicePolicyBuilder using LangGraph\n"

    return {**state, "yaml_output": header + yaml_content + footer}


def _validate_policy(
    state: ServicePolicyState,
    llm: BaseChatModel,
    realm_roles: list,
    service_id: str,
    service_type: str,
    privileges: list,
    verbose: bool,
    max_retries: int,
) -> ServicePolicyState:
    """
    Structural validation of the generated policy.

    Returns:
        Updated ServicePolicyState with errors and validation_passed
    """
    retry_count = state.get("retry_count", 0)
    policy = state["policy_structure"].get("policy", {})
    service_names = [service_id]

    privileges_map = {
        service_id: {
            "service_type": service_type,
            "roles": privileges,
        }
    }

    structural_errors = validate_policy_structure(
        policy, realm_roles, service_names, privileges_map
    )
    # An empty policy is valid for a service-scoped agent: the policy description
    # may simply not grant any permissions to this service's privileges.
    structural_errors = [e for e in structural_errors if e != "Policy is empty"]

    if structural_errors and retry_count < max_retries:
        return {
            **state,
            "errors": structural_errors,
            "validation_passed": False,
            "retry_count": retry_count + 1,
        }

    return {
        **state,
        "errors": structural_errors,
        "validation_passed": len(structural_errors) == 0,
        "retry_count": retry_count,
    }


def _should_retry(state: ServicePolicyState, max_retries: int) -> str:
    """Conditional edge: retry parse or finish."""
    if not state.get("validation_passed", False) and state.get("retry_count", 0) < max_retries:
        errors = state.get("errors", [])
        print(f"\n⚠️  Validation failed (attempt {state['retry_count']}/{max_retries}). Retrying...")
        for i, err in enumerate(errors, 1):
            print(f"  {i}. {err}")
        return "filter_and_extract"
    return END


# ============================================================================
# GRAPH CONSTRUCTION
# ============================================================================

def create_service_policy_builder_graph(
    config: ServicePolicyBuilderConfig,
    realm_roles: list,
    service_id: str,
    service_type: str,
    privileges: list,
):
    """
    Build and compile the service-scoped policy builder graph.

    Args:
        config: ServicePolicyBuilderConfig
        realm_roles: All realm roles [{name, description}]
        service_id: Target Keycloak service ID
        service_type: Service type (e.g. 'Tool', 'Agent') — property of the service
        privileges: Privileges of the target service [{name, description}]

    Returns:
        Compiled LangGraph workflow
    """

    def filter_and_extract_node(state: ServicePolicyState) -> ServicePolicyState:
        return _filter_and_extract_scopes(
            state, config.llm, realm_roles, service_type, privileges, config.verbose
        )

    def build_policy_node(state: ServicePolicyState) -> ServicePolicyState:
        return _build_policy(state)

    def generate_yaml_node(state: ServicePolicyState) -> ServicePolicyState:
        return _generate_yaml(state)

    def validate_policy_node(state: ServicePolicyState) -> ServicePolicyState:
        return _validate_policy(
            state, config.llm, realm_roles, service_id, service_type, privileges,
            config.verbose, config.max_retries
        )

    def should_retry_node(state: ServicePolicyState) -> str:
        return _should_retry(state, config.max_retries)

    workflow = StateGraph(ServicePolicyState)
    workflow.add_node("filter_and_extract", filter_and_extract_node)
    workflow.add_node("build_policy", build_policy_node)
    workflow.add_node("generate_yaml", generate_yaml_node)
    workflow.add_node("validate_policy", validate_policy_node)

    workflow.set_entry_point("filter_and_extract")
    workflow.add_edge("filter_and_extract", "build_policy")
    workflow.add_edge("build_policy", "generate_yaml")
    workflow.add_edge("generate_yaml", "validate_policy")
    workflow.add_conditional_edges(
        "validate_policy",
        should_retry_node,
        {"filter_and_extract": "filter_and_extract", END: END},
    )

    return workflow.compile()


# ============================================================================
# PUBLIC CLASS
# ============================================================================

class ServicePolicyBuilder:
    """
    AI-powered policy builder scoped to a single Keycloak service.

    Given a natural language policy description and a service ID, produces
    a YAML access control policy that contains only the realm-role →
    privilege mappings relevant to that service.

    Workflow:
        1. filter_and_extract  — map each privilege of the service to realm roles
        2. build_policy        — assemble the structured policy dict
        3. generate_yaml       — render YAML with comments
        4. validate_policy     — structural validation with retry
    """

    def __init__(
        self,
        service_id: str,
        realm: str = "demo",
        config_path: Optional[Path] = None,
        llm: Optional[BaseChatModel] = None,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ):
        """
        Args:
            service_id: Keycloak service ID to scope the policy to
            realm: Keycloak realm name (empty string uses the default realm)
            config_path: Path to the AC config YAML; falls back to AC_CONFIG_PATH env var
            llm: LangChain LLM instance; created automatically if not provided
            verbose: Print LLM explanations and validation details
            max_retries: Maximum validation retry attempts
        """
        if llm is None:
            llm_env_path = Path(__file__).parent.parent / "config" / "llm.env"
            llm_instance = create_llm(env_path=llm_env_path, verbose=verbose)
        else:
            llm_instance = llm

        if config_path is not None:
            os.environ["AIAC_PDP_CONFIG_PATH"] = str(config_path)

        self.service_id = service_id
        self.config = ServicePolicyBuilderConfig(
            llm=llm_instance,
            verbose=verbose,
            max_retries=max_retries,
        )

        config_api = Configuration.for_realm(realm)
        
        roles_models = config_api.get_roles()
        self.realm_roles = [
            {"name": r.name, "description": r.description or ""}
            for r in roles_models
        ]

        services = config_api.get_services()
        self.service_type: str = "Tool"  # Default to "Tool" if not found
        self.privileges = []
        for service in services:
            if service.id != service_id:
                continue
            # Handle None case by defaulting to "Tool"
            self.service_type = service.type or "Tool"
            # Service.roles contains the privileges/permissions for this service.
            # service_type is a property of the service, not of individual privileges.
            self.privileges = [
                {"name": role.name, "description": role.description or ""}
                for role in service.roles
            ]
            break

        self.graph = create_service_policy_builder_graph(
            self.config,
            self.realm_roles,
            self.service_id,
            self.service_type,
            self.privileges,
        )

    def get_graph(self):
        """Return the compiled graph for visualization or inspection."""
        return self.graph

    def generate_policy(self, description: str) -> Dict[str, Any]:
        """
        Generate a service-scoped access control policy from a natural language description.

        Args:
            description: Natural language policy description

        Returns:
            dict with keys:
                yaml_output (str)       — YAML policy file content
                policy_structure (dict) — structured policy data
                parsed_scopes (list)    — raw realm-role → privilege mappings
                errors (list)           — validation errors (empty on success)
                success (bool)          — True when no validation errors
                retry_count (int)       — number of validation retries
        """
        initial_state: ServicePolicyState = {
            "description": description,
            "service_id": self.service_id,
            "explanation": "",
            "parsed_scopes": [],
            "policy_structure": {},
            "yaml_output": "",
            "messages": [],
            "errors": [],
            "retry_count": 0,
            "validation_passed": True,
        }

        final_state = self.graph.invoke(initial_state)

        return {
            "yaml_output": final_state["yaml_output"],
            "policy_structure": final_state["policy_structure"],
            "parsed_scopes": final_state["parsed_scopes"],
            "errors": final_state["errors"],
            "success": len(final_state["errors"]) == 0,
            "retry_count": final_state.get("retry_count", 0),
        }

    def save_policy(self, yaml_output: str, filepath: str = "service_policy.yaml"):
        """
        Save the generated policy YAML to a file.

        Args:
            yaml_output: YAML content string
            filepath: Destination file path
        """
        with open(filepath, "w") as f:
            f.write(yaml_output)
        print(f"Service policy saved to {filepath}")


if __name__ == "__main__":
    print("Use ServicePolicyBuilder programmatically or via the CLI.")
    sys.exit(1)
