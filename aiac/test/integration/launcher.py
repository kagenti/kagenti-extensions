"""Shared machinery for the integration-test launchers.

Two halves, both live here so a single module import serves every launcher:

* **Subprocess half** — spawn aiac services as ``uvicorn`` subprocesses, poll each ``GET /health``
  until ready, run some work, tear them down. Used by ``test/pdp/policy/generate_rego.py`` (the
  standalone Rego-dump launcher, which is *not* under ``test/integration/`` and is out of scope for
  the live-cluster rework). ``Service`` / ``start_service`` / ``wait_until_ready`` /
  ``running_services`` / ``terminate`` / ``print_rego_dir`` / ``resolve_output_dir`` exist for it.

* **Live-cluster half** — drive a real rossoctl/Kind cluster with the AuthBridge OPA pipeline wired
  in (see ``k8s/opa-kind-runbook.md``). ``kubectl`` wrappers + ``port_forward`` + ``resolve_pod``
  onboard through the in-cluster Controller; ``mint_token`` / ``jwt_claim`` / ``inbound_probe`` /
  ``outbound_probe`` send **real HTTP requests through AuthBridge** and classify the **real OPA
  plugin's** allow/deny; ``poll_until`` waits for ``bundle-service`` to reflect a CR change; and the
  skip gates (``require_env_or_skip`` / ``require_pipeline`` / ``verify_subject_mapper``) make the
  suite skip cleanly — never false-pass — when the cluster is not wired.

The evaluator is now the deployed plugin, not a standalone OPA-CLI run over dumped ``.rego``: there is
deliberately no ``opa`` binary dependency and no ``.rego``-dump oracle here anymore (handoff 08).

It imports only the standard library and ``requests`` — never ``aiac`` — so a launcher may import
it *before* setting the environment variables the aiac libraries read at import time. ``pytest`` is
imported lazily inside the skip gates (only the live suite uses them, and only under pytest) so the
module stays importable in the standalone launchers.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import requests

log = logging.getLogger(__name__)


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


def _pod_is_ready(pod: dict) -> bool:
    """True iff ``pod`` is ``Running`` with its ``Ready`` condition ``True``."""
    status = pod.get("status", {})
    if status.get("phase") != "Running":
        return False
    return any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
    )


def select_live_pod(items: list[dict]) -> str | None:
    """Pick the **newest live** pod name from a ``kubectl get pods`` ``items`` list, or ``None``.

    "Live" = **not terminating** (no ``metadata.deletionTimestamp``); among those, prefer the newest
    ``Ready`` pod (by ``creationTimestamp``), else the newest non-terminating one. Pure — no I/O — so
    the selection race behind issue #139 is unit-testable without a cluster."""
    live = [p for p in items if not p.get("metadata", {}).get("deletionTimestamp")]
    if not live:
        return None
    ready = [p for p in live if _pod_is_ready(p)]
    chosen = max(ready or live, key=lambda p: p.get("metadata", {}).get("creationTimestamp", ""))
    return chosen.get("metadata", {}).get("name")


