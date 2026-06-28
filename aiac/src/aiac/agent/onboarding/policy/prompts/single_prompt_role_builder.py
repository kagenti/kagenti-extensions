#!/usr/bin/env python3
"""
Single Role Prompt Builder for Scope Mapping

This module contains functions for building LLM prompts used to determine
which privileges a specific role should have access to.
"""

from typing import List

from aiac.pdp.library.configuration.models import Role, Scope


def build_single_role_to_privileges_system_prompt(
    role: Role,
    privileges: List[Scope],
    policy_description: str = "",
) -> str:
    """
    Build a system prompt for mapping a single role to its privileges.

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

    return f"""You are an expert at analyzing access control requirements and mapping roles to the privileges they should hold.
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
     (e.g., "engineering team members", "operations staff", "customer support team").
     These represent human principals. Proceed to privilege analysis.
   - TECHNICAL/CAPABILITY or SYSTEM ROLE: The description characterises a SERVICE
     CAPABILITY ("Access to X", "Provides access to X", "Gateway to X") or is
     a system/internal construct (placeholder like "${{}}", starts with "default-roles-",
     or is clearly an identity-provider artifact).
     If the realm role is NOT a user-facing role → return [] immediately.

2. POLICY CONTEXT IS THE PRIMARY GUIDE — SEMANTIC MATCHING REQUIRED:
   Grant a privilege ONLY when the policy explicitly states this role/user-category
   should have access to the capability described by that privilege.
   POLICY SILENCE = NO ACCESS. Do not infer access from the role's name alone.

   IMPORTANT EXCEPTION — ENABLING GATEWAYS ARE NOT SUBJECT TO POLICY SILENCE:
   The "policy silence = no access" rule does NOT apply to enabling/gateway privileges.
   The policy will never explicitly mention an enabling service by name — it only
   describes what final resources users can access. If the policy grants this role ANY
   access to a downstream resource in a given domain, the enabling gateway for that
   domain is IMPLICITLY REQUIRED and must be granted even if the policy never mentions
   it. See guideline 3 for the full enabling gateway rule.

   SEMANTIC ROLE-TO-CATEGORY MATCHING: The policy may describe user categories using
   broad terms (e.g., "technical personnel", "operational staff", "all users").
   Match these categories against the role's DESCRIPTION, not its name. A role described
   as "operations staff assisting external clients" belongs to the policy category
   "operational staff" even if those exact words do not appear in the policy.
   Always ask: "Does this role's description identify it as a member of any policy user
   category?" If yes, apply all access grants for that category to this role.

3. ENABLING / GATEWAY PRIVILEGES — READ CAREFULLY:
   A privilege is an enabling service if its description says "Access to the X connector",
   "Provides access to X services", "Gateway to X", "Enables access to X", or similar
   phrasing that positions it as a PREREQUISITE to reach a downstream resource.

   CLASSIFICATION — "Provides access to" alone is NOT sufficient. You MUST determine what X
   describes to decide whether a privilege is enabling or a final resource:
   - ENABLING GATEWAY: X is a SERVICE, AGENT, CONNECTOR, PLATFORM, or GATEWAY — including
     when X follows the pattern "[Brand] services", "[Brand] agent", or "[Brand] connector".
     Examples: "Provides access to document processing services" → ENABLING (X = service)
               "Provides access to Acme services" → ENABLING (X = "Acme services" = brand platform)
               "Access to the Acme agent" → ENABLING (X = agent)
   - FINAL RESOURCE: X is specific DATA, REPOSITORIES, FILES, or RECORDS, especially with
     access-level qualifiers ("private", "public", "restricted", "full", "read-only").
     Examples: "Provides access to public document repositories" → FINAL RESOURCE (X = repos + qualifier)
               "Provides access to public Acme repositories" → FINAL RESOURCE (X = repos + "public")
   Apply this ENABLING vs. FINAL RESOURCE classification to every privilege before
   deciding how to handle it. Do NOT treat final-resource privileges as enabling gateways.

   DOMAIN IDENTIFICATION (required for Step 3 matching):
   Extract the PRIMARY BRAND/PRODUCT NAME from each privilege description. Two privileges
   are in the same domain when they share the same primary brand/product name, regardless
   of whether one says "services" and the other says "repositories", "records", or "data":
     • "Provides access to Acme services" → primary brand: "Acme"
     • "Provides access to public Acme repositories" → primary brand: "Acme"
     → Both are in the "Acme" domain. The enabling gateway covers the same domain as
       the final resource — the enabling gateway MUST be included whenever the final
       resource is granted.

   DOMAIN REQUIREMENT: An enabling privilege only applies when the policy covers the same
   domain (e.g., "data warehouse connector" is enabling only for a data warehouse policy).

   GOAL-BASED PRINCIPLE: Ask "What is this role trying to accomplish?" If their access
   goal includes reaching a downstream resource that can only be accessed through an
   enabling service, they MUST receive the enabling privilege — without it, they cannot
   fulfill their access goal at all.

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
   NOTE: Final-resource privileges (e.g., "Provides access to public repositories")
   ARE valid user-facing access grants and MUST be included when the policy grants access
   at the matching level. Do NOT exclude them because their description says "Provides access to X".

