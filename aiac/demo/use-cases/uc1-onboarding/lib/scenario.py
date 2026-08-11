"""The UC-1 onboarding demo's scenario data — a standalone copy of the facts
``test/integration/scenario_uc1.py`` encodes for the pytest ladder, plus the extra data this demo
needs and pytest does not (login profile fields, human-readable intents). Deliberately not imported
from ``test/`` — this demo ships outside the test tree and must run with no ``aiac`` checkout beyond
``demo/``.

Pure data + pure functions: no imports beyond the standard library, no cluster/Keycloak access. The
``expected_*`` helpers are the oracle every driver script (``run-developer.py`` etc.) checks live
decisions against — computed from the pair-lists below, never read from the Rego under test.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Realm + deployment identifiers ---------------------------------------------------------

REALM_DEFAULT = "rossoctl"
DEMO_NAMESPACE_DEFAULT = "team1"
AGENT_WORKLOAD = "github-agent"
TOOL_WORKLOAD = "github-tool"

# Public ROPC client the demo's run-*.py drivers log the demo users in with (see lib/setup_keycloak.py).
ROPC_CLIENT_ID = "aiac-demo-cli"

# username -> the realm role the user holds
USERS: dict[str, str] = {
    "dev-user": "developer",
    "test-user": "tester",
    "devops-user": "devops",
}

# Demo-only shared credential for the ephemeral Keycloak users; never used outside
# local/CI throwaway clusters. Not a production or secret value.
USER_PASSWORD = "password"

# Keycloak 26's declarative user profile requires email/firstName/lastName for role "user" before
# grant_type=password will succeed (VERIFY_PROFILE). Not needed by the pytest ladder (it never logs
# in as these users), but load-bearing here — the whole point of this demo is a real ROPC login.
USER_PROFILE: dict[str, dict[str, str]] = {
    "dev-user": {"email": "dev-user@uc1.demo", "firstName": "Dev", "lastName": "User"},
    "test-user": {"email": "test-user@uc1.demo", "firstName": "Test", "lastName": "User"},
    "devops-user": {"email": "devops-user@uc1.demo", "firstName": "Devops", "lastName": "User"},
}

# --- Realm-role descriptions (the PRB reads these when expanding the abstract policy) --------

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

# --- Discovered entities (what real UC-1 onboarding provisions; recorded here for the oracle) -

AGENT_SCOPES: dict[str, str] = {
    "github-agent.source_operations": (
        "Browse and search code; read, create, and modify repository file contents, branches, "
        "and commits."
    ),
    "github-agent.issue_operations": (
        "Read, search, create, and update issues, comments, sub-issues, and pull requests."
    ),
}

AGENT_ROLES: dict[str, str] = dict(AGENT_SCOPES)

# The provisioned Keycloak client-scope NAMES stay owner-prefixed (that is what Keycloak holds).
# The reworked writer de-prefixes them into the outbound Rego / CR — so the BARE tail
# (``source-read`` …) is the value that arrives in ``input.mcp.params.name`` and the value the
# outbound maps below key on. Keep this distinction in mind: prefixed here (Keycloak), bare in the
# oracle pair-lists and INTENTS (Rego / the invoked MCP tool name).
TOOL_SCOPES: dict[str, str] = {
    "github-tool.source-read": "Read source repository contents: file listings and file bodies. Read-only.",
    "github-tool.source-write": "Create, modify, or delete source repository contents; commit file changes.",
    "github-tool.issues-read": "Read issues and their comment threads. Read-only.",
    "github-tool.issues-write": "Create and update issues: open, edit, comment, and close.",
}

# --- Role -> access facts (the single source of truth the oracle is computed from) -----------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("developer", "github-agent.source_operations"),
    ("developer", "github-agent.issue_operations"),
    ("tester", "github-agent.issue_operations"),
]

# Tool scopes here are BARE (de-prefixed) — they must match the outbound Rego's de-prefixed map
# values and the bare ``input.mcp.params.name`` the drivers send. (Agent-side operator roles keep
# their owner prefix; only the tool-scope value de-prefixes.)
OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("developer", "source-read"),
    ("developer", "source-write"),
    ("developer", "issues-read"),
    ("tester", "issues-read"),
    ("tester", "issues-write"),
]

OUTBOUND_TARGET_PAIRS: list[tuple[str, str]] = [
    ("github-agent.source_operations", "source-read"),
    ("github-agent.source_operations", "source-write"),
    ("github-agent.issue_operations", "issues-read"),
    ("github-agent.issue_operations", "issues-write"),
]

# --- The single abstract policy.md the PRB reads (verbatim; also shown in demo.md) -----------

POLICY_ABSTRACT = """\
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
"""


# --- Human intents driven at the terminal — fixed mapping, no LLM, so the demo is deterministic

@dataclass(frozen=True)
class Intent:
    label: str  # what the driver script prints before asking the tool
    # The BARE invoked MCP tool name this intent maps to — the value that arrives in
    # ``input.mcp.params.name`` (None = inbound-only denial case, no tool call reached).
    function_name: str | None


INTENTS: dict[str, list[Intent]] = {
    "dev-user": [
        Intent("read a file from the repo", "source-read"),
        Intent("commit a small fix", "source-write"),
        Intent("check an issue for repro steps", "issues-read"),
        Intent("close out the issue", "issues-write"),
    ],
    "test-user": [
        Intent("check an issue for repro steps", "issues-read"),
        Intent("file a new bug", "issues-write"),
        Intent("read a file from the repo", "source-read"),
    ],
    "devops-user": [
        Intent("ask the agent anything", None),  # inbound denial — no function_name reached
    ],
}


# --- Oracle: expected verdicts, computed from the pair-lists above, never from generated Rego -

_INBOUND_SOURCES = {role for role, _ in INBOUND_PAIRS}
INBOUND_GRANT_SET: set[tuple[str, str]] = set(INBOUND_PAIRS)
OUTBOUND_SUBJECT_GRANT_SET: set[tuple[str, str]] = set(OUTBOUND_SUBJECT_PAIRS)
OUTBOUND_TARGET_GRANT_SET: set[str] = {fn for _, fn in OUTBOUND_TARGET_PAIRS}


def expected_inbound(subject: str) -> bool:
    """A user may call the agent iff their realm role sources some agent scope."""
    return USERS[subject] in _INBOUND_SOURCES


def expected_outbound(subject: str, function_name: str) -> bool:
    """A user may reach a tool scope iff both gates pass (per-scope AND): their realm role is
    granted it in the user->tool subject gate, and the agent's own operator roles reach it in the
    capability gate."""
    user_ok = (USERS[subject], function_name) in OUTBOUND_SUBJECT_GRANT_SET
    agent_ok = function_name in OUTBOUND_TARGET_GRANT_SET
    return user_ok and agent_ok
