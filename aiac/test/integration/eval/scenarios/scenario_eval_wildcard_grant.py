"""Scenario 7 — wildcard grant: 1 user, 1 agent, 1 tool, retail/inventory domain.

Companion to ``scenario_eval_baseline.py`` (Scenario 1) for ``test_policy_pipeline_eval.py`` (spec:
``docs/specs/integration-test/policy-eval-scenarios.md``). Isolates one aspect: a wildcard-phrased
grant that must be expanded by the PRB to the correct concrete scope set.

Both the user role (``inventory-manager``) and the agent's own role (``inventory_operations``) are
described using an "all inventory operations" wildcard phrase rather than an enumerated scope list.
Ground truth expands the phrase to all three concrete scopes on ``inventory-tool``
(``inventory-check``, ``inventory-adjust``, ``inventory-reorder``) on both sides of the per-scope
AND gate — this scenario tests wildcard-phrase expansion specifically, not any subject/target
asymmetry (that distinction is covered elsewhere, e.g. ``scenario_eval_ambiguous_clause.py``).

Pure data: no imports beyond ``__future__``, mirroring ``scenario_eval_baseline.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-wildcard-grant"
POLICY_FILE = "policy.eval_wildcard_grant.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "inventory-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf against the retail inventory system. It "
            "covers all inventory operations against the inventory tool: checking stock levels, "
            "adjusting counts, and placing reorders."
        ),
        "inbound_scopes": {
            "inventory-access": (
                "Scope granting use of the inventory agent's full inventory-operations "
                "capability — checking stock levels, adjusting counts, and placing reorders."
            ),
        },
        "target_scopes": {},
        "roles": {
            "inventory_operations": (
                "Covers all inventory operations against the inventory tool — checking stock "
                "levels, adjusting counts, and placing reorders."
            ),
        },
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "inventory-tool": {
        "description": (
            "Capability provider Tool for retail inventory management. It performs stock-level "
            "checks, count adjustments, and reorder placements."
        ),
        "scopes": {
            "inventory-check": "Check current stock levels for a product. Read-only.",
            "inventory-adjust": "Adjust the recorded stock count for a product.",
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
        "Inventory Manager — authorized to perform all inventory operations: checking stock "
        "levels, adjusting counts, and placing reorders."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("inventory-manager", "inventory-access"),
]

# Wildcard phrase "all inventory operations" must expand to all three concrete scopes on both
# sides of the per-scope AND gate.
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
