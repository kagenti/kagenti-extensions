"""Shared machinery for the UC-1 onboarding demo's numbered scripts: config, kubectl/opa helpers,
Keycloak helpers, and terminal narration. Standalone by design (some overlap with
``test/integration/launcher.py`` and ``uc1_onboard.py`` is deliberate) — this demo ships and runs
independently of the ``test/`` tree.

Strictly-live, no-fallback demo: every helper here either succeeds or calls ``abort()``/raises. There
is no offline mode and no silent skip — a missing ``opa`` binary or an unreachable cluster stops the
demo with a clear message, it never quietly does less.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import requests

import scenario as scn

HERE = Path(__file__).resolve().parent.parent  # lib/ -> uc1-onboarding/
GENERATED = HERE / "generated"

# The reworked PDP Policy Writer (OPA) is CR-backed: it server-side-applies one
# per-agent AuthorizationPolicy CR (agent.rossoctl.dev/v1alpha1) and, in production, writes
# NO .rego files (k8s/pdp-interface-deployment.yaml keeps POLICY_WRITER_DUMP_REGO off and mounts
# no /rego). So this demo sources its rego straight from the CR's spec.policies[].content — the
# same artifact a live enforcement point consumes — rather than kubectl-cp-ing a dumped file.
# Fully qualified to disambiguate from Istio's security.istio.io AuthorizationPolicy (same short
# name): `kubectl get authorizationpolicies.agent.rossoctl.dev/<name> -n <ns>`.
AUTHZ_POLICY_RESOURCE = "authorizationpolicies.agent.rossoctl.dev"


# ======================================================================================
# Narration — terminal output in the authbridge/demos house style
# ======================================================================================

_COLOR = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def rule() -> None:
    print(_c("2", "-" * 72))


def say(step: str, total: str, title: str) -> None:
    """Bold ``[step/total] title`` section header."""
    print()
    rule()
    print(_c("1", f"[{step}/{total}] {title}"))
    rule()


def note(msg: str) -> None:
    print(f"  {_c('2', '▸')} {msg}")


def ok(msg: str) -> None:
    print(f"  {_c('32', '✓')} {msg}")


def fail(msg: str) -> None:
    print(f"  {_c('31', '✗')} {msg}")


def blocked(msg: str) -> None:
    print(f"  {_c('33', '⛔')} {msg}")


def table(rows: list[tuple[str, ...]], headers: tuple[str, ...] | None = None) -> None:
    all_rows = ([headers] if headers else []) + rows
    widths = [max(len(str(r[i])) for r in all_rows) for i in range(len(all_rows[0]))]
    if headers:
        print("  " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths)))
        print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def abort(msg: str) -> None:
    print(f"\n{_c('31;1', 'ABORT')}: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ======================================================================================
# Config — read env at call time (not import time), so scripts can be re-run in one process
# ======================================================================================


@dataclass(frozen=True)
class Config:
    realm: str
    namespace: str
    admin_realm: str

    controller_namespace: str
    controller_target: str
    controller_local_port: int
    controller_remote_port: int

    store_namespace: str
    store_target: str
    store_local_port: int
    store_remote_port: int

    controller_deployment: str
    policy_configmap: str
    policy_mount_path: str

    onboard_timeout: float

    keycloak_url: str
    keycloak_admin_username: str
    keycloak_admin_password: str

    @property
    def cr_name(self) -> str:
        """The demo agent's ``AuthorizationPolicy`` CR name. The reworked writer places a CR at
        ``identity_ref(agent_id) -> (namespace, name)`` and bundle-service matches it by
        name+namespace; for this demo that name is just ``AGENT_WORKLOAD`` (``github-agent``) in
        ``cfg.namespace`` (``team1``). No per-agent underscore slug any more — the package name is
        fixed (``authbridge.client.{inbound,outbound}.request``), never per-agent."""
        return scn.AGENT_WORKLOAD

    @property
    def inbound_rego(self) -> str:
        """Snapshot-relative path of the inbound rego, mirroring the CR ``policies[].path`` under
        the writer's nested ``<ns>/<name>/`` layout (``rego_dir / cfg.inbound_rego`` is the local
        file ``capture_rego`` writes)."""
        return f"{self.namespace}/{scn.AGENT_WORKLOAD}/inbound/request.rego"

    @property
    def outbound_rego(self) -> str:
        return f"{self.namespace}/{scn.AGENT_WORKLOAD}/outbound/request.rego"


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        abort("required environment variable(s) not set: " + ", ".join(missing))
    return {n: os.environ[n] for n in names}


def load_config() -> Config:
    creds = require_env("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")
    return Config(
        realm=os.environ.get("AIAC_TEST_REALM", scn.REALM_DEFAULT),
        namespace=os.environ.get("AIAC_DEMO_NAMESPACE", scn.DEMO_NAMESPACE_DEFAULT),
        admin_realm=os.environ.get("KEYCLOAK_ADMIN_REALM", "master"),
        controller_namespace=os.environ.get("AIAC_CONTROLLER_NAMESPACE", "aiac-system"),
        controller_target=os.environ.get("AIAC_CONTROLLER_TARGET", "svc/aiac-agent-service"),
        controller_local_port=int(os.environ.get("AIAC_CONTROLLER_LOCAL_PORT", "7070")),
        controller_remote_port=int(os.environ.get("AIAC_CONTROLLER_REMOTE_PORT", "7070")),
        store_namespace=os.environ.get("AIAC_STORE_NAMESPACE", "aiac-system"),
        store_target=os.environ.get("AIAC_STORE_TARGET", "svc/aiac-policy-model-store-service"),
        store_local_port=int(os.environ.get("AIAC_STORE_LOCAL_PORT", "7074")),
        store_remote_port=int(os.environ.get("AIAC_STORE_REMOTE_PORT", "7074")),
        controller_deployment=os.environ.get("AIAC_CONTROLLER_DEPLOYMENT", "aiac-agent"),
        policy_configmap=os.environ.get("AIAC_POLICY_CONFIGMAP", "aiac-policy"),
        policy_mount_path=os.environ.get("AIAC_POLICY_MOUNT_PATH", "/etc/aiac"),
        onboard_timeout=float(os.environ.get("AIAC_ONBOARD_TIMEOUT", "900")),
        keycloak_url=creds["KEYCLOAK_URL"],
        keycloak_admin_username=creds["KEYCLOAK_ADMIN_USERNAME"],
        keycloak_admin_password=creds["KEYCLOAK_ADMIN_PASSWORD"],
    )


# ======================================================================================
# kubectl + opa
# ======================================================================================


def kubectl(*args: str, input_text: str | None = None, timeout: float = 60.0) -> str:
    proc = subprocess.run(
        ["kubectl", *args], input=input_text, capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["kubectl", *args], output=proc.stdout, stderr=proc.stderr
        )
    return proc.stdout


def kubectl_get_json(resource: str, *, namespace: str | None = None) -> dict:
    args = ["get", resource, "-o", "json"]
    if namespace:
        args += ["-n", namespace]
    return json.loads(kubectl(*args))


def kubectl_rollout_status(resource: str, *, namespace: str, timeout: float = 180.0) -> None:
    kubectl("rollout", "status", resource, "-n", namespace, f"--timeout={int(timeout)}s", timeout=timeout + 10)


def terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@contextmanager
def port_forward(
    target: str, *, namespace: str, local_port: int, remote_port: int,
    ready_url: str | None = None, timeout: float = 30.0,
) -> Iterator[str]:
    proc = subprocess.Popen(
        ["kubectl", "port-forward", "-n", namespace, target, f"{local_port}:{remote_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{local_port}"
    output: list[str] = []
    forwarding = threading.Event()

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
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
                abort(f"port-forward to {target} exited early: {''.join(output).strip()}")
            if ready_url is None:
                if forwarding.wait(timeout=0.3):
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
            abort(f"port-forward to {target} not ready within {timeout}s: {''.join(output).strip()}")
        yield base_url
    finally:
        terminate(proc)
        reader.join(timeout=1)


def opa_bin() -> str:
    found = os.environ.get("OPA_BIN") or shutil.which("opa")
    if not found:
        abort("opa binary not found — install opa or set OPA_BIN (this demo has no fallback verifier)")
    return found


def opa_eval(rego_paths: list[Path], query: str, input_doc: dict) -> object:
    cmd = [opa_bin(), "eval", "-f", "json", *sum((["-d", str(p)] for p in rego_paths), []), "--stdin-input", query]
    try:
        proc = subprocess.run(cmd, input=json.dumps(input_doc), capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        abort(f"opa eval timed out after 30s for {query!r} against {rego_paths}")
    if proc.returncode != 0:
        abort(f"opa eval failed for {query!r}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
    except (KeyError, IndexError) as exc:
        abort(f"opa eval produced no value for {query!r} against {rego_paths}: {exc}")


# ======================================================================================
# Keycloak
# ======================================================================================


def connect_admin(cfg: Config):
    from keycloak import KeycloakAdmin

    return KeycloakAdmin(
        server_url=cfg.keycloak_url,
        realm_name=cfg.admin_realm,
        user_realm_name=cfg.admin_realm,
        username=cfg.keycloak_admin_username,
        password=cfg.keycloak_admin_password,
    )


def provision_realm_and_users(admin, cfg: Config) -> None:
    """Idempotently ensure the realm holds the demo's users + realm roles, with the description
    text the PRB reads and the ``aiac.managed`` marker that makes the IdP populate ``actorIds``.

    Also sets ``email``/``firstName``/``lastName``/``emailVerified`` on every user — Keycloak 26's
    declarative user profile requires them for role ``user`` before ``grant_type=password`` will
    succeed, and this demo, unlike the pytest ladder, actually performs a real ROPC login."""
    from keycloak.exceptions import KeycloakError

    try:
        admin.create_realm({"realm": cfg.realm, "enabled": True})
    except KeycloakError:
        pass
    admin.change_current_realm(cfg.realm)

    for name, description in scn.USER_ROLES.items():
        payload = {"name": name, "description": description, "attributes": {"aiac.managed": ["true"]}}
        admin.create_realm_role(payload, skip_exists=True)
        admin.update_realm_role(name, payload)

    for username, role_name in scn.USERS.items():
        user_id = admin.create_user({"username": username, "enabled": True}, exist_ok=True)
        admin.set_user_password(user_id, scn.USER_PASSWORD, temporary=False)
        admin.assign_realm_roles(user_id, [admin.get_realm_role(role_name)])
        profile = scn.USER_PROFILE[username]
        admin.update_user(user_id, {**profile, "emailVerified": True})


def resolve_service_id(admin, cfg: Config, client_name: str) -> str:
    admin.change_current_realm(cfg.realm)
    for client in admin.get_clients():
        if client.get("name") == client_name:
            return client["id"]
    abort(f"no Keycloak client with name {client_name!r} in realm {cfg.realm!r}")


def cleanup_provisioned(admin, cfg: Config) -> None:
    """Delete the realm roles + client scopes UC-1 onboarding provisions (prefixed
    ``github-agent.``/``github-tool.``). Leaves the demo's own developer/tester/devops roles and the
    operator's audience client scopes (``*-aud``) in place."""
    from keycloak.exceptions import KeycloakError

    admin.change_current_realm(cfg.realm)
    prefixes = (f"{scn.AGENT_WORKLOAD}.", f"{scn.TOOL_WORKLOAD}.")

    for role in admin.get_realm_roles():
        name = role.get("name", "")
        if name.startswith(prefixes):
            try:
                admin.delete_realm_role(name)
            except KeycloakError:
                pass

    for scope in admin.get_client_scopes():
        name = scope.get("name", "")
        if name.startswith(prefixes):
            try:
                admin.delete_client_scope(scope["id"])
            except KeycloakError:
                pass