def resolve_pod(selector: str, *, namespace: str) -> str:
    """Return the name of the **newest live** pod matching a label ``selector`` (e.g. ``app=aiac-opa``).

    "Live" = ``status.phase == Running``, the ``Ready`` condition true, and **not terminating** (no
    ``metadata.deletionTimestamp``). This matters during a rolling restart: with ``replicas=1`` and
    ``maxUnavailable=0`` the new pod is created and made Ready *before* the old one is deleted, so the
    old pod lingers ``Terminating`` (up to its grace period) alongside the new one. The old
    ``jsonpath={.items[0]}`` had no ordering or phase filter and could hand back that doomed pod; the
    caller would then pin it (e.g. ``kubectl exec``), the pod would finish terminating, and every
    later exec would fail ``NotFound`` — the intermittent stall behind issue #139. Selecting the
    newest Ready, non-terminating pod (see ``select_live_pod``) avoids that race.

    Falls back to the newest non-terminating pod when none report Ready yet (e.g. resolved mid-startup),
    and raises only when no non-terminating pod matches at all."""
    doc = json.loads(kubectl("get", "pods", "-n", namespace, "-l", selector, "-o", "json"))
    name = select_live_pod(doc.get("items", []))
    if name is None:
        raise RuntimeError(f"no (non-terminating) pod matches selector {selector!r} in namespace {namespace!r}")
    return name


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
# Live AuthBridge probes — the real OPA plugin is the evaluator (handoff 08)
# ======================================================================================
#
# The integration suite no longer evaluates ``.rego`` with a standalone ``opa`` binary. It onboards
# (which upserts the ``AuthorizationPolicy`` CR on the live API), waits for ``bundle-service`` to
# recompose the per-pod bundle, then sends **real HTTP requests through AuthBridge** and reads the
# **real OPA plugin's** decision off the response. AuthBridge's own ``jwt-validation`` + ``mcp-parser``
# build ``input.identity.*`` + ``input.mcp.params.name`` — the test never hand-builds an input doc.
#
# Request shaping + outcome classification follow ``k8s/opa-kind-runbook.md`` exactly (Parts A/B).

KEYCLOAK_CLIENT_ID = "rossoctl"  # the platform client the runbook mints user tokens through
_CURL_IMAGE = "curlimages/curl:8.10.1"  # same throwaway image the runbook probes with


