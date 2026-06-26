#!/usr/bin/env python3
"""
Rego policy file generation for the PDP (Policy Decision Point).

Public API:
    save_policy_rego: Write all Rego files for a Policy to a directory.
"""

from pathlib import Path
from typing import Any, Dict

from aiac.pdp.policy.models import PolicyObjectModel, Rule

__all__ = ["save_policy_rego"]


def _generate_realm_roles_rego(user_to_roles: dict) -> str:
    rego_content = """package authz.realm_roles

# Realm Roles Mapping
# Maps user names to lists of realm role names

realm_roles := {
"""
    user_entries = []
    for username in sorted(user_to_roles):
        username_escaped = username.replace('"', '\\"')
        roles = user_to_roles.get(username, [])
        role_list = []
        for role_name in roles:
            role_name_escaped = role_name.replace('"', '\\"')
            role_list.append(f'"{role_name_escaped}"')
        roles_str = ", ".join(role_list)
        user_entries.append(f'    "{username_escaped}": [{roles_str}]')
    rego_content += ",\n".join(user_entries)
    rego_content += "\n}\n"
    return rego_content


def _generate_privileges_rego(privileges_map: Dict[str, dict], scopes: list) -> str:
    rego_content = """package authz.privileges

# Service Privileges Mapping
# Maps service/client IDs to their available privileges

"""
    scope_names = [scope.name for scope in scopes]
    for service_name, service_info in privileges_map.items():
        rego_content += f'service["{service_name}"] := [\n'
        for priv in service_info["roles"]:
            priv_name = priv.get("name", "")
            priv_desc = priv.get("description", "").replace('"', '\\"')
            rego_content += f'    {{\n'
            rego_content += f'        "name": "{priv_name}",\n'
            rego_content += f'        "description": "{priv_desc}",\n'
            rego_content += f'        "scopes": [\n'
            for scope_name in scope_names:
                scope_name_escaped = scope_name.replace('"', '\\"')
                rego_content += f'            "{scope_name_escaped}",\n'
            rego_content += '        ]\n'
            rego_content += f'    }},\n'
        rego_content += "]\n\n"
    return rego_content


def _generate_default_inbound_rego() -> str:
    return """package authbridge.inbound.request

default allow := false
"""


def _generate_default_outbound_rego() -> str:
    return """package authbridge.outbound.request

default allow := false
"""


def _generate_policy_rego_inbound(
    rules: list[Rule],
    service: str,
    description: str =""
) -> str:
    """
    Generate Rego allow-rules for a single service.

    Returns:
        Rego file content as string
    """

    rego_content = f"""package authbridge.inbound.request

import data.authz.realm_roles.realm_roles

# Access Control Policy
# Uses user -> realm roles mapping to authorize privileges
# Policy entries map realm role names to privilege mappings
# Each entry specifies: service (service name) and privilege (privilege name from that service)

"""
    rego_content += f"# Service: {service}\n"
    if description:
        rego_content += "# Original Policy Description:\n"
        for line in description.strip().split('\n'):
            rego_content += f"#   {line.strip()}\n"
        rego_content += "#\n\n"

    for rule in rules:
        service_escaped = service.replace('"', '\\"')
        role_name_escaped = rule.role.name.replace('"', '\\"')
        rego_content += f"# Actor with role of **{role_name_escaped}**\n"
        rego_content += f"# may access service with id **{service_escaped}**\n"
        rego_content += f'allow if {{\n'
        rego_content += f'  "{role_name_escaped}" in object.get(realm_roles, input.identity.subject, [])\n'
        rego_content += f'  input.a2a.client_id == "{service_escaped}"\n'
        rego_content += "}\n\n"

    return rego_content


def save_policy_rego(
    policy: PolicyObjectModel,
    file_dir: str = "rego_policy",
    realm: str = "demo",
    policy_only: bool = False
) -> None:
    """
    Save Rego files for realm roles, defaults, and per-service access policy.

    Creates:
    - realm_roles.rego: user → realm-role mapping
    - default_inbound.rego / default_outbound.rego: deny-by-default rules
    - generated_policy_<service>.rego: one allow-rule file per service in the policy

    Args:
        policy: Policy model instance
        file_dir: Directory to save Rego files
        realm: Keycloak realm name (used to fetch user-to-roles mapping)
    """
    from aiac.pdp.library.configuration.api import Configuration

    dir_path = Path(file_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    # defaults and user data structure 
    if not policy_only:
        config_api = Configuration.for_realm(realm)
        user_to_roles: dict = {}
        for subject in config_api.get_subjects():
            user_to_roles[subject.username] = [role.name for role in subject.roles]

        realm_roles_path = dir_path / "realm_roles.rego"
        with open(realm_roles_path, "w") as f:
            f.write(_generate_realm_roles_rego(user_to_roles))
        print(f"Realm roles Rego saved to {realm_roles_path}")

        default_inbound_path = dir_path / "default_inbound.rego"
        with open(default_inbound_path, "w") as f:
            f.write(_generate_default_inbound_rego())
        print(f"Default Rego saved to {default_inbound_path}")

        default_outbound_path = dir_path / "default_outbound.rego"
        with open(default_outbound_path, "w") as f:
            f.write(_generate_default_outbound_rego())
        print(f"Default Rego saved to {default_outbound_path}")

    # # Deduplicate services by ID (Service is not hashable — cannot use a set)
    # unique_services: Dict[str, Any] = {}
    # for privs in policy.policy.values():
    #     for priv in privs:
    #         for svc in priv.services:
    #             svc_id = svc.serviceId or svc.name or svc.id
    #             unique_services[svc_id] = svc

    # service_types = {svc_id: svc.type for svc_id, svc in unique_services.items()}
    # policy_structure = {
    #     "policy": {
    #         realm_role: [
    #             {"service": svc.serviceId or svc.name or svc.id, "privilege": priv.name}
    #             for priv in privileges
    #             for svc in priv.services
    #         ]
    #         for realm_role, privileges in policy.policy.items()
    #     }
    # }

    service_id = "Dummy"
    policy_rego = _generate_policy_rego_inbound(policy.rules, service_id)
    safe_name = service_id.replace("/", "_").replace("\\", "_").replace(" ", "_")
    policy_path = dir_path / f"generated_policy_{safe_name}.rego"
    with open(policy_path, "w") as f:
        f.write(policy_rego)
    print(f"Generated policy Rego for service '{service_id}' saved to {policy_path}")
