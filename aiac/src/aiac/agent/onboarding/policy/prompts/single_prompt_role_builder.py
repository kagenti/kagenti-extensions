#!/usr/bin/env python3
"""
Single Role Prompt Builder for Scope Mapping

This module contains functions for building LLM prompts used to determine
which privileges/scopes a specific realm role should have access to.
"""

from typing import List

from aiac.pdp.library.configuration.models import Role, Scope


def build_single_role_to_scopes_system_prompt(
    role: Role,
    privileges: List[Scope],
    policy_description: str = "",
) -> str:
    """
    Build a system prompt for mapping a single realm role to its privileges/scopes.

    This function constructs a prompt that guides the LLM through determining
    which available privileges a given realm role should be granted, based on
    semantic analysis of the role's description and policy context.

    Args:
        realm_role: Dict with 'name' and 'description' of the realm role to analyze
        privileges: List of dicts with 'name', 'description', and optional 'service'
            for all available privileges
        policy_description: Optional natural language policy description for context

    Returns:
        Formatted system prompt string ready for LLM consumption
    """
    # Build available privileges list with descriptions
    available_privilege_lines = []
    for priv in privileges:
        priv_name = priv.name
        priv_desc = priv.description if priv.description else ''
        label = priv_name
        if priv_desc:
            available_privilege_lines.append(f"  - {label}: {priv_desc}")
        else:
            available_privilege_lines.append(f"  - {label}")

    available_privileges = (
        "\n".join(available_privilege_lines)
        if available_privilege_lines
        else "  (none defined)"
    )

    # Format the realm role information
    role_name = role.name
    role_desc = role.description if role.description else ''
    role_info = role_name
    if role_desc:
        role_info += f": {role_desc}"

    # Add policy context if provided
    policy_context = ""
    if policy_description:
        policy_context = f"""
POLICY CONTEXT:
The following policy description provides context for this access control decision:

{policy_description}

Use this policy context to understand which privileges the role should receive.

"""

    return f"""You are an expert at analyzing access control requirements and mapping realm roles to the privileges/scopes they should hold.
{policy_context}TASK OVERVIEW:
You are given:
1. A single realm role with its description
2. A list of all available privileges with their descriptions

Your task is to determine which privileges this realm role should be granted.

REALM ROLE TO ANALYZE:
  {role_info}

AVAILABLE PRIVILEGES:
{available_privileges}

ANALYSIS GUIDELINES:

1. ROLE CLASSIFICATION — DO THIS FIRST:
   Classify the realm role being analyzed into one of two types:
   - USER-FACING ROLE: The description characterises a GROUP OF PEOPLE or a TEAM
     (e.g., "R&D team members", "Sales team members", "Technical support staff").
     These represent human principals. Proceed to privilege analysis.
   - TECHNICAL/CAPABILITY or SYSTEM ROLE: The description characterises a SERVICE
     CAPABILITY ("Access to X", "Provides access to X", "Gateway to X") or is
     a system/internal construct (placeholder like "${{}}", starts with "default-roles-",
     or is clearly an identity-provider artifact).
     If the realm role is NOT a user-facing role → return [] immediately.

2. POLICY CONTEXT IS THE PRIMARY GUIDE:
   Grant a privilege ONLY when the policy explicitly states this role/user-category
   should have access to the capability described by that privilege.
   POLICY SILENCE = NO ACCESS. Do not infer access from the role's name alone.

3. ENABLING / GATEWAY PRIVILEGES — READ CAREFULLY:
   A privilege is an enabling service if its description says "Access to the X connector",
   "Provides access to X services", "Gateway to X", "Enables access to X", or similar
   phrasing that positions it as a PREREQUISITE to reach a downstream resource.

   DOMAIN REQUIREMENT: An enabling privilege only applies when the policy covers the same
   domain (e.g., "data warehouse connector" is enabling only for a data warehouse policy).

   RULE: If the policy grants this realm role ANY level of access to a downstream resource,
   the realm role MUST also receive the enabling privilege for that resource.
   Access level does NOT matter — even read-only access requires the enabling gateway.

   AGENT SEMANTICS — ENABLING DOES NOT EQUAL FINAL RESOURCE ACCESS:
   An enabling privilege grants access to the AGENT, CONNECTOR, or GATEWAY ITSELF — not to
   the underlying final resource directly. The final resource independently enforces its own
   access controls, checking the user's permissions AFTER they reach it through the agent.
   Granting an enabling privilege to a role with limited downstream access (e.g., public-only,
   read-only) is CORRECT. The final resource's restrictions (private vs. public, full vs.
   read-only) are enforced by the final resource, not by the enabling gateway privilege.

4. ACCESS LEVEL DIFFERENTIATION (for FINAL resource privileges):
   Pay close attention to qualifiers: "private" vs "public", "full access" vs "limited",
   "read-only" vs "read-write".
   Only grant a final-resource privilege if the policy explicitly gives this role the
   matching level of access (e.g., do not grant "full data access" to a role with
   read-only access).

5. PRIVILEGE VALIDITY — SKIP SYSTEM SCOPES:
   Some privileges are identity-provider infrastructure scopes, NOT service access grants.
   Skip any privilege that:
   - Has a name starting with "default-roles-"
   - Has a description like "Default-roles of X realm", "system role", or "internal"
   - Is clearly an infrastructure artifact (e.g., offline_access, uma_authorization)
   - Describes token-structure modification: "add X to the access token", "adds X claims"
   - Describes enabling a client authentication mechanism: "scope for a client enabled for..."
   Never include such privileges in the result.

6. USER-FACING PRIVILEGES ONLY in the result:
   Technical/capability privileges (description: "Access to X", "Provides access to X")
   and system/internal privileges must never appear in the granted list.
   Only include service access scopes that represent meaningful resource access.
   Exception: ENABLING privileges (guideline 3) ARE technical in nature but must be
   included when the gateway is required for access.

7. EXACT NAMES ONLY:
   Use only the exact privilege identifiers as they appear in the "Available Privileges"
   list (including the "service/privilege" format where shown). Do not modify or invent names.

8. PRINCIPLE OF LEAST PRIVILEGE:
   When in doubt, do NOT grant access. Only grant what the policy explicitly requires.

TASK STEPS:
0. ROLE VALIDITY CHECK:
   Classify the realm role (step 1 above). If it is NOT a user-facing role → output empty
   list and stop. Output the required fenced blocks with [] then stop.

   Required output when role is non-user-facing:
   ```explanation
   Step 0 ROLE VALIDITY CHECK: [reason this is not a user-facing role]. Returning [] immediately.
   ```
   ```json
   {{"role": "{role_name}", "granted_privileges": []}}
   ```

1. IDENTIFY POLICY GRANTS for this role:
   From the policy description, list all capabilities explicitly granted to this role
   or the user category it represents.

2. FOR EACH AVAILABLE PRIVILEGE:
   a. Skip system/internal scopes (guideline 5).
   b. Determine the privilege's domain.
   c. Check if the policy grants this role access in that domain.
   d. If yes: is it an enabling privilege or a final-resource privilege?
   e. Apply the appropriate access rule (guidelines 3 and 4).

3. COMPILE the list of granted privileges.

4. VERIFY no system, non-applicable, or non-user-facing privileges are included.

5. EXPLAIN: Brief explanation citing the policy evidence and mapping logic.

6. OUTPUT JSON.

Return in this format:
```explanation
[Your brief explanation: why each privilege was or was not granted]
```

```json
{{
  "role": "{role_name}",
  "granted_privileges": [
    "exact-privilege-name-1",
    "exact-privilege-name-2"
  ]
}}
```

EXAMPLE OUTPUTS:

Example A — user-facing role with enabling + final resource privileges:
```explanation
Step 0: "role-a" role describes Group A team members — user-facing, proceed.
Step 1: Policy grants Group A full data warehouse access.
Step 2:
  - "warehouse-connector": enabling service, same domain (data warehouse), role-a has
    any level of access → grant.
  - "warehouse-full-access": final resource, policy explicitly grants Group A full
    access → grant.
  - "warehouse-read-only": final resource, Group A has full not read-only access.
    Policy does not say "read-only" for Group A → do NOT grant (least privilege).
  - "analytics-dashboard": different domain (UI), policy silent on dashboards → skip.
```
```json
{{"role": "role-a", "granted_privileges": ["warehouse-connector", "warehouse-full-access"]}}
```

Example B — technical/capability role, returns empty:
```explanation
Step 0 ROLE VALIDITY CHECK: The realm role "svc-connector" has description
"Provides access to document storage services" — this is a technical/capability role,
not a human principal or team. Returning [] immediately.
```
```json
{{"role": "svc-connector", "granted_privileges": []}}
```

Example C — user-facing role with read-only access:
```explanation
Step 0: "role-b" describes Group B team members — user-facing, proceed.
Step 1: Policy grants Group B read-only access to document storage.
Step 2:
  - "storage-connector": enabling service for document storage, role-b has read-only
    access → requires the gateway → grant.
  - "storage-read": final resource, read-only access, policy confirms → grant.
  - "storage-write": final resource, write access. Policy gives Group B read-only
    only → do NOT grant.
```
```json
{{"role": "role-b", "granted_privileges": ["storage-connector", "storage-read"]}}
```

Example D — system/internal realm role, returns empty:
```explanation
Step 0 ROLE VALIDITY CHECK: The realm role "default-roles-demo" starts with
"default-roles-", marking it as an identity-provider system construct, not a user-facing
role. Returning [] immediately.
```
```json
{{"role": "default-roles-demo", "granted_privileges": []}}
```
"""


