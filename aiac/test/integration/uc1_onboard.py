"""Shared harness for the UC-1 onboarding integration-test ladder (rungs 1–3).

Spec: ``docs/specs/integration-test/uc1-onboarding-pipeline.md``; live loop shape (handoff 08):
``docs/opa-kind-runbook.md``. The evaluator is now the **deployed AuthBridge OPA plugin**, not a
standalone OPA-CLI run over dumped ``.rego`` — there is no ``.rego`` dump and no ``opa`` binary here.

Every rung follows the same shape against **one** live rossoctl/Kind cluster with the AuthBridge OPA
pipeline wired into both legs:

    Keycloak cleanup + policy-store clear
      → onboard the rung's workloads in order (POST /apply/service/{id} on the in-cluster Controller,
        which upserts the AuthorizationPolicy CR on the live API)
      → enable the outbound token-exchange leg (Part B: route + optional client scope + agent restart)
      → poll bundle-service + OPA until this run's CR is reflected in real decisions
      → drive REAL HTTP requests through AuthBridge and assert the real plugin's allow/deny
      → Keycloak cleanup + CR delete

The only thing that differs between rungs is *which workloads are onboarded and in what order*, so
all the machinery lives here and each ``test_uc1_onboard_*.py`` supplies just its own oracle
(verdicts computed from ``scenario_uc1.py``) and live assertions.

This module owns:

* **Config** (env, spec § Configuration) — single stack, no variants.
* **Keycloak** — ``connect_admin`` / ``provision_realm_and_users`` (the fixture UC-1 does *not* do) /
  ``resolve_service_id`` (route-safe trigger id = internal client UUID) / ``cleanup_provisioned``.
* **Onboarding** — ``ensure_agent_policy`` (mount the PRB's ``policy.md``) + ``onboard``.
* **Outbound-leg prep (Part B)** — ``ensure_github_tool_route`` / ``grant_exchange_scope`` /
  ``restart_agent`` so ``token-exchange`` runs and OPA is actually consulted on the outbound leg.
* **Live decision oracle + probes** — ``expected_inbound`` / ``expected_outbound_bare`` (verdicts from
  ``scenario_uc1``, keyed on the **bare** runtime tool names AuthBridge sends) and ``inbound_decision``
  / ``outbound_decision`` (mint a user token, send a real request through AuthBridge, classify the
  real plugin's response).
* **``onboarded_stack``** — the whole per-rung fixture flow, parameterised by the ordered workload
  list; each rung wraps it in a one-line session fixture and yields a probe context.

It imports only stdlib + ``requests`` + ``launcher`` + the pure-data ``scenario_uc1`` (never
``aiac``), so it is importable before the env-before-import dance, exactly like ``scenario_uc1`` and
``launcher``. It defines **no** ``test_*`` functions, so pytest does not collect it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import requests

HERE = Path(__file__).resolve().parent  # test/integration/
REPO_ROOT = HERE.parents[1]  # -> aiac/
if str(REPO_ROOT) not in sys.path:  # so ``import test.integration.*`` resolves
    sys.path.insert(0, str(REPO_ROOT))

from test.integration import scenario_uc1 as scn  # noqa: E402
from test.integration.launcher import (  # noqa: E402
    inbound_probe,
    inbound_outcome,
    kubectl,
    kubectl_rollout_status,
    mint_token,
    outbound_probe,
    outbound_outcome,
    poll_until,
    port_forward,
    require_env,
    require_env_or_skip,
    require_pipeline,
    resolve_pod,
    verify_subject_mapper,
)

log = logging.getLogger(__name__)

# --- Config (env) — spec § Configuration; single stack (no variants) ------------------------
TEST_REALM = os.environ.get("AIAC_TEST_REALM", scn.REALM_DEFAULT)
NAMESPACE = os.environ.get("AIAC_DEMO_NAMESPACE", scn.DEMO_NAMESPACE_DEFAULT)
ADMIN_REALM = os.environ.get("KEYCLOAK_ADMIN_REALM", "master")

# Controller (in-cluster) reached via ``kubectl port-forward``. Target/namespace/ports are
# overridable; defaults match the deployed AIAC stack (svc/aiac-agent-service:7070 in aiac-system).
CONTROLLER_NAMESPACE = os.environ.get("AIAC_CONTROLLER_NAMESPACE", "aiac-system")
CONTROLLER_TARGET = os.environ.get("AIAC_CONTROLLER_TARGET", "svc/aiac-agent-service")
CONTROLLER_LOCAL_PORT = int(os.environ.get("AIAC_CONTROLLER_LOCAL_PORT", "7070"))
CONTROLLER_REMOTE_PORT = int(os.environ.get("AIAC_CONTROLLER_REMOTE_PORT", "7070"))

# Policy Store (in-cluster) reached via ``kubectl port-forward`` to clear stale SPMs before a run.
# The store's SQLite lives on a StatefulSet PV that survives image redeploys, so pre-fix cruft
# would otherwise accumulate across runs (onboarding appends with override=False). Defaults match
# the deployed stack (svc/aiac-policy-model-store-service:7074 in aiac-system).
STORE_NAMESPACE = os.environ.get("AIAC_STORE_NAMESPACE", "aiac-system")
STORE_TARGET = os.environ.get("AIAC_STORE_TARGET", "svc/aiac-policy-model-store-service")
STORE_LOCAL_PORT = int(os.environ.get("AIAC_STORE_LOCAL_PORT", "7074"))
STORE_REMOTE_PORT = int(os.environ.get("AIAC_STORE_REMOTE_PORT", "7074"))

# The Controller Deployment + the abstract policy.md the PRB reads. The test mounts this policy on
# the Controller pod as a precondition-fixup (see ``ensure_agent_policy``); it is NOT written into
# any committed deployment manifest — the deployment stays free of test-specific config.
CONTROLLER_DEPLOYMENT = os.environ.get("AIAC_CONTROLLER_DEPLOYMENT", "aiac-agent")
POLICY_CONFIGMAP = os.environ.get("AIAC_POLICY_CONFIGMAP", "aiac-policy")
POLICY_MOUNT_PATH = os.environ.get("AIAC_POLICY_MOUNT_PATH", "/etc/aiac")

# Onboarding drives the real PRB, which makes several LLM calls over the whole role/scope universe;
# against a slow reasoning model that can take minutes. Configurable so slow endpoints don't spuriously
# fail the run (``AIAC_ONBOARD_TIMEOUT``, seconds).
ONBOARD_TIMEOUT = float(os.environ.get("AIAC_ONBOARD_TIMEOUT", "900"))

# --- Live-cluster loop knobs (handoff 08) ---------------------------------------------------

# The workload Deployment to restart after Part B so it reloads the new outbound route (and, on its
# OPA sidecar's next poll, the recomposed bundle). Defaults to the agent workload name.
AGENT_DEPLOYMENT = os.environ.get("AIAC_AGENT_DEPLOYMENT", scn.AGENT_WORKLOAD)

# SPIFFE trust domain the operator registers the demo workloads under (the ``spiffe://<td>/ns/...``
# authority). Matches the rossoctl Kind cluster's default; override for a differently-named cluster.
TRUST_DOMAIN = os.environ.get("AIAC_TRUST_DOMAIN", "localtest.me")

# After a CR is upserted, ``bundle-service`` recomposes the namespace bundle and each pod's OPA polls
# it on its own (~20–30 s) interval, and ``token-exchange`` needs a moment to settle after the agent
# restart. ``onboarded_stack`` polls real decisions until they converge, up to this budget (seconds).
BUNDLE_TIMEOUT = float(os.environ.get("AIAC_BUNDLE_TIMEOUT", "300"))
BUNDLE_POLL_INTERVAL = float(os.environ.get("AIAC_BUNDLE_POLL_INTERVAL", "10"))


# ======================================================================================
# Expected-verdict oracle (pure functions over the scenario_uc1 truth table)
# ======================================================================================
#
# Two naming registers meet here (see ``scenario_uc1`` docstring): the *provisioned* grant sets stay
# PREFIXED (``github-tool.source-read`` — what UC-1 writes into Keycloak + the CR data maps), while
# the *request the test sends and the outcome it expects* are keyed on the BARE runtime names
# AuthBridge's mcp-parser puts in ``input.mcp.params.name`` (``source-read``). The grant-set constants
# below are the prefixed provisioned truth (for the fixture-independent oracle-contract tests); the
# ``expected_*`` helpers decide live outcomes over the bare names.

# Prefixed provisioned truth — the exact strings UC-1 provisions and the PCE writes into the CR maps.
INBOUND_GRANT_SET: set[tuple[str, str]] = set(scn.INBOUND_PAIRS)
OUTBOUND_SUBJECT_GRANT_SET: set[tuple[str, str]] = set(scn.OUTBOUND_SUBJECT_PAIRS)
OUTBOUND_TARGET_GRANT_SET: set[tuple[str, str]] = set(scn.OUTBOUND_TARGET_PAIRS)

_INBOUND_SOURCES = {role for role, _ in scn.INBOUND_PAIRS}  # user-roles reaching some agent scope


def expected_inbound(subject: str) -> bool:
    """A user may call the agent iff their realm role sources some agent scope (``INBOUND_PAIRS``).
    Unaffected by tool onboarding — the same for every rung."""
    return scn.USERS[subject] in _INBOUND_SOURCES


def expected_outbound_bare(subject: str, tool_bare: str) -> bool:
    """A user's outbound call to a **bare** tool name (``source-read``) is allowed iff **both** gates
    pass (per-scope AND): their realm role reaches it in the user→tool subject gate
    (``OUTBOUND_SUBJECT_BARE``) **and** the agent's own operator roles reach it in the capability gate
    (``OUTBOUND_TARGET_BARE``). This is the tool-onboarded oracle (rungs 2 & 3); rung 1's gate is
    empty (no tool onboarded), so rung 1 supplies its own all-deny oracle."""
    user_ok = (scn.USERS[subject], tool_bare) in scn.OUTBOUND_SUBJECT_BARE
    agent_ok = tool_bare in scn.OUTBOUND_TARGET_BARE
    return user_ok and agent_ok


def expected_inbound_decision(subject: str) -> str:
    """The inbound oracle as a decision string (``"allow"`` / ``"deny"``) — comparable to the live
    ``inbound_decision`` outcome."""
    return "allow" if expected_inbound(subject) else "deny"


def expected_outbound_decision(subject: str, tool_bare: str) -> str:
    """The tool-onboarded outbound oracle as a decision string — comparable to the live
    ``outbound_decision`` outcome (rungs 2 & 3)."""
    return "allow" if expected_outbound_bare(subject, tool_bare) else "deny"


# ======================================================================================
# Keycloak provisioning + cleanup (the fixture UC-1 does NOT do)
# ======================================================================================


def connect_admin():
    """Connect to the admin realm so the fixture can provision users + clean up provisioned entities."""
    from keycloak import KeycloakAdmin

    creds = require_env("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")
    return KeycloakAdmin(
        server_url=creds["KEYCLOAK_URL"],
        realm_name=ADMIN_REALM,
        user_realm_name=ADMIN_REALM,
        username=creds["KEYCLOAK_ADMIN_USERNAME"],
        password=creds["KEYCLOAK_ADMIN_PASSWORD"],
    )


def provision_realm_and_users(admin, realm: str) -> None:
    """Idempotently ensure ``realm`` holds ``scenario_uc1``'s users + realm roles with the
    descriptions the PRB reads. Realm roles carry the ``aiac.managed`` marker so the IdP populates
    each role's ``actorIds`` (member usernames) — the PCE needs them to build the ``subject_roles``
    map the inbound/outbound gates key on. Never deletes/recreates — reruns converge (spec: shared
    realm, leave-in-place)."""
    from keycloak.exceptions import KeycloakError

    try:
        admin.create_realm({"realm": realm, "enabled": True})
    except KeycloakError:
        pass  # already exists — leave in place
    admin.change_current_realm(realm)

    for name, description in scn.USER_ROLES.items():
        payload = {"name": name, "description": description, "attributes": {"aiac.managed": ["true"]}}
        admin.create_realm_role(payload, skip_exists=True)
        admin.update_realm_role(name, payload)  # ensure the marker on a pre-existing role too

    for username, role_name in scn.USERS.items():
        user_id = admin.create_user({"username": username, "enabled": True}, exist_ok=True)
        admin.set_user_password(user_id, scn.USER_PASSWORD, temporary=False)
        admin.assign_realm_roles(user_id, [admin.get_realm_role(role_name)])


def resolve_service_id(admin, realm: str, client_name: str) -> str:
    """Return the **route-safe trigger id** — the Keycloak *internal client UUID* (``client['id']``)
    of the client whose *name* is ``client_name``.

    Not the ``clientId``: under SPIRE that is a SPIFFE URI (``spiffe://.../github-agent``) whose
    slashes the single-segment ``/apply/service/{id}`` route cannot carry; the Controller resolves
    the trigger via ``admin.get_client(id)``, which keys on the UUID."""
    admin.change_current_realm(realm)
    for client in admin.get_clients():
        if client.get("name") == client_name:
            return client["id"]
    raise AssertionError(f"no Keycloak client with name {client_name!r} in realm {realm!r}")


def cleanup_provisioned(admin, realm: str) -> None:
    """Delete the entities UC-1 onboarding provisions — the realm role(s) and client scopes prefixed
    ``github-agent.`` / ``github-tool.`` — so each run starts from a clean slate and reruns converge.

    Leaves the fixture's own ``developer`` / ``tester`` / ``devops`` roles, the operator's audience
    client scopes (``*-aud``, no ``.`` after the workload), and everything else in place. Best-effort:
    a delete of an already-absent entity is ignored."""
    from keycloak.exceptions import KeycloakError

    admin.change_current_realm(realm)
    prefixes = (f"{scn.AGENT_WORKLOAD}.", f"{scn.TOOL_WORKLOAD}.")

    for role in admin.get_realm_roles():
        name = role.get("name", "")
        if name.startswith(prefixes):
            try:
                admin.delete_realm_role(name)
            except KeycloakError as exc:
                log.warning("cleanup: delete realm role %r failed: %s", name, exc)

    for scope in admin.get_client_scopes():
        name = scope.get("name", "")
        if name.startswith(prefixes):
            try:
                admin.delete_client_scope(scope["id"])
            except KeycloakError as exc:
                log.warning("cleanup: delete client scope %r failed: %s", name, exc)


def clear_policy_store() -> None:
    """Drop every persisted SPM from the in-cluster Policy Store before a run — the store-side twin
    of ``cleanup_provisioned``'s Keycloak reset.

    The store's SQLite lives on a StatefulSet PV that outlives image redeploys, and onboarding
    appends to each SPM with ``override=False``; without this the store accumulates pre-fix cruft
    (stale role-id generations, retired ``*-aud`` edges, cross-run pollution) that the PCE replays
    into every regenerated policy — so a fixed pipeline still emits defective policy. Clearing here
    guarantees each run derives its policy from only the edges this run onboarded.

    Hits ``DELETE /policy/services`` directly through a port-forward (the harness never imports
    ``aiac``). Best-effort about *reachability* — a store that is unreachable (or a port-forward that
    won't come up) is tolerated and only warned about. But a store that answers with a **non-2xx**
    means the clear actually failed: proceeding would run the rung on dirty state (stale SPMs
    replayed into every regenerated policy), so that case fails loudly rather than silently."""
    try:
        with port_forward(
            STORE_TARGET,
            namespace=STORE_NAMESPACE,
            local_port=STORE_LOCAL_PORT,
            remote_port=STORE_REMOTE_PORT,
            ready_url=f"http://127.0.0.1:{STORE_LOCAL_PORT}/health",
        ) as base_url:
            resp = requests.delete(f"{base_url}/policy/services", timeout=30)
    except (requests.ConnectionError, requests.Timeout, RuntimeError) as exc:
        # Store unreachable / port-forward failed — best-effort, must not fail the run.
        log.warning("clear_policy_store: store unreachable, skipping clear (%s)", exc)
        return
    if not (200 <= resp.status_code < 300):
        raise AssertionError(
            f"clear_policy_store: DELETE /policy/services returned HTTP {resp.status_code} — the "
            f"store was reached but the clear failed, so the run would proceed on dirty state "
            f"(stale SPMs): {resp.text[:500]}"
        )
    log.info("cleared Policy Store SPMs (HTTP %s)", resp.status_code)


# ======================================================================================
# Onboarding trigger + policy (the one mutable stack precondition the ladder owns)
# ======================================================================================


def ensure_agent_policy(namespace: str) -> None:
    """Ensure the PRB's ``policy.md`` is mounted in the Controller pod — the one mutable stack
    precondition the ladder owns, so a fresh AIAC stack needs no manual patching.

    Phase-1's PRB reads the single abstract policy from ``AIAC_POLICY_FILE`` (default
    ``/etc/aiac/policy.md``). This idempotently provisions that file as a ConfigMap and mounts it on
    the Controller Deployment, rolling out **only** when the mount is absent. The policy text is
    ``scenario_uc1.POLICY_ABSTRACT`` — the same abstract policy the scenario's verdicts assume. It is
    never written into a committed deployment manifest (that stays free of test config) and is left
    in place on teardown (benign, and keeps reruns fast)."""
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": POLICY_CONFIGMAP, "namespace": namespace},
        "data": {"policy.md": scn.POLICY_ABSTRACT},
    }
    apply_out = kubectl("apply", "-f", "-", input_text=json.dumps(cm))
    # ``kubectl apply`` reports "created"/"configured" when the ConfigMap's content differs from the
    # cluster and "unchanged" when it matches; use that to decide whether a rollout is needed.
    cm_changed = "unchanged" not in apply_out

    mounted = kubectl(
        "get", "deployment", CONTROLLER_DEPLOYMENT, "-n", namespace,
        "-o", "jsonpath={.spec.template.spec.volumes[*].configMap.name}",
    )
    if POLICY_CONFIGMAP in mounted.split():
        # Already mounted, so no deployment patch (and thus no rollout) is triggered. But a projected
        # ConfigMap volume only re-syncs on the kubelet's own (~minute) cadence, and the PRB reads
        # policy.md once at startup — so if we just changed the ConfigMap's content the running pod
        # would keep serving the stale policy. Force a rollout + wait so the new policy.md is in
        # place before onboarding; skip it when apply reported the ConfigMap unchanged (fast reruns).
        if cm_changed:
            kubectl("rollout", "restart", f"deployment/{CONTROLLER_DEPLOYMENT}", "-n", namespace)
            kubectl_rollout_status(f"deployment/{CONTROLLER_DEPLOYMENT}", namespace=namespace)
        return  # already mounted — content is now current

    patch = {
        "spec": {"template": {"spec": {
            "volumes": [{"name": "aiac-policy", "configMap": {"name": POLICY_CONFIGMAP}}],
            "containers": [{
                "name": CONTROLLER_DEPLOYMENT,
                "volumeMounts": [
                    {"name": "aiac-policy", "mountPath": POLICY_MOUNT_PATH, "readOnly": True}
                ],
            }],
        }}}
    }
    kubectl(
        "patch", "deployment", CONTROLLER_DEPLOYMENT, "-n", namespace,
        "--type", "strategic", "-p", json.dumps(patch),
    )
    kubectl_rollout_status(f"deployment/{CONTROLLER_DEPLOYMENT}", namespace=namespace)


def onboard(base_url: str, service_id: str) -> None:
    """``POST /apply/service/{service_id}`` against the Controller; assert 200. This upserts the
    ``AuthorizationPolicy`` CR on the live Kubernetes API (bundle-service picks it up)."""
    resp = requests.post(f"{base_url}/apply/service/{service_id}", timeout=ONBOARD_TIMEOUT)
    assert resp.status_code == 200, (
        f"onboard {service_id!r} at {base_url}: HTTP {resp.status_code} — {resp.text[:500]}"
    )


def delete_agent_cr() -> None:
    """Best-effort delete of the agent's ``AuthorizationPolicy`` CR so each run starts and ends from a
    clean policy slate (the CR is named for the agent workload, matched by bundle-service against the
    SPIFFE SA segment). Ignored if absent; a delete failure is logged, not raised."""
    try:
        kubectl(
            "delete", "authorizationpolicy", scn.AGENT_WORKLOAD, "-n", NAMESPACE,
            "--ignore-not-found", timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        log.warning("delete_agent_cr: %s", (exc.stderr or exc.output or exc))


# ======================================================================================
# Outbound token-exchange leg prep (runbook Part B) — so OPA is actually consulted outbound
# ======================================================================================
#
# The outbound OPA gate is only reached if ``token-exchange`` first intercepts + exchanges the agent's
# call to github-tool. That needs: (B.1) an outbound route for the github-tool host, (B.2) the agent's
# Keycloak client granted the github-tool audience scope as optional, and (B.3) the agent restarted so
# it reloads the route. Without this the call would pass through unexchanged and never reach OPA.


def _tool_audience() -> str:
    """The RFC 8693 ``audience`` for the github-tool exchange — its SPIFFE ID."""
    return f"spiffe://{TRUST_DOMAIN}/ns/{NAMESPACE}/sa/{scn.TOOL_WORKLOAD}"


def _tool_aud_scope() -> str:
    """The realm client-scope whose audience mapper stamps the github-tool audience (runbook B.2)."""
    return f"agent-{NAMESPACE}-{scn.TOOL_WORKLOAD}-aud"


def ensure_github_tool_route(namespace: str) -> None:
    """Ensure ``authproxy-routes`` carries an outbound route for the github-tool host (runbook B.1).

    Reads the current ``routes.yaml``, appends the github-tool route if it is not already present
    (preserving any existing routes, e.g. the weather route), and patches it back. Creates the
    ConfigMap if it does not exist. Idempotent: a second call is a no-op when the route is present."""
    tool = scn.TOOL_WORKLOAD
    route_block = (
        f'- host: "{tool}"\n'
        f'  target_audience: "{_tool_audience()}"\n'
        f'  token_scopes: "openid {_tool_aud_scope()}"\n'
    )
    try:
        current = kubectl(
            "get", "configmap", "authproxy-routes", "-n", namespace,
            "-o", r"jsonpath={.data.routes\.yaml}", timeout=30,
        )
    except subprocess.CalledProcessError:
        current = ""  # ConfigMap (or key) absent — treat as empty, create below

    if f'host: "{tool}"' in current or f"host: {tool}" in current:
        return  # already routed
    new_routes = (current + ("\n" if current.strip() else "") + route_block) if current.strip() else route_block

    patch = {"data": {"routes.yaml": new_routes}}
    try:
        kubectl(
            "patch", "configmap", "authproxy-routes", "-n", namespace,
            "--type", "merge", "-p", json.dumps(patch), timeout=30,
        )
    except subprocess.CalledProcessError:
        # ConfigMap does not exist yet — create it with just the github-tool route.
        cm = {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "authproxy-routes", "namespace": namespace},
            "data": {"routes.yaml": new_routes},
        }
        kubectl("apply", "-f", "-", input_text=json.dumps(cm), timeout=30)


def grant_exchange_scope(admin) -> None:
    """Grant the agent's Keycloak client the github-tool audience scope as **optional** (runbook B.2),
    so the ``client_credentials`` token exchange to the github-tool audience succeeds.

    Resolves the agent client by its SPIFFE ``clientId`` (``.../sa/github-agent``) or its
    ``name`` (``{ns}/github-agent``), and the client scope by name (``agent-{ns}-github-tool-aud``).
    Idempotent — Keycloak's assign-optional-scope is a PUT."""
    from keycloak.exceptions import KeycloakError

    admin.change_current_realm(TEST_REALM)
    aud_scope = _tool_aud_scope()

    client_uuid = None
    for client in admin.get_clients():
        cid = client.get("clientId", "")
        if cid.endswith(f"/sa/{scn.AGENT_WORKLOAD}") or client.get("name") == f"{NAMESPACE}/{scn.AGENT_WORKLOAD}":
            client_uuid = client["id"]
            break
    if client_uuid is None:
        raise AssertionError(
            f"no Keycloak client for {NAMESPACE}/{scn.AGENT_WORKLOAD} in realm {TEST_REALM!r} — is "
            "the agent registered by the operator?"
        )

    scope = next((s for s in admin.get_client_scopes() if s.get("name") == aud_scope), None)
    if scope is None:
        raise AssertionError(
            f"client scope {aud_scope!r} not found in realm {TEST_REALM!r} — is {scn.TOOL_WORKLOAD} "
            "deployed + registered by the operator?"
        )
    try:
        admin.add_client_optional_client_scope(client_uuid, scope["id"], {})
    except KeycloakError as exc:
        log.info("grant_exchange_scope: assign %r returned (benign if already assigned): %s", aud_scope, exc)


def restart_agent(namespace: str) -> None:
    """Restart the agent Deployment so it reloads the outbound route (routes are read once at
    startup — runbook B.3) and its OPA sidecar re-fetches the recomposed bundle on its next poll."""
    kubectl("rollout", "restart", f"deployment/{AGENT_DEPLOYMENT}", "-n", namespace, timeout=60)
    kubectl_rollout_status(f"deployment/{AGENT_DEPLOYMENT}", namespace=namespace, timeout=180)


# ======================================================================================
# Live decisions — mint a user token, send a real request through AuthBridge, classify the plugin
# ======================================================================================


def inbound_decision(ctx: dict, user: str) -> str:
    """Mint a fresh ``user`` token and send a real inbound request through AuthBridge; return the real
    OPA plugin's classified decision (``"allow"`` 200 / ``"deny"`` 403 / ``"error"`` otherwise)."""
    token = mint_token(user, scn.USER_PASSWORD, keycloak_url=ctx["keycloak_url"], realm=ctx["realm"])
    code, _ = inbound_probe(token, namespace=ctx["namespace"], agent_service=scn.AGENT_WORKLOAD)
    return inbound_outcome(code)


def resolve_agent_pod() -> str:
    """Resolve the **current** live agent pod (newest Running+Ready, non-terminating — see
    ``resolve_pod``). Re-resolved per outbound probe rather than pinned once at fixture setup: the
    outbound leg ``kubectl exec``s into a specific pod (unlike inbound, which reaches the agent through
    its pod-agnostic Service), so a pod name captured before/during ``restart_agent``'s rolling
    replacement can go stale and every later exec then fails ``NotFound`` -> ``"error"`` forever (issue
    #139). Resolving fresh self-heals across any pod churn."""
    return resolve_pod(f"app.kubernetes.io/name={scn.AGENT_WORKLOAD}", namespace=NAMESPACE)


def outbound_decision(ctx: dict, user: str, tool_bare: str) -> str:
    """Mint a fresh ``user`` token, drive an outbound MCP ``tools/call`` for the **bare** ``tool_bare``
    through AuthBridge's forward proxy (token-exchange → OPA), and return the real plugin's classified
    decision (``"deny"`` for an OPA error frame or 403; ``"allow"`` for a non-OPA 200; ``"error"`` for
    a 503/transport failure)."""
    token = mint_token(user, scn.USER_PASSWORD, keycloak_url=ctx["keycloak_url"], realm=ctx["realm"])
    code, body = outbound_probe(token, tool_bare, namespace=ctx["namespace"], agent_pod=resolve_agent_pod())
    return outbound_outcome(code, body)


# ======================================================================================
# Per-rung fixture flow — cleanup → onboard (in order) → Part B → poll bundle → yield → cleanup
# ======================================================================================


@contextmanager
def onboarded_stack(workloads: list[str]) -> Iterator[dict]:
    """Run one rung's whole live flow and yield a probe ``ctx`` for its assertions.

    ``ctx`` = ``{"admin", "namespace", "agent_pod", "keycloak_url", "realm", "tool_onboarded"}``.

    Flow: skip cleanly (never false-pass) if the pipeline is not wired or the integration env is
    unset; provision users/roles + clear the store; onboard the given ``workloads`` **in order**
    through the real in-cluster UC-1 Controller (``POST /apply/service/{id}``, upserting the CR);
    enable the outbound leg (Part B: route + optional client scope + agent restart); then **poll real
    decisions** until ``bundle-service`` + OPA reflect this run's CR (and token-exchange has settled)
    before yielding. Keycloak cleanup + CR delete run before and after; the clients are left
    registered as before (spec § Per-rung flow). The workload order is the rung's identity — e.g.
    rung 2 passes ``[agent, tool]`` so tool onboarding retroactively completes the agent's outbound
    gate; rung 3 passes ``[tool, agent]`` and must converge to the same live decisions."""
    # Skip gates first — before any cluster mutation (acceptance #4: skip, never false-pass).
    require_pipeline(namespace=NAMESPACE, workloads=[scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD])
    creds = require_env_or_skip("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")
    keycloak_url = creds["KEYCLOAK_URL"]

    admin = connect_admin()
    delete_agent_cr()  # before — clean policy slate (drop any prior run's CR)
    cleanup_provisioned(admin, TEST_REALM)  # before — clean slate (Keycloak)
    clear_policy_store()  # before — clean slate (Policy Store SPMs; PV survives redeploys)
    provision_realm_and_users(admin, TEST_REALM)  # BEFORE onboarding (PRB reads the role universe)
    # username->sub mapper + Direct Access Grants are a one-time realm prereq the fixture does NOT
    # provision; skip (don't fail) if a token can't be minted or its ``sub`` isn't the username.
    verify_subject_mapper(
        keycloak_url=keycloak_url, realm=TEST_REALM, user="dev-user", password=scn.USER_PASSWORD
    )
    ensure_agent_policy(CONTROLLER_NAMESPACE)  # mount the PRB's policy.md if the stack lacks it

    tool_onboarded = scn.TOOL_WORKLOAD in workloads
    try:
        service_ids = [
            resolve_service_id(admin, TEST_REALM, f"{NAMESPACE}/{workload}") for workload in workloads
        ]
        with port_forward(
            CONTROLLER_TARGET,
            namespace=CONTROLLER_NAMESPACE,
            local_port=CONTROLLER_LOCAL_PORT,
            remote_port=CONTROLLER_REMOTE_PORT,
            ready_url=f"http://127.0.0.1:{CONTROLLER_LOCAL_PORT}/health",
        ) as base_url:
            for service_id in service_ids:  # onboard in the rung's order
                onboard(base_url, service_id)

        # Part B — enable the outbound token-exchange leg so OPA is actually consulted outbound. Done
        # after onboarding so the restarted agent (and its OPA sidecar) picks up both the new route
        # and, on its next poll, the recomposed bundle.
        ensure_github_tool_route(NAMESPACE)
        grant_exchange_scope(admin)
        restart_agent(NAMESPACE)
        # Resolved once for the ctx contract, but the live outbound path re-resolves per probe
        # (``resolve_agent_pod``) so a pod replaced after this point can't poison the whole run (#139).
        agent_pod = resolve_agent_pod()

        ctx = {
            "admin": admin,
            "namespace": NAMESPACE,
            "agent_pod": agent_pod,
            "keycloak_url": keycloak_url,
            "realm": TEST_REALM,
            "tool_onboarded": tool_onboarded,
        }

        # Wait for bundle-service + OPA to reflect THIS run's CR (and token-exchange to settle) before
        # any assertion. Readiness signals, all deterministic for this scenario regardless of a stale
        # CR (which the run replaced): dev-user reaches the agent (inbound allow), devops-user is
        # blocked (inbound deny — proves the restrictive client-scoped gate is live, not the allow-all
        # baseline), and dev-user's outbound source-read has reached its terminal verdict —
        # ``allow`` once a tool is onboarded, ``deny`` for the empty-gate agent-only rung. Polling the
        # outbound signal to a *definitive* allow/deny (not ``error``) also waits out the post-restart
        # token-exchange 503 window.
        expected_source_read = "allow" if tool_onboarded else "deny"

        def _ready() -> bool:
            return (
                inbound_decision(ctx, "dev-user") == "allow"
                and inbound_decision(ctx, "devops-user") == "deny"
                and outbound_decision(ctx, "dev-user", "source-read") == expected_source_read
            )

        if not poll_until(_ready, timeout=BUNDLE_TIMEOUT, interval=BUNDLE_POLL_INTERVAL):
            # Surface the RAW outbound (code, body) — not just the classified outcome — so a stalled
            # run is self-diagnosing (issue #139). The classifier collapses three very different
            # failures into ``"error"``; the raw ``(code, body)`` tells them apart in one shot:
            #   * ``code=None`` + body ``"outbound probe exec failed: ..."`` — the *probe's* ``kubectl
            #     exec`` failed (e.g. the agent pod was replaced by a restart and its name went stale,
            #     ``NotFound``). A harness/pod issue, not the pipeline — OPA was never reached.
            #   * ``code=503`` — the ``token-exchange`` leg failed upstream (audience refused, IdP
            #     unreachable), so OPA was never consulted.
            #   * a genuine OPA policy stall can't show as ``"error"`` at all: it surfaces as ``"deny"``
            #     (HTTP 200 + an OPA error frame), because the generated Rego always carries
            #     ``default allow := false``.
            # Re-resolve the pod fresh here (not the possibly-stale ``ctx["agent_pod"]``) so the raw
            # line reflects the *current* live pod.
            ob_token = mint_token(
                "dev-user", scn.USER_PASSWORD, keycloak_url=ctx["keycloak_url"], realm=ctx["realm"]
            )
            ob_code, ob_body = outbound_probe(
                ob_token, "source-read", namespace=ctx["namespace"], agent_pod=resolve_agent_pod()
            )
            raise RuntimeError(
                f"live pipeline did not converge within {BUNDLE_TIMEOUT:.0f}s after onboarding "
                f"{workloads} + Part B: inbound(dev-user)={inbound_decision(ctx, 'dev-user')!r} "
                f"inbound(devops-user)={inbound_decision(ctx, 'devops-user')!r} "
                f"outbound(dev-user,source-read)={outbound_outcome(ob_code, ob_body)!r} "
                f"[raw: HTTP {ob_code}, body={ob_body[:300]!r}] "
                f"(expected allow / deny / {expected_source_read}). code=None + 'exec failed' body = a "
                "stale/gone agent pod (harness); 503 = token-exchange never came up (OPA not reached); "
                "a real policy stall would read 'deny', never 'error' — see docs/opa-kind-runbook.md "
                "and issue #139."
            )
        yield ctx
    finally:
        delete_agent_cr()  # after — drop this run's CR
        cleanup_provisioned(admin, TEST_REALM)  # after — restore the pre-run Keycloak state
