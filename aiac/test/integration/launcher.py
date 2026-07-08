"""Shared machinery for the standalone integration-test launchers.

Both ``test/pdp/policy/generate_rego.py`` (5.2) and ``test/integration/policy_pipeline.py`` (5.3)
spawn aiac services as ``uvicorn`` subprocesses, poll each ``GET /health`` until ready, run some
work, and tear the subprocesses down. This module holds that shared lifecycle so neither launcher
duplicates it.

It imports only the standard library and ``requests`` — never ``aiac`` — so a launcher may import
it *before* setting the environment variables the aiac libraries read at import time.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import requests


def ensure_on_path(*paths: Path) -> None:
    """Prepend each path to ``sys.path`` (once), so a launcher can import ``aiac`` from ``src``
    and the shared ``test.integration`` modules from the repo root."""
    for path in paths:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def require_env(*names: str) -> dict[str, str]:
    """Return the values of the named environment variables, or exit non-zero listing every one
    that is unset or empty. Used by launchers for inputs that have no safe default (Keycloak
    admin creds, LLM endpoint)."""
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print(
            "error: required environment variable(s) not set: " + ", ".join(missing),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return {name: os.environ[name] for name in names}


def resolve_output_dir(default: Path) -> Path:
    """Resolve ``REGO_OUTPUT_DIR`` (falling back to ``default``) to an absolute path."""
    return Path(os.environ.get("REGO_OUTPUT_DIR", default)).resolve()


@dataclass
class Service:
    """A ``uvicorn``-hostable ASGI app to run as a subprocess."""

    module_app: str  # e.g. "aiac.pdp.service.policy.opa.main:app"
    port: int
    host: str = "127.0.0.1"
    env: dict[str, str] = field(default_factory=dict)  # per-service extra env

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def start_service(service: Service, *, src: Path) -> subprocess.Popen:
    """Spawn ``service`` as a ``uvicorn`` subprocess with ``src`` on ``PYTHONPATH`` and the
    service's extra env applied on top of the current environment."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(service.env)
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            service.module_app,
            "--host",
            service.host,
            "--port",
            str(service.port),
        ],
        env=env,
    )


def wait_until_ready(base_url: str, *, timeout: float = 30.0) -> None:
    """Poll ``GET {base_url}/health`` until it returns 200, or raise after ``timeout`` seconds."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            if requests.get(f"{base_url}/health", timeout=1).status_code == 200:
                return
        except requests.RequestException as exc:
            last_err = exc
        time.sleep(0.3)
    raise RuntimeError(f"service not ready at {base_url} within {timeout}s ({last_err})")


def terminate(proc: subprocess.Popen) -> None:
    """SIGTERM ``proc`` and wait briefly, escalating to SIGKILL if it does not exit."""
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@contextmanager
def running_services(services: list[Service], *, src: Path, timeout: float = 30.0) -> Iterator[None]:
    """Spawn every service, poll each ``/health``, yield, then terminate them all in ``finally``.

    Every spawned subprocess is torn down even if a later spawn or health poll fails.
    """
    procs: list[subprocess.Popen] = []
    try:
        for service in services:
            procs.append(start_service(service, src=src))
        for service in services:
            wait_until_ready(service.base_url, timeout=timeout)
        yield
    finally:
        for proc in procs:
            terminate(proc)


def print_rego_dir(output_dir: Path) -> None:
    """Print the output directory and the ``.rego`` files it contains (the launcher's result)."""
    print(f"Rego written to: {output_dir}")
    for path in sorted(output_dir.glob("*.rego")):
        print(f"  {path.name}")
