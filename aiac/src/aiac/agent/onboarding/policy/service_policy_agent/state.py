#!/usr/bin/env python3
"""
State Definitions for Service Policy Agent

TypedDict state structure for the LangGraph workflow that generates a
partial access control policy scoped to a single Keycloak service.
"""

from typing import TypedDict, Annotated, List, Dict, Any, Optional
from operator import add

from aiac.pdp.policy.models import Policy


class ServicePolicyState(TypedDict):
    """
    State for the service-scoped policy building workflow.

    Attributes:
        description: Natural language policy description
        service_name: Keycloak service name to scope the policy to
        explanation: LLM explanation of the privilege mappings
        parsed_scopes: List of {role, privileges} mappings; each privilege dict
            carries a 'service' key holding a Service object (not a string)
        policy_structure: Fully constructed Policy model (set by _build_policy)
        yaml_output: Final YAML-formatted policy string
        messages: Accumulated LLM messages
        errors: Validation errors - replaced on each validation attempt
        retry_count: Number of validation retry attempts
        validation_passed: Whether the last validation pass succeeded
    """
    description: str
    service_name: str
    explanation: str
    parsed_scopes: List[Dict[str, Any]]
    policy_structure: Optional[Policy]
    yaml_output: str
    messages: Annotated[List, add]
    errors: List[str]
    retry_count: int
    validation_passed: bool
