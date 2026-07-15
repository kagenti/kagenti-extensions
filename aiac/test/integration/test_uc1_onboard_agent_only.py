"""Rung 1 of the UC-1 onboarding ladder — onboard the **agent only**.

The simplest rung (issue ``testing/5.4.1-uc1-onboard-agent-only.md``; spec
``docs/specs/integration-test/uc1-onboarding-pipeline.md``): drive the **real** in-cluster UC-1
Service Onboarding agent (``POST /apply/service/{id}``) for **only** the ``github-agent`` — the
``github-tool`` is deployed + registered but **not** onboarded — then assert the agent-side outcome.
Proves agent discovery + inbound policy generation stand alone, and that the outbound user gate is
correctly **empty** when no tool has been onboarded.

Single AIAC stack, OPA filesystem-stub writer, single abstract ``policy.md`` (the two-stack /
two-variant machinery of the discarded ``test_uc1_onboarding_pipeline.py`` is gone). Reuses
``scenario_uc1.py`` (truth tables — the oracle), ``probe_uc1.rego`` (user-gate probe), and
``launcher.py`` (kubectl / port-forward / opa helpers).

Per-rung flow (spec § Per-rung flow): **Keycloak cleanup → onboard agent → validate end state →
Keycloak cleanup**. Deployment + client registration are **preconditions**, not test steps.

*Onboard + evaluate — no A2A traffic, no live enforcement* (phase-1 out of scope).

Resolving the trigger id (spec § Resolving ``{service_id}``): the ``POST /apply/service/{service_id}``
route is a single path segment and the Controller resolves it via ``admin.get_client(service_id)``,
which keys on the Keycloak **internal client UUID** — *not* the ``clientId`` (a SPIFFE URI with
slashes under SPIRE, which the single-segment route cannot carry). This module therefore resolves the
client whose **name** is ``"{ns}/github-agent"`` and triggers with its ``id`` (UUID).

Run (needs a live Kagenti/Kind cluster with the AIAC stack + OPA filesystem-stub writer, the demo
workloads deployed + registered into ``AIAC_TEST_REALM``, a real LLM in-pod, and ``opa`` on PATH or
``$OPA_BIN``):

    .venv/bin/pytest test/integration/test_uc1_onboard_agent_only.py -m integration -v

Without ``-m integration`` the suite is not collected; without ``opa`` it skips at runtime.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest
import requests

pytestmark = pytest.mark.integration

HERE = Path(__file__).resolve().parent  # test/integration/
REPO_ROOT = HERE.parents[1]  # -> aiac/
sys.path.insert(0, str(REPO_ROOT))  # so ``import test.integration.*`` resolves

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

AGENT_SLUG = scn.AGENT_WORKLOAD.replace("-", "_")  # github-agent -> github_agent
INBOUND_REGO = f"{AGENT_SLUG}.inbound.rego"
OUTBOUND_REGO = f"{AGENT_SLUG}.outbound.rego"


# ======================================================================================
# Expected-verdict oracle (pure functions over the scenario_uc1 truth table)
# ======================================================================================
#
# Rung 1 is the exception in the ladder: with no tool onboarded there are no tool scopes in the
# universe, so the outbound **user gate is empty** (all deny) and the outbound-subject grant set is
# ``∅`` — regardless of ``scenario_uc1.OUTBOUND_SUBJECT_PAIRS`` (which is the rung-2/3 table). Inbound
# is unaffected. These functions encode that rung-1 contract; verdicts are computed here, never read
# from the Rego under test.

_INBOUND_SOURCES = {role for role, _ in scn.INBOUND_PAIRS}  # user-roles reaching some agent scope

# Rung 1's expected grant sets (the oracle for the semantic-equivalence check).
RUNG1_INBOUND = set(scn.INBOUND_PAIRS)
RUNG1_OUTBOUND_SUBJECT: set[tuple[str, str]] = set()  # ∅ — no tool onboarded


def expected_inbound(subject: str) -> bool:
    """A user may call the agent iff their realm role sources some agent scope (``INBOUND_PAIRS``)."""
    return scn.USERS[subject] in _INBOUND_SOURCES


def expected_outbound(subject: str, function_name: str) -> bool:
    """Rung 1: the outbound user gate is entirely empty (no tool onboarded), so every
    ``(subject, function_name)`` is denied."""
    return (scn.USERS[subject], function_name) in RUNG1_OUTBOUND_SUBJECT


# ======================================================================================
# Oracle contract tests — fixture-independent; pin rung 1's defining decisions
# ======================================================================================
#
# These need neither the cluster nor ``opa``: they assert the rung-1 oracle itself (the intended
# policy the live decisions below are checked against). If these are wrong, every live assertion is
# meaningless — so they are the tracer bullet.


@pytest.mark.parametrize(
    "subject, allowed",
    [("dev-user", True), ("test-user", True), ("devops-user", False)],
)
def test_inbound_oracle(subject: str, allowed: bool) -> None:
    """Inbound: dev-user ✅, test-user ✅, devops-user ❌ (devops sources no agent scope)."""
    assert expected_inbound(subject) is allowed


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound_oracle_all_deny(subject: str, function_name: str) -> None:
    """Rung 1's defining property: the outbound user gate is empty, so every ``(subject, function)``
    is denied — no tool was onboarded, so no tool scope is in the universe."""
    assert expected_outbound(subject, function_name) is False


def test_rung1_grant_set_oracle() -> None:
    """Rung 1 grant-set oracle: inbound == the ``scenario_uc1`` inbound truth table; the
    outbound-subject grant set is empty."""
    assert RUNG1_INBOUND == set(scn.INBOUND_PAIRS)
    assert RUNG1_OUTBOUND_SUBJECT == set()


# ======================================================================================
# Keycloak provisioning + cleanup (the fixture UC-1 does NOT do)
# ======================================================================================


def _connect_admin():
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
    precondition this test owns, so a fresh AIAC stack needs no manual patching.

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


def _onboard(base_url: str, service_id: str) -> None:
    """``POST /apply/service/{service_id}`` against the Controller; assert 200."""
    resp = requests.post(f"{base_url}/apply/service/{service_id}", timeout=ONBOARD_TIMEOUT)
    assert resp.status_code == 200, (
        f"onboard {service_id!r} at {base_url}: HTTP {resp.status_code} — {resp.text[:500]}"
    )


def _rego_dir() -> Path:
    """Host dir the captured ``.rego`` is copied to: ``$REGO_OUTPUT_DIR`` if set, else a fresh test
    temp dir (spec § Configuration). A temp default keeps captured artifacts out of the repo and
    lets each run verify freshly generated policy — never a stale artifact."""
    base = os.environ.get("REGO_OUTPUT_DIR")
    path = Path(base) if base else Path(tempfile.mkdtemp(prefix="aiac-uc1-rung1-rego-"))
    path.mkdir(parents=True, exist_ok=True)
    for stale in path.glob("*.rego"):
        stale.unlink()
    return path


def _writer_pod() -> str:
    """Resolve the pod hosting the OPA filesystem-stub writer (the ``aiac-pdp-policy-opa`` container
    lives inside the interface pod)."""
    return OPA_POD or resolve_pod(OPA_SELECTOR, namespace=OPA_NAMESPACE)


def _clear_writer_rego(pod: str) -> None:
    """Delete any stale ``.rego`` in the writer's output dir **before** onboarding, so the run
    captures only freshly-generated policy — a leftover from a previous run must never let a broken
    pipeline pass green. (The host capture dir is cleared too, in ``_rego_dir``.)"""
    kubectl(
        "exec", "-n", OPA_NAMESPACE, pod, "-c", OPA_CONTAINER, "--",
        "sh", "-c", f"rm -f {OPA_REGO_PATH.rstrip('/')}/*.rego",
    )


def _capture_rego(pod: str, rego_dir: Path) -> None:
    """``kubectl cp`` the agent's inbound + outbound Rego from the writer container into ``rego_dir``."""
    for filename in (INBOUND_REGO, OUTBOUND_REGO):
        kubectl_cp(
            pod, f"{OPA_REGO_PATH.rstrip('/')}/{filename}", rego_dir / filename,
            namespace=OPA_NAMESPACE, container=OPA_CONTAINER,
        )


