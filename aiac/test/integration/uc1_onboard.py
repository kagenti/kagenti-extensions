"""Shared harness for the UC-1 onboarding integration-test ladder (rungs 1–3).

Spec: ``docs/specs/integration-test/uc1-onboarding-pipeline.md``. Every rung follows the same
**Keycloak cleanup → onboard (in the rung's order) → validate end state → Keycloak cleanup** shape
against **one** in-cluster AIAC stack (Controller + Policy Store + OPA filesystem-stub writer). The
only thing that differs between rungs is *which workloads are onboarded and in what order* — so all
the machinery to do it lives here, and each ``test_uc1_onboard_*.py`` module supplies just its own
oracle (the expected verdicts, computed from ``scenario_uc1.py``) and live assertions.

This module owns:

* **Config** (env, spec § Configuration) — single stack, no variants.
* **Keycloak** — ``connect_admin`` / ``provision_realm_and_users`` (the fixture UC-1 does *not* do) /
  ``resolve_service_id`` (route-safe trigger id = internal client UUID) / ``cleanup_provisioned``.
* **Onboarding + Rego capture** — ``ensure_agent_policy`` (mount the PRB's ``policy.md``), ``onboard``
  (``POST /apply/service/{id}``), and the writer-pod ``/rego`` capture helpers.
* **Grant-set extraction** — re-derive the inbound / outbound-subject grant sets from the generated
  Rego via ``opa eval`` (the semantic-equivalence oracle).
* **``onboarded_stack``** — the whole per-rung fixture flow, parameterised by the ordered list of
  workloads to onboard; each rung wraps it in a one-line session fixture.

It imports only stdlib + ``requests`` + ``launcher`` + the pure-data ``scenario_uc1`` (never
``aiac``), so it is importable before the env-before-import dance, exactly like ``scenario_uc1`` and
``launcher``. It defines **no** ``test_*`` functions, so pytest does not collect it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
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
    kubectl,
    kubectl_cp,
    kubectl_rollout_status,
    opa_bin,
    opa_eval,
    port_forward,
    require_env,
    resolve_pod,
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

# OPA-writer pod to ``kubectl cp`` the generated .rego from (name explicit or via label selector).
# The OPA filesystem-stub writer runs as a container INSIDE the interface pod (not its own pod),
# so the .rego is captured from that container. Defaults match the deployed stack.
OPA_NAMESPACE = os.environ.get("AIAC_OPA_NAMESPACE", "aiac-system")
OPA_SELECTOR = os.environ.get("AIAC_OPA_SELECTOR", "app=aiac-interface")
OPA_CONTAINER = os.environ.get("AIAC_OPA_CONTAINER", "aiac-pdp-policy-opa")
OPA_POD = os.environ.get("AIAC_OPA_POD")
OPA_REGO_PATH = os.environ.get("AIAC_OPA_REGO_PATH", "/rego")

AGENT_SLUG = f"{NAMESPACE}_{scn.AGENT_WORKLOAD}".replace("-", "_")  # team1/github-agent -> team1_github_agent
INBOUND_REGO = f"{AGENT_SLUG}.inbound.rego"
OUTBOUND_REGO = f"{AGENT_SLUG}.outbound.rego"
AGENT_REGO_FILES = (INBOUND_REGO, OUTBOUND_REGO)

# The outbound user-gate probe (``data.probe.outbound.allow``), shared by every rung.
PROBE_UC1 = HERE / "probe_uc1.rego"


# ======================================================================================
# Shared inbound oracle (identical for every rung — inbound is unaffected by tool onboarding)
# ======================================================================================
#
# A user may call the agent iff their realm role sources some agent scope (``INBOUND_PAIRS``). This
# holds for all rungs; only the *outbound* gate differs (empty for rung 1, the full
# ``OUTBOUND_SUBJECT_PAIRS`` once a tool is onboarded), so ``expected_outbound`` stays rung-local.

_INBOUND_SOURCES = {role for role, _ in scn.INBOUND_PAIRS}  # user-roles reaching some agent scope
INBOUND_GRANT_SET: set[tuple[str, str]] = set(scn.INBOUND_PAIRS)


def expected_inbound(subject: str) -> bool:
    """A user may call the agent iff their realm role sources some agent scope (``INBOUND_PAIRS``)."""
    return scn.USERS[subject] in _INBOUND_SOURCES


# ======================================================================================
# Shared outbound oracle for the tool-onboarded rungs (rungs 2 & 3)
# ======================================================================================
#
# Once **any** tool is onboarded, the agent's outbound user gate is the full user→tool grant set
# (``OUTBOUND_SUBJECT_PAIRS``) — and it is the same set **regardless of onboarding order** (spec:
# *Onboarding order is irrelevant*). Rung 2 (agent→tool) fills it in when the tool is onboarded after
# the agent; rung 3 (tool→agent) produces it in one pass. Both converge here, so both share this
# oracle; only rung 1 (no tool) is the exception and keeps its own empty-gate oracle. Verdicts are
# computed from this, never read from the Rego under test.

OUTBOUND_SUBJECT_GRANT_SET: set[tuple[str, str]] = set(scn.OUTBOUND_SUBJECT_PAIRS)


def expected_outbound_with_tool(subject: str, function_name: str) -> bool:
    """A user may reach a tool scope iff their realm role is granted it in the full user→tool gate
    (``OUTBOUND_SUBJECT_PAIRS``) — the gate any tool onboarding fills in on the agent's model,
    order-independently (rungs 2 & 3)."""
    return (scn.USERS[subject], function_name) in OUTBOUND_SUBJECT_GRANT_SET


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


# ======================================================================================
# Onboarding trigger + Rego capture
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
    kubectl("apply", "-f", "-", input_text=json.dumps(cm))

    mounted = kubectl(
        "get", "deployment", CONTROLLER_DEPLOYMENT, "-n", namespace,
        "-o", "jsonpath={.spec.template.spec.volumes[*].configMap.name}",
    )
    if POLICY_CONFIGMAP in mounted.split():
        return  # already mounted — no rollout needed

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
    """``POST /apply/service/{service_id}`` against the Controller; assert 200."""
    resp = requests.post(f"{base_url}/apply/service/{service_id}", timeout=ONBOARD_TIMEOUT)
    assert resp.status_code == 200, (
        f"onboard {service_id!r} at {base_url}: HTTP {resp.status_code} — {resp.text[:500]}"
    )


def fresh_rego_dir(prefix: str) -> Path:
    """Host dir the captured ``.rego`` is copied to: ``$REGO_OUTPUT_DIR`` if set, else a fresh test
    temp dir named with ``prefix`` (spec § Configuration). A temp default keeps captured artifacts out
    of the repo and lets each run verify freshly generated policy — never a stale artifact. Any stale
    ``.rego`` in the dir is cleared so a broken pipeline can't pass green on leftovers."""
    base = os.environ.get("REGO_OUTPUT_DIR")
    path = Path(base) if base else Path(tempfile.mkdtemp(prefix=prefix))
    path.mkdir(parents=True, exist_ok=True)
    for stale in path.glob("*.rego"):
        stale.unlink()
    return path


