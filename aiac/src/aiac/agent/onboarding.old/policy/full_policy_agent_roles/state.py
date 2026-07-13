#!/usr/bin/env python3
"""
State Definitions for Role-Based Policy Builder

This module defines the TypedDict state structure used by the LangGraph
workflow for role-centric policy generation.
"""

from typing import TypedDict, Annotated, List, Optional
from operator import add

from aiac.policy.model.models import PolicyRule

class PolicyState(TypedDict):
    """
    State dictionary for the role-based policy building LangGraph workflow.

    Attributes:
        description: Original natural language policy description
        policy: Fully constructed Policy model
        messages: Accumulated list of LLM messages (for conversation history)
        errors: List of validation errors (replaced on each validation attempt)
        retry_count: Number of validation retry attempts made
        validation_passed: Boolean flag indicating if validation succeeded
    """
    description: str
    policy: Optional[list[PolicyRule]]
    messages: Annotated[List, add]  # Annotated with 'add' for accumulation
    errors: List[str]               # NOT accumulated - replaced on each validation attempt
    retry_count: int
    validation_passed: bool         # Boolean flag for retry decision, not accumulated
