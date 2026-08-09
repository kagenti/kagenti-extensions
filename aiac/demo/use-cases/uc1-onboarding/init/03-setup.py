#!/usr/bin/env python3
"""Provision the demo's Keycloak users/roles, mount the PRB's policy.md on the Controller, run the
token-exchange Keycloak setup, and resolve + print both workloads' internal client UUIDs (the
trigger ids ``04-onboard-agent.py``/``05-onboard-tool.py`` need — the ``clientId`` is a slash-bearing
SPIFFE URI the single-segment ``/apply/service/{id}`` route can't carry).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import scenario as scn
import setup_keycloak
from _lib import connect_admin, ensure_agent_policy, load_config, note, ok, provision_realm_and_users, resolve_service_id, say


def main() -> None:
    cfg = load_config()

    say("1", "4", "Provision users + roles (with the login-profile fix)")
    admin = connect_admin(cfg)
    provision_realm_and_users(admin, cfg)
    for username, role in scn.USERS.items():
        ok(f"{username} -> {role}")

    say("2", "4", "Mount policy.md on the Controller")
    ensure_agent_policy(cfg)
    ok(f"policy.md mounted ({len(scn.POLICY_ABSTRACT.splitlines())} lines)")

    say("3", "4", "Resolve client UUIDs + configure token exchange")
    agent_uuid = resolve_service_id(admin, cfg, f"{cfg.namespace}/{scn.AGENT_WORKLOAD}")
    tool_uuid = resolve_service_id(admin, cfg, f"{cfg.namespace}/{scn.TOOL_WORKLOAD}")
    note(f"{scn.AGENT_WORKLOAD} client uuid: {agent_uuid}")
    note(f"{scn.TOOL_WORKLOAD} client uuid: {tool_uuid}")
    setup_keycloak.run(admin, cfg, agent_uuid=agent_uuid)
    ok("token exchange configured")

    say("4", "4", "Done")
    print(f"\nAgent service id: {agent_uuid}")
    print(f"Tool service id:  {tool_uuid}")
    print("\nNext: make onboard-agent")


if __name__ == "__main__":
    main()