5. PRIVILEGE VALIDITY — SKIP SYSTEM SCOPES:
   Some privileges are identity-provider infrastructure privileges, NOT service access grants.
   Skip any privilege that:
   - Has a name starting with "default-roles-"
   - Has a description like "Default-roles of X realm", "system role", or "internal"
   - Is clearly an infrastructure artifact (e.g., offline_access, uma_authorization)
   - Describes token-structure modification: "add X to the access token", "adds X claims"
   - Describes enabling a client authentication mechanism: "privilege/scope for a client enabled for..."
   Never include such privileges in the result.

6. WHAT TO INCLUDE AND EXCLUDE:
   - INCLUDE: ENABLING gateway privileges (guideline 3) when the gateway is required.
   - INCLUDE: FINAL RESOURCE privileges (guideline 4) when the policy explicitly grants access
     at the matching level. "Provides access to public X repositories" is a final-resource
     privilege — it IS a valid user-facing grant, NOT a technical exclusion.
   - EXCLUDE: System/internal privileges (guideline 5).
   - EXCLUDE: Privileges in unrelated domains (guideline 2 — policy silence = no access).
   IMPORTANT: Do NOT blanket-exclude privileges because their description says
   "Provides access to X" or "Access to X". Use the ENABLING vs. FINAL RESOURCE
   classification from guideline 3 to decide, then apply guidelines 3 or 4 accordingly.

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
   Use the role's DESCRIPTION (not its name) to determine which policy user category
   this role belongs to. Semantic match is required — "field operations staff" belongs
   to the "operational staff" category; "operations staff assisting customers" belongs
   to "other operational staff" / "other personnel". List every capability the policy
   grants to that category.

2. FIRST PASS — FINAL RESOURCES ONLY:
   Scan the available privileges for FINAL RESOURCE privileges (guideline 3: X is data,
   repositories, files, or records with an access-level qualifier).
   For each one: skip system privileges (guideline 5); check whether the policy grants
   this role the matching access level in that domain; if yes, include it.
   Do NOT consider enabling/gateway privileges in this pass.

3. SECOND PASS — ENABLING GATEWAYS (mandatory):
   For each final-resource privilege included in step 2:
   a. Extract its PRIMARY DOMAIN: the brand/product/service name in its description
      (e.g., "Acme" from "Provides access to public Acme repositories").
   b. Find EVERY enabling/gateway privilege in the available list whose description
      contains the SAME primary brand/product/service name.
      Matching rule: shared brand name = same domain, even if one says "services" and
      the other says "repositories" or "records".
      Example: "Provides access to Acme services" → brand "Acme" → MATCHES
               "Provides access to public Acme repositories" → brand "Acme".
   c. Add ALL matched enabling privileges to the granted list — even if the policy never
      mentions the enabling service by name. The policy only describes final resources;
      the enabling gateway is implicitly required whenever any downstream access is granted.
   REMINDER: "policy silence" does NOT apply here — see guideline 2 EXCEPTION.

4. COMPILE the combined result from steps 2 and 3. Exclude any system/internal
   privileges (guideline 5) and privileges from unrelated domains.

5. VERIFY: Confirm every enabling gateway from step 3 is present. If any is missing,
   add it now. This is a hard requirement — a mapping with a final-resource privilege
   but without its enabling gateway is always incomplete.

6. EXPLAIN: Brief explanation citing the policy evidence and mapping logic.

7. OUTPUT JSON.

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

Example A — user-facing role, two-pass analysis:
Available privileges:
  - "warehouse-connector": Access to the data warehouse connector
  - "warehouse-full-access": Access to full data warehouse records
  - "warehouse-read-only": Access to read-only data warehouse records
  - "analytics-dashboard": Access to the analytics dashboard UI