# ======================================================================================
# Grant-set extraction (semantic-equivalence oracle, re-derived from the generated Rego)
# ======================================================================================


def _opa_dump(rego: Path, ref: str) -> object:
    """Return the value of a Rego data ref (a map/list, not a boolean) via ``opa eval``."""
    return opa_eval([rego], ref, {})


def outbound_subject_grants(rego_dir: Path) -> set[tuple[str, str]]:
    """User->tool grant set from the outbound Rego's ``outbound_subject_role_scopes`` map (``∅`` for
    rung 1: no tool onboarded, so the map is empty)."""
    m = _opa_dump(rego_dir / OUTBOUND_REGO, "data.authz.github_agent.outbound.outbound_subject_role_scopes")
    return {(role, scope) for role, scopes in (m or {}).items() for scope in scopes}


def inbound_grants(rego_dir: Path) -> set[tuple[str, str]]:
    """User-role->agent-scope grant set from the inbound Rego's ``role_scopes`` restricted to the
    published ``agent_scopes`` (a role may list scopes the agent does not expose; those don't grant)."""
    rego = rego_dir / INBOUND_REGO
    role_scopes = _opa_dump(rego, "data.authz.github_agent.inbound.role_scopes") or {}
    agent_scopes = set(_opa_dump(rego, "data.authz.github_agent.inbound.agent_scopes") or [])
    return {
        (role, scope)
        for role, scopes in role_scopes.items()
        for scope in scopes
        if scope in agent_scopes
    }