def writer_pod() -> str:
    """Resolve the pod hosting the OPA filesystem-stub writer (the ``aiac-pdp-policy-opa`` container
    lives inside the interface pod)."""
    return OPA_POD or resolve_pod(OPA_SELECTOR, namespace=OPA_NAMESPACE)


def clear_writer_rego(pod: str) -> None:
    """Delete any stale ``.rego`` in the writer's output dir **before** onboarding, so the run
    captures only freshly-generated policy — a leftover from a previous run must never let a broken
    pipeline pass green. (The host capture dir is cleared too, in ``fresh_rego_dir``.)"""
    kubectl(
        "exec", "-n", OPA_NAMESPACE, pod, "-c", OPA_CONTAINER, "--",
        "sh", "-c", f"rm -f {OPA_REGO_PATH.rstrip('/')}/*.rego",
    )


def writer_rego_files(pod: str) -> list[str]:
    """List the ``.rego`` basenames present in the writer's output dir (the ground truth for the
    file-set assertions). Lets a rung assert *what the pipeline actually wrote* — e.g. that no
    ``github_tool.*.rego`` exists even after the tool is onboarded — rather than only what was
    copied to the host."""
    out = kubectl(
        "exec", "-n", OPA_NAMESPACE, pod, "-c", OPA_CONTAINER, "--",
        "sh", "-c", f"ls -1 {OPA_REGO_PATH.rstrip('/')}/*.rego 2>/dev/null || true",
    )
    return [Path(line.strip()).name for line in out.splitlines() if line.strip()]


def capture_rego(pod: str, rego_dir: Path) -> None:
    """``kubectl cp`` the agent's inbound + outbound Rego from the writer container into ``rego_dir``.

    Only the two agent files are copied: under UC-1 the tool is a pure target and the pipeline emits
    no ``github_tool.*.rego`` for any rung. (Use ``writer_rego_files`` to assert that on the pod.)"""
    for filename in AGENT_REGO_FILES:
        kubectl_cp(
            pod, f"{OPA_REGO_PATH.rstrip('/')}/{filename}", rego_dir / filename,
            namespace=OPA_NAMESPACE, container=OPA_CONTAINER,
        )


