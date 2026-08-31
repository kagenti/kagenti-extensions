"""Semantic-perturbation sibling of ``scenario_eval_ambiguous_clause.py`` (spec:
``docs/specs/eval/policy-eval-robustness-consistency.md``).

Same structure (names, ``USERS``, all pair-lists are byte-identical to the original), reworded
prose only. See ``scenario_eval_baseline_perturbed.py`` for the general rationale. The reworded
policy text preserves the same broad-sounding leading phrase plus explicit narrowing qualifier as
the original — "enrollment information" reads broadly on its own, but the clause's qualifier
confines it to current status only.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-ambiguous-clause"
POLICY_FILE = "policy.eval_ambiguous_clause_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "registrar-agent": {
        "description": (
            "An autonomous agent that acts for a user against the student enrollment system, "
            "looking up a student's current enrollment status and past enrollment record."
        ),
        "inbound_scopes": {
            "agent-scope-enrollment-status-access": (
                "Lets a holder use the registrar agent's current-enrollment-status lookup "
                "ability."
            ),
            "agent-scope-enrollment-history-access": (
                "Lets a holder use the registrar agent's enrollment-history lookup ability."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-registrar-operations": (
                "Covers looking up a student's current enrollment status and past enrollment "
                "record."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "enrollment-tool": {
        "description": (
            "A capability provider for student enrollment records, performing lookups of a "
            "student's current status and past enrollment record."
        ),
        "scopes": {
            "tool-scope-enrollment-status": (
                "Look up a student's current enrollment status (enrolled, withdrawn, or on leave). "
                "No write access."
            ),
            "tool-scope-enrollment-history": (
                "Look up a student's past enrollment record across terms, including earlier status "
                "changes. No write access."
            ),
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "advisor-user": "user-role-enrollment-advisor",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-enrollment-advisor": "Enrollment Advisor: may look up enrollment information for advising purposes.",
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-enrollment-advisor", "agent-scope-enrollment-status-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-registrar-operations", "tool-scope-enrollment-status"),
    ("agent-role-registrar-operations", "tool-scope-enrollment-history"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-enrollment-advisor", "tool-scope-enrollment-status"),
]