# ======================================================================================
# Session fixture — cleanup → onboard agent only → capture rego → yield → cleanup
# ======================================================================================


@pytest.fixture(scope="session")
def onboarded() -> dict:
    """Provision users/roles, onboard **only** the agent, capture its Rego, and yield the live
    ``admin`` handle + captured ``rego_dir``. Keycloak cleanup runs before and after; the clients are
    left registered as before (spec § Per-rung flow).

    ``opa`` absence **skips** the whole suite up front (before any cluster work), so a missing oracle
    binary never masquerades as a failure."""
    opa_bin()  # skip early if opa is absent — cheaper than skipping after the onboard
    require_env("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")

    admin = _connect_admin()
    cleanup_provisioned(admin, TEST_REALM)  # before — clean slate
    provision_realm_and_users(admin, TEST_REALM)  # BEFORE onboarding (PRB reads the role universe)
    ensure_agent_policy(CONTROLLER_NAMESPACE)  # mount the PRB's policy.md if the stack lacks it

    rego_dir = _rego_dir()  # fresh host dir; stale .rego cleared so a broken pipeline can't pass green
    writer_pod = _writer_pod()
    _clear_writer_rego(writer_pod)  # clear stale .rego in the writer BEFORE onboarding
    try:
        agent_id = resolve_service_id(admin, TEST_REALM, f"{NAMESPACE}/{scn.AGENT_WORKLOAD}")
        with port_forward(
            CONTROLLER_TARGET,
            namespace=CONTROLLER_NAMESPACE,
            local_port=CONTROLLER_LOCAL_PORT,
            remote_port=CONTROLLER_REMOTE_PORT,
        ) as base_url:
            _onboard(base_url, agent_id)  # ONLY the agent — the tool is deployed but not onboarded

        _capture_rego(writer_pod, rego_dir)
        missing = [f for f in (INBOUND_REGO, OUTBOUND_REGO) if not (rego_dir / f).is_file()]
        if missing:
            raise RuntimeError(
                f"agent onboard produced no {missing} in {rego_dir} — the pipeline failed or the "
                "OPA filesystem-stub writer is not deployed (spec § Blocked by)."
            )
        yield {"admin": admin, "rego_dir": rego_dir}
    finally:
        cleanup_provisioned(admin, TEST_REALM)  # after — restore the pre-run state


