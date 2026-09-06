"""Semantic-perturbation sibling of ``scenario_eval_wildcard_grant.py`` (spec:
``docs/specs/eval/policy-eval-robustness-consistency.md``).

Same structure (names, ``USERS``, all pair-lists are byte-identical to the original), reworded
prose only. See ``scenario_eval_baseline_perturbed.py`` for the general rationale. The reworded
policy text preserves the same wildcard-phrased grant as the original.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-wildcard-grant"
POLICY_FILE = "policy.eval_wildcard_grant_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "inventory-agent": {
        "description": (
            "An autonomous agent that acts for a user against the retail inventory system, "
            "handling every inventory operation the inventory tool offers: stock-level checks, "
            "count adjustments, and reorders."
        ),
        "inbound_scopes": {
            "agent-scope-inventory-access": (
                "Lets a holder use the inventory agent's complete set of inventory abilities — "
                "stock-level checks, count adjustments, and reorders."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-inventory-operations": (
                "Covers every inventory operation against the inventory tool — stock-level "
                "checks, count adjustments, and reorders."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "inventory-tool": {
        "description": (
            "A capability provider for retail inventory management, handling stock-level checks, "
            "count adjustments, and reorders."
        ),
        "scopes": {
            "tool-scope-inventory-check": "Look up current stock levels for a product without changing anything.",
            "tool-scope-inventory-adjust": "Change the recorded stock count for a product.",
            "tool-scope-inventory-reorder": "Place a reorder for a product.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "manager-user": "user-role-inventory-manager",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-inventory-manager": (
        "Inventory Manager: may carry out every inventory operation — stock-level checks, count "
        "adjustments, and reorders."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-inventory-manager", "agent-scope-inventory-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-inventory-operations", "tool-scope-inventory-check"),
    ("agent-role-inventory-operations", "tool-scope-inventory-adjust"),
    ("agent-role-inventory-operations", "tool-scope-inventory-reorder"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-inventory-manager", "tool-scope-inventory-check"),
    ("user-role-inventory-manager", "tool-scope-inventory-adjust"),
    ("user-role-inventory-manager", "tool-scope-inventory-reorder"),
]
