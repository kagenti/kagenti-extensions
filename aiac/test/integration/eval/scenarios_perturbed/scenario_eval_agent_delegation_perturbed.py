"""Semantic-perturbation sibling of ``scenario_eval_agent_delegation.py`` (spec:
``docs/specs/integration-test/policy-eval-robustness-consistency.md``).

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
            "and updating shipment manifests, and able to hand off customs-clearance work to the "
            "customs agent as part of a coordinated shipment."
        ),
        "inbound_scopes": {
            "dispatch-access": (
                "Lets a holder use the dispatch agent's shipment-coordination abilities — "
                "creating and updating manifests, and coordinating customs clearance for a "
                "shipment."
            ),
        },
        "target_scopes": {},
        "roles": {
            "dispatch_operations": (
                "Covers creating and updating shipment manifests, and handing off "
                "customs-clearance work to the customs agent as part of a coordinated shipment."
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
        "target_scopes": {
            "customs-clearance": (
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
            "manifest-read": "Look up shipment manifests — contents and status — without changing anything.",
            "manifest-write": "Create and update shipment manifests.",
        },
    },
}

# --- Users ----------------------------------------------------------------------------------
#
# Two contrasting roles: both may call dispatch-agent and reach manifest-tool; only
# shipment-coordinator additionally holds the delegated customs-clearance scope.

USERS: dict[str, str] = {
    "coordinator-user": "shipment-coordinator",
    "dock-user": "dock-worker",
}

USER_PASSWORD = "password"

USER_ROLES: dict[str, str] = {
    "shipment-coordinator": (
        "Shipment Coordinator: may create and update shipment manifests via the dispatch agent, "
        "and may have customs clearance carried out on the shipment's behalf as part of that "
        "coordinated process."
    ),
    "dock-worker": (
        "Dock Worker: may create and update shipment manifests via the dispatch agent for routine "
        "loading and unloading. May not have customs clearance carried out on the shipment's "
        "behalf."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------
#
# Byte-identical to the original — reworded descriptions above don't change the truth table.

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("shipment-coordinator", "dispatch-access"),
    ("dock-worker", "dispatch-access"),
]

OUTBOUND_PAIRS: list[tuple[str, str]] = [
    ("dispatch_operations", "manifest-read"),
    ("dispatch_operations", "manifest-write"),
    ("dispatch_operations", "customs-clearance"),
]

OUTBOUND_SUBJECT_PAIRS: list[tuple[str, str]] = [
    ("shipment-coordinator", "manifest-read"),
    ("shipment-coordinator", "manifest-write"),
    ("shipment-coordinator", "customs-clearance"),
    ("dock-worker", "manifest-read"),
    ("dock-worker", "manifest-write"),
]