def client_secret(admin, cfg: Config, client_uuid: str) -> str:
    admin.change_current_realm(cfg.realm)
    return admin.get_client_secrets(client_uuid)["value"]


def clear_policy_store(cfg: Config) -> None:
    """``DELETE /policy/services`` on the in-cluster Policy Store — non-optional. Its SQLite lives
    on a surviving PV and onboarding appends with ``override=False``, so a store that answers with a
    non-2xx means the clear actually failed and this run would proceed on dirty state."""
    with port_forward(
        cfg.store_target, namespace=cfg.store_namespace,
        local_port=cfg.store_local_port, remote_port=cfg.store_remote_port,
        ready_url=f"http://127.0.0.1:{cfg.store_local_port}/health",
    ) as base_url:
        resp = requests.delete(f"{base_url}/policy/services", timeout=30)
    if not (200 <= resp.status_code < 300):
        abort(f"clear_policy_store: DELETE /policy/services returned HTTP {resp.status_code}: {resp.text[:500]}")


def ensure_agent_policy(cfg: Config) -> None:
    """Ensure the PRB's ``policy.md`` (``scenario.POLICY_ABSTRACT``) is mounted on the Controller
    Deployment, rolling out only when the ConfigMap content or the mount actually changed."""
    cm = {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": cfg.policy_configmap, "namespace": cfg.controller_namespace},
        "data": {"policy.md": scn.POLICY_ABSTRACT},
    }
    apply_out = kubectl("apply", "-f", "-", input_text=json.dumps(cm))
    cm_changed = "unchanged" not in apply_out

    mounted = kubectl(
        "get", "deployment", cfg.controller_deployment, "-n", cfg.controller_namespace,
        "-o", "jsonpath={.spec.template.spec.volumes[*].configMap.name}",
    )
    if cfg.policy_configmap in mounted.split():
        if cm_changed:
            kubectl("rollout", "restart", f"deployment/{cfg.controller_deployment}", "-n", cfg.controller_namespace)
            kubectl_rollout_status(f"deployment/{cfg.controller_deployment}", namespace=cfg.controller_namespace)
        return

    patch = {
        "spec": {"template": {"spec": {
            "volumes": [{"name": "aiac-policy", "configMap": {"name": cfg.policy_configmap}}],
            "containers": [{
                "name": cfg.controller_deployment,
                "volumeMounts": [{"name": "aiac-policy", "mountPath": cfg.policy_mount_path, "readOnly": True}],
            }],
        }}}
    }
    kubectl("patch", "deployment", cfg.controller_deployment, "-n", cfg.controller_namespace, "--type", "strategic", "-p", json.dumps(patch))
    kubectl_rollout_status(f"deployment/{cfg.controller_deployment}", namespace=cfg.controller_namespace)


