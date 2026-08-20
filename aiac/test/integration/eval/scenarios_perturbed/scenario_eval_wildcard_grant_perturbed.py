"""Semantic-perturbation sibling of ``scenario_eval_wildcard_grant.py`` (spec:
``docs/specs/integration-test/policy-eval-robustness-consistency.md``).

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
            "inventory-access": (
                "Lets a holder use the inventory agent's complete set of inventory abilities — "
                "stock-level checks, count adjustments, and reorders."
            ),
        },
        "target_scopes": {},
        "roles": {
            "inventory_operations": (
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
            "inventory-check": "Look up current stock levels for a product without changing anything.",
            "inventory-adjust": "Change the recorded stock count for a product.",
            "inventory-reorder": "Place a reorder for a product.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------

USERS: dict[str, str] = {
    "manager-user": "inventory-manager",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "inventory-manager": (
        "Inventory Manager: may carry out every inventory operation — stock-level checks, count "
        "adjustments, and reorders."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("inventory-manager", "inventory-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("inventory_operations", "inventory-check"),
    ("inventory_operations", "inventory-adjust"),
    ("inventory_operations", "inventory-reorder"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("inventory-manager", "inventory-check"),
    ("inventory-manager", "inventory-adjust"),
    ("inventory-manager", "inventory-reorder"),
]
