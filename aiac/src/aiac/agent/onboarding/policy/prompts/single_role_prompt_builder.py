#!/usr/bin/env python3
"""
Single Role Prompt Builder for Access Mapping

This module contains functions for building LLM prompts used to determine
which real roles should have access to a specific privilege.
"""

from typing import List

from aiac.idp.configuration.models import Role, Scope

def build_single_privilege_to_roles_system_prompt(
    privilege: Scope,
    roles: List[Role],
    policy_description: str = "",
) -> str:
    """
    Build a system prompt for mapping a single privilege to realm roles.

    This function constructs a comprehensive prompt that guides the LLM through
    the process of determining which realm roles should have access to a specific
    privilege based on semantic analysis of role descriptions and policy context.

    Args:
        roles: List of dicts with 'name' and 'description' for realm roles
        privilege: Dict with 'name' and 'description' for the privilege to analyze
        policy_description: Optional natural language policy description for context

    Returns:
        Formatted system prompt string ready for LLM consumption
    """
    # Build available realm roles list with descriptions
    available_roles_lines = []
    for role in roles:
        role_name = role.name
        role_desc = role.description if role.description else ''
        if role_desc:
            available_roles_lines.append(f"  - {role_name}: {role_desc}")
        else:
            available_roles_lines.append(f"  - {role_name}")

    available_roles = (
        "\n".join(available_roles_lines)
        if available_roles_lines
        else "  (none defined)"
    )

    # Format the privilege information
    privilege_name = privilege.name
    privilege_desc = privilege.description if privilege.description else ''
    privilege_info = privilege_name
    if privilege_desc:
        privilege_info += f": {privilege_desc}"

    # Add policy context if provided
    policy_context = ""
    if policy_description:
        policy_context = f"""
POLICY CONTEXT:
The following policy description provides context for this access control decision:

{policy_description}

Use this policy context to understand the access requirements and make informed decisions
about which real roles should have access to the service role.

"""

    return f"""You are an expert at analyzing access control requirements and mapping privilege capabilities to appropriate user roles.
{policy_context}TASK OVERVIEW:
You are given:
1. A list of all available realm roles with their descriptions
2. A single privilege with its description

Your task is to determine which realm roles should have access to this privilege.

AVAILABLE REALM ROLES:
{available_roles}

PRIVILEGE TO ANALYZE:
{privilege_info}

ANALYSIS GUIDELINES:
1. IDENTIFY AND MAP ALL USER CATEGORIES (CRITICAL):
   - The policy may describe multiple user categories (e.g., "Group A", "Group B")
   - Each user category MUST map to at least one realm role
   - Use role descriptions to find the best match for each category
   - Broad terms (e.g., "all other staff") may map to multiple realm roles

2. ENABLING / GATEWAY SERVICES - CRITICAL - READ CAREFULLY:
   An enabling service is one whose description says it provides access TO another service
   or technology. Common phrasings include: "Access to the X connector", "Provides access
   to X services", "Gateway to X", "Enables access to X", "Access to the X agent".
   Examples: "Access to the data warehouse connector", "Provides access to the storage service",
   "Access to the payment gateway", "Access to the data pipeline".

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

   AGENT SEMANTICS — ENABLING DOES NOT EQUAL FINAL RESOURCE ACCESS:
   - The enabling service grants access to the AGENT, CONNECTOR, or GATEWAY ITSELF —
     not to the underlying final resource directly.
   - The final resource (e.g., a storage service, data warehouse) independently enforces its
     own access controls, checking the user's permissions AFTER they reach it through the agent.
   - Do NOT assume an enabling privilege grants unrestricted access to the final resource.
     Access restrictions on the final resource are enforced by the final resource, not by
     the enabling service.
   - Example: "svc-agent" grants access to the external service agent tool. The external
     service itself still checks whether the user can access restricted vs. open resources.
     Granting "svc-agent" to a role with open-only downstream access is CORRECT — the
     restricted resource check is enforced by the external service, not by the agent privilege.

   DO NOT confuse enabling services with final resource roles:
   - ENABLING: "Access to the data warehouse connector" - needed by everyone with data access
   - FINAL: "Access to public data files" - needed only by those with public access
   - FINAL: "Access to confidential data records" - needed only by those with full access

   DO NOT exclude user categories based on their realm role name:
   - A "role-b" realm role that needs data access still needs the data warehouse connector
   - A "role-c" realm role that needs read-only access still needs the enabling service
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
   - POLICY SILENCE = NO ACCESS: If the policy description does not mention this service's
     domain at all, return []. Do NOT infer access from the user role name or from what
     that role type might typically do in their job.
     Access is determined solely by what the POLICY TEXT explicitly states.
   - Exception: enabling/gateway services are required by all users of the downstream resource.

5. EXACT NAMES ONLY:
   - Use ONLY the exact role names from the "Available Real Roles" list
   - Do not modify, abbreviate, or create new role names

6. USER-FACING ROLES ONLY — FILTER BEFORE ANALYSIS:
   The available realm roles list may contain a mix of role types. You MUST classify each role
   before using it and ONLY include USER-FACING ROLES in roles_with_access.

   HOW TO CLASSIFY:
   - USER-FACING ROLE: The description characterises a GROUP OF PEOPLE or a TEAM
     (e.g., "engineering team members", "operations staff", "customer support team").
     These represent human principals who receive access. ONLY these are eligible.
   - TECHNICAL/CAPABILITY ROLE: The description characterises a SERVICE CAPABILITY —
     phrases like "Access to X", "Provides access to X", "Gateway to X", "Enables X".
     These represent service identities or token audiences, NOT human principals.
     NEVER include them in roles_with_access.
   - SYSTEM/INTERNAL ROLE: The description is a placeholder (e.g., starts with "${{"),
     or the role is clearly an infrastructure / identity-provider internal construct.
     NEVER include them in roles_with_access.

   NAMING CONFLICT WARNING: A realm role may share the same name or description as the
   privilege being analysed. Do NOT assign the privilege to such a role on that basis alone.
   Apply the classification above; if the role is a technical/capability or system role, exclude it.

TASK STEPS:
0. PRE-ANALYSIS CHECKS (two parts — do both before any further analysis):
   a. PRIVILEGE VALIDITY CHECK: Determine whether the privilege being analyzed is a real
      service capability or a system/internal identity-provider construct.
      System/internal privilege indicators (ANY one is sufficient to disqualify):
        * Name starts with "default-roles-" (identity-provider default realm composite role)
        * Description says "Default-roles of X realm", "system role", "internal", or is absent
          and the name itself does not describe a service capability
        * The privilege is clearly an infrastructure or identity-provider artifact rather than
          a meaningful access privilege (e.g., offline_access, uma_authorization)
        * The privilege's primary function is to MODIFY TOKEN STRUCTURE or ENABLE A CLIENT
          AUTHENTICATION MECHANISM rather than to grant access to a downstream resource.
          Indicators of this pattern:
          - Description says "add X to the access token" or "adds X to the token"
            (the privilege is modifying what goes into a token, not granting resource access)
          - Description says "scope/privilege for a client enabled for [some mechanism]"
            (the privilege enables a client-side authentication mechanism, not resource access)
          - Description says "adds claims", "adds X claims to", "authentication context",
            "allowed web origins to the access token"
          ANY of these indicate an identity-provider infrastructure privilege — NOT a service
          capability. Treat them the same as offline_access or uma_authorization.
      If ANY indicator applies → you MUST still output the required fenced blocks below
      with an empty list, then stop. Do NOT skip the JSON block. Do NOT continue to further steps.

      Required output when any indicator applies:
      ```explanation
      Step 0a PRIVILEGE VALIDITY CHECK: [reason it is a system/internal privilege]. Returning [] immediately.
      ```
      ```json
      {{"privilege": "[privilege-name]", "roles_with_access": []}}
      ```
      System/internal privileges must never be granted to any realm role.
   b. PRE-FILTER REALM ROLES: Scan the full available realm roles list and identify ONLY the
      USER-FACING ROLES (those describing human principals / teams). Record this filtered set.
      All subsequent steps operate on this filtered set ONLY.
      CRITICAL: A role whose description says "Access to X", "Access to the X interface",
      "Access to X services", "Provides access to X", "Gateway to X", or similar
      service-capability phrases is a TECHNICAL/CAPABILITY ROLE, NOT a human principal,
      even if the role name might imply users (e.g., "portal-ui" → description "Access to the
      portal UI interface" → TECHNICAL, exclude it). Do NOT include such roles as user-facing.
1. RELEVANCE CHECK: What is the DOMAIN of this privilege (e.g., "data warehouse", "UI dashboards", "payments")?
   What is the DOMAIN of the policy subject? If they are DIFFERENT domains, return [] immediately.
   Do NOT continue to the next steps.
   IMPORTANT: The policy must explicitly mention the privilege's domain. Do NOT reason from
   the user role name (e.g., "some roles use UI tools too") — that is forbidden here.
   - "Access to the monitoring dashboard UI" — domain: dashboards. Policy about data warehouse — DIFFERENT → []
   - "Access to the data warehouse connector" — domain: data warehouse. Policy about data warehouse — SAME → continue
   - "Access to confidential data records" — domain: data warehouse. Policy about data warehouse — SAME → continue
   - "Access to the demo UI interface" — domain: web UI. Policy about document storage — DIFFERENT → []
     (Even though engineers may use demo UIs in general, the policy says nothing about UI access → [])
2. CLASSIFY this privilege: is it a FINAL resource privilege or an ENABLING/GATEWAY service?
   - ENABLING/GATEWAY: description says "access to [some service/agent/pipeline/gateway]",
     "provides access to [some service/technology]", "gateway to [...]", or similar phrasing
     that positions this role as a PREREQUISITE to reach the downstream resource —
     AND the service is in the same domain as the policy
   - FINAL RESOURCE: description says "access to [data/repos/files/records]", especially
     with an access-level qualifier ("public", "private", "read-only", "full")
   NOTE: A privilege named "X-agent" or "X-gateway" with a description like
   "Provides access to X services" IS an enabling service, NOT a final resource.
   DOMAIN MATCHING — SAME BRAND = SAME DOMAIN: Extract the PRIMARY BRAND/PRODUCT NAME
   from this privilege's description and from the policy. "Provides access to [Brand]
   services" and "access to [Brand] repositories" share the same primary brand → SAME
   domain. Do NOT treat "[Brand] services" and "[Brand] repositories" as different domains.
3. IDENTIFY USER CATEGORIES: List all user categories mentioned in the policy.
4. APPLY RULE:
   - ENABLING/GATEWAY: grant to ALL user categories that need the downstream resource
   - FINAL RESOURCE: grant only to categories with explicit access to this specific capability
5. MAP TO REALM ROLES: For each included user category, find matching realm role(s) from the
   USER-FACING ROLES identified in step 0. Do NOT use technical/capability or system roles.
6. VERIFY: Every included user category maps to at least one user-facing realm role, and no
   technical/capability or system roles appear in the result.
7. EXPLAIN: Brief explanation citing the domain check, classification, policy evidence, and mapping.
8. OUTPUT JSON: List of realm role names that should have access.

Return in this format:
```explanation
[Your brief explanation: why relevant or not, which user categories
need access, how they map to realm roles]
```

```json
{{
  "privilege": "{privilege_name}",
  "roles_with_access": [
    "exact-realm-role-name-1",
    "exact-realm-role-name-2"
  ]
}}
```

EXAMPLE OUTPUTS:

Example A — domain mismatch, not relevant to policy subject:
```explanation
Step 1 RELEVANCE CHECK: privilege domain is "monitoring dashboard UI". Policy domain is
"data warehouse access". These are DIFFERENT domains — dashboard UI is unrelated to data
warehouse access. Returning [] immediately without further analysis.
Note: Even if engineers or analysts typically use dashboard UIs, the policy is silent
about UI access. POLICY SILENCE = NO ACCESS.
```
```json
{{"privilege": "monitoring-dashboard", "roles_with_access": []}}
```

Example A2 — domain mismatch: UI privilege, document storage policy:
```explanation
Step 1 RELEVANCE CHECK: privilege domain is "analytics dashboard UI". Policy domain is
"document storage access". These are DIFFERENT domains. The policy mentions only document
storage; it says nothing about any UI or dashboard. POLICY SILENCE = NO ACCESS.
Returning [] immediately. (The fact that certain roles may use dashboards in general is
irrelevant — access is determined by the policy text, not by job function assumptions.)
```
```json
{{"privilege": "analytics-dashboard", "roles_with_access": []}}
```

Example B — enabling/gateway service (ALL users who need the downstream resource):
```explanation
Step 1 RELEVANCE CHECK: privilege domain is "data warehouse connector". Policy domain is
"data warehouse access". SAME domain — continue.
Step 2 CLASSIFY: ENABLING SERVICE — "Access to the data warehouse connector" is a prerequisite
service, not a final resource. Policy identifies two user categories: Group A (full access)
and Group B (read-only). Both need ANY level of data warehouse access, so both need this
enabling service. Access level does NOT matter for enabling services.
Realm role mapping: role-a → Group A, role-b → Group B.
```
```json
{{"privilege": "warehouse-connector", "roles_with_access": ["role-a", "role-b"]}}
```

Example C — restricted privilege, limited access:
```explanation
Step 1 RELEVANCE CHECK: privilege domain is "confidential data records". Policy domain is
"data warehouse access". SAME domain — continue.
Step 2 CLASSIFY: FINAL RESOURCE — provides access to restricted data records.
Policy states Group A can access both restricted and public data; Group B can access
public data only. Only Group A has explicit access to restricted data.
Realm role mapping: role-a → Group A.
```
```json
{{"privilege": "restricted-data-access", "roles_with_access": ["role-a"]}}
```

Example D — enabling/gateway service using "Provides access to" phrasing:
```explanation
Step 1 RELEVANCE CHECK: privilege domain is "document storage services". Policy domain is
"document storage access". SAME domain — continue.
Step 2 CLASSIFY: ENABLING SERVICE — "Provides access to document storage services" positions
this as a prerequisite gateway; without it, no user can reach document storage at all.
Policy identifies two user categories: Group A (→ role-a) gets full access; Group B
(→ role-b) gets read-only access. Both need ANY level of document storage access,
so BOTH need this enabling service. Access level does NOT matter for enabling services.
Realm role mapping: role-a → Group A, role-b → Group B.
```
```json
{{"privilege": "storage-agent", "roles_with_access": ["role-a", "role-b"]}}
```

Example E — system/internal privilege (starts with "default-roles-"):
```explanation
Step 0a PRIVILEGE VALIDITY CHECK: The privilege "default-roles-demo" starts with
"default-roles-", which marks it as an identity-provider default realm composite role —
a system/internal construct, not a user-facing service capability.
Returning [] immediately. No further analysis is performed.
```
```json
{{"privilege": "default-roles-demo", "roles_with_access": []}}
```

Example F — token-modifying scope (adds origins/claims to the token, not a service capability):
```explanation
Step 0a PRIVILEGE VALIDITY CHECK: The privilege "web-origins" has description
"add allowed web origins to the access token". Its primary function is to modify
token structure — it adds data to the token rather than granting access to any
downstream resource or service. This is an identity-provider infrastructure privilege.
Returning [] immediately.
```
```json
{{"privilege": "web-origins", "roles_with_access": []}}
```

Example G — client-mechanism privilege (enables an authentication mechanism, not resource access):
```explanation
Step 0a PRIVILEGE VALIDITY CHECK: The privilege "service_account" has description
"Specific privilege for a client enabled for service accounts". Its function is to enable
a client authentication mechanism, not to grant access to a downstream resource or
service. This is an identity-provider infrastructure privilege. Returning [] immediately.
```
```json
{{"privilege": "service_account", "roles_with_access": []}}
```
"""


