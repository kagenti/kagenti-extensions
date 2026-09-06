"""Scenario 3 — agent-to-agent delegation: 2 users, 2 agents, 1 tool, logistics/shipping domain.

Companion to ``scenario_eval_baseline.py`` (``eval/``) for
``test_policy_pipeline_eval.py`` (spec: ``docs/specs/eval/policy-eval-scenarios.md``).
Unlike every other scenario in the family, this module lives directly under ``test/integration/``
(a sibling of ``scenario.py``/``scenario_uc1.py``), not under ``eval/`` — the one
deliberate exception in this suite's file layout, called out in the spec's *Location* section. It
is still imported and driven by the shared ``eval/test_policy_pipeline_eval.py`` harness; only its
file location differs, not its test wiring.

This scenario isolates the suite's **one** aspect: the agent-to-agent ``target_scopes`` delegation
mechanism (``src/aiac/policy/computation/engine.py``) — an Agent-typed service standing in as an
outbound-reach target for another agent, exercised through the exact same mechanism a tool uses, no
separate code path.

``dispatch-agent`` coordinates shipment dispatch: it owns a tool (``manifest-tool``) and can
delegate agent-scope-customs-clearance actions to ``customs-agent`` as part of a coordinated shipment.
``customs-agent`` owns the delegation target (``agent-scope-customs-clearance``, a scope, not a tool) and has
**no** ``inbound_scopes`` of its own — it is reachable ONLY as a delegation target. Two contrasting
realm roles demonstrate the mechanism is a real per-scope grant, not an automatic side effect of
calling ``dispatch-agent``: ``user-role-shipment-coordinator`` holds the delegated scope,
``user-role-dock-worker`` does not, even though both may call ``dispatch-agent`` and both reach
``manifest-tool``.

Because ``customs-agent`` has no ``inbound_scopes`` of its own, this scenario is also the cleanest
demonstration of the suite's "Further Notes" finding: ``delegation_scopes`` and ``inbound_scopes``
are indistinguishable once provisioned into Keycloak, because both map onto the same Keycloak client
(``customs-agent``'s). So ``user-role-shipment-coordinator`` — granted ``agent-scope-customs-clearance`` purely for
delegation purposes — also, unavoidably, passes ``customs-agent``'s own inbound gate *directly*,
with no delegation involved and no ``dispatch-agent`` call required. ``user-role-dock-worker``, holding
neither, is refused entry to ``customs-agent`` from either direction.

Pure data: no imports beyond ``__future__``, mirroring ``scenario.py``.
"""

from __future__ import annotations

# --- Realm ------------------------------------------------------------------------------------

REALM_DEFAULT = "aiac-pp-eval-agent-delegation"
POLICY_FILE = "policy.eval_agent_delegation.md"

# --- Agents -------------------------------------------------------------------------------------

AGENTS: dict[str, dict] = {
    "team1/dispatch-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to coordinate shipment dispatch. It "
            "creates and updates shipment manifests, and can delegate agent-scope-customs-clearance actions "
            "to the customs agent as part of a coordinated shipment."
        ),
        "inbound_scopes": {
            "agent-scope-dispatch-access": (
                "Scope granting use of the dispatch agent's shipment-coordination capability — "
                "creating and updating manifests, and coordinating customs clearance for a "
                "shipment."
            ),
        },
        "delegation_scopes": {},
        "roles": {
            "agent-role-dispatch-operations": (
                "Covers creating and updating shipment manifests, and delegating "
                "agent-scope-customs-clearance actions to the customs agent as part of a coordinated "
                "shipment."
            ),
        },
    },
    "team1/customs-agent": {
        "description": (
            "Autonomous Agent acting on a user's behalf to clear shipments through customs. It "
            "accepts delegated clearance requests from the dispatch agent as part of a "
            "coordinated shipment; it has no tools of its own."
        ),
        "inbound_scopes": {},
        "delegation_scopes": {
            "agent-scope-customs-clearance": (
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
            "tool-scope-manifest-read": "Read shipment manifests: contents and status. Read-only.",
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
        "Shipment Coordinator — authorized to create and update shipment manifests through the "
        "dispatch agent, and to have customs clearance carried out on the shipment's behalf as "
        "part of that coordinated process."
    ),
    "user-role-dock-worker": (
        "Dock Worker — authorized to create and update shipment manifests through the dispatch "
        "agent for day-to-day loading and unloading; not authorized to have customs clearance "
        "carried out on the shipment's behalf."
    ),
}

# --- Role -> access facts (name-level; the single source of truth) --------------------------

INBOUND_PAIRS: list[tuple[str, str]] = [
    ("user-role-shipment-coordinator", "agent-scope-dispatch-access"),
    ("user-role-dock-worker", "agent-scope-dispatch-access"),
    # No row names customs-agent's own inbound scope — it has none. Reachability comes entirely
    # from OUTBOUND_SUBJECT_PAIRS below, via the target-scope-delegation half of expected_inbound().
]

# Agent role -> target scope. Only agent-role-dispatch-operations is populated — customs-agent's "roles" is
# empty (it has no tools of its own to reach).
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
    # No row grants user-role-dock-worker agent-scope-customs-clearance — the contrasting role without delegation.
]
