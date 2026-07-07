"""Generate github-agent Rego by driving the live PDP Policy Writer (OPA) stub.

Standalone (NOT pytest, NOT CI). Launches the stub as a uvicorn subprocess
writing to a known local dir, applies a PolicyModel through the PDP policy
library, shuts the service down, and prints the output dir. Inspect the .rego
files by hand.

Run:
    .venv/bin/python test/pdp/policy/generate_rego.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import requests

SRC = Path(__file__).resolve().parents[3] / "src"        # -> aiac/src
sys.path.insert(0, str(SRC))

from aiac.idp.configuration.models import Role, Scope                          # noqa: E402
from aiac.policy.model.models import AgentPolicyModel, PolicyModel, PolicyRule  # noqa: E402

PORT = int(os.environ.get("PORT", "7072"))
BASE_URL = f"http://127.0.0.1:{PORT}"
OUTPUT_DIR = Path(
    os.environ.get("REGO_OUTPUT_DIR", Path(__file__).parent / "rego_out")
).resolve()


def build_model() -> PolicyModel:
    developer     = Role(id="role-developer",     name="developer",     composite=False)
    tester        = Role(id="role-tester",        name="tester",        composite=False)
    source_helper = Role(id="role-source-helper", name="source-helper", composite=False)
    issues_helper = Role(id="role-issues-helper", name="issues-helper", composite=False)
    source_access = Scope(id="scope-source-access", name="source-access")
    issues_access = Scope(id="scope-issues-access", name="issues-access")
    source_read   = Scope(id="scope-source-read",   name="source-read")
    source_write  = Scope(id="scope-source-write",  name="source-write")
    issues_read   = Scope(id="scope-issues-read",   name="issues-read")
    issues_write  = Scope(id="scope-issues-write",  name="issues-write")

    agent = AgentPolicyModel(
        agent_id="github-agent",
        agent_roles=[source_helper, issues_helper],
        agent_scopes=[source_access, issues_access],
        source_roles={},
        subject_roles={"dev-user": [developer], "test-user": [tester]},
        target_scopes={"github-tool": [source_read, source_write, issues_read, issues_write]},
        inbound_rules=[
            PolicyRule(role=developer, scope=source_access),
            PolicyRule(role=developer, scope=issues_access),
            PolicyRule(role=tester,    scope=issues_access),
        ],
        outbound_rules=[
            PolicyRule(role=source_helper, scope=source_read),
            PolicyRule(role=source_helper, scope=source_write),
            PolicyRule(role=issues_helper, scope=issues_read),
            PolicyRule(role=issues_helper, scope=issues_write),
        ],
        outbound_subject_rules=[
            PolicyRule(role=developer, scope=source_read),
            PolicyRule(role=developer, scope=source_write),
            PolicyRule(role=developer, scope=issues_read),
            PolicyRule(role=tester,    scope=issues_read),
            PolicyRule(role=tester,    scope=issues_write),
        ],
    )
    return PolicyModel(agents=[agent])


def start_service() -> subprocess.Popen:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["REGO_OUTPUT_DIR"] = str(OUTPUT_DIR)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "aiac.pdp.service.policy.opa.main:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        env=env,
    )


def wait_until_ready(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"service not ready at {BASE_URL} within {timeout}s")


def main() -> None:
    os.environ["AIAC_PDP_POLICY_URL"] = BASE_URL          # consumed by the library
    from aiac.pdp.policy.library.api import apply_policy    # import after env is set

    proc = start_service()
    try:
        wait_until_ready()
        apply_policy(build_model())
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f"Rego written to: {OUTPUT_DIR}")
    for path in sorted(OUTPUT_DIR.glob("*.rego")):
        print(f"  {path.name}")


if __name__ == "__main__":
    main()