# ======================================================================================
# Live tests — Keycloak entities + opa-eval decisions (verdicts computed from scenario_uc1)
# ======================================================================================


def test_agent_role_and_scopes_provisioned(onboarded: dict) -> None:
    """Keycloak holds the agent's realm role + the two AgentCard scopes with their descriptions."""
    admin = onboarded["admin"]
    admin.change_current_realm(TEST_REALM)

    role = admin.get_realm_role(scn.AGENT_ROLE)
    assert role and role.get("name") == scn.AGENT_ROLE, f"missing realm role {scn.AGENT_ROLE!r}"

    scopes = {s["name"]: (s.get("description") or "") for s in admin.get_client_scopes()}
    for name, description in scn.AGENT_SCOPES.items():
        assert name in scopes, f"missing agent scope {name!r}"
        assert scopes[name] == description, (
            f"agent scope {name!r} description mismatch: {scopes[name]!r} != {description!r}"
        )


def test_no_tool_scopes_provisioned(onboarded: dict) -> None:
    """The tool was not onboarded, so no ``github-tool.*`` scope exists (UC-1-provisioned scopes are
    prefixed ``github-tool.``; the operator's ``*-aud`` audience scopes are not and don't count)."""
    admin = onboarded["admin"]
    admin.change_current_realm(TEST_REALM)
    tool_scopes = [
        s["name"] for s in admin.get_client_scopes()
        if s.get("name", "").startswith(f"{scn.TOOL_WORKLOAD}.")
    ]
    assert not tool_scopes, f"unexpected tool scopes provisioned: {tool_scopes}"


@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(onboarded: dict, subject: str) -> None:
    """Inbound gate allows a user iff their role may reach some discovered agent scope
    (dev-user ✅, test-user ✅, devops-user ❌)."""
    rego = onboarded["rego_dir"] / INBOUND_REGO
    allowed = opa_eval([rego], "data.authz.github_agent.inbound.allow", {"subject": subject})
    assert allowed == expected_inbound(subject), subject


@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound_all_deny(onboarded: dict, subject: str, function_name: str) -> None:
    """Outbound user gate (via ``probe_uc1.rego``) denies every ``(subject, function)`` — the gate is
    empty because no tool was onboarded (exact-name match on full discovered scope names)."""
    rego = onboarded["rego_dir"] / OUTBOUND_REGO
    allowed = opa_eval(
        [rego, HERE / "probe_uc1.rego"],
        "data.probe.outbound.allow",
        {"subject": subject, "function_name": function_name},
    )
    assert allowed == expected_outbound(subject, function_name), f"{subject} / {function_name}"


def test_only_agent_rego_present(onboarded: dict) -> None:
    """Exactly the two agent files on disk; explicitly no ``github_tool.*.rego`` (the tool is a pure
    target — no rules written for it, and it was not onboarded)."""
    rego_dir = onboarded["rego_dir"]
    assert not list(rego_dir.glob("github_tool*.rego")), "unexpected tool Rego emitted"
    for filename in (INBOUND_REGO, OUTBOUND_REGO):
        assert (rego_dir / filename).is_file(), f"missing {filename}"


def test_inbound_grant_set_matches_truth_table(onboarded: dict) -> None:
    """The inbound grant set re-derived from the Rego equals ``scenario_uc1``'s inbound truth table —
    catching verdict-neutral over/under-grants the coarse allow/deny oracle cannot see."""
    got = inbound_grants(onboarded["rego_dir"])
    assert got == RUNG1_INBOUND, f"inbound: missing={RUNG1_INBOUND - got} extra={got - RUNG1_INBOUND}"


def test_outbound_subject_grant_set_empty(onboarded: dict) -> None:
    """The outbound-subject grant set re-derived from the Rego is ``∅`` — no tool onboarded, so the
    user gate is empty."""
    got = outbound_subject_grants(onboarded["rego_dir"])
    assert got == RUNG1_OUTBOUND_SUBJECT, f"expected empty outbound gate, got {got}"
