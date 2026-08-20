#!/usr/bin/env python3
"""Reset the demo to a clean slate: delete UC-1's provisioned Keycloak roles/scopes, clear the
Policy Store (non-optional — its SQLite survives on a PV and onboarding appends with
``override=False``), delete the agent's ``AuthorizationPolicy`` CR (the reworked writer is
CR-backed — there is no ``/rego`` file to wipe), and clear the local ``generated/`` copy.

Kept separate from ``03-setup.py`` so a presenter can re-run just the reset between takes.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from _lib import GENERATED, cleanup_provisioned, clear_policy_store, clear_writer_rego, connect_admin, load_config, ok, say


def main() -> None:
    cfg = load_config()

    say("1", "4", "Clear Keycloak: delete github-agent.*/github-tool.* roles + scopes")
    admin = connect_admin(cfg)
    cleanup_provisioned(admin, cfg)
    ok("Keycloak provisioned entities cleared")

    say("2", "4", "Clear Policy Store: DELETE /policy/services")
    clear_policy_store(cfg)
    ok("Policy Store cleared")

    say("3", "4", "Delete the agent's AuthorizationPolicy CR")
    clear_writer_rego(cfg)
    ok(f"deleted AuthorizationPolicy {cfg.cr_name!r} in {cfg.namespace!r} (if present)")

    say("4", "4", "Clear local generated/ snapshots")
    if GENERATED.exists():
        shutil.rmtree(GENERATED)
    GENERATED.mkdir(parents=True, exist_ok=True)
    ok(f"cleared {GENERATED}")

    print("\nBaseline is clean.")


if __name__ == "__main__":
    main()
