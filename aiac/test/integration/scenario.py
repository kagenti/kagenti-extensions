"""The canonical ``github-agent`` scenario — single source of truth for both launchers.

The 5.2 launcher (``test/pdp/policy/generate_rego.py``) hand-builds a ``PolicyModel`` from
these names + role→access pair-sets; the 5.3 launcher (``test/integration/policy_pipeline.py``)
provisions the same entities into a live Keycloak realm and feeds the descriptions to the PRB.
Keeping the scenario in one place is what the spec's *Further Notes* mandate — the role→access
facts, the entity/role/scope descriptions, and both ``policy.md`` variants must stay mutually
consistent (spec: ``docs/specs/integration-test/policy-pipeline.md``, *Scenario* +
*Scenario inputs*).

Pure data: this module imports nothing (no aiac, no stdlib beyond the language) so a launcher can
import it before the env-before-import step. Dict insertion order is significant — it is preserved
into the generated Rego, so it matches the 5.2 launcher's original literal order.
"""

from __future__ import annotations

# --- Realm + entity identifiers -------------------------------------------------------------

# MUST be a throwaway realm: this suite's ``provision_keycloak_admin`` does ``delete_realm`` +
# ``create_realm`` on it every run, so pointing it (via ``AIAC_TEST_REALM``) at a shared realm like
# ``rossoctl`` DESTROYS that realm's contents. UC-1 uses its own realm (``scenario_uc1.REALM_DEFAULT``).
REALM_DEFAULT = "aiac-pp"
AGENT_ID = "github-agent"
TOOL_ID = "github-tool"

# username -> the realm role the user holds
USERS: dict[str, str] = {
    "dev-user": "developer",
    "test-user": "tester",
    "devops-user": "devops",
}

# Fixed dev password for the provisioned test users (throwaway realm).
USER_PASSWORD = "password"

# --- Descriptions (verbatim from the spec's *Scenario inputs*) ------------------------------
#
# These descriptions feed the PRB's LLM role→scope mapping. They do NOT drive service typing:
# the IdP types services via the canonical ``client.type`` attribute (set by the launcher through
# ``config.set_service_type``), not by inferring "Agent"/"Tool" from the description text.

AGENT_DESCRIPTION = (
    "Autonomous Agent acting on a user's behalf against source repositories and an issue tracker. "
    "It inspects and changes repository source contents and reads, creates, and updates issues and "
    "their threads."
)

TOOL_DESCRIPTION = (
    "Capability provider Tool for source repositories and an issue tracker. It performs read and "
    "write operations on repository source contents and on issues and their comment threads."
)

# name -> description. Realm roles held by users.
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
    # Deny-by-default control: devops appears in no INBOUND/OUTBOUND pair below. Its description is
    # deliberately unrelated to source and issue work, so the PRB derives no agent or tool scope for
    # it and deny-by-default leaves devops-user denied everywhere.
    "devops": (
        "DevOps — an operations user who manages deployment infrastructure and runtime "
        "environments; does not author source code and does not manage the issue tracker."
    ),
}

# name -> description. The github-agent's client roles — the per-skill operator roles, named to
# match exactly what UC-1 provisioning emits from the AgentCard skill ids
# (``github-agent.source_operations`` / ``github-agent.issue_operations``; see scenario_uc1.py).
#
# NOTE: the ``_operations`` suffix (not the actor-noun ``operator``) also sidesteps the PRB-auditor
# actor-confusion that the former plural ``issues-operator`` was chosen to dodge: the singular
# ``issue-operator`` read to the LLM auditor as an actor competing with the ``tester`` subject and
# made it wrongly reject the valid (tester, issues-write) grant. ``issue_operations`` reads as a
# category of actions, not a person, so that collision does not apply. (Historical detail:
# issues/agent/3.20-policy-rules-builder.md, "Follow-up: auditor relationship-scoping".)
AGENT_ROLES: dict[str, str] = {
    "source_operations": (
        "Covers read and write access to source repository contents — listing, reading, creating, "
        "and modifying files."
    ),
    "issue_operations": (
        "Covers read and write access to the issue tracker — reading, filing, updating, and "
        "commenting on issues and their threads."
    ),
}

# name -> description. Agent-boundary scopes exposed by the github-agent.
AGENT_SCOPES: dict[str, str] = {
    "source-access": (
        "Scope granting use of a source-code capability — invoking source-code functions such as "
        "reading and changing repository contents."
    ),
    "issues-access": (
        "Scope granting use of an issue-management capability — invoking issue functions such as "
        "reading and updating issues."
    ),
}

# name -> description. Fine-grained operations exposed by the github-tool.
TOOL_SCOPES: dict[str, str] = {
    "source-read": "Read source repository contents: file listings and file bodies. Read-only.",
    "source-write": "Create, modify, or delete source repository contents; commit file changes.",
    "issues-read": "Read issues and their comment threads. Read-only.",
    "issues-write": "Create and update issues: open, edit, comment, and close.",
}

# --- Role → access facts (name-level; the single source of truth) ---------------------------
#
# Identical to the 5.2 launcher's hand-built rule lists and to policy.explicit.md. Each set maps
# 1:1 to a PRB mapping and to a generated Rego gate:
#   (a) INBOUND_PAIRS          — user role  -> agent scope  (inbound; user may call the agent)
#   (b) OUTBOUND_SUBJECT_PAIRS — user role  -> tool scope   (outbound subject; user may reach tool)
#   (c) OUTBOUND_PAIRS         — agent role -> tool scope   (outbound target; agent may reach tool)

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("developer", "source-access"),
    ("developer", "issues-access"),
    ("tester", "issues-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("source_operations", "source-read"),
    ("source_operations", "source-write"),
    ("issue_operations", "issues-read"),
    ("issue_operations", "issues-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("developer", "source-read"),
    ("developer", "source-write"),
    ("developer", "issues-read"),
    ("tester", "issues-read"),
    ("tester", "issues-write"),
]
