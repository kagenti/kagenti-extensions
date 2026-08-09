"""Scenario 1 — baseline-scale: 5 users, 3 agents, 4 tools, cleanly and unambiguously specified.

Companion to ``scenario.py`` (the canonical single-agent/single-tool scenario), generalized to
many entities for ``test_policy_pipeline_eval.py`` (spec: ``docs/specs/integration-test/
policy-eval-scenarios.md``). Unlike ``scenario.py``, entity names are deliberately decoupled from
role names (a "tester"-named user whose actual role edits source; an "HR"-named user whose role
manages deployment) so a passing per-cell truth table demonstrates the pipeline is keying off the
declared role/scope facts, not off name resemblance.

This scenario also includes the suite's one deliberate **agent-to-agent** target grant:
``concierge-agent``'s client role ``orchestration_operations`` is granted ``code-delegation``, a
scope owned by ``scribe-agent`` (not a tool). This exercises the exact same ``target_scopes``
mechanism (``src/aiac/policy/computation/engine.py``) that an agent uses to reach a tool, with an
Agent-typed service standing in as the target — no separate code path exists for this case.

Pure data: no imports beyond ``__future__``, mirroring ``scenario.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-baseline"
POLICY_FILE = "policy.eval_baseline.md"

# --- Agents -------------------------------------------------------------------------------------
#
# Each agent owns:
#   - "inbound_scopes": its own agent-boundary scope(s), granted to USER roles so a user may call
#     the agent (these feed INBOUND_PAIRS and the inbound Rego gate).
#   - "target_scopes": scopes owned by the agent that stand in as an agent-to-agent delegation
#     target for a DIFFERENT agent's outbound reach — never granted to a user role directly for
#     inbound purposes, only ever appearing as the *scope* half of an OUTBOUND_PAIRS /
#     OUTBOUND_SUBJECT_PAIRS row. Only ``scribe-agent`` has one ("code-delegation"), which is what
#     ``concierge-agent`` is granted below — the suite's one deliberate agent-to-agent grant.
#   - "roles": its client (service-account) roles, granted TARGET scopes (on a tool or another
#     agent) so the agent may reach that target on a caller's behalf.

AGENTS: dict[str, dict] = {
    "scribe-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against a source repository. It "
            "inspects and changes repository source contents, and can accept delegated "
            "source-side actions requested by another agent during a coordinated release."
        ),
        "inbound_scopes": {
            "code-access": (
                "Scope granting use of the scribe agent's source-code capability — inspecting "
                "and modifying repository contents."
            ),
        },
        "target_scopes": {
            "code-delegation": (
                "Scope granting a coordinating agent the ability to direct source-side actions "
                "on its behalf during a release (for example tagging or freezing a branch). Not "
                "intended for direct end-user access."
            ),
        },
        "roles": {
            "code_operations": (
                "Covers read and write access to source repository contents — listing, reading, "
                "creating, and modifying files."
            ),
        },
    },
    "librarian-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against an issue tracker. It reads, "
            "files, and updates issues and their comment threads."
        ),
        "inbound_scopes": {
            "tracker-access": (
                "Scope granting use of the librarian agent's issue-tracking capability — "
                "reading and updating issues."
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
    "concierge-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to orchestrate deployments. It triggers "
            "and monitors deployments, reads stored secrets needed for a release, and can "
            "delegate source-side actions to another agent as part of a coordinated release."
        ),
        "inbound_scopes": {
            "orchestration-access": (
                "Scope granting use of the concierge agent's deployment-orchestration "
                "capability — triggering deployments, monitoring status, and coordinating "
                "dependent actions."
            ),
        },
        "target_scopes": {},
        "roles": {
            "orchestration_operations": (
                "Covers triggering and monitoring deployments, reading stored secrets for "
                "release purposes, and delegating source-side actions to another agent during a "
                "release."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "quill-tool": {
        "description": (
            "Capability provider Tool for a source repository. It performs read and write "
            "operations on repository source contents."
        ),
        "scopes": {
            "quill-read": "Read source repository contents: file listings and file bodies. Read-only.",
            "quill-write": "Create, modify, or delete source repository contents; commit file changes.",
        },
    },
    "ledger-tool": {
        "description": (
            "Capability provider Tool for an issue tracker. It performs read and write "
            "operations on issues and their comment threads."
        ),
        "scopes": {
            "ledger-read": "Read issues and their comment threads. Read-only.",
            "ledger-write": "Create and update issues: open, edit, comment, and close.",
        },
    },
    "beacon-tool": {
        "description": (
            "Capability provider Tool for deployment orchestration. It triggers deployments and "
            "reports their status."
        ),
        "scopes": {
            "beacon-deploy": "Trigger a deployment of a service to its runtime environment.",
            "beacon-status": "Read the current status and history of deployments. Read-only.",
        },
    },
    "vault-tool": {
        "description": (
            "Capability provider Tool for stored secrets and credentials. It performs read "
            "operations on secrets used at release time."
        ),
        "scopes": {
            "vault-read": "Read stored secrets and credentials. Read-only.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# username -> the realm role the user holds. Names are deliberately decoupled from role duties
# for all but one user (interns plausibly do get read-only access — not everything needs to be
# jarring for the scenario to prove the point).

USERS: dict[str, str] = {
    "tester-user": "code-editor",  # named "tester", role actually edits source
    "hr-user": "deploy-manager",  # named "hr", role actually orchestrates deployment
    "finance-user": "issue-triager",  # named "finance", role actually triages issues
    "intern-user": "read-only-observer",  # aligned: interns plausibly get read-only access
    "sales-user": "security-reviewer",  # named "sales", role actually reviews secrets
}

USER_PASSWORD = "password"

# name -> description. Realm roles held by users.
USER_ROLES: dict[str, str] = {
    "code-editor": (
        "Code Editor — authorized to inspect and modify the source repository's contents "
        "directly; not involved in issue triage or deployment."
    ),
    "deploy-manager": (
        "Deploy Manager — authorized to trigger and monitor deployments through the "
        "deployment-orchestration agent, which may delegate source actions on the manager's "
        "behalf; no direct access to the source repository or its agent; not involved in issue "
        "triage."
    ),
    "issue-triager": (
        "Issue Triager — authorized to read and update the issue tracker: filing, triaging, and "
        "closing issues; does not touch source or deployment infrastructure."
    ),
    "read-only-observer": (
        "Read-Only Observer — authorized to view (never modify) both source contents and "
        "issue-tracker records, for oversight purposes; no write access anywhere."
    ),
    "security-reviewer": (
        "Security Reviewer — authorized to read stored secrets and credentials for audit "
        "purposes via the deployment-orchestration layer; no other access."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Same three pair-sets as ``scenario.py``, generalized to many agents/tools. Scope names are
# unique across the whole scenario, so (role, scope) pairs are unambiguous; ownership (which
# agent/tool a scope belongs to) is resolved by scanning AGENTS/TOOLS, not encoded here.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("code-editor", "code-access"),
    ("deploy-manager", "orchestration-access"),
    ("issue-triager", "tracker-access"),
    ("read-only-observer", "code-access"),
    ("read-only-observer", "tracker-access"),
    ("security-reviewer", "orchestration-access"),
]

# Agent role -> target scope. The last two rows are the agent-to-agent grant: both are on
# ``orchestration_operations`` (concierge-agent's client role), and "code-delegation" is owned by
# scribe-agent, not by a tool.
OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("code_operations", "quill-read"),
    ("code_operations", "quill-write"),
    ("tracker_operations", "ledger-read"),
    ("tracker_operations", "ledger-write"),
    ("orchestration_operations", "beacon-deploy"),
    ("orchestration_operations", "beacon-status"),
    ("orchestration_operations", "vault-read"),
    ("orchestration_operations", "code-delegation"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("code-editor", "quill-read"),
    ("code-editor", "quill-write"),
    ("deploy-manager", "beacon-deploy"),
    ("deploy-manager", "beacon-status"),
    ("deploy-manager", "code-delegation"),
    ("issue-triager", "ledger-read"),
    ("issue-triager", "ledger-write"),
    ("read-only-observer", "quill-read"),
    ("read-only-observer", "ledger-read"),
    ("security-reviewer", "vault-read"),
]
