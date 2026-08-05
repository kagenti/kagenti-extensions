"""Shared machinery for the integration-test launchers.

The subprocess half — spawn aiac services as ``uvicorn`` subprocesses, poll each ``GET /health``
until ready, run some work, tear them down — is used by ``test/pdp/policy/generate_rego.py`` (5.2)
and ``test/integration/test_policy_pipeline.py`` (5.3).

The cluster half — ``kubectl`` cp, ``kubectl port-forward``, ``resolve_pod``, and the ``opa``
oracle — is used by the UC-1 onboarding ladder (``test/integration/test_uc1_onboard_agent_only.py``
and its rung-2/3 siblings, 5.4), which drives a real rossoctl/Kind cluster rather than in-process
subprocesses.

It imports only the standard library and ``requests`` — never ``aiac`` — so a launcher may import
it *before* setting the environment variables the aiac libraries read at import time. ``pytest`` is
imported lazily inside ``opa_bin`` (only 5.4 uses it, and only under pytest) so the module stays
importable in the standalone launchers.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
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


# ======================================================================================
# Cluster helpers (5.4) — kubectl apply/delete/rollout/get/cp + port-forward
# ======================================================================================
#
# Thin wrappers around the ``kubectl`` CLI (no in-process K8s client — keeps launcher.py
# dependency-free and mirrors what an operator would run by hand). Every call honours
# ``KUBECONFIG`` from the environment. Failures raise ``subprocess.CalledProcessError`` with the
# captured stderr, so the caller's assertion message names the failing command.


def kubectl(*args: str, input_text: str | None = None, timeout: float = 60.0) -> str:
    """Run ``kubectl <args>`` and return stdout (raising on non-zero exit). ``input_text`` is
    piped to stdin (e.g. for ``kubectl apply -f -``)."""
    proc = subprocess.run(
        ["kubectl", *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["kubectl", *args], output=proc.stdout, stderr=proc.stderr
        )
    return proc.stdout


def kubectl_apply(manifest_path: Path, *, namespace: str | None = None) -> None:
    """``kubectl apply -f <manifest_path>`` (optionally ``-n <namespace>``)."""
    args = ["apply", "-f", str(manifest_path)]
    if namespace:
        args += ["-n", namespace]
    kubectl(*args)


def kubectl_delete(manifest_path: Path, *, namespace: str | None = None, timeout: float = 120.0) -> None:
    """``kubectl delete -f <manifest_path> --ignore-not-found`` — safe to call in teardown even if
    the workloads are already gone."""
    args = ["delete", "-f", str(manifest_path), "--ignore-not-found", "--wait=true"]
    if namespace:
        args += ["-n", namespace]
    kubectl(*args, timeout=timeout)


def kubectl_rollout_status(resource: str, *, namespace: str, timeout: float = 180.0) -> None:
    """Block until ``resource`` (e.g. ``deployment/github-tool``) is rolled out, or raise."""
    kubectl(
        "rollout", "status", resource, "-n", namespace, f"--timeout={int(timeout)}s", timeout=timeout + 10
    )


def kubectl_get_json(resource: str, *, namespace: str | None = None) -> dict:
    """``kubectl get <resource> -o json`` parsed to a dict (a single object or a ``List``)."""
    args = ["get", resource, "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    return json.loads(kubectl(*args))


def kubectl_cp(pod: str, remote_path: str, local_path: Path, *, namespace: str, container: str | None = None) -> None:
    """``kubectl cp <ns>/<pod>:<remote_path> <local_path>`` — copy a file/dir out of a pod."""
    args = ["cp", f"{namespace}/{pod}:{remote_path}", str(local_path)]
    if container:
        args += ["-c", container]
    kubectl(*args, timeout=120.0)


def resolve_pod(selector: str, *, namespace: str) -> str:
    """Return the name of the first pod matching a label ``selector`` (e.g. ``app=aiac-opa``)."""
    out = kubectl(
        "get", "pods", "-n", namespace, "-l", selector,
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    if not out.strip():
        raise RuntimeError(f"no pod matches selector {selector!r} in namespace {namespace!r}")
    return out.strip()


@contextmanager
def port_forward(target: str, *, namespace: str, local_port: int, remote_port: int,
                 ready_url: str | None = None, timeout: float = 30.0) -> Iterator[str]:
    """Run ``kubectl port-forward <target> <local>:<remote>`` for the duration of the block,
    yielding the local ``http://127.0.0.1:<local_port>`` base URL.

    ``target`` is a kubectl port-forward target (``svc/aiac-controller``, ``deploy/...``, ``pod/...``).
    The forward is not yielded until it is actually up: if ``ready_url`` is given it is polled until
    it answers (any HTTP status); otherwise the tunnel's own ``Forwarding from ...`` line is awaited
    (used for targets that expose no HTTP readiness path). A background thread drains the merged stdout/stderr the
    whole time — both to detect that line and so the OS pipe buffer can never fill and deadlock
    kubectl — and its captured output is surfaced if the forward exits early or never comes up.
    """
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, target, f"{local_port}:{remote_port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{local_port}"
    output: list[str] = []
    forwarding = threading.Event()

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:  # blocks in the thread, never on the main path
            output.append(line)
            if "Forwarding from" in line:
                forwarding.set()

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        deadline = time.time() + timeout
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                reader.join(timeout=1)
                raise RuntimeError(
                    f"port-forward to {target} exited early: {''.join(output).strip()}"
                )
            if ready_url is None:
                if forwarding.wait(timeout=0.3):  # tunnel announced it is up
                    ready = True
                    break
            else:
                try:
                    requests.get(ready_url, timeout=1)
                    ready = True
                    break
                except requests.RequestException:
                    time.sleep(0.3)
        if not ready:
            raise RuntimeError(
                f"port-forward to {target} not ready within {timeout}s: {''.join(output).strip()}"
            )
        yield base_url
    finally:
        terminate(proc)
        reader.join(timeout=1)


# ======================================================================================
# OPA oracle (5.4) — standalone ``opa eval`` as the verification binary
# ======================================================================================


def opa_bin() -> str:
    """Path to the ``opa`` binary (``$OPA_BIN`` -> ``PATH``), or ``pytest.skip`` the calling test.
    Absence SKIPS, never fails (spec/issue acceptance)."""
    found = os.environ.get("OPA_BIN") or shutil.which("opa")
    if not found:
        import pytest

        pytest.skip("opa binary not found (set OPA_BIN or add opa to PATH)")
    return found


def opa_eval(rego_paths: list[Path], query: str, input_doc: dict) -> object:
    """Evaluate ``query`` against the given Rego file(s) with ``input_doc`` on stdin, returning the
    result value. Raises (via ``check=True``) if OPA rejects the Rego or the query errors."""
    cmd = [
        opa_bin(),
        "eval",
        "-f",
        "json",
        *sum((["-d", str(p)] for p in rego_paths), []),
        "--stdin-input",
        query,
    ]
    out = subprocess.run(
        cmd, input=json.dumps(input_doc), capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)["result"][0]["expressions"][0]["value"]
