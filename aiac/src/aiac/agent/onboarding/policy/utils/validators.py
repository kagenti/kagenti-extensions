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
    service_roles_map: Dict[str, List[Dict[str, str]]]
) -> List[str]:
    """
    Perform structural validation on the policy.
    
    Checks that all realm roles, services, and service roles exist in the
    configuration and that the policy structure is valid.
    
    Args:
        policy: The policy dictionary to validate
        realm_roles: List of dicts with 'name' and 'description' for realm roles
        service_names: List of valid service names
        service_roles_map: Dict mapping service names to list of role dicts with 'name' and 'description'
        
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
    for realm_role, service_role_mappings in policy.items():
        # Validate realm role name
        if not realm_role:
            structural_errors.append("Found empty realm role name")
        elif realm_role not in realm_role_names:
            structural_errors.append(
                f"Realm role '{realm_role}' is not in the preset realm roles. "
                f"Available roles: {', '.join(realm_role_names)}"
            )
        
        # Check if realm role has any mappings
        if not service_role_mappings:
            structural_errors.append(
                f"Realm role '{realm_role}' has no service role mappings assigned"
            )
        
        # Validate each service role mapping
        for mapping in service_role_mappings:
            if not isinstance(mapping, dict):
                structural_errors.append(
                    f"Invalid mapping format in realm role '{realm_role}': "
                    f"must be a dict with 'service' and 'role' keys"
                )
                continue
            
            service = mapping.get('service', '')
            role = mapping.get('role', '')
            
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
            
            # Validate role name for the service
            if not role:
                structural_errors.append(
                    f"Found empty role name for service '{service}' in realm role '{realm_role}'"
                )
            elif service in service_roles_map:
                # Extract role names from the service roles map
                service_role_names = [r['name'] for r in service_roles_map[service]]
                if role not in service_role_names:
                    available_roles = (
                        ', '.join(service_role_names)
                        if service_role_names
                        else '(none)'
                    )
                    structural_errors.append(
                        f"Role '{role}' for service '{service}' in realm role '{realm_role}' "
                        f"is not valid. Available roles for {service}: {available_roles}"
                    )
    
    return structural_errors



