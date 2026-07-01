#!/usr/bin/env python3
"""
Role-Based Policy Builder - Main Module

This module provides the PolicyBuilder class that orchestrates the
AI-powered generation of Keycloak access control policies from natural
language descriptions using LangGraph workflows.

Unlike full_policy_agent (which iterates over privileges via SinglePrivilegeMapper),
this agent iterates over realm **roles** via SingleRoleMapper and determines which
privileges each role should be granted.

Workflow Stages:
    1. build_policy: For each role, call SingleRoleMapper to determine its privileges,
                     then aggregate all Rules into a PolicyObjectModel.
    2. validate_policy: Validate structure (with retry).
"""

from typing import Optional, Dict
from pathlib import Path
import sys
from dataclasses import dataclass

from langgraph.graph import StateGraph, END
from langchain_core.language_models import BaseChatModel

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import Role, Service
from config import create_llm
from full_policy_agent_roles.state import PolicyState
from config.constants import MAX_VALIDATION_RETRIES
from aiac.pdp.policy.models import PolicyObjectModel, Rule
from single_role_agent import SingleRoleMapper
from utils.validators import validate_policy_structure
from aiac.pdp.policy.builders.yaml import _generate_yaml_output


@dataclass
class PolicyBuilderConfig:
    """
    Configuration for PolicyBuilder agent.

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

def _build_policy(
    state: PolicyState,
    llm: BaseChatModel,
    roles: list[Role],
    privileges_map: dict,
    verbose: bool,
) -> PolicyState:
    """
    Map each realm role to its privileges using SingleRoleMapper, then aggregate
    results into a PolicyObjectModel.

    For every role, SingleRoleMapper determines which privileges that role should
    hold. The per-role results are collected into a flat list of Rule objects.

    Args:
        state: Current PolicyState with 'description' field
        llm: LLM instance for processing
        roles: List of available realm roles
        privileges_map: Dict mapping service names to privileges
        verbose: Whether to print detailed output

    Returns:
        Updated PolicyState with policy and explanation
    """
    # Flatten all scopes across services into a single list for each role mapper
    all_scopes = []
    for _, service_info in privileges_map.items():
        all_scopes.extend(service_info["scopes"])

    explanations = []
    policy_rules: list[Rule] = []

    for role in roles:
        mapper = SingleRoleMapper(
            llm=llm,
            verbose=verbose,
            role=role,
            privileges=all_scopes,
        )

        result = mapper.generate_policy(description=state["description"])

        explanations.append(
            f"**{role.name}**: {result.explanation}"
        )

        policy_rules.extend(result.rules)

    policy = PolicyObjectModel(
        rules=policy_rules,
        explanation="\n\n".join(explanations) if explanations else "",
    )

    return {
        **state,
        "policy": policy,
        "messages": [],
        "errors": [],
        "retry_count": state.get("retry_count", 0),
        "validation_passed": True,
    }


def _validate_policy(
    state: PolicyState,
    roles: list[Role],
    privileges_map: dict,
    max_retries: int,
) -> PolicyState:
    """
    Validate the generated Policy model against structural rules.

    Args:
        state: PolicyState with 'policy' as a PolicyObjectModel instance
        roles: List of available realm roles
        privileges_map: Dict mapping service names to privileges
        max_retries: Maximum retry attempts

    Returns:
        Updated PolicyState with errors and validation_passed fields
    """
    retry_count = state.get("retry_count", 0)
    policy_obj: Optional[PolicyObjectModel] = state.get("policy")

    structural_errors = validate_policy_structure(
        policy_obj,
        roles,
        privileges_map,
    )

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


def _should_retry_validation(state: PolicyState, max_retries: int) -> str:
    """
    Determine if validation should retry by going back to build_policy_node.

    This is a conditional edge function for the LangGraph state machine.

    Args:
        state: Current PolicyState containing validation results
        max_retries: Maximum retry attempts allowed

    Returns:
        "build_policy" if validation failed and retries remain, otherwise END
    """
    validation_passed = state.get("validation_passed", False)
    retry_count = state.get("retry_count", 0)
    errors = state.get("errors", [])

    if not validation_passed and retry_count < max_retries:
        print(
            f"\n⚠️  Validation failed (attempt {retry_count}/{max_retries}). "
            "Retrying from build_policy_node..."
        )
        if errors:
            print("\nValidation Errors (from this attempt):")
            for i, error in enumerate(errors, 1):
                print(f"  {i}. {error}")
            print()
        return "build_policy"

    return END


def create_role_policy_builder_graph(
    config: PolicyBuilderConfig,
    roles: list[Role],
    privileges_map: dict,
):
    """
    Create and compile the role-based policy builder graph.

    Args:
        config: PolicyBuilderConfig instance
        roles: List of available realm roles
        privileges_map: Dict mapping service names to privileges

    Returns:
        Compiled LangGraph workflow
    """

    def build_policy_node(state: PolicyState) -> PolicyState:
        return _build_policy(state, config.llm, roles, privileges_map, config.verbose)

    def validate_policy_node(state: PolicyState) -> PolicyState:
        return _validate_policy(state, roles, privileges_map, config.max_retries)

    def should_retry_node(state: PolicyState) -> str:
        return _should_retry_validation(state, config.max_retries)

    workflow = StateGraph(PolicyState)

    workflow.add_node("build_policy", build_policy_node)
    workflow.add_node("validate_policy", validate_policy_node)

    workflow.set_entry_point("build_policy")
    workflow.add_edge("build_policy", "validate_policy")

    workflow.add_conditional_edges(
        "validate_policy",
        should_retry_node,
        {
            "build_policy": "build_policy",
            END: END,
        },
    )

    return workflow.compile()


class PolicyBuilder:
    """
    AI-powered role-centric access control policy builder using LangGraph.

    Unlike PolicyBuilder (which iterates over privileges), this builder iterates
    over realm roles and uses SingleRoleMapper to determine which privileges each
    role should be granted.

    Workflow Stages:
        1. build_policy: For each role invoke SingleRoleMapper, then aggregate Rules
        2. validate_policy: Validate structure and semantics (with retry)
    """

    def __init__(
        self,
        llm: BaseChatModel,
        realm: str = "demo",
        verbose: bool = True,
        max_retries: int = MAX_VALIDATION_RETRIES,
    ):
        """
        Initialize the role-based policy builder.

        Args:
            realm: Realm name for fetching configuration data
            llm: Optional LangChain LLM instance. Created automatically if not provided.
            verbose: If True, print LLM explanations and validation details
            max_retries: Maximum validation retry attempts
        """
        self.realm = realm

        self.config = PolicyBuilderConfig(
            llm=llm,
            verbose=verbose,
            max_retries=max_retries,
        )

        config_api = Configuration.for_realm(realm)
        subjects = config_api.get_subjects()
        all_roles = [r for sublist in [subject.roles for subject in subjects] for r in sublist]
        role_map: dict[str,Role] = {}
        for r in all_roles:
            role_map[r.name] = r
        roles = list(role_map.values())
        print(f"Got {len(roles)} roles")
        self.roles = [r for r in roles if r.description]

        services = config_api.get_services()
        print(f"Got {len(services)} services")
        self.privileges_map: Dict[str, dict] = {}
        self.service_names: list[str] = []
        self.services_by_name: Dict[str, Service] = {}
        for service in services:
            if not service.description or not ("Demo" in service.description):
                continue
            service_name = service.name or service.id
            print(f"Service {service_name} added: <{service.description}> <{service.type}>")
            described_scopes = [scope for scope in service.scopes if scope.description]
            if not described_scopes:
                continue
            self.privileges_map[service_name] = {
                "service_type": service.type,
                "scopes": described_scopes,
            }
            self.service_names.append(service_name)
            self.services_by_name[service_name] = service

        self.graph = create_role_policy_builder_graph(
            self.config,
            self.roles,
            self.privileges_map,
        )

    def get_graph(self):
        """Return the compiled graph for visualization or inspection."""
        return self.graph

    def generate_policy(self, description: str) -> PolicyObjectModel:
        """
        Generate an access control policy from a natural language description.

        Args:
            description: Natural language description of the access control policy

        Returns:
            PolicyObjectModel instance with the generated policy and explanation.

        Raises:
            ValueError: If validation fails after all retries.
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
        if not hasattr(self, "_last_policy_structure") or self._last_policy_structure is None:
            raise ValueError(
                "No policy available. Generate a policy first using generate_policy()."
            )
        return _generate_yaml_output(self._last_policy_structure)


if __name__ == "__main__":
    print("Please use aiac_cli.py to run the role-based policy builder.")
    sys.exit(1)
