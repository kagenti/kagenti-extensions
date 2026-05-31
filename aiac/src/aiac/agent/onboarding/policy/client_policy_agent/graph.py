#!/usr/bin/env python3
"""
Client Policy Agent

Generates a partial access control policy that contains only the rules
relevant for a single specified Keycloak client.  Inputs are a natural
language policy description and a client ID; output is a YAML policy
with realm-role → client-role mappings scoped to that client.

Workflow:
    1. filter_and_extract  — run SingleRoleMapper for every role of the
                             given client and aggregate the results.
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
from client_policy_agent.state import ClientPolicyState
from config.constants import MAX_VALIDATION_RETRIES
from aiac.keycloak.library import api_from_config
from single_role_agent import SingleRoleMapper
from utils.validators import validate_policy_structure


@dataclass
class ClientPolicyBuilderConfig:
    """
    Configuration for the ClientPolicyBuilder agent.

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
    state: ClientPolicyState,
    llm: BaseChatModel,
    realm_roles: list,
    client_roles: list,
    verbose: bool,
) -> ClientPolicyState:
    """
    Run SingleRoleMapper for every role of the target client and invert the
    results into the {role → client_roles} structure used by _build_policy.

    Args:
        state: Current ClientPolicyState (needs 'description' and 'client_id')
        llm: LLM instance
        realm_roles: All available realm roles [{name, description}]
        client_roles: Roles belonging to the target client [{name, description}]
        verbose: Whether to print detailed output

    Returns:
        Updated ClientPolicyState with parsed_scopes and explanation
    """
    client_id = state["client_id"]
    mapper = SingleRoleMapper(llm=llm, verbose=verbose)

    explanations: list[str] = []
    realm_role_to_client_roles: dict = {}

    for client_role in client_roles:
        result = mapper.map_role(
            policy_description=state["description"],
            client_name=client_id,
            client_role=client_role,
            realm_roles=realm_roles,
        )

        if result.get("explanation"):
            explanations.append(f"{client_id}/{client_role['name']}: {result['explanation']}")

        for realm_role_name in result.get("real_roles_with_access", []):
            realm_role_to_client_roles.setdefault(realm_role_name, []).append(
                {"client": client_id, "role": client_role["name"]}
            )

    parsed_scopes = [
        {"role": realm_role, "client_roles": cr_list}
        for realm_role, cr_list in realm_role_to_client_roles.items()
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


def _build_policy(state: ClientPolicyState) -> ClientPolicyState:
    """
    Assemble the structured policy dict from parsed_scopes.

    Returns:
        Updated ClientPolicyState with policy_structure
    """
    policy: dict = {}
    for entry in state["parsed_scopes"]:
        policy[entry["role"]] = entry["client_roles"]

    return {**state, "policy_structure": {"policy": policy}}


def _generate_yaml(state: ClientPolicyState) -> ClientPolicyState:
    """
    Render the policy structure as a YAML string with explanatory comments.

    Returns:
        Updated ClientPolicyState with yaml_output
    """
    client_id = state.get("client_id", "")
    header = (
        "# Partial Access Control Policy\n"
        f"# Scoped to client: {client_id}\n"
        "# Maps realm roles to the client roles they may access.\n\n"
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
    footer = "\n# Generated by ClientPolicyBuilder using LangGraph\n"

    return {**state, "yaml_output": header + yaml_content + footer}


def _validate_policy(
    state: ClientPolicyState,
    llm: BaseChatModel,
    realm_roles: list,
    client_id: str,
    client_roles: list,
    verbose: bool,
    max_retries: int,
) -> ClientPolicyState:
    """
    Structural validation of the generated policy.

    Returns:
        Updated ClientPolicyState with errors and validation_passed
    """
    retry_count = state.get("retry_count", 0)
    policy = state["policy_structure"].get("policy", {})
    client_names = [client_id]
    client_roles_map = {client_id: client_roles}

    structural_errors = validate_policy_structure(
        policy, realm_roles, client_names, client_roles_map
    )
    # An empty policy is valid for a client-scoped agent: the policy description
    # may simply not grant any permissions to this client's services.
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


def _should_retry(state: ClientPolicyState, max_retries: int) -> str:
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

def create_client_policy_builder_graph(
    config: ClientPolicyBuilderConfig,
    realm_roles: list,
    client_id: str,
    client_roles: list,
):
    """
    Build and compile the client-scoped policy builder graph.

    Args:
        config: ClientPolicyBuilderConfig
        realm_roles: All realm roles [{name, description}]
        client_id: Target Keycloak client ID
        client_roles: Roles of the target client [{name, description}]

    Returns:
        Compiled LangGraph workflow
    """

    def filter_and_extract_node(state: ClientPolicyState) -> ClientPolicyState:
        return _filter_and_extract_scopes(
            state, config.llm, realm_roles, client_roles, config.verbose
        )

    def build_policy_node(state: ClientPolicyState) -> ClientPolicyState:
        return _build_policy(state)

    def generate_yaml_node(state: ClientPolicyState) -> ClientPolicyState:
        return _generate_yaml(state)

    def validate_policy_node(state: ClientPolicyState) -> ClientPolicyState:
        return _validate_policy(
            state, config.llm, realm_roles, client_id, client_roles,
            config.verbose, config.max_retries
        )

    def should_retry_node(state: ClientPolicyState) -> str:
        return _should_retry(state, config.max_retries)

    workflow = StateGraph(ClientPolicyState)
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

class ClientPolicyBuilder:
    """
    AI-powered policy builder scoped to a single Keycloak client.

    Given a natural language policy description and a client ID, produces
    a YAML access control policy that contains only the realm-role →
    client-role mappings relevant to that client.

    Workflow:
        1. filter_and_extract  — map each role of the client to realm roles
        2. build_policy        — assemble the structured policy dict
        3. generate_yaml       — render YAML with comments
        4. validate_policy     — structural validation with retry
    """

    def __init__(
        self,
        client_id: str,
        realm: str = "",
        config_path: Optional[Path] = None,
        llm: Optional[BaseChatModel] = None,
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ):
        """
        Args:
            client_id: Keycloak client ID to scope the policy to
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
            os.environ["AC_CONFIG_PATH"] = str(config_path)

        self.client_id = client_id
        self.config = ClientPolicyBuilderConfig(
            llm=llm_instance,
            verbose=verbose,
            max_retries=max_retries,
        )

        realm_roles_models = api_from_config.get_realm_roles(realm=realm)
        self.realm_roles = [
            {"name": r.name, "description": r.description or ""}
            for r in realm_roles_models
        ]

        client_roles_map = api_from_config.get_client_roles_map(realm=realm)
        self.client_roles = client_roles_map.get(client_id, [])

        self.graph = create_client_policy_builder_graph(
            self.config,
            self.realm_roles,
            self.client_id,
            self.client_roles,
        )

    def get_graph(self):
        """Return the compiled graph for visualization or inspection."""
        return self.graph

    def generate_policy(self, description: str) -> Dict[str, Any]:
        """
        Generate a client-scoped access control policy from a natural language description.

        Args:
            description: Natural language policy description

        Returns:
            dict with keys:
                yaml_output (str)       — YAML policy file content
                policy_structure (dict) — structured policy data
                parsed_scopes (list)    — raw realm-role → client-role mappings
                errors (list)           — validation errors (empty on success)
                success (bool)          — True when no validation errors
                retry_count (int)       — number of validation retries
        """
        initial_state: ClientPolicyState = {
            "description": description,
            "client_id": self.client_id,
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

    def save_policy(self, yaml_output: str, filepath: str = "client_policy.yaml"):
        """
        Save the generated policy YAML to a file.

        Args:
            yaml_output: YAML content string
            filepath: Destination file path
        """
        with open(filepath, "w") as f:
            f.write(yaml_output)
        print(f"Client policy saved to {filepath}")


if __name__ == "__main__":
    print("Use ClientPolicyBuilder programmatically or via the CLI.")
    sys.exit(1)
