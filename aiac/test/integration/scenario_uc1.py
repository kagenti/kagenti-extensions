"""The UC-1 (discovery-driven) ``github-agent`` scenario — the oracle for the UC-1 onboarding
integration-test ladder (``test_uc1_onboard_agent_only.py`` and its rung-2/3 siblings).

Sibling of the hand-provisioned ``scenario.py`` (kept separate so 5.2/5.3 are untouched). Same
*role -> access facts and truth tables*; the difference is **provenance and naming**. Here the
agent/tool roles and scopes are not hand-picked — they are what **real UC-1 onboarding** discovers
and provisions from the deployed workloads, so every scope is **workload-prefixed**
(``github-tool.source-read``, ``github-agent.source_operations``) and the agent contributes **one
operator role per skill** (``github-agent.source_operations`` / ``github-agent.issue_operations``),
each mirroring its skill scope's name + description. The pair-lists below are therefore expressed
over those **discovered, prefixed** names — exactly the strings the generated Rego data maps contain
— so the test's expected verdicts are *computed from* this module, never from the Rego under test.

Outbound access is a **per-scope two-gate AND**: a requested tool scope is allowed iff **both** the
user role reaches it (subject gate, ``OUTBOUND_SUBJECT_PAIRS``) **and** the agent's own per-skill
operator role reaches it (capability gate, ``OUTBOUND_TARGET_PAIRS``). The capability gate is mapped
from the operator-role **descriptions** by the PRB (capability-match under ``generic_policy.md``); in
UC-1 the agent reaches all four tool scopes, so the AND reduces to the subject gate for the verdicts
but both gates are populated and probed.

This module is **pure data**: it imports nothing (no ``aiac``, no stdlib beyond the language) so the
test can import it before its env-before-import step, just like ``scenario.py``.

Fact triad (spec ``docs/specs/integration-test/uc1-onboarding-pipeline.md``): the *Scenario* table,
the single abstract ``policy.md`` (``POLICY_ABSTRACT`` below), and the pair-lists here must all
agree. The generic entity/role/scope descriptions are functional and keyword-free and must not
contradict the facts. (The prior explicit variant + cross-variant equivalence are deferred to the
two-policy rung ``testing/5.4.4``; the two-stack topology that served both variants is discarded.)
"""

from __future__ import annotations

# --- Realm + deployment identifiers ---------------------------------------------------------

# The realm the deployed AIAC stack operates on (the cluster's ``aiac-pdp-config`` ConfigMap sets
# ``KEYCLOAK_REALM=rossoctl`` on the Controller + IdP-config pods, so the UC-1 harness must resolve
# and provision against the same realm). Never deleted/recreated; the operator registers the demo
# namespace's clients into it. Override with ``AIAC_TEST_REALM`` if the stack runs on another realm.
REALM_DEFAULT = "rossoctl"

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

# Fixed dev password for the provisioned test users. The realm, users, and roles are provisioned
# idempotently and left in place across runs (never deleted/recreated — see ``REALM_DEFAULT``).
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

# The per-skill operator roles UC-1 emits — one per skill, mirroring each scope's name +
# description (``analyze_agent`` builds ``roles`` from the same skills as ``scopes``). Their
# descriptions are what the PRB capability-match reads to grant the agent's outbound access on a
# domain basis, populating the agent->tool capability gate (``OUTBOUND_TARGET_PAIRS`` below).
AGENT_ROLES: dict[str, str] = dict(AGENT_SCOPES)

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
#   INBOUND_PAIRS          — user role     -> agent scope  (inbound; user may call the agent)
#   OUTBOUND_SUBJECT_PAIRS — user role     -> tool scope   (outbound subject gate; user reaches the tool)
#   OUTBOUND_TARGET_PAIRS  — operator role -> tool scope   (outbound capability gate; agent reaches the tool)

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

# Agent->tool capability gate: the per-skill operator role -> tool scope pairs the PRB
# capability-match maps from the operator-role descriptions (source_operations -> the two source
# scopes; issue_operations -> the two issue scopes). The agent reaches all four tool scopes, so this
# gate is fully populated (no longer degenerate) and both gates are probed as a per-scope AND.
OUTBOUND_TARGET_PAIRS: list[tuple[str, str]] = [
    ("github-agent.source_operations", "github-tool.source-read"),
    ("github-agent.source_operations", "github-tool.source-write"),
    ("github-agent.issue_operations", "github-tool.issues-read"),
    ("github-agent.issue_operations", "github-tool.issues-write"),
]

# --- The single abstract policy.md (baked into the AIAC stack out of band) ------------------
#
# The AIAC pod mounts its own ``policy.md`` (via AIAC_POLICY_FILE); the test does not feed it at
# runtime. It lives here as the fact-triad anchor — verbatim from the spec's *Scenario inputs*. It
# is USER-INTENT-ONLY: it states only what users may do; it does not name the agent's operator roles.
# The agent's own capability (the ``OUTBOUND_TARGET_PAIRS`` gate) comes from the generic rubric
# (``generic_policy.md``) applied to the operator-role descriptions, not from naming those roles in
# this policy. Intent-only prose; the PRB/LLM expands intent into the discovered scopes via the
# entity/role descriptions.
#
# (The prior explicit enumerated variant and the cross-variant equivalence check are deferred to the
# two-policy rung ``testing/5.4.4``; the two-stack topology that served both variants is discarded.)
POLICY_ABSTRACT = """\
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
"""
