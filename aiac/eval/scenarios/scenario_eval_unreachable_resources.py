"""Scenario 4 — unreachable resources: 1 user, 2 agents, 2 tools, healthcare/clinic domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/eval/policy-eval-scenarios.md``). Isolates one aspect: silent authoring
gaps that produce **emergent** (not hand-picked) unreachability under deny-by-default, merged
across both entity kinds an unreachable resource can be — an agent and a tool — since both fall out
of the exact same mechanism (a scope or role simply never named in the policy document).

- **``billing-agent`` is a fully unreachable agent** (see ``EXPECT_NO_REGO``). It has a real
  Keycloak client, an inbound scope, and a client role — provisioned like any other agent — but the
  policy document never mentions it. A plausible real-world cause: the billing service was stood up
  ahead of the access policy meant to cover it, and the policy author never circled back.
- **``insurance-tool`` is an unreachable tool.** It exists with a real scope
  (``tool-scope-insurance-verify``) but no agent role is ever granted it anywhere in the policy text — an
  omission in the "Agent roles -> tool operations" section, not a deliberate denial.

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-unreachable-resources"
POLICY_FILE = "policy.eval_unreachable_resources.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "team1/intake-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to manage patient intake. It schedules "
            "appointments and reads and updates patient records."
        ),
        "inbound_scopes": {
            "agent-scope-intake-access": (
                "Scope granting use of the intake agent's patient-intake capability — scheduling "
                "appointments and reading and updating patient records."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-intake-operations": (
                "Covers read and write access to patient records — reading and updating patient "
                "record contents."
            ),
        },
    },
    "team1/billing-agent": {
        "description": (
            "Autonomous Agent intended to manage patient billing and invoicing. Provisioned "
            "ahead of the access policy meant to govern it; no policy language yet describes who "
            "may call it or what it may reach."
        ),
        "inbound_scopes": {
            "agent-scope-billing-access": (
                "Scope granting use of the billing agent's invoicing capability — creating and "
                "reading patient invoices. Not yet granted to any user role in the policy "
                "document."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-billing-operations": (
                "Covers read and write access to patient invoices. Not yet granted to any target "
                "in the policy document."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "records-tool": {
        "description": (
            "Capability provider Tool for patient records. It performs read and write operations "
            "on patient record contents."
        ),
        "scopes": {
            "tool-scope-records-read": "Read patient records: demographics and visit history. Read-only.",
            "tool-scope-records-write": "Create and update patient records.",
        },
    },
    "insurance-tool": {
        "description": (
            "Capability provider Tool for insurance coverage verification. It performs read "
            "operations against a patient's insurance details. No agent role is ever granted its "
            "scope anywhere in the policy document — it is unreachable by design."
        ),
        "scopes": {
            "tool-scope-insurance-verify": (
                "Verify a patient's insurance coverage details. No agent role is ever granted "
                "this scope anywhere in the policy document — it is unreachable by design."
            ),
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "clerk-user": "user-role-front-desk-clerk",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-front-desk-clerk": (
        "Front Desk Clerk — authorized to schedule appointments and read and update patient "
        "records through the intake agent; not involved in billing or insurance verification."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-front-desk-clerk", "agent-scope-intake-access"),
    # No row names agent-scope-billing-access (billing-agent is fully unreachable) — a silent gap by design.
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-intake-operations", "tool-scope-records-read"),
    ("agent-role-intake-operations", "tool-scope-records-write"),
    # No row names agent-role-billing-operations (billing-agent is fully unreachable) and no row names
    # tool-scope-insurance-verify (insurance-tool is unreachable) — both silent gaps by design.
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-front-desk-clerk", "tool-scope-records-read"),
    ("user-role-front-desk-clerk", "tool-scope-records-write"),
]

# --- Emergent unreachability -----------------------------------------------------------------
#
# billing-agent: zero rows above name agent-scope-billing-access (its only inbound scope) or
# agent-role-billing-operations (its only role), and no other agent has any delegation_scopes for it to be
# granted through — truly unreachable from every direction in this scenario's own ground truth.
EXPECT_NO_REGO: frozenset[str] = frozenset({"team1/billing-agent"})