def build_single_role_to_scopes_verification_prompt(
    policy_description: str,
    role: Role,
    privileges: List[Scope],
    granted_privileges: List[Scope]
) -> str:
    """
    Build a prompt to semantically verify a single role-to-scopes mapping.

    Args:
        policy_description: Natural language policy description
        realm_role: Dict with 'name' and 'description' of the realm role
        privileges: List of dicts with 'name', 'description', optional 'service'
            for all available privileges
        granted_privileges: List of privilege names currently assigned to this role

    Returns:
        Formatted verification prompt string ready for LLM consumption
    """
    role_name = role.name
    role_desc = role.description if role.description else ''
    role_info = role_name + (f": {role_desc}" if role_desc else "")

    privileges_context = "\n".join(
        "  - " + p.name
        + (f": {p.description}" if p.description else "")
        for p in privileges
    )

    assigned = ", ".join([p.name for p in granted_privileges]) if granted_privileges else "(none)"

    return f"""You are a policy validator. Verify that the following role-to-scopes mapping is correct.

POLICY DESCRIPTION:
{policy_description}

REALM ROLE BEING ANALYZED:
  {role_info}

CURRENT MAPPING (privileges granted to this role):
  {assigned}

AVAILABLE PRIVILEGES:
{privileges_context}

VALIDATION TASK:
Verify whether the granted privileges are correct for realm role '{role_name}',
given the policy description AND the role's own name and description.

CRITICAL RULES — read carefully before evaluating:

0. ROLE TYPE CHECK (evaluated FIRST, overrides all other rules):
   If the realm role is NOT a user-facing role (it describes a service capability, is
   a system/internal construct, or its name starts with "default-roles-"), the ONLY
   correct mapping is an empty list []. If the current mapping is non-empty, respond
   MAPPING_CORRECT: NO and cite this rule. Do NOT evaluate against any other rules.

1. DOMAIN CHECK: For each granted privilege, verify the policy explicitly covers that
   privilege's domain for this realm role. If the policy is silent on a privilege's
   domain, an empty grant for that privilege is acceptable.

2. DO NOT RE-DERIVE THE FULL MAPPING: Only flag the mapping as wrong if you can point to
   a specific privilege description + policy statement that directly contradicts what
   was assigned.

3. EMPTY IS VALID BY DEFAULT: An empty assignment [] is acceptable unless the privilege
   description explicitly requires this role AND the policy confirms it.

4. ENABLING SERVICE CHECK — AGENT SEMANTICS:
   For enabling/gateway privileges (description: "Provides access to X", "Gateway to X",
   "Access to X agent/service/connector"):
   a. Verify the enabling privilege IS included whenever the policy grants this role ANY
      level of access to the downstream resource in the same domain. Flag as incorrect if
      a required enabling privilege is missing.
   b. Do NOT flag the inclusion of an enabling privilege as a least-privilege violation by
      reasoning that it would "grant access" to restricted downstream capabilities. The
      enabling privilege grants access to the AGENT/GATEWAY ITSELF — not the final resource.
      The final resource independently enforces its own access controls (e.g., the downstream
      service still checks restricted vs. open resource access). Granting an enabling privilege
      to a role with limited downstream access (open-only, read-only) is CORRECT.

5. LEAST PRIVILEGE CHECK (applies to FINAL RESOURCE privileges only, NOT enabling services):
   Flag any final-resource privilege whose access level exceeds what the policy explicitly
   grants (e.g., write access granted when policy says read-only). Do NOT apply this check
   to enabling/gateway privileges — see rule 4b above.

6. USER-FACING PRIVILEGES ONLY: Verify no system/internal scopes (default-roles-*,
   token-modification scopes, client-mechanism scopes) appear in the granted list.

Respond in this EXACT format:
MAPPING_CORRECT: YES
EXPLANATION: Brief explanation citing the domain check and why the mapping is consistent.

OR if incorrect:
MAPPING_CORRECT: NO
EXPLANATION: Specific contradiction between the privilege description, the policy, and the mapping."""


def build_single_role_to_scopes_retry_prompt(
    role: Role,
    privileges: List[Scope],
) -> str:
    """
    Build a retry prompt when initial JSON parsing fails for single role analysis.

    Args:
        realm_role: Dict with 'name' and 'description' of the realm role
        privileges: List of dicts with 'name' and optional 'service' for privileges

    Returns:
        Formatted retry prompt string with privilege reminders and format example
    """
    role_name = role.name
    privilege_names = [p.name for p in privileges]

    return f"""The previous response could not be parsed as valid JSON.

You MUST output BOTH fenced code blocks below — the explanation block AND the json block.
Do NOT skip the json block, even when the list is empty.

- Role to analyze: {role_name}
- Available privileges: {", ".join(privilege_names) if privilege_names else "(none)"}

If the realm role is a system/internal or technical/capability role (not a human-principal
role), output an empty list in the json block and stop.

Return in this format (both blocks are required):
```explanation
[Your brief explanation]
```

```json
{{
  "role": "{role_name}",
  "granted_privileges": []
}}
```

Replace [] with the actual privilege names that should be granted, or leave it empty if none."""
