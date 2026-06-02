#!/usr/bin/env python3
"""
State Definitions for Single Privilege Mapper

This module defines the TypedDict state structure used by the LangGraph
workflow for mapping a single privilege to real roles that should have access.
"""

from typing import TypedDict, Annotated, List, Dict
from operator import add


class SinglePrivilegeState(TypedDict):
    """
    State dictionary for the single privilege mapping LangGraph workflow.

    Attributes:
        policy_description: Natural language policy description (context for the mapping)
        service_name: Name of the service that owns the privilege
        privilege: Dict with 'name' and 'description' of the privilege to analyze
        realm_roles: List of available realm roles with descriptions
        explanation: LLM's explanation of which real roles should have access
        real_roles_with_access: List of realm role names that should have access
        messages: Accumulated list of LLM messages (for conversation history)
        errors: List of validation errors (replaced on each validation attempt)
        retry_count: Number of validation retry attempts made
        validation_passed: Boolean flag indicating if validation succeeded
    """
    policy_description: str
    service_name: str
    privilege: Dict[str, str]
    realm_roles: List[Dict[str, str]]
    explanation: str
    real_roles_with_access: List[str]
    messages: Annotated[List, add]  # Annotated with 'add' for accumulation
    errors: List[str]  # NOT accumulated - replaced on each validation attempt
    retry_count: int
    validation_passed: bool  # Boolean flag for retry decision, not accumulated

# Made with Bob