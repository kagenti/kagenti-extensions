#!/usr/bin/env python3
"""
Single Role Prompt Builder for Access Mapping

This module contains functions for building LLM prompts used to determine
which real roles should have access to a specific client role.
"""

from typing import List, Dict, Optional


def build_single_role_system_prompt(
    realm_roles: List[Dict[str, str]],
    client_role: Dict[str, str],
    policy_description: str = "",
    client_name: str = "",
) -> str:
    """
    Build a system prompt for mapping a single client role to realm roles.

    This function constructs a comprehensive prompt that guides the LLM through
    the process of determining which realm roles should have access to a specific
    client role based on semantic analysis of role descriptions and policy context.

    Args:
        realm_roles: List of dicts with 'name' and 'description' for realm roles
        client_role: Dict with 'name' and 'description' for the client role to analyze
        policy_description: Optional natural language policy description for context
        client_name: Name of the client service that owns the role

    Returns:
        Formatted system prompt string ready for LLM consumption
    """
    # Build available realm roles list with descriptions
    available_roles_lines = []
    for role in realm_roles:
        role_name = role['name']
        role_desc = role.get('description', '')
        if role_desc:
            available_roles_lines.append(f"  - {role_name}: {role_desc}")
        else:
            available_roles_lines.append(f"  - {role_name}")

    available_roles = (
        "\n".join(available_roles_lines)
        if available_roles_lines
        else "  (none defined)"
    )

    # Format the client role information
    client_role_name = client_role['name']
    client_role_desc = client_role.get('description', '')
    client_role_info = client_role_name
    if client_role_desc:
        client_role_info += f": {client_role_desc}"

    # Add policy context if provided
    policy_context = ""
    if policy_description:
        policy_context = f"""
POLICY CONTEXT:
The following policy description provides context for this access control decision:

{policy_description}

Use this policy context to understand the access requirements and make informed decisions
about which real roles should have access to the client role.

"""

    return f"""You are an expert at analyzing access control requirements and mapping client role capabilities to appropriate user roles.
{policy_context}TASK OVERVIEW:
You are given:
1. A list of all available realm roles with their descriptions
2. A single client role with its description

Your task is to determine which realm roles should have access to this client role.

AVAILABLE REALM ROLES:
{available_roles}

CLIENT ROLE TO ANALYZE:
{client_role_info}

ANALYSIS GUIDELINES:
1. IDENTIFY AND MAP ALL USER CATEGORIES (CRITICAL):
   - The policy may describe multiple user categories (e.g., "Group A", "Group B")
   - Each user category MUST map to at least one realm role
   - Use role descriptions to find the best match for each category
   - Broad terms (e.g., "all other staff") may map to multiple realm roles

2. ENABLING / GATEWAY SERVICES - CRITICAL - READ CAREFULLY:
   An enabling service is one whose description says it provides access TO another service
   or technology (e.g., "Access to the data warehouse connector", "Access to the payment
   gateway", "Access to the data pipeline").

   DOMAIN REQUIREMENT - AN ENABLING SERVICE MUST BE IN THE SAME DOMAIN AS THE POLICY:
   - "Access to the data warehouse connector" IS an enabling service for a data warehouse policy (same domain)
   - "Access to the monitoring dashboard UI" is NOT an enabling service for a data warehouse policy (different domain)
   - "Access to the payment gateway" is NOT an enabling service for a document storage policy (different domains)
   - Even if a role description matches the "access to [service]" pattern, it is only enabling
     if the service is directly required to reach the resource the policy is about.

   RULE: ALL user categories that need the downstream resource at ANY access level
   MUST be granted this enabling role.

   ACCESS LEVEL DOES NOT MATTER FOR ENABLING SERVICES:
   - "read-only access to data files" still requires the data warehouse connector
   - "limited access to data" still requires the data pipeline service
   - The enabling service is a prerequisite - without it, the user cannot reach the
     downstream resource at all, regardless of how limited their access is.

   DO NOT confuse enabling services with final resource roles:
   - ENABLING: "Access to the data warehouse connector" - needed by everyone with data access
   - FINAL: "Access to public data files" - needed only by those with public access
   - FINAL: "Access to confidential data records" - needed only by those with full access

   DO NOT exclude user categories based on their realm role name:
   - A "sales" realm role that needs data access still needs the data warehouse connector
   - A "support" realm role that needs read-only access still needs the enabling service
   - The realm role name is irrelevant - only whether the policy grants them ANY access matters

   EXAMPLE: Policy says "Group A gets full data warehouse access; Group B (including
   non-technical roles) gets read-only data warehouse access".
   - Role "Access to the data warehouse connector": BOTH Group A AND Group B need it - ["role-a", "role-b"]
   - Role "Full data access": only Group A - ["role-a"]
   - Role "Read-only data access": only Group B - ["role-b"]

3. ACCESS LEVEL DIFFERENTIATION (only for FINAL resource roles):
   - Pay close attention to access-level qualifiers: "private" vs "public",
     "full access" vs "limited", "read-only" vs "read-write"
   - For a "both X and Y" capability: grant BOTH roles to the relevant categories
   - For "only X" capability: grant ONLY the X role
   - Access level differentiation applies only when there are multiple roles for the SAME
     final resource (e.g., data-full-access vs data-read-only), NOT for enabling services.

4. PRINCIPLE OF LEAST PRIVILEGE AND POLICY SILENCE:
   - Grant access ONLY when explicitly required by the policy or role description
   - When in doubt, do NOT grant access
   - POLICY SILENCE = NO ACCESS: If the policy description does not mention this client's
     service or domain at all, return []. Do NOT infer access from the user role name
     (e.g., "developer") or from what that user type might typically do in their job.
     Access is determined solely by what the POLICY TEXT explicitly states.
   - Exception: enabling/gateway services are required by all users of the downstream resource.

5. EXACT NAMES ONLY:
   - Use ONLY the exact role names from the "Available Real Roles" list
   - Do not modify, abbreviate, or create new role names

TASK STEPS:
1. RELEVANCE CHECK: What is the DOMAIN of this client role (e.g., "data warehouse", "UI dashboards", "payments")?
   What is the DOMAIN of the policy subject? If they are DIFFERENT domains, return [] immediately.
   Do NOT continue to the next steps.
   IMPORTANT: The policy must explicitly mention the client role's domain. Do NOT reason from
   the user role name (e.g., "developers use demo UIs too") — that is forbidden here.
   - "Access to the monitoring dashboard UI" — domain: dashboards. Policy about data warehouse — DIFFERENT → []
   - "Access to the data warehouse connector" — domain: data warehouse. Policy about data warehouse — SAME → continue
   - "Access to confidential data records" — domain: data warehouse. Policy about data warehouse — SAME → continue
   - "Access to the demo UI interface" — domain: web UI. Policy about GitHub repos — DIFFERENT → []
     (Even though "developers" may use demo UIs in general, the policy says nothing about UI access → [])
2. CLASSIFY this client role: is it a FINAL resource role or an ENABLING/GATEWAY service?
   - ENABLING/GATEWAY: description says "access to [some service/agent/pipeline/gateway]"
     AND the service is in the same domain as the policy
   - FINAL RESOURCE: description says "access to [data/repos/files/records]"
3. IDENTIFY USER CATEGORIES: List all user categories mentioned in the policy.
4. APPLY RULE:
   - ENABLING/GATEWAY: grant to ALL user categories that need the downstream resource
   - FINAL RESOURCE: grant only to categories with explicit access to this specific capability
5. MAP TO REALM ROLES: For each included user category, find matching realm role(s).
6. VERIFY: Every included user category maps to at least one realm role.
7. EXPLAIN: Brief explanation citing the domain check, classification, policy evidence, and mapping.
8. OUTPUT JSON: List of realm role names that should have access.

Return in this format:
```explanation
[Your brief explanation: why relevant or not, which user categories
need access, how they map to realm roles]
```

```json
{{
  "client_role": "{client_role_name}",
  "real_roles_with_access": [
    "exact-realm-role-name-1",
    "exact-realm-role-name-2"
  ]
}}
```

EXAMPLE OUTPUTS:

Example A — domain mismatch, not relevant to policy subject:
```explanation
Step 1 RELEVANCE CHECK: client role domain is "monitoring dashboard UI". Policy domain is
"data warehouse access". These are DIFFERENT domains — dashboard UI is unrelated to data
warehouse access. Returning [] immediately without further analysis.
Note: Even if "developers" or "analysts" typically use dashboard UIs, the policy is silent
about UI access. POLICY SILENCE = NO ACCESS.
```
```json
{{"client_role": "monitoring-dashboard", "real_roles_with_access": []}}
```

Example A2 — domain mismatch: UI role, GitHub policy:
```explanation
Step 1 RELEVANCE CHECK: client role domain is "demo UI interface". Policy domain is
"GitHub repository access". These are DIFFERENT domains. The policy mentions only GitHub
repositories; it says nothing about any UI or web interface. POLICY SILENCE = NO ACCESS.
Returning [] immediately. (The fact that "developers" may use demo UIs is irrelevant —
access is determined by the policy text, not by job function assumptions.)
```
```json
{{"client_role": "demo-ui", "real_roles_with_access": []}}
```

Example B — enabling/gateway service (ALL users who need the downstream resource):
```explanation
Step 1 RELEVANCE CHECK: client role domain is "data warehouse connector". Policy domain is
"data warehouse access". SAME domain — continue.
Step 2 CLASSIFY: ENABLING SERVICE — "Access to the data warehouse connector" is a prerequisite
service, not a final resource. Policy identifies two user categories: Group A (full access)
and Group B (read-only). Both need ANY level of data warehouse access, so both need this
enabling service. Access level does NOT matter for enabling services.
Realm role mapping: role-a → Group A, role-b → Group B.
```
```json
{{"client_role": "warehouse-connector", "real_roles_with_access": ["role-a", "role-b"]}}
```

Example C — restricted role, limited access:
```explanation
Step 1 RELEVANCE CHECK: client role domain is "confidential data records". Policy domain is
"data warehouse access". SAME domain — continue.
Step 2 CLASSIFY: FINAL RESOURCE — provides access to restricted data records.
Policy states Group A can access both restricted and public data; Group B can access
public data only. Only Group A has explicit access to restricted data.
Realm role mapping: role-a → Group A.
```
```json
{{"client_role": "restricted-data-access", "real_roles_with_access": ["role-a"]}}
```
"""