Policy: "Group A team members have full data warehouse access."
```explanation
Step 0: "role-a" describes Group A team members — user-facing, proceed.
Step 1: "role-a" description matches policy category "Group A". Policy grants full data
  warehouse access.
Step 2 FIRST PASS — FINAL RESOURCES:
  - "warehouse-full-access": FINAL RESOURCE (full data warehouse records). Policy grants
    Group A full access → include.
  - "warehouse-read-only": FINAL RESOURCE (read-only). Policy grants full, not read-only
    → skip (least privilege).
  - "analytics-dashboard": different domain (UI) — policy silent → skip.
  - "warehouse-connector": ENABLING GATEWAY — skip this pass, handled in step 3.
Step 3 SECOND PASS — ENABLING GATEWAYS:
  - From step 2, "warehouse-full-access" is in the data warehouse domain.
  - "warehouse-connector" is an enabling gateway for data warehouse domain → ADD.
    (Policy never names this service, but it is implicitly required — policy silence
    exception applies.)
Step 4: Combined result: ["warehouse-connector", "warehouse-full-access"].
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

Example C — user-facing role with limited access, two-pass:
Available privileges:
  - "storage-connector": Access to the document storage connector
  - "storage-read": Access to read-only document storage files
  - "storage-write": Access to read-write document storage files
Policy: "Group B team members have read-only access to document storage."
```explanation
Step 0: "role-b" describes Group B team members — user-facing, proceed.
Step 1: "role-b" description matches policy category "Group B". Policy grants read-only
  document storage access.
Step 2 FIRST PASS — FINAL RESOURCES:
  - "storage-read": FINAL RESOURCE (read-only files). Policy grants Group B read-only
    access → include.
  - "storage-write": FINAL RESOURCE (read-write). Policy gives read-only only → skip.
  - "storage-connector": ENABLING GATEWAY — skip this pass, handled in step 3.
Step 3 SECOND PASS — ENABLING GATEWAYS:
  - From step 2, "storage-read" is in the document storage domain.
  - "storage-connector" is an enabling gateway for document storage domain → ADD.
    (Policy silence on the connector is not a blocker — exception applies.)
Step 4: Combined result: ["storage-connector", "storage-read"].
```
```json
{{"role": "role-b", "granted_privileges": ["storage-connector", "storage-read"]}}
```

Example D — semantic matching + two-pass with limited downstream access:
Available privileges:
  - "svc-agent": Provides access to data processing services
  - "svc-public-access": Provides access to public data records
  - "svc-full-access": Provides access to private data records
Policy: "Primary team members can access both public and private data records.
         Other operational staff can access public data records only."
Role being analyzed: "ops-team": Operations staff assisting external clients
```explanation
Step 0: "ops-team" describes operations staff — user-facing, proceed.
Step 1: "Operations staff assisting external clients" → semantic match to policy category
  "other operational staff" (exact wording differs — semantic match accepted).
  Policy grants this role access to public data records only.
Step 2 FIRST PASS — FINAL RESOURCES:
  - "svc-public-access": FINAL RESOURCE (public data records). Policy grants this role
    public access → include.
  - "svc-full-access": FINAL RESOURCE (private data records). Policy gives this role
    public-only → skip.
  - "svc-agent": ENABLING GATEWAY — skip this pass, handled in step 3.
Step 3 SECOND PASS — ENABLING GATEWAYS:
  - From step 2, "svc-public-access" ("public data records") → primary domain: "data".
  - Scan enabling gateways: "svc-agent" ("data processing services") → primary domain:
    "data" → SAME domain as "svc-public-access" → ADD.
    Policy never mentions the enabling service — but policy silence exception applies:
    the enabling gateway is implicitly required because this role has downstream access.
