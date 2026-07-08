"""The canonical ``github-agent`` scenario — single source of truth for both launchers.

The 5.2 launcher (``test/pdp/policy/generate_rego.py``) hand-builds a ``PolicyModel`` from
these names + role→access pair-sets; the 5.3 launcher (``test/integration/policy_pipeline.py``)
provisions the same entities into a live Keycloak realm and feeds the descriptions to the PRB.
Keeping the scenario in one place is what the spec's *Further Notes* mandate — the role→access
facts, the entity/role/scope descriptions, and both ``policy.md`` variants must stay mutually
consistent (spec: ``inception/requirements/integration-test/policy-pipeline.md``, *Scenario* +
*Scenario inputs*).

Pure data: this module imports nothing (no aiac, no stdlib beyond the language) so a launcher can
import it before the env-before-import step. Dict insertion order is significant — it is preserved
into the generated Rego, so it matches the 5.2 launcher's original literal order.
"""

from __future__ import annotations

# --- Realm + entity identifiers -------------------------------------------------------------

REALM_DEFAULT = "aiac-e2e"
AGENT_ID = "github-agent"
TOOL_ID = "github-tool"

# username -> the realm role the user holds
USERS: dict[str, str] = {
    "dev-user": "developer",
    "test-user": "tester",
}

# Fixed dev password for the provisioned test users (throwaway realm).
USER_PASSWORD = "password"

# --- Descriptions (verbatim from the spec's *Scenario inputs*) ------------------------------
#
# The client descriptions deliberately contain the words "Agent" / "Tool" so the IdP library's
# type inference (``_build_service``) tags them Agent / Tool — the tool tag is what makes the PCE
# omit the tool model.

AGENT_DESCRIPTION = (
    "GitHub Agent — an autonomous agent that acts on a user's GitHub source repositories and "
    "issue tracker on the user's behalf. It performs source-code work (inspecting repository "
    "file contents and committing changes) and issue-management work (reading issue threads and "
    "creating or updating issues). Its source-code responsibility is represented by the "
    "`source-helper` client role and gated at the agent boundary by the `source-access` scope; "
    "its issue-management responsibility is represented by the `issues-helper` client role and "
    "gated by the `issues-access` scope. The agent does not call GitHub directly — it delegates "
    "each concrete operation to the `github-tool`, so its own scopes describe capabilities it may "
    "exercise while the tool's scopes describe the operations those capabilities resolve to."
)

TOOL_DESCRIPTION = (
    "GitHub Tool — a capability provider that exposes fine-grained, least-privilege operations "
    "against GitHub source repositories and the issue tracker. It offers four distinct "
    "operations, each represented by its own scope: read source (`source-read`) and write source "
    "(`source-write`) for repository file contents, and read issues (`issues-read`) and write "
    "issues (`issues-write`) for the issue tracker. The tool performs the actual GitHub calls; "
    "every caller (such as the `github-agent` acting for a user) must present the specific scope "
    "for each operation it invokes."
)

# name -> description. Realm roles held by users.
USER_ROLES: dict[str, str] = {
    "developer": (
        "Developer — an engineering user who works on the codebase. A developer needs full read "
        "and write access to source repository contents (to inspect and change code) and read "
        "access to the issue tracker (to see reported work), but does not modify issues. "
        "Resolves to source read, source write, and issues read."
    ),
    "tester": (
        "Tester — a quality-assurance user who works through the issue tracker. A tester needs "
        "full read and write access to issues (to file, triage, and update defect and test "
        "reports) but does not touch source repository contents. Resolves to issues read and "
        "issues write."
    ),
}

# name -> description. The github-agent's client roles.
AGENT_ROLES: dict[str, str] = {
    "source-helper": (
        "The github-agent's client role for source-code operations. Groups the agent's ability "
        "to read and write repository source content; gated at the agent boundary by "
        "`source-access`, and downstream resolves to the tool's `source-read` / `source-write`."
    ),
    "issues-helper": (
        "The github-agent's client role for issue operations. Groups the agent's ability to read "
        "and write issues; gated at the agent boundary by `issues-access`, and downstream "
        "resolves to the tool's `issues-read` / `issues-write`."
    ),
}

# name -> description. Agent-boundary scopes exposed by the github-agent.
AGENT_SCOPES: dict[str, str] = {
    "source-access": (
        "Agent-boundary scope granting use of the github-agent's source capability (the "
        "`source-helper` role). A user holding it may invoke the agent's source-code functions."
    ),
    "issues-access": (
        "Agent-boundary scope granting use of the github-agent's issues capability (the "
        "`issues-helper` role). A user holding it may invoke the agent's issue functions."
    ),
}

# name -> description. Fine-grained operations exposed by the github-tool.
TOOL_SCOPES: dict[str, str] = {
    "source-read": (
        "Tool operation: read source repository contents (file listings and file bodies). "
        "Read-only."
    ),
    "source-write": (
        "Tool operation: create, modify, or delete source repository contents (commits / file "
        "writes)."
    ),
    "issues-read": "Tool operation: read issues and their comments/threads. Read-only.",
    "issues-write": "Tool operation: create and update issues (open, edit, comment, close).",
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
    ("source-helper", "source-read"),
    ("source-helper", "source-write"),
    ("issues-helper", "issues-read"),
    ("issues-helper", "issues-write"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("developer", "source-read"),
    ("developer", "source-write"),
    ("developer", "issues-read"),
    ("tester", "issues-read"),
    ("tester", "issues-write"),
]
