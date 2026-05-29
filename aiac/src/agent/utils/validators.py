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
    client_names: List[str],
    client_roles_map: Dict[str, List[Dict[str, str]]]
) -> List[str]:
    """
    Perform structural validation on the policy.
    
    Checks that all realm roles, clients, and client roles exist in the
    configuration and that the policy structure is valid.
    
    Args:
        policy: The policy dictionary to validate
        realm_roles: List of dicts with 'name' and 'description' for realm roles
        client_names: List of valid client names
        client_roles_map: Dict mapping client names to list of role dicts with 'name' and 'description'
        
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
    for realm_role, client_role_mappings in policy.items():
        # Validate realm role name
        if not realm_role:
            structural_errors.append("Found empty realm role name")
        elif realm_role not in realm_role_names:
            structural_errors.append(
                f"Realm role '{realm_role}' is not in the preset realm roles. "
                f"Available roles: {', '.join(realm_role_names)}"
            )
        
        # Check if realm role has any mappings
        if not client_role_mappings:
            structural_errors.append(
                f"Realm role '{realm_role}' has no client role mappings assigned"
            )
        
        # Validate each client role mapping
        for mapping in client_role_mappings:
            if not isinstance(mapping, dict):
                structural_errors.append(
                    f"Invalid mapping format in realm role '{realm_role}': "
                    f"must be a dict with 'client' and 'role' keys"
                )
                continue
            
            client = mapping.get('client', '')
            role = mapping.get('role', '')
            
            # Validate client name
            if not client:
                structural_errors.append(
                    f"Found empty client name in realm role '{realm_role}'"
                )
            elif client not in client_names:
                structural_errors.append(
                    f"Client '{client}' in realm role '{realm_role}' is not in "
                    f"the preset client names. Available clients: {', '.join(client_names)}"
                )
            
            # Validate role name for the client
            if not role:
                structural_errors.append(
                    f"Found empty role name for client '{client}' in realm role '{realm_role}'"
                )
            elif client in client_roles_map:
                # Extract role names from the client roles map
                client_role_names = [r['name'] for r in client_roles_map[client]]
                if role not in client_role_names:
                    available_roles = (
                        ', '.join(client_role_names)
                        if client_role_names
                        else '(none)'
                    )
                    structural_errors.append(
                        f"Role '{role}' for client '{client}' in realm role '{realm_role}' "
                        f"is not valid. Available roles for {client}: {available_roles}"
                    )
    
    return structural_errors



