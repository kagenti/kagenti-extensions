#!/usr/bin/env python3
"""
State Definitions for Single Privilege Mapper
"""

from aiac.pdp.library.configuration.models import Role, Scope
from base_mapper.state import BaseMappingState


class SinglePrivilegeState(BaseMappingState):
    """
    State for the single-privilege role mapping workflow.

    Extends BaseMappingState with privilege-specific fields:
        privilege: The privilege to analyze
        roles: List of available realm roles
        roles_with_access: Realm roles determined to have access to the privilege
    """
    privilege: Scope
    roles: list[Role]
    roles_with_access: list[Role]
