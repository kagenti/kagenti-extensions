"""Scenario 1 — baseline: 3 users, 2 agents, 2 tools, cleanly and unambiguously specified.

Companion to ``scenario.py`` (the canonical single-agent/single-tool scenario) and to
``scenario_uc1.py`` (the UC-1 onboarding oracle) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/eval/policy-eval-scenarios.md``). This is the suite's one deliberately
code/user-role-devops-flavored (software-engineering) scenario — every other scenario in the family uses a
non-code domain.

Reuses ``scenario_uc1.py``'s exact three realm roles verbatim (``user-role-developer``/``user-role-tester``/``user-role-devops``,
same descriptions, same ``USERS`` mapping), scaled down to a minimal 2-agent/2-tool cast: a
source-repository agent/tool pair and an issue-tracker agent/tool pair, mirroring UC-1's own
role->access facts (``user-role-developer`` reaches both agents; ``user-role-tester`` reaches only the tracker agent;
``user-role-devops`` reaches neither — deny-by-default, exactly as in UC-1).

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
    "team1/repo-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against a source repository. It "
            "inspects and changes repository source contents."
        ),
        "inbound_scopes": {
            "agent-scope-repo-access": (
                "Scope granting use of the repo agent's source-code capability — inspecting and "
                "modifying repository contents."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-repo-operations": (
                "Covers read and write access to source repository contents — listing, reading, "
                "creating, and modifying files."
            ),
        },
    },
    "team1/tracker-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against an issue tracker. It reads, "
            "files, and updates issues and their comment threads."
        ),
        "inbound_scopes": {
            "agent-scope-tracker-access": (
                "Scope granting use of the tracker agent's issue-tracking capability — reading "
                "and updating issues."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-tracker-operations": (
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
            "tool-scope-repo-read": "Read source repository contents: file listings and file bodies. Read-only.",
            "tool-scope-repo-write": "Create, modify, or delete source repository contents; commit file changes.",
        },
    },
    "tracker-tool": {
        "description": (
            "Capability provider Tool for an issue tracker. It performs read and write "
            "operations on issues and their comment threads."
        ),
        "scopes": {
            "tool-scope-tracker-read": "Read issues and their comment threads. Read-only.",
            "tool-scope-tracker-write": "Create and update issues: open, edit, comment, and close.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# Identical to scenario_uc1.py's USERS mapping — same usernames, same realm roles.

USERS: dict[str, str] = {
    "dev-user": "user-role-developer",
    "test-user": "user-role-tester",
    "devops-user": "user-role-devops",
}

USER_PASSWORD = "password"

# name -> description. Verbatim from scenario_uc1.py's USER_ROLES: user-role-devops is deliberately
# unrelated to source/issue work, so it appears in no pair-list below and is denied everywhere by
# deny-by-default, exactly as in UC-1.
USER_ROLES: dict[str, str] = {
    "user-role-developer": (
        "Developer — an engineering user who develops the source codebase (writing and maintaining "
        "code) and fixes code defects reported in the issue tracker; works primarily in source and "
        "consults issues for defect reports."
    ),
    "user-role-tester": (
        "Tester — a quality-assurance user who verifies software quality and tracks defects through "
        "the issue tracker: filing, triaging, and updating issue reports; works in the issue "
        "tracker, not in source."
    ),
    "user-role-devops": (
        "DevOps — an operations user who manages deployment infrastructure and runtime "
        "environments; does not author source code and does not manage the issue tracker."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Mirrors scenario_uc1.py's INBOUND_PAIRS/OUTBOUND_SUBJECT_PAIRS/OUTBOUND_TARGET_PAIRS decisions
# exactly, over these scenario's own (unprefixed) scope names.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-developer", "agent-scope-repo-access"),
    ("user-role-developer", "agent-scope-tracker-access"),
    ("user-role-tester", "agent-scope-tracker-access"),
    # No row for user-role-devops — deny-by-default, same as UC-1's devops-user.
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-repo-operations", "tool-scope-repo-read"),
    ("agent-role-repo-operations", "tool-scope-repo-write"),
    ("agent-role-tracker-operations", "tool-scope-tracker-read"),
    ("agent-role-tracker-operations", "tool-scope-tracker-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-developer", "tool-scope-repo-read"),
    ("user-role-developer", "tool-scope-repo-write"),
    ("user-role-developer", "tool-scope-tracker-read"),
    ("user-role-tester", "tool-scope-tracker-read"),
    ("user-role-tester", "tool-scope-tracker-write"),
]
