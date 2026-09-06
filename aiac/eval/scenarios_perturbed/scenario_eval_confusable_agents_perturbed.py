"""Semantic-perturbation sibling of ``scenario_eval_confusable_agents.py`` (spec:
``docs/specs/eval/policy-eval-robustness-consistency.md``).

Same structure (names, ``USERS``, all pair-lists, and ``IDENTITY_CONFUSION_PROBES`` are
byte-identical to the original), reworded prose only. See
``scenario_eval_baseline_perturbed.py`` for the general rationale.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-confusable-agents"
POLICY_FILE = "policy.eval_confusable_agents_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "coach-agent": {
        "description": (
            "An autonomous agent that handles team rosters and practice schedules on a user's "
            "behalf."
        ),
        "inbound_scopes": {
            "agent-scope-coaching-access": (
                "Lets a holder use the coaching agent's roster and scheduling abilities."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-coaching-operations": "Covers looking up the team roster and updating the practice schedule.",
        },
    },
    "coach-review-agent": {
        "description": (
            "An autonomous agent that records and looks up player performance evaluations on a "
            "user's behalf. Has nothing to do with rosters or scheduling — no overlap with "
            "coach-agent."
        ),
        "inbound_scopes": {
            "agent-scope-review-access": (
                "Lets a holder use the coach-review agent's performance-evaluation abilities."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-review-operations": (
                "Covers looking up and recording player performance evaluations."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "roster-tool": {
        "description": (
            "A capability provider for team rosters and practice schedules, handling lookups of "
            "the roster and updates to the practice schedule."
        ),
        "scopes": {
            "tool-scope-roster-read": "Look up the current team roster without changing anything.",
            "tool-scope-schedule-write": "Create and update the practice schedule.",
        },
    },
    "evaluation-tool": {
        "description": (
            "A capability provider for player performance evaluations, handling both lookups and "
            "updates of evaluation records."
        ),
        "scopes": {
            "tool-scope-evaluation-read": "Look up a player's performance evaluation records without changing anything.",
            "tool-scope-evaluation-write": "Create and update a player's performance evaluation records.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "trainer-user": "user-role-team-trainer",
    "analyst-user": "user-role-performance-analyst",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-team-trainer": (
        "Team Trainer: may look up the team roster and update the practice schedule via the "
        "coaching agent. Has nothing to do with performance evaluations."
    ),
    "user-role-performance-analyst": (
        "Performance Analyst: may look up and record player performance evaluations via the "
        "coach-review agent. Has nothing to do with rosters or scheduling."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-team-trainer", "agent-scope-coaching-access"),
    ("user-role-performance-analyst", "agent-scope-review-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-coaching-operations", "tool-scope-roster-read"),
    ("agent-role-coaching-operations", "tool-scope-schedule-write"),
    ("agent-role-review-operations", "tool-scope-evaluation-read"),
    ("agent-role-review-operations", "tool-scope-evaluation-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-team-trainer", "tool-scope-roster-read"),
    ("user-role-team-trainer", "tool-scope-schedule-write"),
    ("user-role-performance-analyst", "tool-scope-evaluation-read"),
    ("user-role-performance-analyst", "tool-scope-evaluation-write"),
]

# --- Identity/boundary-confusion probes --------------------------------------------------------

IDENTITY_CONFUSION_PROBES: list[tuple[str, str, bool]] = [
    ("service-account-coach-agent", "coach-review-agent", False),
    ("service-account-coach-review-agent", "coach-agent", False),
]