def mint_token(
    username: str,
    password: str,
    *,
    keycloak_url: str,
    realm: str,
    client_id: str = KEYCLOAK_CLIENT_ID,
    scope: str = "openid",
    timeout: float = 30.0,
) -> str:
    """Mint a user access token via the OIDC password grant (runbook A.1 / B.4).

    Requires Direct Access Grants enabled on ``client_id`` and the user's password set; a token whose
    ``sub`` is the username further needs the realm's ``username -> sub`` mapper (see
    ``verify_subject_mapper``). Raises ``requests.HTTPError`` on a non-2xx token response so the caller
    can turn a mint failure into a skip."""
    resp = requests.post(
        f"{keycloak_url.rstrip('/')}/realms/{realm}/protocol/openid-connect/token",
        data={
            "client_id": client_id,
            "username": username,
            "password": password,
            "grant_type": "password",
            "scope": scope,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def jwt_claim(token: str, claim: str) -> object:
    """Best-effort decode of a JWT payload claim (no signature check — for the ``sub`` sanity gate).

    Splits off the payload segment, pads it to a base64url boundary, and returns ``claim`` (``None``
    if absent). Raises on a structurally invalid token."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get(claim)


def _parse_curl_output(stdout: str) -> tuple[int | None, str]:
    """Split a probe pod's stdout into ``(http_code, body)``.

    The probe scripts append a ``HTTP_CODE:<n>`` sentinel after the response body (inbound curl ``-w``)
    or emit an ``AB_HTTP:<n>`` / ``AB_ERR:<msg>`` marker (outbound python). Returns ``(None, stdout)``
    when no code marker is present (a failed probe — classified as ``"error"`` upstream)."""
    code: int | None = None
    body_lines: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("HTTP_CODE:") or stripped.startswith("AB_HTTP:"):
            try:
                code = int(stripped.split(":", 1)[1])
            except ValueError:
                code = None
            continue
        if stripped.startswith("AB_ERR:"):
            return None, stripped[len("AB_ERR:") :].strip()
        body_lines.append(line)
    return code, "\n".join(body_lines).strip()


def inbound_probe(
    token: str,
    *,
    namespace: str,
    agent_service: str,
    port: int = 8080,
    timeout: float = 120.0,
) -> tuple[int | None, str]:
    """Send an inbound request through AuthBridge as ``token`` and return ``(http_code, body)``.

    Mirrors the runbook's ``probe_as`` (A.2): a throwaway ``curlimages/curl`` pod in ``namespace``
    POSTs a ``ping/nonexistent`` JSON-RPC method to the agent Service — enough to clear
    ``jwt-validation`` + OPA and reach (or be blocked before) the app, without triggering the CrewAI
    flow. ``curl -w`` appends the sentinel ``HTTP_CODE:<n>`` line the caller parses. ``--command`` is
    used to override the image entrypoint robustly (a deliberate deviation from the runbook's bare
    ``-- sh -c``). Any kubectl/scheduling failure returns ``(None, <message>)`` -> classified
    ``"error"`` so a poll keeps waiting rather than crashing."""
    url = f"http://{agent_service}.{namespace}.svc.cluster.local:{port}/"
    script = (
        "curl -s -m 15 -w '\\nHTTP_CODE:%{http_code}\\n' "
        f"-X POST {url} "
        "-H 'Content-Type: application/json' -H \"Authorization: Bearer $TOK\" "
        "-d '{\"jsonrpc\":\"2.0\",\"id\":\"1\",\"method\":\"ping/nonexistent\",\"params\":{}}'"
    )
    pod_name = f"probe-inbound-{uuid.uuid4().hex[:8]}"
    try:
        out = kubectl(
            "run", pod_name,
            "-n", namespace,
            "--image", _CURL_IMAGE,
            "--restart=Never", "--rm", "--attach", "--quiet",
            f"--env=TOK={token}",
            "--command", "--", "sh", "-c", script,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or getattr(exc, "output", "") or str(exc)
        return None, f"inbound probe pod failed: {stderr}".strip()
    return _parse_curl_output(out)


def outbound_probe(
    token: str,
    tool_name: str,
    *,
    namespace: str,
    agent_pod: str,
    tool_url: str = "http://github-tool:9090/mcp",
    container: str = "agent",
    timeout: float = 120.0,
) -> tuple[int | None, str]:
    """Drive an outbound MCP ``tools/call`` through AuthBridge's forward proxy and return
    ``(http_code, body)``.

    Mirrors the runbook's outbound probe (B.4) but invokes a **bare** tool (``params.name = tool_name``,
    e.g. ``source-read``) instead of ``tools/list``, so AuthBridge's ``mcp-parser`` surfaces
    ``input.mcp.params.name`` and OPA's per-tool outbound gate is actually exercised. The agent app
    container has ``HTTP_PROXY=127.0.0.1:8081`` (the forward proxy) and ``python3``; ``token-exchange``
    uses the carried ``dev-user`` bearer as the RFC 8693 subject token. The python emits an
    ``AB_HTTP:<n>`` marker + body (or ``AB_ERR:<msg>``) the caller parses. Any exec failure returns
    ``(None, <message>)`` -> ``"error"``."""
    script = (
        "import urllib.request, urllib.error, json\n"
        f"tok = {json.dumps(token)}\n"
        f"name = {json.dumps(tool_name)}\n"
        f"url = {json.dumps(tool_url)}\n"
        'op = urllib.request.build_opener('
        'urllib.request.ProxyHandler({"http": "http://127.0.0.1:8081"}))\n'
        'body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": "tools/call",'
        ' "params": {"name": name, "arguments": {}}}).encode()\n'
        'req = urllib.request.Request(url, data=body, headers={'
        '"Content-Type": "application/json",'
        ' "Accept": "application/json, text/event-stream",'
        ' "Authorization": "Bearer " + tok})\n'
        "try:\n"
        "    r = op.open(req, timeout=15)\n"
        '    print("AB_HTTP:%d" % r.status)\n'
        '    print(r.read().decode("utf-8", "replace"))\n'
        "except urllib.error.HTTPError as e:\n"
        '    print("AB_HTTP:%d" % e.code)\n'
        '    print(e.read().decode("utf-8", "replace"))\n'
        "except Exception as e:\n"
        '    print("AB_ERR:%s" % e)\n'
    )
    try:
        out = kubectl(
            "exec", "-i", "-n", namespace, agent_pod, "-c", container,
            "--", "python3", "-",
            input_text=script,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or getattr(exc, "output", "") or str(exc)
        return None, f"outbound probe exec failed: {stderr}".strip()
    return _parse_curl_output(out)


def inbound_outcome(code: int | None) -> str:
    """Classify an inbound probe: HTTP 200 -> ``"allow"`` (reached the app), 403 -> ``"deny"`` (OPA
    blocked it), anything else -> ``"error"`` (runbook A.4)."""
    if code == 200:
        return "allow"
    if code == 403:
        return "deny"
    return "error"


def outbound_outcome(code: int | None, body: str) -> str:
    """Classify an outbound probe by **body**, per runbook B.4.

    On an MCP-shaped request AuthBridge renders an OPA denial as a JSON-RPC error frame **at HTTP 200**
    (``error.data.plugin == "opa"`` / ``error.data.error == "policy.forbidden"``) — not an HTTP error.
    So: ``503`` (token-exchange failed before OPA) or any other non-200/403 -> ``"error"``; a plain
    ``403`` (non-MCP-shaped rejection fallback) -> ``"deny"``; HTTP 200 with an OPA error frame ->
    ``"deny"``; HTTP 200 with any other body (a ``result`` frame, or a tool-level error that means OPA
    *allowed* the call) -> ``"allow"``. Classify by the frame, never the transport status."""
    if code is None:
        return "error"
    if code == 403:
        return "deny"
    if code != 200:
        return "error"
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return "error"
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        data = err.get("data")
        if isinstance(data, dict) and (
            data.get("plugin") == "opa" or data.get("error") == "policy.forbidden"
        ):
            return "deny"
    return "allow"


def poll_until(
    predicate: Callable[[], bool], *, timeout: float, interval: float = 5.0
) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout`` seconds elapse; return whether it did.

    Exceptions from ``predicate`` (a probe against an ephemeral pod / a bundle still rebuilding) are
    swallowed and retried — the point is to wait out ``bundle-service``'s rebuild + OPA poll latency
    without ``sleep``-and-hope (handoff 08 §2.2)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception as exc:  # noqa: BLE001 — a transient probe failure is expected while waiting
            log.debug("poll_until: predicate raised (retrying): %s", exc)
        time.sleep(interval)
    return False


# ======================================================================================
# Skip gates — the suite skips (never false-passes) when the live pipeline is not wired
# ======================================================================================


def require_env_or_skip(*names: str) -> dict[str, str]:
    """Like ``require_env`` but ``pytest.skip`` (not exit) when a variable is unset — so a developer
    running the integration marker without ``test/integration/.env`` sourced gets a clean skip, not a
    crash. ``pytest`` is imported lazily so the module stays importable outside pytest."""
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        import pytest

        pytest.skip(
            "integration env not set: "
            + ", ".join(missing)
            + " — source test/integration/.env (see aiac/CLAUDE.md)."
        )
    return {name: os.environ[name] for name in names}


def _kubectl_try(*args: str, timeout: float = 30.0) -> tuple[bool, str, str]:
    """Run ``kubectl`` returning ``(ok, stdout, error)`` instead of raising — for the readiness probe,
    where any failure (unreachable API, absent resource) is a *skip reason*, not a test error."""
    try:
        return True, kubectl(*args, timeout=timeout), ""
    except FileNotFoundError:
        return False, "", "kubectl not found on PATH"
    except subprocess.CalledProcessError as exc:
        return False, exc.output or "", (exc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return False, "", "kubectl timed out"


def pipeline_unwired_reason(*, namespace: str, workloads: list[str]) -> str | None:
    """Return ``None`` when the live AuthBridge OPA pipeline is fully wired for ``namespace``, else a
    human-readable reason the suite should skip. Checks (cheap -> specific): ``kubectl`` present, the
    ``AuthorizationPolicy`` CRD served, ``bundle-service`` Running, the ``opa`` plugin on **both** legs
    of ``namespace``'s AuthBridge runtime config, and each ``workloads`` pod Running."""
    if shutil.which("kubectl") is None:
        return "kubectl not on PATH"

    ok, _, err = _kubectl_try("get", "crd", "authorizationpolicies.agent.rossoctl.dev", "-o", "name")
    if not ok:
        return f"AuthorizationPolicy CRD not served / cluster unreachable ({err or 'no output'})"

    ok, out, err = _kubectl_try(
        "get", "pods", "-n", "rossoctl-system", "-l", "app=bundle-service",
        "-o", "jsonpath={.items[*].status.phase}",
    )
    if not ok:
        return f"cannot query bundle-service in rossoctl-system ({err})"
    if "Running" not in out:
        return "bundle-service is not Running in rossoctl-system"

    ok, out, err = _kubectl_try(
        "get", "configmap", "authbridge-runtime-config", "-n", namespace,
        "-o", r"jsonpath={.data.config\.yaml}",
    )
    if not ok:
        return f"authbridge-runtime-config not found in {namespace} ({err})"
    wired = out.count("name: opa")
    if wired < 2:
        return (
            f"OPA plugin not wired into both legs in {namespace} (found {wired} of 2) — "
            "run k8s/opa-kind-enable.sh"
        )

    for workload in workloads:
        ok, out, err = _kubectl_try(
            "get", "pods", "-n", namespace, "-l", f"app.kubernetes.io/name={workload}",
            "-o", "jsonpath={.items[*].status.phase}",
        )
        if not ok:
            return f"cannot query workload {workload!r} in {namespace} ({err})"
        if "Running" not in out:
            return f"workload {workload!r} is not Running in {namespace}"

    return None


def require_pipeline(*, namespace: str, workloads: list[str]) -> None:
    """``pytest.skip`` with a clear message when the live pipeline is not wired (acceptance #4)."""
    reason = pipeline_unwired_reason(namespace=namespace, workloads=workloads)
    if reason:
        import pytest

        pytest.skip(
            f"live AuthBridge OPA pipeline not wired: {reason}. Stand it up with "
            "k8s/opa-kind-enable.sh (see k8s/opa-kind-runbook.md)."
        )


def verify_subject_mapper(
    *, keycloak_url: str, realm: str, user: str, password: str, client_id: str = KEYCLOAK_CLIENT_ID
) -> str:
    """Mint a token for ``user`` and ``pytest.skip`` unless its ``sub`` equals ``user``.

    The live loop keys OPA decisions on ``input.identity.subject`` (the token ``sub``), which equals
    the username only when the realm carries the ``username -> sub`` mapper and Direct Access Grants
    are enabled on ``client_id`` — a one-time Keycloak prerequisite the fixture does **not** provision
    (runbook Prerequisites). Skipping here (rather than failing every decision) keeps a mis-provisioned
    realm from masquerading as a policy bug. Returns the minted token on success."""
    import pytest

    try:
        token = mint_token(user, password, keycloak_url=keycloak_url, realm=realm, client_id=client_id)
    except Exception as exc:  # noqa: BLE001 — any mint failure is a skippable prerequisite gap
        pytest.skip(
            f"cannot mint a {user!r} token in realm {realm!r} via client {client_id!r}: {exc}. "
            "Enable Direct Access Grants on the client and set the user's password "
            "(see k8s/opa-kind-runbook.md Prerequisites)."
        )
    sub = jwt_claim(token, "sub")
    if sub != user:
        pytest.skip(
            f"token 'sub' is {sub!r}, not {user!r} — the realm's username->sub protocol mapper is "
            "missing (see k8s/opa-kind-runbook.md Prerequisites)."
        )
    return token
