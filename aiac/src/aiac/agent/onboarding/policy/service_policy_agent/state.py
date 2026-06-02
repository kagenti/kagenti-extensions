#!/usr/bin/env python3
"""
State Definitions for Service Policy Agent

TypedDict state structure for the LangGraph workflow that generates a
partial access control policy scoped to a single Keycloak service.
"""

from typing import TypedDict, Annotated, List, Dict, Any
from operator import add


class ServicePolicyState(TypedDict):
    """
    State for the service-scoped policy building workflow.

    Attributes:
        description: Natural language policy description
        service_id: Keycloak service ID to scope the policy to
        explanation: LLM explanation of the role mappings
        parsed_scopes: List of {role, service_roles} mappings (realm-role to service-roles)
        policy_structure: Structured policy dict ready for YAML conversion
        yaml_output: Final YAML-formatted policy string
        messages: Accumulated LLM messages
        errors: Validation errors - replaced on each validation attempt
        retry_count: Number of validation retry attempts
        validation_passed: Whether the last validation pass succeeded
    """
    description: str
    service_id: str
    explanation: str
    parsed_scopes: List[Dict[str, Any]]
    policy_structure: Dict[str, Any]
    yaml_output: str
    messages: Annotated[List, add]
    errors: List[str]
    retry_count: int
    validation_passed: bool
