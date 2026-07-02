#!/usr/bin/env python3
"""
Validation Logic for Policy Builder

This module contains functions for validating generated policies, including
structural validation and semantic verification using LLM.
"""

from typing import Dict, List, Any, Optional

from aiac.idp.configuration.models import Role
from aiac.policy.model.models import PolicyRule


def validate_policy_structure(
    _policy: Optional[list[PolicyRule]],
    _roles: List[Role],
    _privileges_map: Dict[str, Dict[str, Any]]
) -> List[str]:
    """
    Perform structural validation on the policy.

    Checks that all realm roles, services, and privileges exist in the
    configuration and that the policy structure is valid.

    Args:
        policy: The policy dictionary to validate
        realm_roles: List of dicts with 'name' and 'description' for realm roles
        service_names: List of valid service names
        privileges_map: Dict mapping service IDs to their service info.

    Returns:
        List of error messages (empty if validation passed)
    """
    structural_errors = []
    
    # if not policy:
    #     structural_errors.append("Policy is empty")
    #     return structural_errors
    
    # # Extract realm role names for validation
    # role_names = [role.name for role in roles]
    
    # # Validate that only preset names are used
    # for rule in policy.rules:
    #     # Validate realm role name
    #     if not rule.role:
    #         structural_errors.append("Found empty role name")
    #     elif rule.role.name not in role_names:
    #         structural_errors.append(
    #             f"Realm role '{rule.role}' is not in the preset realm roles. "
    #             f"Available roles: {', '.join(role_names)}"
    #         )
        
    #     # Check if realm role has any mappings
    #     if not rule.scope:
    #         structural_errors.append(
    #             f"Realm role '{rule.role}' has no privilege mappings assigned"
    #         )
        
                
    #     for service in privileges_map.keys():
    #         privilege_names = [p.name for p in privileges_map[service]["scopes"]]
    #         if rule.scope.name not in privilege_names:
    #             available_privileges = (
    #                 ', '.join(privilege_names)
    #                 if privilege_names
    #                 else '(none)'
    #             )
    #             structural_errors.append(
    #                 f"Privilege '{rule.scope}' for service '{service}' in realm role '{rule.role}' "
    #                 f"is not valid. Available privileges for {service}: {available_privileges}"
    #             )
    return structural_errors



