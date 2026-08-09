"""Scenario 1 — baseline: 3 users, 2 agents, 2 tools, cleanly and unambiguously specified.

Companion to ``scenario.py`` (the canonical single-agent/single-tool scenario) and to
``scenario_uc1.py`` (the UC-1 onboarding oracle) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/integration-test/policy-eval-scenarios.md``). This is the suite's one deliberately
code/devops-flavored (software-engineering) scenario — every other scenario in the family uses a
non-code domain.

Reuses ``scenario_uc1.py``'s exact three realm roles verbatim (``developer``/``tester``/``devops``,
same descriptions, same ``USERS`` mapping), scaled down to a minimal 2-agent/2-tool cast: a
source-repository agent/tool pair and an issue-tracker agent/tool pair, mirroring UC-1's own
role->access facts (``developer`` reaches both agents; ``tester`` reaches only the tracker agent;
``devops`` reaches neither — deny-by-default, exactly as in UC-1).

Unlike ``scenario_eval_baseline.py``'s previous revision, this scenario carries **no**
agent-to-agent delegation grant — that mechanism now has its own dedicated scenario,
``scenario_eval_agent_delegation.py`` (``test/integration/``).

Pure data: no imports beyond ``__future__``, mirroring ``scenario.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-baseline"
POLICY_FILE = "policy.eval_baseline.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "repo-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against a source repository. It "
            "inspects and changes repository source contents."
        ),
        "inbound_scopes": {
            "repo-access": (
                "Scope granting use of the repo agent's source-code capability — inspecting and "
                "modifying repository contents."
            ),
        },
        "target_scopes": {},
        "roles": {
            "repo_operations": (
                "Covers read and write access to source repository contents — listing, reading, "
                "creating, and modifying files."
            ),
        },
    },
    "tracker-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against an issue tracker. It reads, "
            "files, and updates issues and their comment threads."
        ),
        "inbound_scopes": {
            "tracker-access": (
                "Scope granting use of the tracker agent's issue-tracking capability — reading "
                "and updating issues."
            ),
        },
        "target_scopes": {},
        "roles": {
            "tracker_operations": (
                "Covers read and write access to the issue tracker — reading, filing, updating, "
                "and commenting on issues and their threads."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "repo-tool": {
        "description": (
            "Capability provider Tool for a source repository. It performs read and write "
            "operations on repository source contents."
        ),
        "scopes": {
            "repo-read": "Read source repository contents: file listings and file bodies. Read-only.",
            "repo-write": "Create, modify, or delete source repository contents; commit file changes.",
        },
    },
    "tracker-tool": {
        "description": (
            "Capability provider Tool for an issue tracker. It performs read and write "
            "operations on issues and their comment threads."
        ),
        "scopes": {
            "tracker-read": "Read issues and their comment threads. Read-only.",
            "tracker-write": "Create and update issues: open, edit, comment, and close.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# Identical to scenario_uc1.py's USERS mapping — same usernames, same realm roles.

USERS: dict[str, str] = {
    "dev-user": "developer",
    "test-user": "tester",
    "devops-user": "devops",
}

USER_PASSWORD = "password"

# name -> description. Verbatim from scenario_uc1.py's USER_ROLES: devops is deliberately
# unrelated to source/issue work, so it appears in no pair-list below and is denied everywhere by
# deny-by-default, exactly as in UC-1.
USER_ROLES: dict[str, str] = {
    "developer": (
        "Developer — an engineering user who develops the source codebase (writing and maintaining "
        "code) and fixes code defects reported in the issue tracker; works primarily in source and "
        "consults issues for defect reports."
    ),
    "tester": (
        "Tester — a quality-assurance user who verifies software quality and tracks defects through "
        "the issue tracker: filing, triaging, and updating issue reports; works in the issue "
        "tracker, not in source."
    ),
    "devops": (
        "DevOps — an operations user who manages deployment infrastructure and runtime "
        "environments; does not author source code and does not manage the issue tracker."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Mirrors scenario_uc1.py's INBOUND_PAIRS/OUTBOUND_SUBJECT_PAIRS/OUTBOUND_TARGET_PAIRS decisions
# exactly, over these scenario's own (unprefixed) scope names.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("developer", "repo-access"),
    ("developer", "tracker-access"),
    ("tester", "tracker-access"),
    # No row for devops — deny-by-default, same as UC-1's devops-user.
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
