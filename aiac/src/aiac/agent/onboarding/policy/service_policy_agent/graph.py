#!/usr/bin/env python3
"""
Service Policy Agent

Generates a partial access control policy that contains only the rules
relevant for a single specified Keycloak service.  Inputs are a natural
language policy description and a service name; output is a YAML policy
with realm-role → service-role mappings scoped to that service.

Workflow:
    1. filter_and_extract  — run SingleRoleMapper for every role of the
                             given service and aggregate the results.
    2. build_policy        — assemble the {policy: {realm_role: [...]}} dict.
    3. generate_yaml       — render YAML with header comments.
    4. validate_policy     — structural validation with retry.
"""

from typing import Optional
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
from aiac.pdp.library.configuration.models import Service
from aiac.pdp.policy.models import Policy, Priviledge
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
    service: Optional[Service],
    privileges: list,
    verbose: bool,
) -> ServicePolicyState:
    """
    Run SingleRoleMapper for every privilege of the target service and invert the
    results into the {role to privileges} structure used by _build_policy.

    The 'service' key in each privilege dict holds the Service object (not a string)
    so that _build_policy can construct a typed Policy directly.

    Args:
        state: Current ServicePolicyState (needs 'description' and 'service_name')
        llm: LLM instance
        realm_roles: All available realm roles [{'name': str, 'description': str}]
        service: Service object for this scope (None if service was not found in config)
        privileges: Privileges belonging to the target service [{'name': str, 'description': str}]
        verbose: Whether to print detailed output

    Returns:
        Updated ServicePolicyState with parsed_scopes and explanation
    """
    service_name = state["service_name"]
    mapper = SinglePrivilegeMapper(llm=llm, verbose=verbose)

    explanations: list[str] = []
    realm_role_to_privileges: dict = {}

    for privilege in privileges:
        result = mapper.map_role(
            policy_description=state["description"],
            service_name=service_name,
            privilege=privilege,
            realm_roles=realm_roles,
        )

        roles_with_access = result.get("real_roles_with_access", [])
        if roles_with_access and result.get("explanation"):
            explanations.append(f"{service_name}/{privilege['name']}: {result['explanation']}")

        for realm_role_name in roles_with_access:
            realm_role_to_privileges.setdefault(realm_role_name, []).append(
                {
                    "service": service,  # Service object — may be None if service not found
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
    Build a typed Policy model from parsed_scopes.

    Each privilege dict carries a Service object under 'service'; privileges
    are grouped by name so each Priviledge holds a list of Service objects.

    Returns:
        Updated ServicePolicyState with policy_structure set to a Policy instance
    """
    raw: dict = {}
    for entry in state["parsed_scopes"]:
        role_name = entry["role"]
        priv_to_services: dict = {}
        for p in entry["privileges"]:
            svc = p["service"]  # Service object stored by _filter_and_extract_scopes
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


def _generate_yaml(state: ServicePolicyState) -> ServicePolicyState:
    """
    Render the Policy model as a YAML string with explanatory comments.

    Converts policy_structure (a Policy instance) to a plain dict before
    passing to yaml.dump so the output matches the expected YAML format.

    Returns:
        Updated ServicePolicyState with yaml_output
    """
    service_name = state.get("service_name", "")
    header = (
        "# Partial Access Control Policy\n"
        f"# Scoped to service: {service_name}\n"
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

    policy_obj: Optional[Policy] = state.get("policy_structure")
    if policy_obj is None:
        policy_dict: dict = {"policy": {}}
    else:
        policy_dict = {
            "policy": {
                realm_role: [
                    {"service": svc.serviceId or svc.name or svc.id, "privilege": priv.name}
                    for priv in privileges
                    for svc in priv.services
                ]
                for realm_role, privileges in policy_obj.policy.items()
            }
        }

    yaml_content = yaml.dump(
        policy_dict,
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
    service_name: str,
    service: Optional[Service],
    privileges: list,
    verbose: bool,
    max_retries: int,
) -> ServicePolicyState:
    """
    Structural validation of the generated Policy model.

    Converts the Policy back to a raw dict for validate_policy_structure,
    which expects {realm_role: [{"service": str, "privilege": str}]}.

    Returns:
        Updated ServicePolicyState with errors and validation_passed
    """
    retry_count = state.get("retry_count", 0)
    policy_obj: Optional[Policy] = state.get("policy_structure")
    service_type = (service.type or "Tool") if service else "Tool"

    if policy_obj is None:
        raw_policy: dict = {}
    else:
        raw_policy = {
            realm_role: [
                {"service": svc.serviceId or svc.name or svc.id, "privilege": priv.name}
                for priv in privs
                for svc in priv.services
            ]
            for realm_role, privs in policy_obj.policy.items()
        }

    privileges_map = {
        service_name: {
            "service_type": service_type,
            "scopes": privileges,
        }
    }

    structural_errors = validate_policy_structure(
        raw_policy, realm_roles, [service_name], privileges_map
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
    service_name: str,
    service: Optional[Service],
    privileges: list,
):
    """
    Build and compile the service-scoped policy builder graph.

    Args:
        config: ServicePolicyBuilderConfig
        realm_roles: All realm roles [{name, description}]
        service_name: Service name
        service: Service object (None if the service was not found in config)
        privileges: Privileges of the target service [{name, description}]

    Returns:
        Compiled LangGraph workflow
    """

    def filter_and_extract_node(state: ServicePolicyState) -> ServicePolicyState:
        return _filter_and_extract_scopes(
            state, config.llm, realm_roles, service, privileges, config.verbose
        )

    def build_policy_node(state: ServicePolicyState) -> ServicePolicyState:
        return _build_policy(state)

    def generate_yaml_node(state: ServicePolicyState) -> ServicePolicyState:
        return _generate_yaml(state)

    def validate_policy_node(state: ServicePolicyState) -> ServicePolicyState:
        return _validate_policy(
            state, config.llm, realm_roles, service_name, service, privileges,
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

    Given a natural language policy description and a service name, produces
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
        service_name: str,
        realm: str = "demo",
        llm: Optional[BaseChatModel] = None,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ):
        """
        Args:
            service_name: service name to scope the policy to
            realm: realm name
            llm: LangChain LLM instance; created automatically if not provided
            verbose: Print LLM explanations and validation details
            max_retries: Maximum validation retry attempts
        """
        if llm is None:
            llm_env_path = Path(__file__).parent.parent / "config" / "llm.env"
            llm_instance = create_llm(env_path=llm_env_path, verbose=verbose)
        else:
            llm_instance = llm

        self.service_name = service_name
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
        self._service_obj: Optional[Service] = None
        for service in services:
            if service.serviceId != service_name:
                continue
            # Handle None case by defaulting to "Tool"
            self.service_type = service.type or "Tool"
            self._service_obj = service
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
            self.service_name,
            self._service_obj,
            self.privileges,
        )

    def get_graph(self):
        """Return the compiled graph for visualization or inspection."""
        return self.graph

    def generate_policy(self, description: str) -> Policy:
        """
        Generate a service-scoped access control policy from a natural language description.

        Args:
            description: Natural language policy description

        Returns:
            Policy model instance with the generated policy and explanation.

        Raises:
            ValueError: If validation fails after all retries.
        """
        initial_state: ServicePolicyState = {
            "description": description,
            "service_name": self.service_name,
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

        self._last_yaml_output = final_state["yaml_output"]

        return final_state["policy_structure"]

    def get_yaml_output(self) -> str:
        """
        Return the YAML output from the last generate_policy() call.

        Raises:
            ValueError: If no policy has been generated yet.
        """
        if not hasattr(self, "_last_yaml_output"):
            raise ValueError("No policy available. Call generate_policy() first.")
        return self._last_yaml_output

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
