"""The UC-1 (discovery-driven) ``github-agent`` scenario — the oracle for
``test_uc1_onboarding_pipeline.py``.

Sibling of the hand-provisioned ``scenario.py`` (kept separate so 5.2/5.3 are untouched). Same
*role -> access facts and truth tables*; the difference is **provenance and naming**. Here the
agent/tool roles and scopes are not hand-picked — they are what **real UC-1 onboarding** discovers
and provisions from the deployed workloads, so every scope is **workload-prefixed**
(``github-tool.source-read``, ``github-agent.source_operations``) and the agent contributes a single
generic ``github-agent.agent`` role. The pair-lists below are therefore expressed over those
**discovered, prefixed** names — exactly the strings the generated Rego data maps contain — so the
test's expected verdicts are *computed from* this module, never from the Rego under test.

This module is **pure data**: it imports nothing (no ``aiac``, no stdlib beyond the language) so the
test can import it before its env-before-import step, just like ``scenario.py``.

Fact triad (spec ``docs/specs/integration-test/uc1-onboarding-pipeline.md`` -> *Further
Notes*): the *Scenario* table, **both** ``policy.md`` variants (``POLICY_EXPLICIT`` /
``POLICY_ABSTRACT`` below), and the pair-lists here must all agree. The generic entity/role/scope
descriptions are functional and keyword-free and must not contradict the facts.
"""

from __future__ import annotations

# --- Realm + deployment identifiers ---------------------------------------------------------

# Dedicated test realm (never deleted/recreated). The operator registers the demo namespace's
# clients into it because the fixture sets KEYCLOAK_REALM on that namespace's authbridge-config.
REALM_DEFAULT = "aiac-uc1-e2e"

# Namespace the demo workloads deploy into (operator registers clients as "{ns}/{workload}").
DEMO_NAMESPACE_DEFAULT = "team1"

# Workload names == Service names == Keycloak client.name suffix. The trigger id is the Keycloak
# *clientId* of the client whose *name* is "{ns}/{workload}" (a SPIFFE URI under SPIRE, else the
# bare "{ns}/{workload}"); the test resolves it by name, never by assuming the string.
AGENT_WORKLOAD = "github-agent"
TOOL_WORKLOAD = "github-tool"

# username -> the realm role the user holds
USERS: dict[str, str] = {
    "dev-user": "developer",
    "test-user": "tester",
    "devops-user": "devops",
}

# Fixed dev password for the provisioned test users (throwaway realm).
USER_PASSWORD = "password"

# --- Realm-role descriptions (provisioned by the fixture; verbatim from the spec) -----------
#
# The PRB reads these descriptions when expanding the abstract policy. ``devops`` is deliberately
# unrelated to source/issue work: it appears in no pair-list and neither policy variant, so
# deny-by-default leaves devops-user denied inbound and on every outbound function.

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

# --- Discovered entities (what real UC-1 onboarding provisions) -----------------------------
#
# These are NOT provisioned by the test — UC-1 discovers them (tool scopes from the MCP
# ``tools/list`` manifest, agent role/scopes from the AgentCard skills) and writes them into
# Keycloak. They are recorded here only so the pair-lists and the grant-set equivalence check can
# reference the exact prefixed strings the generated Rego contains.

# The single generic agent role UC-1 emits (hardcoded ``f"{workload}.agent"``, description
# "Agent role"). It cannot be mapped to specific tool scopes under deny-by-default, so the
# agent->tool gate (``target_ok``) is degenerate/empty — documented, not asserted (see
# ``OUTBOUND_TARGET_PAIRS`` below and the spec's *The agent->tool gate*).
AGENT_ROLE = "github-agent.agent"

# name -> description. Agent-boundary scopes, from the AgentCard skills (verbatim descriptions).
AGENT_SCOPES: dict[str, str] = {
    "github-agent.source_operations": (
        "Browse and search code; read, create, and modify repository file contents, branches, "
        "and commits."
    ),
    "github-agent.issue_operations": (
        "Read, search, create, and update issues, comments, sub-issues, and pull requests."
    ),
}

# name -> description. Fine-grained tool operations, from the simplified tool's MCP ``tools/list``
# (verbatim descriptions — identical text to ``scenario.py``'s tool scopes, only prefixed).
TOOL_SCOPES: dict[str, str] = {
    "github-tool.source-read": "Read source repository contents: file listings and file bodies. Read-only.",
    "github-tool.source-write": "Create, modify, or delete source repository contents; commit file changes.",
    "github-tool.issues-read": "Read issues and their comment threads. Read-only.",
    "github-tool.issues-write": "Create and update issues: open, edit, comment, and close.",
}

# --- Role -> access facts (over the DISCOVERED, prefixed names; the single source of truth) --
#
# Identical *decisions* to ``scenario.py``; only the scope-name strings are prefixed. Each set maps
# 1:1 to a generated Rego gate:
#   INBOUND_PAIRS          — user role  -> agent scope  (inbound; user may call the agent)
#   OUTBOUND_SUBJECT_PAIRS — user role  -> tool scope   (outbound subject; user may reach the tool)
#   OUTBOUND_TARGET_PAIRS  — agent role -> tool scope   (outbound target; DEGENERATE under UC-1)

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("developer", "github-agent.source_operations"),
    ("developer", "github-agent.issue_operations"),
    ("tester", "github-agent.issue_operations"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("developer", "github-tool.source-read"),
    ("developer", "github-tool.source-write"),
    ("developer", "github-tool.issues-read"),
    ("tester", "github-tool.issues-read"),
    ("tester", "github-tool.issues-write"),
]

# Agent->tool gate. Deliberately EMPTY: UC-1's single generic ``github-agent.agent`` role maps to no
# specific tool scope under deny-by-default, so ``target_ok`` is degenerate. The outbound probe
# evaluates ``subject_ok`` only (spec: *user-gating dimension only*); this list documents the empty
# gate and is not probed.
OUTBOUND_TARGET_PAIRS: list[tuple[str, str]] = []

# --- The two policy.md variants (baked into the two AIAC stacks out of band) ----------------
#
# The AIAC pods mount their own ``policy.md`` (via AIAC_POLICY_FILE); the test does not feed these
# at runtime. They live here as the fact-triad anchor — verbatim from the spec's *Scenario inputs*.
# Both are USER-INTENT-ONLY: neither names the agent role (naming it would populate ``target_ok``
# and break both the user-gate-only decision and cross-variant equivalence). Both must yield the
# SAME discovered grant set.

# Version 1 (explicit): enumerates each (user-role -> discovered scope) pair by its full prefixed
# name. No agent-role->tool-scope section. Deny by default.
POLICY_EXPLICIT = """\
# Access Control Policy — github-agent / github-tool

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call the agent)
- developer may use github-agent.source_operations and github-agent.issue_operations.
- tester may use github-agent.issue_operations.

## Users → tool operations (outbound subject; user may reach the tool)
- developer may perform github-tool.source-read, github-tool.source-write, and github-tool.issues-read.
- tester may perform github-tool.issues-read and github-tool.issues-write.
"""

# Version 2 (abstract): intent-only prose. Same facts; relies on the PRB/LLM to expand intent into
# the discovered scopes via the entity/role descriptions.
POLICY_ABSTRACT = """\
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
"""
