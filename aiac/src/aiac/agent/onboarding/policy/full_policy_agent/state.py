#!/usr/bin/env python3
"""
State Definitions for Policy Builder

This module defines the TypedDict state structure used by the LangGraph
workflow for policy generation.
"""

from typing import TypedDict, Annotated, List, Dict, Any
from operator import add


class PolicyState(TypedDict):
    """
    State dictionary for the policy building LangGraph workflow.
    
    This TypedDict defines the state that flows through the state machine.
    Each node in the graph can read from and write to these fields.
    
    Attributes:
        description: Original natural language policy description
        explanation: LLM's explanation of how it mapped the policy
        parsed_scopes: List of role-to-privilege mappings built by aggregating SingleRoleMapper results
        policy_structure: Structured policy dictionary ready for YAML conversion
        yaml_output: Final YAML-formatted policy string
        messages: Accumulated list of LLM messages (for conversation history)
        errors: List of validation errors (replaced on each validation attempt)
        retry_count: Number of validation retry attempts made
        validation_passed: Boolean flag indicating if validation succeeded
    """
    description: str
    explanation: str
    parsed_scopes: List[Dict[str, Any]]
    policy_structure: Dict[str, Any]
    yaml_output: str
    messages: Annotated[List, add]  # Annotated with 'add' for accumulation
    errors: List[str]  # NOT accumulated - replaced on each validation attempt
    retry_count: int
    validation_passed: bool  # Boolean flag for retry decision, not accumulated

# Made with Bob
