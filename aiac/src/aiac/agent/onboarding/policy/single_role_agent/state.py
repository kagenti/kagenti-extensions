#!/usr/bin/env python3
"""
State Definitions for Single Role Scope Mapper
"""

from aiac.idp.configuration.models import Role, Scope
from base_mapper.state import BaseMappingState


class SingleRoleState(BaseMappingState):
    """
    State for the single-role to privileges mapping workflow.

    Extends BaseMappingState with role-specific fields:
        role: The realm role to analyze
        privileges: Available privileges to assign
        granted_privileges: Privileges determined to belong to this role
    """
    role: Role
    privileges: list[Scope]
    granted_privileges: list[Scope]
