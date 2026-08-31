"""Scenario 9 — confusable agents: 2 users, 2 agents, 2 tools, sports/coaching domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/eval/policy-eval-scenarios.md``). Isolates one aspect: a deliberately
confusable agent-name pair (``coach-agent`` / ``coach-review-agent``) with entirely non-overlapping
access, plus the identity/boundary-confusion probe this pairing enables.

Keycloak auto-creates a ``service-account-<clientId>`` user for each confidential client with
``serviceAccountsEnabled`` — one such synthetic identity exists per agent. Under deny-by-default,
neither agent's own service-account identity should be admitted through the *other* agent's inbound
gate, even though the two agent names differ by only one word. ``IDENTITY_CONFUSION_PROBES`` checks
both directions.

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-confusable-agents"
POLICY_FILE = "policy.eval_confusable_agents.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "team1/coach-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to manage team rosters and practice "
            "schedules."
        ),
        "inbound_scopes": {
            "agent-scope-coaching-access": (
                "Scope granting use of the coaching agent's roster and scheduling capability."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-coaching-operations": "Covers reading team rosters and updating practice schedules.",
        },
    },
    "team1/coach-review-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to record and read player performance "
            "evaluations. Unrelated to roster or scheduling access; no overlap with coach-agent."
        ),
        "inbound_scopes": {
            "agent-scope-review-access": (
                "Scope granting use of the coach-review agent's performance-evaluation "
                "capability."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-review-operations": (
                "Covers reading and recording player performance evaluations."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "roster-tool": {
        "description": (
            "Capability provider Tool for team rosters and practice schedules. It performs read "
            "operations on the roster and write operations on the practice schedule."
        ),
        "scopes": {
            "tool-scope-roster-read": "Read the current team roster. Read-only.",
            "tool-scope-schedule-write": "Create and update the practice schedule.",
        },
    },
    "evaluation-tool": {
        "description": (
            "Capability provider Tool for player performance evaluations. It performs read and "
            "write operations on evaluation records."
        ),
        "scopes": {
            "tool-scope-evaluation-read": "Read a player's performance evaluation records. Read-only.",
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
        "Team Trainer — authorized to read the team roster and update the practice schedule "
        "through the coaching agent; not involved in performance evaluations."
    ),
    "user-role-performance-analyst": (
        "Performance Analyst — authorized to read and record player performance evaluations "
        "through the coach-review agent; not involved in rosters or scheduling."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

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
#
# Each agent's own Keycloak service-account identity must not be admitted through the other
# agent's inbound gate, despite the two agent names differing by only one word.
IDENTITY_CONFUSION_PROBES: list[tuple[str, str, bool]] = [
    ("service-account-team1/coach-agent", "team1/coach-review-agent", False),
    ("service-account-team1/coach-review-agent", "team1/coach-agent", False),
]
