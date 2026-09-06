"""Builds ``Role``/``Scope`` objects directly from a scenario module's data, with no Keycloak
involved (spec: ``docs/specs/eval/policy-eval-robustness-consistency.md``).

The consistency/robustness suites are scoped to the PRB's raw output only (no OPA/PCE/k8s in the
loop, per that spec's Testing Decisions), so they have no need for ``test_policy_pipeline_eval.py``'s
``provision_keycloak_admin``/``provision_via_config``/``_read_back`` trio, which exist purely to get
real Keycloak-backed ``Role``/``Scope`` objects for the full pipeline run. ``orchestrate_prb()``
only ever reads ``.name``/``.description`` off these objects (plus the scenario module's own dict
order, for candidate-list ordering) — everything else on ``Role``/``Scope`` has a safe default, so a
synthetic ``id`` is sufficient.
"""

from __future__ import annotations

from types import ModuleType

from aiac.idp.configuration.models import Role, RoleKind, Scope


def build_roles_and_scopes(scenario: ModuleType) -> tuple[dict[str, Role], dict[str, Scope]]:
    """Construct every ``Role``/``Scope`` a scenario's ``orchestrate_prb()`` call could need:
    one ``Role`` (``kind=USER``) per ``scenario.USER_ROLES`` entry, one ``Role`` (``kind=AGENT``)
    per agent role, and one ``Scope`` per agent inbound/target scope and per tool scope
    (``serviceId`` set to the owning agent/tool id, mirroring the real IdP's one-owner-per-scope
    invariant). Synthetic ``id``s only — nothing downstream (the PRB, ``grant_sets()``) reads them."""
    roles: dict[str, Role] = {
        name: Role(id=f"role-user-{name}", name=name, description=desc, composite=False, kind=RoleKind.USER)
        for name, desc in scenario.USER_ROLES.items()
    }
    scopes: dict[str, Scope] = {}
    for agent_id, agent in scenario.AGENTS.items():
        for name, desc in agent["inbound_scopes"].items():
            scopes[name] = Scope(id=f"scope-{name}", name=name, description=desc, serviceId=agent_id)
        for name, desc in agent.get("delegation_scopes", {}).items():
            scopes[name] = Scope(id=f"scope-{name}", name=name, description=desc, serviceId=agent_id)
        for name, desc in agent["roles"].items():
            roles[name] = Role(
                id=f"role-agent-{name}", name=name, description=desc, composite=False,
                kind=RoleKind.AGENT, actorIds=[agent_id],
            )
    for tool_id, tool in scenario.TOOLS.items():
        for name, desc in tool["scopes"].items():
            scopes[name] = Scope(id=f"scope-{name}", name=name, description=desc, serviceId=tool_id)
    return roles, scopes
