"""Scenario 3 — agent-to-agent delegation: 2 users, 2 agents, 1 tool, logistics/shipping domain.

Companion to ``scenario_eval_baseline.py`` (``test/integration/eval/``) for
``test_policy_pipeline_eval.py`` (spec: ``docs/specs/integration-test/policy-eval-scenarios.md``).
Unlike every other scenario in the family, this module lives directly under ``test/integration/``
(a sibling of ``scenario.py``/``scenario_uc1.py``), not under ``test/integration/eval/`` — the one
deliberate exception in this suite's file layout, called out in the spec's *Location* section. It
is still imported and driven by the shared ``eval/test_policy_pipeline_eval.py`` harness; only its
file location differs, not its test wiring.

This scenario isolates the suite's **one** aspect: the agent-to-agent ``target_scopes`` delegation
mechanism (``src/aiac/policy/computation/engine.py``) — an Agent-typed service standing in as an
outbound-reach target for another agent, exercised through the exact same mechanism a tool uses, no
separate code path.

``dispatch-agent`` coordinates shipment dispatch: it owns a tool (``manifest-tool``) and can
delegate customs-clearance actions to ``customs-agent`` as part of a coordinated shipment.
``customs-agent`` owns the delegation target (``customs-clearance``, a scope, not a tool) and has
**no** ``inbound_scopes`` of its own — it is reachable ONLY as a delegation target. Two contrasting
realm roles demonstrate the mechanism is a real per-scope grant, not an automatic side effect of
calling ``dispatch-agent``: ``shipment-coordinator`` holds the delegated scope,
``dock-worker`` does not, even though both may call ``dispatch-agent`` and both reach
``manifest-tool``.

Because ``customs-agent`` has no ``inbound_scopes`` of its own, this scenario is also the cleanest
demonstration of the suite's "Further Notes" finding: ``target_scopes`` and ``inbound_scopes`` are
indistinguishable once provisioned into Keycloak, because both map onto the same Keycloak client
(``customs-agent``'s). So ``shipment-coordinator`` — granted ``customs-clearance`` purely for
delegation purposes — also, unavoidably, passes ``customs-agent``'s own inbound gate *directly*,
with no delegation involved and no ``dispatch-agent`` call required. ``dock-worker``, holding
neither, is refused entry to ``customs-agent`` from either direction.

Pure data: no imports beyond ``__future__``, mirroring ``scenario.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-agent-delegation"
POLICY_FILE = "policy.eval_agent_delegation.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "dispatch-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to coordinate shipment dispatch. It "
            "creates and updates shipment manifests, and can delegate customs-clearance actions "
            "to the customs agent as part of a coordinated shipment."
        ),
        "inbound_scopes": {
            "dispatch-access": (
                "Scope granting use of the dispatch agent's shipment-coordination capability — "
                "creating and updating manifests, and coordinating customs clearance for a "
                "shipment."
            ),
        },
        "target_scopes": {},
        "roles": {
            "dispatch_operations": (
                "Covers creating and updating shipment manifests, and delegating "
                "customs-clearance actions to the customs agent as part of a coordinated "
                "shipment."
            ),
        },
    },
    "customs-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to clear shipments through customs. It "
            "accepts delegated clearance requests from the dispatch agent as part of a "
            "coordinated shipment; it has no tools of its own."
        ),
        "inbound_scopes": {},
        "target_scopes": {
            "customs-clearance": (
                "Scope granting a coordinating agent the ability to have a shipment cleared "
                "through customs on its behalf. Not owned by a tool — owned by the customs "
                "agent itself."
            ),
        },
        "roles": {},
    },
}

# --- Tools --------------------------------------------------------------------------------------

TOOLS: dict[str, dict] = {
    "manifest-tool": {
        "description": (
            "Capability provider Tool for shipment manifests. It performs read and write "
            "operations on manifest contents and status."
        ),
        "scopes": {
            "manifest-read": "Read shipment manifests: contents and status. Read-only.",
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
        "Shipment Coordinator — authorized to create and update shipment manifests through the "
        "dispatch agent, and to have customs clearance carried out on the shipment's behalf as "
        "part of that coordinated process."
    ),
    "dock-worker": (
        "Dock Worker — authorized to create and update shipment manifests through the dispatch "
        "agent for day-to-day loading and unloading; not authorized to have customs clearance "
        "carried out on the shipment's behalf."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("shipment-coordinator", "dispatch-access"),
    ("dock-worker", "dispatch-access"),
    # No row names customs-agent's own inbound scope — it has none. Reachability comes entirely
    # from OUTBOUND_SUBJECT_PAIRS below, via the target-scope-delegation half of expected_inbound().
]

# Agent role -> target scope. Only dispatch_operations is populated — customs-agent's "roles" is
# empty (it has no tools of its own to reach).
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
    # No row grants dock-worker customs-clearance — the contrasting role without delegation.
]
