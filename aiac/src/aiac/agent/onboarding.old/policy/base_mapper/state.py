#!/usr/bin/env python3
"""
Base state definition shared by single-item mapper agents.
"""

from typing import TypedDict, Annotated
from operator import add


class BaseMappingState(TypedDict):
    """
    Common fields shared by SinglePrivilegeState and SingleRoleState.

    Attributes:
        policy_description: Natural language policy description (context)
        explanation: LLM explanation of the mapping
        messages: Accumulated LLM messages
        errors: Validation errors - replaced on each validation attempt
        retry_count: Number of validation retry attempts made
        validation_passed: Whether the last validation pass succeeded
    """
    policy_description: str
    explanation: str
    messages: Annotated[list, add]
    errors: list[str]
    retry_count: int
    validation_passed: bool