def build_semantic_verification_prompt(
    policy_description: str,
    client_name: str,
    client_role: Dict[str, str],
    realm_roles: List[Dict[str, str]],
    real_roles_with_access: List[str],
) -> str:
    """
    Build a prompt to semantically verify a single role mapping.

    Args:
        policy_description: Natural language policy description
        client_name: Name of the client that owns the role
        client_role: Dict with 'name' and 'description' of the client role
        realm_roles: List of dicts with 'name' and 'description' for all realm roles
        real_roles_with_access: List of realm role names currently assigned

    Returns:
        Formatted verification prompt string ready for LLM consumption
    """
    client_role_name = client_role['name']
    client_role_desc = client_role.get('description', '')
    client_role_info = client_role_name + (f" ({client_role_desc})" if client_role_desc else "")

    realm_roles_context = "\n".join(
        f"  - {r['name']}" + (f": {r.get('description', '')}" if r.get('description') else "")
        for r in realm_roles
    )

    assigned_roles = ", ".join(real_roles_with_access) if real_roles_with_access else "(none)"

    return f"""You are a policy validator. Verify that the following role mapping is correct.

POLICY DESCRIPTION:
{policy_description}

CLIENT ROLE BEING ANALYZED:
  Client: {client_name}
  Role: {client_role_info}

CURRENT MAPPING (realm roles that have access to this client role):
  {assigned_roles}

AVAILABLE REALM ROLES:
{realm_roles_context}

VALIDATION TASK:
Based on the policy description, verify if granting access to client role '{client_role_name}' \
from client '{client_name}' to realm roles [{assigned_roles}] is correct.

Consider:
- Are the correct user groups (realm roles) included?
- Are any user groups incorrectly included or excluded?
- Does the mapping match the access requirements stated in the policy description?

Respond in this EXACT format:
MAPPING_CORRECT: YES
EXPLANATION: Brief explanation of why the mapping is correct.

OR if incorrect:
MAPPING_CORRECT: NO
EXPLANATION: Specific description of what is wrong with the mapping."""


def build_single_role_retry_prompt(
    realm_roles: List[Dict[str, str]],
    client_role: Dict[str, str]
) -> str:
    """
    Build a retry prompt when initial JSON parsing fails for single role analysis.

    Args:
        realm_roles: List of dicts with 'name' and 'description' for realm roles
        client_role: Dict with 'name' and 'description' for the client role

    Returns:
        Formatted retry prompt string with role reminders and format example
    """
    realm_role_names = [role['name'] for role in realm_roles]
    client_role_name = client_role['name']

    return f"""The previous response could not be parsed as valid JSON.

Please provide the mapping again using ONLY these preset names:
- Available real roles: {", ".join(realm_role_names) if realm_role_names else "(none)"}
- Client role to analyze: {client_role_name}

Remember: Return a list of real role names that should have access to the client role.

Return in this format:
```explanation
[Your brief explanation]
```

```json
{{
  "client_role": "{client_role_name}",
  "real_roles_with_access": [
    "exact-realm-role-name-1",
    "exact-realm-role-name-2"
  ]
}}
```"""