# ======================================================================================
# Grant-set extraction (semantic-equivalence oracle, re-derived from the generated Rego)
# ======================================================================================


def opa_dump(rego: Path, ref: str) -> object:
    """Return the value of a Rego data ref (a map/list, not a boolean) via ``opa eval``."""
    return opa_eval([rego], ref, {})


def outbound_subject_grants(rego_dir: Path) -> set[tuple[str, str]]:
    """User->tool grant set from the outbound Rego's ``outbound_subject_role_scopes`` map (``∅`` when
    no tool has been onboarded, the full ``OUTBOUND_SUBJECT_PAIRS`` once one has)."""
    m = opa_dump(
        rego_dir / OUTBOUND_REGO,
        f"data.authz.{AGENT_SLUG}.outbound.outbound_subject_role_scopes",
    )
    return {(role, scope) for role, scopes in (m or {}).items() for scope in scopes}


def inbound_grants(rego_dir: Path) -> set[tuple[str, str]]:
    """User-role->agent-scope grant set from the inbound Rego's ``role_scopes`` restricted to the
    published ``agent_scopes`` (a role may list scopes the agent does not expose; those don't grant)."""
    rego = rego_dir / INBOUND_REGO
    role_scopes = opa_dump(rego, f"data.authz.{AGENT_SLUG}.inbound.role_scopes") or {}
    agent_scopes = set(opa_dump(rego, f"data.authz.{AGENT_SLUG}.inbound.agent_scopes") or [])
    return {
        (role, scope)
        for role, scopes in role_scopes.items()
        for scope in scopes
        if scope in agent_scopes
    }


# ======================================================================================
# Per-rung fixture flow — cleanup → onboard (in order) → capture rego → yield → cleanup
# ======================================================================================


@contextmanager
def onboarded_stack(workloads: list[str], *, rego_prefix: str) -> Iterator[dict]:
    """Run one rung's whole flow and yield ``{"admin", "rego_dir", "writer_pod"}``.

    Provision users/roles, onboard the given ``workloads`` **in order** through the real in-cluster
    UC-1 Controller (``POST /apply/service/{id}``, one call each within a single port-forward),
    capture the agent's Rego, and yield the live handles for the rung's assertions. Keycloak cleanup
    runs before and after; the clients are left registered as before (spec § Per-rung flow).

    ``opa`` absence **skips** the whole suite up front (before any cluster work), so a missing oracle
    binary never masquerades as a failure. The workload order is the rung's identity — e.g. rung 2
    passes ``[agent, tool]`` so tool onboarding retroactively completes the agent's outbound gate."""
    opa_bin()  # skip early if opa is absent — cheaper than skipping after the onboard
    require_env("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")

    admin = connect_admin()
    cleanup_provisioned(admin, TEST_REALM)  # before — clean slate
    provision_realm_and_users(admin, TEST_REALM)  # BEFORE onboarding (PRB reads the role universe)
    ensure_agent_policy(CONTROLLER_NAMESPACE)  # mount the PRB's policy.md if the stack lacks it

    rego_dir = fresh_rego_dir(rego_prefix)  # fresh host dir; stale .rego cleared
    pod = writer_pod()
    clear_writer_rego(pod)  # clear stale .rego in the writer BEFORE onboarding
    try:
        service_ids = [
            resolve_service_id(admin, TEST_REALM, f"{NAMESPACE}/{workload}") for workload in workloads
        ]
        with port_forward(
            CONTROLLER_TARGET,
            namespace=CONTROLLER_NAMESPACE,
            local_port=CONTROLLER_LOCAL_PORT,
            remote_port=CONTROLLER_REMOTE_PORT,
        ) as base_url:
            for service_id in service_ids:  # onboard in the rung's order
                onboard(base_url, service_id)

        capture_rego(pod, rego_dir)
        missing = [f for f in AGENT_REGO_FILES if not (rego_dir / f).is_file()]
        if missing:
            raise RuntimeError(
                f"onboarding {workloads} produced no {missing} in {rego_dir} — the pipeline failed "
                "or the OPA filesystem-stub writer is not deployed (spec § Blocked by)."
            )
        yield {"admin": admin, "rego_dir": rego_dir, "writer_pod": pod}
    finally:
        cleanup_provisioned(admin, TEST_REALM)  # after — restore the pre-run state
