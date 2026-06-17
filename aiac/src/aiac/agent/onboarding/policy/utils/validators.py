#!/usr/bin/env python3
"""
Validation Logic for Policy Builder

This module contains functions for validating generated policies, including
structural validation and semantic verification using LLM.
"""

from typing import Dict, List, Any


def validate_policy_structure(
    policy: Dict[str, Any],
    realm_roles: List[Dict[str, str]],
    service_names: List[str],
    privileges_map: Dict[str, Dict[str, Any]]
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
            Each value is ``{"service_type": str, "roles": [{"name": str, "description": str}]}``.
            service_type is a property of the service, not of individual roles.

    Returns:
        List of error messages (empty if validation passed)
    """
    structural_errors = []
    
    if not policy:
        structural_errors.append("Policy is empty")
        return structural_errors
    
    # Extract realm role names for validation
    realm_role_names = [role['name'] for role in realm_roles]
    
    # Validate that only preset names are used
    for realm_role, privilege_mappings in policy.items():
        # Validate realm role name
        if not realm_role:
            structural_errors.append("Found empty realm role name")
        elif realm_role not in realm_role_names:
            structural_errors.append(
                f"Realm role '{realm_role}' is not in the preset realm roles. "
                f"Available roles: {', '.join(realm_role_names)}"
            )
        
        # Check if realm role has any mappings
        if not privilege_mappings:
            structural_errors.append(
                f"Realm role '{realm_role}' has no privilege mappings assigned"
            )
        
        # Validate each privilege mapping
        for mapping in privilege_mappings:
            if not isinstance(mapping, dict):
                structural_errors.append(
                    f"Invalid mapping format in realm role '{realm_role}': "
                    f"must be a dict with 'service' and 'privilege' keys"
                )
                continue
            
            service = mapping.get('service', '')
            privilege = mapping.get('privilege', '')
            
            # Validate service name
            if not service:
                structural_errors.append(
                    f"Found empty service name in realm role '{realm_role}'"
                )
            elif service not in service_names:
                structural_errors.append(
                    f"Service '{service}' in realm role '{realm_role}' is not in "
                    f"the preset service names. Available services: {', '.join(service_names)}"
                )
            
            # Validate privilege name for the service
            if not privilege:
                structural_errors.append(
                    f"Found empty privilege name for service '{service}' in realm role '{realm_role}'"
                )
            elif service in privileges_map:
                privilege_names = [p['name'] for p in privileges_map[service]["roles"]]
                if privilege not in privilege_names:
                    available_privileges = (
                        ', '.join(privilege_names)
                        if privilege_names
                        else '(none)'
                    )
                    structural_errors.append(
                        f"Privilege '{privilege}' for service '{service}' in realm role '{realm_role}' "
                        f"is not valid. Available privileges for {service}: {available_privileges}"
                    )
    
    return structural_errors



