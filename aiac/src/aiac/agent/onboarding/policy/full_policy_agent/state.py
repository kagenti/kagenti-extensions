#!/usr/bin/env python3
"""
State Definitions for Policy Builder

This module defines the TypedDict state structure used by the LangGraph
workflow for policy generation.
"""

from typing import TypedDict, Annotated, List, Dict, Any, Optional
from operator import add

from aiac.pdp.policy.models import Policy


class PolicyState(TypedDict):
    """
    State dictionary for the policy building LangGraph workflow.

    Attributes:
        description: Original natural language policy description
        explanation: LLM's explanation of how it mapped the policy
        parsed_scopes: List of role-to-privilege mappings; each privilege dict
            carries a 'service' key holding a Service object (not a string)
        policy_structure: Fully constructed Policy model (set by _build_policy)
        yaml_output: Final YAML-formatted policy string
        messages: Accumulated list of LLM messages (for conversation history)
        errors: List of validation errors (replaced on each validation attempt)
        retry_count: Number of validation retry attempts made
        validation_passed: Boolean flag indicating if validation succeeded
    """
    description: str
    explanation: str
    parsed_scopes: List[Dict[str, Any]]
    policy_structure: Optional[Policy]
    yaml_output: str
    messages: Annotated[List, add]  # Annotated with 'add' for accumulation
    errors: List[str]  # NOT accumulated - replaced on each validation attempt
    retry_count: int
    validation_passed: bool  # Boolean flag for retry decision, not accumulated

# Made with Bob
