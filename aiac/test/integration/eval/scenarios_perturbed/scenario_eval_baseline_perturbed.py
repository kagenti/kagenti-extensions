"""Semantic-perturbation sibling of ``scenario_eval_baseline.py`` (spec:
``docs/specs/integration-test/policy-eval-robustness-consistency.md``).

Same structure (names, ``USERS``, and all pair-lists are byte-identical to the original), reworded
prose only: every ``AGENTS``/``TOOLS``/``USER_ROLES`` description below is a different phrasing of
the same meaning as its original counterpart, and the paired policy text
(``policy.eval_baseline_perturbed.md``) says the same thing as ``policy.eval_baseline.md`` in
different words. This lets ``truth(scenario_eval_baseline)`` apply unchanged to rules produced from
this module.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-baseline"
POLICY_FILE = "policy.eval_baseline_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "repo-agent": {
        "description": (
            "An autonomous agent that acts for a user against a source-code repository, able to "
            "look at and change what's stored in it."
        ),
        "inbound_scopes": {
            "repo-access": (
                "Lets a holder use the repo agent's source-code abilities: looking at repository "
                "contents and changing them."
            ),
        },
        "target_scopes": {},
        "roles": {
            "repo_operations": (
                "Covers both reading and writing source repository contents: listing files, "
                "reading them, creating new ones, and editing existing ones."
            ),
        },
    },
    "tracker-agent": {
        "description": (
            "An autonomous agent that acts for a user against an issue tracker, handling reading, "
            "filing, and updating issues along with their comment threads."
        ),
        "inbound_scopes": {
            "tracker-access": (
                "Lets a holder use the tracker agent's issue-tracking abilities: reading and "
                "updating issues."
            ),
        },
        "target_scopes": {},
        "roles": {
            "tracker_operations": (
                "Covers both reading and writing on the issue tracker: reading, filing, updating, "
                "and commenting on issues and their threads."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "repo-tool": {
        "description": (
            "A capability provider for a source repository, carrying out read and write "
            "operations against what's stored in it."
        ),
        "scopes": {
            "repo-read": "Look at repository contents — file listings and file bodies — without changing anything.",
            "repo-write": "Add, edit, or remove repository contents, including committing file changes.",
        },
    },
    "tracker-tool": {
        "description": (
            "A capability provider for an issue tracker, carrying out read and write operations "
            "on issues and their comment threads."
        ),
        "scopes": {
            "tracker-read": "Look at issues and their comment threads without changing anything.",
            "tracker-write": "Open, edit, comment on, and close issues.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# Identical to the original's USERS mapping — same usernames, same realm roles.

USERS: dict[str, str] = {
    "dev-user": "developer",
    "test-user": "tester",
    "devops-user": "devops",
}

USER_PASSWORD = "password"

# name -> description. Reworded from the original; devops still appears in no pair-list below and
# is denied everywhere by deny-by-default.
USER_ROLES: dict[str, str] = {
    "developer": (
        "Developer: an engineer who builds out the codebase and resolves bugs logged in the issue "
        "tracker. Mostly lives in the source tree, checking the tracker for defect reports as "
        "needed."
    ),
    "tester": (
        "Tester: a QA specialist whose job is verifying quality and following defects through the "
        "issue tracker — filing them, triaging them, and keeping them updated. Doesn't touch the "
        "source tree."
    ),
    "devops": (
        "DevOps: handles deployment infrastructure and the runtime environment. Doesn't write "
        "source code and doesn't manage the issue tracker."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("developer", "repo-access"),
    ("developer", "tracker-access"),
    ("tester", "tracker-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("repo_operations", "repo-read"),
    ("repo_operations", "repo-write"),
    ("tracker_operations", "tracker-read"),
    ("tracker_operations", "tracker-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("developer", "repo-read"),
    ("developer", "repo-write"),
    ("developer", "tracker-read"),
    ("tester", "tracker-read"),
    ("tester", "tracker-write"),
]