def onboard(cfg: Config, base_url: str, service_id: str) -> None:
    resp = requests.post(f"{base_url}/apply/service/{service_id}", timeout=cfg.onboard_timeout)
    if resp.status_code != 200:
        abort(f"onboard {service_id!r} at {base_url}: HTTP {resp.status_code} — {resp.text[:500]}")


def clear_writer_rego(cfg: Config) -> None:
    """Reset the writer's state for the demo agent by deleting its AuthorizationPolicy CR.

    The reworked writer is CR-backed (no ``/rego`` file dump in production), so "clear the
    writer's rego" means "delete the CR" — the next onboard server-side-applies a fresh one.
    Idempotent: ``--ignore-not-found`` treats an already-absent CR as success."""
    kubectl(
        "delete", AUTHZ_POLICY_RESOURCE, cfg.cr_name,
        "-n", cfg.namespace, "--ignore-not-found",
    )


def capture_rego(cfg: Config, rego_dir: Path) -> None:
    """Fetch the agent's generated rego straight from its AuthorizationPolicy CR and write each
    ``spec.policies[].content`` into ``rego_dir`` under the writer's nested ``<ns>/<name>/<path>``
    layout, so ``opa eval`` / ``show-state.py`` read the very bytes a live enforcement point would
    (the CR is production's source of truth — there is no ``/rego`` file to ``kubectl cp``)."""
    cr = kubectl_get_json(f"{AUTHZ_POLICY_RESOURCE}/{cfg.cr_name}", namespace=cfg.namespace)
    policies = cr.get("spec", {}).get("policies", [])
    if not policies:
        abort(
            f"AuthorizationPolicy {cfg.cr_name!r} in namespace {cfg.namespace!r} has no "
            "spec.policies — did the onboard call actually write the CR?"
        )
    for policy in policies:
        dest = rego_dir / cfg.namespace / cfg.cr_name / policy["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(policy["content"])


# ======================================================================================
# ROPC login + RFC 8693 token exchange
# ======================================================================================


def decode_jwt_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def ropc_login(cfg: Config, client_id: str, username: str, password: str) -> dict:
    """``grant_type=password`` against ``client_id`` (a public client with direct-access-grants
    enabled). Aborts on any non-token response — this demo does a real login, not a stub."""
    resp = requests.post(
        f"{cfg.keycloak_url}/realms/{cfg.realm}/protocol/openid-connect/token",
        data={"grant_type": "password", "client_id": client_id, "username": username, "password": password, "scope": "openid"},
        timeout=15,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "access_token" not in body:
        abort(f"ROPC login for {username!r} via {client_id!r} failed: HTTP {resp.status_code} — {body or resp.text[:300]}")
    return body


def token_exchange(cfg: Config, *, client_id: str, client_secret_value: str, subject_token: str, audience: str) -> dict:
    """RFC 8693 token exchange. Client auth MUST be form-encoded, not HTTP Basic — ``client_id`` is
    a SPIFFE URI containing ``://``, which breaks Basic-auth credential parsing."""
    resp = requests.post(
        f"{cfg.keycloak_url}/realms/{cfg.realm}/protocol/openid-connect/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "client_id": client_id,
            "client_secret": client_secret_value,
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience,
        },
        timeout=15,
    )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "access_token" not in body:
        abort(f"token exchange (audience={audience!r}) failed: HTTP {resp.status_code} — {body or resp.text[:300]}")
    return body


# ======================================================================================
# drive() — the shared engine behind run-developer.py / run-tester.py / run-devops.py
# ======================================================================================


def drive(username: str) -> None:
    """Run one user's intents end to end against ``generated/02-after-tool/``: ROPC login, the
    inbound gate (stopping — as a feature, not an error — on denial), an RFC 8693 exchange proving
    the live flow, then the per-intent outbound gate. Every verdict is checked against
    ``scenario.expected_inbound``/``expected_outbound``; any mismatch aborts naming the offending
    ``(subject, function_name)`` rather than printing a quietly-wrong table."""
    cfg = load_config()
    role = scn.USERS[username]
    rego_dir = GENERATED / "02-after-tool"
    inbound_rego = rego_dir / cfg.inbound_rego
    outbound_rego = rego_dir / cfg.outbound_rego
    if not (inbound_rego.is_file() and outbound_rego.is_file()):
        abort(
            f"no policy found at {rego_dir} — run `make onboard-agent` and `make onboard-tool` first "
            "(run-*.py always drives against the after-tool snapshot)"
        )

    admin = connect_admin(cfg)

    say("1", "3", f"{username} ({role}): ROPC login")
    login = ropc_login(cfg, scn.ROPC_CLIENT_ID, username, scn.USER_PASSWORD)
    subject_token = login["access_token"]
    ok("logged in")

    say("2", "3", "Inbound gate: may this user call the agent?")
    # Fixed package + live-plugin input shape. This demo path is an end-user ROPC login with no
    # platform source client, so we send NO input.identity.client_id — source_ok is satisfied by
    # the writer's `source_ok if { not input.identity.client_id }` rule.
    inbound_allowed = bool(opa_eval(
        [inbound_rego], "data.authbridge.client.inbound.request.allow",
        {"identity": {"subject": username}},
    ))
    expected_in = scn.expected_inbound(username)
    if inbound_allowed != expected_in:
        abort(f"inbound mismatch for subject={username!r}: opa said {inbound_allowed}, expected {expected_in}")

    if not inbound_allowed:
        intent = scn.INTENTS[username][0]
        blocked(f"{intent.label!r} -> blocked at inbound (the intended {username} story, not an error)")
        table([(username, role, intent.label, "blocked at inbound")], headers=("user", "role", "intent", "result"))
        return
    ok("inbound allowed")

    say("3", "3", "Per-intent outbound gate (via a real RFC 8693 exchange)")
    agent_uuid = resolve_service_id(admin, cfg, f"{cfg.namespace}/{scn.AGENT_WORKLOAD}")
    agent_client_id = admin.get_client(agent_uuid)["clientId"]
    secret = client_secret(admin, cfg, agent_uuid)

    # target_allow_scopes is keyed by the FULL target service id (a SPIFFE id), with bare
    # de-prefixed scope values. next(iter(...)) still yields the id to exchange for.
    target_scopes = opa_eval([outbound_rego], "data.authbridge.client.outbound.request.target_allow_scopes", {}) or {}
    if not target_scopes:
        abort(f"outbound rego at {outbound_rego} has no target_allow_scopes — is the tool onboarded?")
    target_uri = next(iter(target_scopes))

    token_exchange(cfg, client_id=agent_client_id, client_secret_value=secret, subject_token=subject_token, audience=target_uri)
    note(f"exchanged token; aud includes {target_uri}")

    rows: list[tuple[str, ...]] = []
    for intent in scn.INTENTS[username]:
        allowed = bool(opa_eval(
            [outbound_rego], "data.authbridge.client.outbound.request.allow",
            {"identity": {"subject": username, "service_id": target_uri},
             "mcp": {"params": {"name": intent.function_name}}},
        ))
        expected_out = scn.expected_outbound(username, intent.function_name)
        if allowed != expected_out:
            abort(
                f"outbound mismatch for (subject={username!r}, function_name={intent.function_name!r}): "
                f"opa said {allowed}, expected {expected_out}"
            )
        (ok if allowed else fail)(f"{intent.label} -> {intent.function_name}: {'allowed' if allowed else 'denied'}")
        rows.append((username, intent.label, intent.function_name, "allowed" if allowed else "denied"))

    print()
    table(rows, headers=("user", "intent", "tool scope", "result"))