def build_single_privilege_to_roles_verification_prompt(
    policy_description: str,
    privilege: Scope,
    roles: List[Role],
    roles_with_access: List[Role],
) -> str:
    """
    Build a prompt to semantically verify a single privilege mapping.

    Args:
        policy_description: Natural language policy description
        privilege: Dict with 'name' and 'description' of the privilege
        roles: List of dicts with 'name' and 'description' for all realm roles
        roles_with_access: List of realm role names currently assigned

    Returns:
        Formatted verification prompt string ready for LLM consumption
    """
    privilege_name = privilege.name
    privilege_desc = privilege.description if privilege.description else ''
    privilege_info = privilege_name + (f": {privilege_desc}" if privilege_desc else "")

    role_context = "\n".join(
        f"  - {r.name}" + (f": {r.description if r.description else ''}" if r.description else "")
        for r in roles
    )

    assigned_roles = ", ".join([role.name for role in roles_with_access]) if roles_with_access else "(none)"

    return f"""You are a policy validator. Verify that the following privilege mapping is correct.

POLICY DESCRIPTION:
{policy_description}

PRIVILEGE BEING ANALYZED:
  {privilege_info}

CURRENT MAPPING (realm roles that have access to this privilege):
  {assigned_roles}

AVAILABLE REALM ROLES:
{role_context}

VALIDATION TASK:
Verify whether the assigned realm roles are correct for privilege '{privilege_name}', \
given the policy description AND the privilege's own name and description.

CRITICAL RULES — read carefully before evaluating:

0. PRIVILEGE SYSTEM-ROLE CHECK (evaluated FIRST, overrides all other rules):
   Determine whether the privilege is a system/internal identity-provider construct.
   System/internal privilege indicators (ANY one is sufficient):
     * Name starts with "default-roles-" (identity-provider default realm composite role)
     * Description says "Default-roles of X realm", "system role", or "internal"
     * The privilege is clearly an infrastructure artifact, not a service access privilege
     * Description says "add X to the access token", "adds X to the token",
       "adds X claims", or "add allowed ... to the access token" — the privilege modifies
       token structure rather than granting resource access
     * Description says "scope/privilege for a client enabled for [mechanism]" — the privilege
       enables a client authentication mechanism, not resource access
   If ANY indicator applies:
     - The ONLY correct mapping is an empty list [].
     - If the current mapping is non-empty, respond MAPPING_CORRECT: NO and cite this rule.
     - Do NOT evaluate the mapping against any other rules.

1. DOMAIN CHECK FIRST: Determine the domain of this privilege from its name and description
   (e.g., "document storage", "data warehouse", "UI dashboard").
   If the policy description does NOT explicitly address this privilege's domain, the mapping
   cannot be evaluated against the policy — accept any assignment including empty and return
   MAPPING_CORRECT: YES.

2. DO NOT RE-DERIVE THE FULL MAPPING: You are verifying an existing mapping, not computing
   a new one. Only flag the mapping as wrong if you can point to a specific privilege
   description + policy statement that directly contradicts what was assigned.

3. EMPTY IS VALID BY DEFAULT: An empty assignment [] is acceptable unless the privilege
   description explicitly requires certain realm roles AND the policy confirms those users
   need access to this specific privilege.

4. FOCUS ON THIS PRIVILEGE ONLY: Do not reason about what roles are required by the policy
   in general. Only ask: "Is the mapping for THIS specific privilege consistent with its
   description and the policy?"
   CROSS-PRIVILEGE REASONING IS FORBIDDEN: Never flag the mapping as incorrect because
   an assigned role is also missing OTHER privileges. Whether a role should additionally
   receive OTHER privileges from OTHER mappings is entirely out of scope here. The sole
   question is: for each role listed in the current mapping, is it correct that they have
   THIS specific privilege? If a role is entitled to BOTH a limited-access and a full-access
   privilege, assigning the limited-access privilege to that role is CORRECT — the missing
   full-access privilege is handled by a separate mapping and must not affect this verdict.

5. ENABLING/GATEWAY PRIVILEGE — AGENT SEMANTICS (applies before access-level checks):
   CLASSIFICATION DISTINCTION — "Provides access to" alone is NOT sufficient to classify
   a privilege as enabling. You MUST determine what X describes:
   - ENABLING: X is a SERVICE, AGENT, CONNECTOR, PLATFORM, or GATEWAY — the prerequisite
     "door" that users must pass through to reach the downstream resource.
     Examples: "access to [domain] services", "[domain] connector", "[domain] gateway"
   - FINAL RESOURCE: X is specific DATA, FILES, RECORDS, or REPOSITORIES, especially with
     access-level qualifiers ("private", "public", "confidential", "read-only", "full").
     Examples: "access to private [resource type]", "access to public [resource type]"

   If the privilege is a FINAL RESOURCE, do NOT apply enabling-service logic. Instead,
   apply access-level differentiation: only roles explicitly granted access to this specific
   resource type (per the policy) need it.

   If this privilege IS an enabling/gateway service (X describes a service/agent/connector):
   - The assignment grants access to the AGENT/GATEWAY ITSELF — NOT to the final resource.
   - The final resource (e.g., a storage service, data warehouse) independently enforces
     its own access controls, checking the user's permissions after they reach it through the agent.
   - Do NOT flag the assignment as incorrect by reasoning that it would "grant access" to
     restricted downstream capabilities (e.g., private repositories, confidential records).
   - The ONLY valid check for an enabling privilege is: does each assigned role need ANY
     level of access to the downstream resource (per the policy)?
   - DOWNSTREAM RESOURCE IDENTIFICATION — BRAND/PRODUCT DOMAIN MATCHING:
     The "downstream resource" is identified by the PRIMARY BRAND/PRODUCT NAME in the
     enabling privilege's description. Two items are in the same domain when they share the
     same primary brand/product name, regardless of whether one mentions "services" and the
     other mentions "repositories", "records", "data", or "files":
       * "Provides access to [Brand] services" → primary brand: "[Brand]"
       * "Provides access to public [Brand] repositories" → primary brand: "[Brand]"
       * "Access to the [Brand] agent" → primary brand: "[Brand]"
     These all belong to the "[Brand]" domain. Therefore: if the policy grants a role ANY
     access to a "[Brand]" resource (e.g., "[Brand] repositories", "[Brand] records"), that
     role DOES need access to the "[Brand] services" enabling gateway — even if the policy
     only mentions "[Brand] repositories" and never names the enabling service.
     FORBIDDEN: Do NOT treat "[Brand] services" and "[Brand] repositories" as different
     domains. They share the same primary brand name — policy silence about the service name
     is NOT a reason to exclude the enabling gateway from the mapping.
   - POLICY CATEGORY MATCHING: When checking whether a role needs ANY access, match by
     the role's DESCRIPTION against the policy's user categories — do NOT require the
     exact role name to appear in the policy text. Broad policy categories are INCLUSIVE:
     * Broad categories like "Other personnel" or "Other [group]" cover every role whose
       description fits the semantic meaning of that group — not just roles whose exact
       name appears in the policy text.
     * NEVER say a role lacks authorisation solely because its exact name is absent from
       the policy. Use the role's description to determine which policy category it fits.
     * Example: policy says "Other personnel can access X". A role whose description
       identifies a non-primary staff group IS other personnel → assignment is CORRECT.
     * Example: policy says "Other [category] members can access X". A role whose
       description identifies it as a member of that category → CORRECT.
   - Access-level restrictions (public-only, read-only, limited) are enforced by the
     downstream resource, not by the gateway privilege. Do NOT apply least-privilege
     reasoning about the final resource when evaluating an enabling service assignment.
   - Example: policy says role-b gets open-resource-only access. Assigning
     "svc-agent" (enabling service) to role-b is CORRECT — the downstream service enforces
     the open-resource restriction. The enabling privilege just allows them to reach the agent.

6. USER-FACING ROLES ONLY: Verify that every assigned role represents a human principal or
   team (e.g., its description characterises a group of people such as "engineering team members").
   If any assigned role is a technical/capability role (description: "Access to X",
   "Provides access to X", "Enables X") or a system/internal role (placeholder description
   starting with "${{"), mark MAPPING_CORRECT: NO and explain which role is invalid.

7. CLOSED-WORLD ASSUMPTION — ONLY REASON ABOUT LISTED ROLES:
   The AVAILABLE REALM ROLES list is the complete and authoritative set of roles in the
   system. Do NOT speculate about roles that might exist but are not listed. Do NOT flag
   a mapping as incomplete because unlisted roles might hypothetically belong to a policy
   category. If all roles from the available list that match a policy category are
   correctly assigned, the mapping is COMPLETE and CORRECT — regardless of what roles
   could theoretically exist outside the list.

Respond in this EXACT format:
MAPPING_CORRECT: YES
EXPLANATION: Brief explanation citing the domain check and why the mapping is consistent.

OR if incorrect:
MAPPING_CORRECT: NO
EXPLANATION: Specific contradiction between the privilege description, the policy, and the mapping."""


def build_single_privilege_to_roles_retry_prompt(
    privilege: Scope,
    roles: List[Role]
) -> str:
    """
    Build a retry prompt when initial JSON parsing fails for single privilege analysis.

    Args:
        roles: List of dicts with 'name' and 'description' for realm roles
        privilege: Dict with 'name' and 'description' for the privilege

    Returns:
        Formatted retry prompt string with role reminders and format example
    """
    role_names = [role.name for role in roles]
    privilege_name = privilege.name

    return f"""The previous response could not be parsed as valid JSON.

You MUST output BOTH fenced code blocks below — the explanation block AND the json block.
Do NOT skip the json block, even when the list is empty.

- Available real roles: {", ".join(role_names) if role_names else "(none)"}
- Privilege to analyze: {privilege_name}

If the privilege is a system/internal role (e.g., name starts with "default-roles-"),
output an empty list in the json block and stop.

Return in this format (both blocks are required):
```explanation
[Your brief explanation]
```

```json
{{
  "privilege": "{privilege_name}",
  "roles_with_access": []
}}
```

Replace [] with the actual role names that should have access, or leave it empty if none."""
