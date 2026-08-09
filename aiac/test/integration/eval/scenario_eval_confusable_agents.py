"""Scenario 9 — confusable agents: 2 users, 2 agents, 2 tools, sports/coaching domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/integration-test/policy-eval-scenarios.md``). Isolates one aspect: a deliberately
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
    "coach-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to manage team rosters and practice "
            "schedules."
        ),
        "inbound_scopes": {
            "coaching-access": (
                "Scope granting use of the coaching agent's roster and scheduling capability."
            ),
        },
        "target_scopes": {},
        "roles": {
            "coaching_operations": "Covers reading team rosters and updating practice schedules.",
        },
    },
    "coach-review-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to record and read player performance "
            "evaluations. Unrelated to roster or scheduling access; no overlap with coach-agent."
        ),
        "inbound_scopes": {
            "review-access": (
                "Scope granting use of the coach-review agent's performance-evaluation "
                "capability."
            ),
        },
        "target_scopes": {},
        "roles": {
            "review_operations": (
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
            "roster-read": "Read the current team roster. Read-only.",
            "schedule-write": "Create and update the practice schedule.",
        },
    },
    "evaluation-tool": {
        "description": (
            "Capability provider Tool for player performance evaluations. It performs read and "
            "write operations on evaluation records."
        ),
        "scopes": {
            "evaluation-read": "Read a player's performance evaluation records. Read-only.",
            "evaluation-write": "Create and update a player's performance evaluation records.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "trainer-user": "team-trainer",
    "analyst-user": "performance-analyst",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "team-trainer": (
        "Team Trainer — authorized to read the team roster and update the practice schedule "
        "through the coaching agent; not involved in performance evaluations."
    ),
    "performance-analyst": (
        "Performance Analyst — authorized to read and record player performance evaluations "
        "through the coach-review agent; not involved in rosters or scheduling."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("team-trainer", "coaching-access"),
    ("performance-analyst", "review-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("coaching_operations", "roster-read"),
    ("coaching_operations", "schedule-write"),
    ("review_operations", "evaluation-read"),
    ("review_operations", "evaluation-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("team-trainer", "roster-read"),
    ("team-trainer", "schedule-write"),
    ("performance-analyst", "evaluation-read"),
    ("performance-analyst", "evaluation-write"),
]

# --- Identity/boundary-confusion probes --------------------------------------------------------
#
# Each agent's own Keycloak service-account identity must not be admitted through the other
# agent's inbound gate, despite the two agent names differing by only one word.
IDENTITY_CONFUSION_PROBES: list[tuple[str, str, bool]] = [
    ("service-account-coach-agent", "coach-review-agent", False),
    ("service-account-coach-review-agent", "coach-agent", False),
]