Step 4: Combined result: ["svc-agent", "svc-public-access"].
```
```json
{{"role": "ops-team", "granted_privileges": ["svc-agent", "svc-public-access"]}}
```

Example E — system/internal realm role, returns empty:
```explanation
Step 0 ROLE VALIDITY CHECK: The realm role "default-roles-demo" starts with
"default-roles-", marking it as an identity-provider system construct, not a user-facing
role. Returning [] immediately.
```
```json
{{"role": "default-roles-demo", "granted_privileges": []}}
```
"""


def build_single_role_to_privileges_verification_prompt(
    policy_description: str,
    role: Role,
    privileges: List[Scope],
    granted_privileges: List[Scope]
) -> str:
    """
    Build a prompt to semantically verify a single role-to-privileges mapping.

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

    return f"""You are a policy validator. Verify that the following role-to-privileges mapping is correct.

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
   SEMANTIC ROLE-TO-CATEGORY MATCHING: Use the role's DESCRIPTION to determine which
   policy user category it belongs to — do not require an exact name match. A role
   described as "field operations staff" belongs to the "operational staff" category.
   Always ask: "Does this role's description identify it as a member of any policy user
   category?" and apply the access grants for that category.

2. DO NOT RE-DERIVE THE FULL MAPPING: Only flag the mapping as wrong if you can point to
   a specific privilege description + policy statement that directly contradicts what
   was assigned.
   EXCEPTION — ENABLING GATEWAY COMPLETENESS (see rule 4a): A missing enabling gateway
   privilege IS a direct contradiction. Perform the rule 4a check independently of this
   rule — a missing enabling gateway must be flagged even if everything assigned is correct.

3. EMPTY IS VALID BY DEFAULT: An empty assignment [] is acceptable unless the privilege
   description explicitly requires this role AND the policy confirms it.

4. ENABLING SERVICE CHECK — AGENT SEMANTICS (mandatory check — do this before concluding YES):
   CLASSIFICATION (do this FIRST): "Provides access to" alone does NOT determine whether
   a privilege is enabling or a final resource. You MUST check what X describes:
   - ENABLING GATEWAY: X is a SERVICE, AGENT, CONNECTOR, or PLATFORM (the prerequisite
     "door" users pass through to reach the downstream resource).
   - FINAL RESOURCE: X is DATA, REPOSITORIES, FILES, or RECORDS with an access-level
     qualifier ("public", "private", "full", "read-only", etc.).
   Apply the correct rule based on this classification.

   For ENABLING/GATEWAY privileges:
   a. MANDATORY: For each final-resource privilege in the current mapping, identify its
      domain and check whether there is an enabling/gateway privilege for the same domain
      in the available list. If that enabling privilege is NOT in the current mapping,
      respond MAPPING_CORRECT: NO. This check is mandatory and independent of rule 2 —
      a missing enabling gateway is always incorrect regardless of whether the rest of the
      mapping is consistent.
   b. NEVER flag the inclusion of an enabling privilege — for any reason. An enabling
      privilege does NOT require a separate explicit policy statement to justify it; its
      justification comes entirely from the policy granting ANY downstream access in the
      same domain.

      FORBIDDEN arguments (any of the following = your reasoning is wrong):
        - "not explicitly justified by the policy"
        - "policy only mentions repositories, not services"
        - "ambiguous"
        - "doesn't clarify access restrictions"
        - "could grant unrestricted access to the downstream resource"
        - "least privilege requires excluding this because the role has limited access"
        - ANY claim that the enabling gateway's lack of an access-level qualifier is a problem

      The enabling privilege grants access to the AGENT/GATEWAY ITSELF ONLY — not to the
      final resource. Access-level restrictions (public-only, read-only) are enforced
      EXCLUSIVELY by the downstream resource after the user passes through the gateway.
      A mapping that contains a final-resource privilege AND its corresponding enabling
      gateway is COMPLETE AND CORRECT — no further qualification or clarification is needed.

   c. ORPHANED GATEWAY CHECK: If the current mapping contains an enabling/gateway privilege
      but contains NO final-resource privilege from the same domain, AND the policy
      explicitly grants this role access to a final resource in that domain, respond
      MAPPING_CORRECT: NO. The enabling gateway alone is insufficient — the final-resource
      privilege must also be present.
      NOTE: this check is strictly one-directional. Rule 4a covers "final resource present
      but enabling gateway missing." Rule 4c covers "enabling gateway present but final
      resource missing." Do NOT invent other converse or extended interpretations.

5. LEAST PRIVILEGE CHECK (applies to FINAL RESOURCE privileges ONLY):
   Flag any final-resource privilege whose access level exceeds what the policy explicitly
   grants (e.g., write access granted when policy says read-only).
   STRICTLY FORBIDDEN: Do NOT apply this check to enabling/gateway privileges under any
   circumstances. An enabling gateway's lack of an explicit access-level qualifier is NOT
   a least-privilege violation — access-level enforcement is solely the downstream resource's
   responsibility. See rule 4b above.

6. USER-FACING PRIVILEGES ONLY: Verify no system/internal privileges (default-roles-*,
   token-modification scopes, client-mechanism privileges) appear in the granted list.

Respond in this EXACT format:
MAPPING_CORRECT: YES
EXPLANATION: Brief explanation citing the domain check and why the mapping is consistent.

OR if incorrect:
MAPPING_CORRECT: NO
EXPLANATION: Specific contradiction between the privilege description, the policy, and the mapping."""


def build_single_role_to_privileges_retry_prompt(
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
