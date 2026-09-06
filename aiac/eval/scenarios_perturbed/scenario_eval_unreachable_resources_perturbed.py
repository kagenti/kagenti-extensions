"""Semantic-perturbation sibling of ``scenario_eval_unreachable_resources.py`` (spec:
``docs/specs/eval/policy-eval-robustness-consistency.md``).

Same structure (names, ``USERS``, all pair-lists, and ``EXPECT_NO_REGO`` are byte-identical to the
original), reworded prose only. See ``scenario_eval_baseline_perturbed.py`` for the general
rationale.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-unreachable-resources"
POLICY_FILE = "policy.eval_unreachable_resources_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "intake-agent": {
        "description": (
            "An autonomous agent that handles patient intake on a user's behalf: booking "
            "appointments and reading and updating patient records."
        ),
        "inbound_scopes": {
            "agent-scope-intake-access": (
                "Lets a holder use the intake agent's patient-intake abilities — booking "
                "appointments and reading and updating patient records."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-intake-operations": (
                "Covers both reading and writing patient records — looking at record contents and "
                "updating them."
            ),
        },
    },
    "billing-agent": {
        "description": (
            "An autonomous agent meant to handle patient billing and invoicing, stood up before "
            "the access policy that was supposed to cover it — no policy language yet says who "
            "may call it or what it may reach."
        ),
        "inbound_scopes": {
            "agent-scope-billing-access": (
                "Lets a holder use the billing agent's invoicing abilities — creating and reading "
                "patient invoices. Not yet handed to any user role in the policy text."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-billing-operations": (
                "Covers both reading and writing patient invoices. Not yet handed to any target in "
                "the policy text."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "records-tool": {
        "description": (
            "A capability provider for patient records, carrying out read and write operations on "
            "what's stored in them."
        ),
        "scopes": {
            "tool-scope-records-read": "Look at patient records — demographics and visit history — without changing anything.",
            "tool-scope-records-write": "Create and update patient records.",
        },
    },
    "insurance-tool": {
        "description": (
            "A capability provider for insurance-coverage verification, performing lookups against "
            "a patient's insurance details. No agent role is ever handed its scope anywhere in the "
            "policy text — it's unreachable on purpose."
        ),
        "scopes": {
            "tool-scope-insurance-verify": (
                "Look up a patient's insurance coverage details. No agent role is ever handed this "
                "scope anywhere in the policy text — it's unreachable on purpose."
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
        "Front Desk Clerk: can book appointments and read and update patient records via the "
        "intake agent. Has nothing to do with billing or insurance verification."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-front-desk-clerk", "agent-scope-intake-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-intake-operations", "tool-scope-records-read"),
    ("agent-role-intake-operations", "tool-scope-records-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-front-desk-clerk", "tool-scope-records-read"),
    ("user-role-front-desk-clerk", "tool-scope-records-write"),
]

# --- Emergent unreachability -----------------------------------------------------------------

EXPECT_NO_REGO: frozenset[str] = frozenset({"billing-agent"})
