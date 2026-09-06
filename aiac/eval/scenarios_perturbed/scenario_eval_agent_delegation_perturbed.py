"""Semantic-perturbation sibling of ``scenario_eval_agent_delegation.py`` (spec:
``docs/specs/eval/policy-eval-robustness-consistency.md``).

Note the asymmetry: the original lives at ``test/integration/`` top level (a deliberate exception
in the base suite's file layout), but this perturbed sibling lives here in
``eval/scenarios_perturbed/`` alongside every other scenario's perturbed sibling — the robustness
suite treats all 8 scenarios uniformly regardless of where their originals happen to live.

Same structure (names, ``USERS``, all pair-lists are byte-identical to the original), reworded
prose only. See ``scenario_eval_baseline_perturbed.py`` for the general rationale.

Pure data: no imports beyond ``__future__``, mirroring the original.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-agent-delegation"
POLICY_FILE = "policy.eval_agent_delegation_perturbed.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "dispatch-agent": {
        "description": (
            "An autonomous agent that coordinates shipment dispatch on a user's behalf: creating "
            "and updating shipment manifests, and able to hand off agent-scope-customs-clearance work to the "
            "customs agent as part of a coordinated shipment."
        ),
        "inbound_scopes": {
            "agent-scope-dispatch-access": (
                "Lets a holder use the dispatch agent's shipment-coordination abilities — "
                "creating and updating manifests, and coordinating customs clearance for a "
                "shipment."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-dispatch-operations": (
                "Covers creating and updating shipment manifests, and handing off "
                "agent-scope-customs-clearance work to the customs agent as part of a coordinated shipment."
            ),
        },
    },
    "customs-agent": {
        "description": (
            "An autonomous agent that clears shipments through customs on a user's behalf, taking "
            "on clearance work handed off from the dispatch agent as part of a coordinated "
            "shipment. Has no tools of its own."
        ),
        "inbound_scopes": {},
        "delegation_scopes": {
            "agent-scope-customs-clearance": (
                "Lets a coordinating agent get a shipment cleared through customs on its behalf. "
                "Owned by the customs agent itself, not by a tool."
            ),
        },
        "roles": {},
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "manifest-tool": {
        "description": (
            "A capability provider for shipment manifests, handling both reads and writes of "
            "manifest contents and status."
        ),
        "scopes": {
            "tool-scope-manifest-read": "Look up shipment manifests — contents and status — without changing anything.",
            "tool-scope-manifest-write": "Create and update shipment manifests.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# Two contrasting roles: both may call dispatch-agent and reach manifest-tool; only
# user-role-shipment-coordinator additionally holds the delegated agent-scope-customs-clearance scope.

USERS: dict[str, str] = {
    "coordinator-user": "user-role-shipment-coordinator",
    "dock-user": "user-role-dock-worker",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "user-role-shipment-coordinator": (
        "Shipment Coordinator: may create and update shipment manifests via the dispatch agent, "
        "and may have customs clearance carried out on the shipment's behalf as part of that "
        "coordinated process."
    ),
    "user-role-dock-worker": (
        "Dock Worker: may create and update shipment manifests via the dispatch agent for routine "
        "loading and unloading. May not have customs clearance carried out on the shipment's "
        "behalf."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-shipment-coordinator", "agent-scope-dispatch-access"),
    ("user-role-dock-worker", "agent-scope-dispatch-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("agent-role-dispatch-operations", "tool-scope-manifest-read"),
    ("agent-role-dispatch-operations", "tool-scope-manifest-write"),
    ("agent-role-dispatch-operations", "agent-scope-customs-clearance"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("user-role-shipment-coordinator", "tool-scope-manifest-read"),
    ("user-role-shipment-coordinator", "tool-scope-manifest-write"),
    ("user-role-shipment-coordinator", "agent-scope-customs-clearance"),
    ("user-role-dock-worker", "tool-scope-manifest-read"),
    ("user-role-dock-worker", "tool-scope-manifest-write"),
]
