"""Scenario 10 — empty descriptions: 1 user, 1 agent, 1 tool, agriculture/irrigation domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/eval/policy-eval-scenarios.md``). Isolates one aspect: entity, role, and
scope descriptions are empty or near-empty, so the PRB has no semantic content to infer intent
from beyond the bare identifiers themselves. Only the policy document's explicit, plainly-worded
grant sentences carry any meaning in this scenario — every (role, scope) pair below is named
outright in ``policy.eval_empty_descriptions.md`` rather than left for the PRB to derive from a
description.

Ground truth: despite every description being the empty string, the explicitly named grants must
still be honored — absent descriptions are not a reason to deny access the policy text plainly
grants, nor to invent access it doesn't.

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-empty-descriptions"
POLICY_FILE = "policy.eval_empty_descriptions.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "team1/irrigation-agent": {
        "description": "",
        "inbound_scopes": {
            "agent-scope-irrigation-access": "",
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-irrigation-operations": "",
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "valve-tool": {
        "description": "",
        "scopes": {
            "tool-scope-valve-open": "",
            "tool-scope-valve-close": "",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "operator-user": "user-role-field-operator",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-field-operator": "",
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-field-operator", "agent-scope-irrigation-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-irrigation-operations", "tool-scope-valve-open"),
    ("agent-role-irrigation-operations", "tool-scope-valve-close"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-field-operator", "tool-scope-valve-open"),
    ("user-role-field-operator", "tool-scope-valve-close"),
]
