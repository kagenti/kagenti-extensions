#!/usr/bin/env python3
"""Onboard the ``github-agent`` workload: ``POST /apply/service/{uuid}`` behind a port-forward to
the Controller, then capture the generated ``.rego`` into ``generated/01-after-agent/`` — the first
pause's evidence (before the tool exists, the agent's outbound gate is still empty)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import scenario as scn
from _lib import GENERATED, capture_rego, connect_admin, load_config, note, ok, onboard, port_forward, resolve_service_id, say, writer_pod


def main() -> None:
    cfg = load_config()
    admin = connect_admin(cfg)

    say("1", "3", f"Resolve {scn.AGENT_WORKLOAD} service id")
    service_id = resolve_service_id(admin, cfg, f"{cfg.namespace}/{scn.AGENT_WORKLOAD}")
    note(f"service id: {service_id}")

    say("2", "3", "Onboard (POST /apply/service/{id}) — this drives the PRB and can take minutes")
    with port_forward(cfg.controller_target, namespace=cfg.controller_namespace, local_port=cfg.controller_local_port, remote_port=cfg.controller_remote_port, ready_url=f"http://127.0.0.1:{cfg.controller_local_port}/health") as base_url:
        onboard(cfg, base_url, service_id)
    ok("onboarding call returned 200")

    say("3", "3", "Capture generated Rego")
    rego_dir = GENERATED / "01-after-agent"
    pod = writer_pod(cfg)
    capture_rego(cfg, pod, rego_dir)
    for f in (cfg.inbound_rego, cfg.outbound_rego):
        ok(f"{rego_dir / f}")

    print(f"\nAgent onboarded. Snapshot: {rego_dir}")
    print("Next: make show   (or: make onboard-tool)")


if __name__ == "__main__":
    main()
