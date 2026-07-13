"""End-to-end UC-1 onboarding-pipeline integration test — the generated Rego is the artifact under
test, produced by *real UC-1 onboarding of really-deployed workloads*.

Discovery-driven sibling of ``test_policy_pipeline.py`` (5.3): identical scenario facts and truth
tables (``scenario_uc1.py`` mirrors ``scenario.py``'s decisions), but where 5.3 hand-provisions the
agent/tool roles/scopes with clean bare names and calls the PRB directly, this test **infers** them
via the production trigger — it deploys the simplified ``github-tool`` + the real ``github-agent`` to
a live Kagenti/Kind cluster, drives the in-cluster UC-1 Service Onboarding agent
(``POST /apply/service/{client_id}``) for each, and asserts the emitted Rego decides correctly with
the standalone ``opa eval`` binary.

Because real UC-1 names every scope ``{workload}.{name}`` and emits one generic ``github-agent.agent``
role, the Rego is **semantically similar but not byte-identical** to 5.3's: workload-prefixed names,
and a degenerate (empty) agent->tool gate. The outbound probe therefore evaluates the **user gate
only** (``subject_ok``) — phase-1's user-gating dimension — and matches ``function_name`` to a scope
by **exact string equality** (both sides already prefixed). See
``docs/specs/integration-test/uc1-onboarding-pipeline.md``.

*Deploy + discover + evaluate — no A2A traffic, no live enforcement* (phase-1 out of scope).

Topology (spec § Topology): AIAC runs **in-cluster** (so UC-1's ``analyze_tool`` can reach the tool's
``*.svc.cluster.local`` MCP endpoint); the test triggers over HTTP via ``kubectl port-forward``. Two
independent AIAC stacks serve the two ``policy.md`` variants (``explicit`` / ``abstract``) — a
documented **precondition**, addressed by ``AIAC_EXPLICIT_URL`` / ``AIAC_ABSTRACT_URL``. The IdP
Configuration Service + Keycloak are shared. Each variant's ``.rego`` is ``kubectl cp``'d out to
``rego_out_uc1/<variant>/`` (kept separate from 5.3's ``rego_out/``) and evaluated on the host.

Run (needs a live cluster + operator + Keycloak + the two AIAC stacks + real LLM in-pod; ``opa`` on
PATH or ``$OPA_BIN``; realm defaults to ``aiac-uc1-e2e``):
    .venv/bin/pytest test/integration/test_uc1_onboarding_pipeline.py -m integration -v
Without ``-m integration`` the suite is not collected; without ``opa`` each node skips at runtime.
"""

from __future__ import annotations

import logging
import os
import sys
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
    kubectl_apply,
    kubectl_cp,
    kubectl_delete,
    kubectl_rollout_status,
    opa_eval,
    port_forward,
    require_env,
    resolve_pod,
)

log = logging.getLogger(__name__)

VARIANTS = ("explicit", "abstract")

# --- Config (env) — spec § Configuration ----------------------------------------------------
TEST_REALM = os.environ.get("AIAC_TEST_REALM", scn.REALM_DEFAULT)
NAMESPACE = os.environ.get("AIAC_DEMO_NAMESPACE", scn.DEMO_NAMESPACE_DEFAULT)
ADMIN_REALM = os.environ.get("KEYCLOAK_ADMIN_REALM", "master")
VARIANT_URL = {
    "explicit": os.environ.get("AIAC_EXPLICIT_URL", "http://127.0.0.1:7070"),
    "abstract": os.environ.get("AIAC_ABSTRACT_URL", "http://127.0.0.1:7080"),
}
# OPA-writer pod + rego path per variant (for kubectl cp). Pod name may be given explicitly or
# resolved from a label selector; the writer's output dir defaults to /rego (REGO_OUTPUT_DIR).
OPA_NAMESPACE = os.environ.get("AIAC_OPA_NAMESPACE", NAMESPACE)
OPA_REGO_PATH = os.environ.get("AIAC_OPA_REGO_PATH", "/rego")
OPA_POD_ENV = {"explicit": "AIAC_OPA_POD_EXPLICIT", "abstract": "AIAC_OPA_POD_ABSTRACT"}
OPA_SELECTOR = {  # fallback when the pod name is not given explicitly
    "explicit": os.environ.get("AIAC_OPA_SELECTOR_EXPLICIT", "app=aiac-opa-explicit"),
    "abstract": os.environ.get("AIAC_OPA_SELECTOR_ABSTRACT", "app=aiac-opa-abstract"),
}
# Host base dir for captured Rego. Distinct from 5.3's rego_out/ so the two suites never clobber.
REGO_BASE = Path(os.environ.get("REGO_OUTPUT_DIR", str(HERE / "rego_out_uc1")))

# Demo manifests (repo-relative).
TOOL_MANIFEST = REPO_ROOT / "demo/tools/github_tool/k8s/github-tool-deployment.yaml"
AGENT_CONFIGMAPS = REPO_ROOT / "demo/agents/github_agent/k8s/configmaps.yaml"
AGENT_MANIFEST = REPO_ROOT / "demo/agents/github_agent/k8s/github-agent-deployment.yaml"

INBOUND_REGO = "github_agent.inbound.rego"
OUTBOUND_REGO = "github_agent.outbound.rego"

# ======================================================================================
# Keycloak provisioning (users + realm roles) — the test fixture UC-1 does NOT do
# ======================================================================================


def _connect_admin():
    """Connect to the admin realm so the fixture can create the test realm + provision users."""
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
    """Idempotently ensure the dedicated realm exists and holds ``scenario_uc1``'s users + realm
    roles (with descriptions the PRB reads). Never deletes/recreates — reruns converge (spec: shared
    realm, leave-in-place)."""
    from keycloak.exceptions import KeycloakError

    try:
        admin.create_realm({"realm": realm, "enabled": True})
    except KeycloakError:
        pass  # already exists — leave in place
    admin.change_current_realm(realm)

    for name, description in scn.USER_ROLES.items():
        admin.create_realm_role({"name": name, "description": description}, skip_exists=True)

    for username, role_name in scn.USERS.items():
        user_id = admin.create_user({"username": username, "enabled": True}, exist_ok=True)
        admin.set_user_password(user_id, scn.USER_PASSWORD, temporary=False)
        admin.assign_realm_roles(user_id, [admin.get_realm_role(role_name)])


def resolve_client_id(admin, realm: str, client_name: str) -> str:
    """Return the Keycloak *clientId* of the client whose *name* is ``client_name`` (the operator
    sets ``client.name = "{ns}/{workload}"``; the id is a SPIFFE URI under SPIRE, else the same
    string). The UC-1 trigger takes this clientId — never assume the bare string."""
    admin.change_current_realm(realm)
    for client in admin.get_clients():
        if client.get("name") == client_name:
            return client["clientId"]
    raise AssertionError(f"no Keycloak client with name {client_name!r} in realm {realm!r}")


# ======================================================================================
# Cluster deployment + readiness
# ======================================================================================


def _set_namespace_realm(namespace: str, realm: str) -> None:
    """Set ``KEYCLOAK_REALM`` on the namespace's ``authbridge-config`` ConfigMap so the operator
    registers this namespace's clients into the test realm (per-namespace; the operator preserves an
    admin/CI-set value — operator issue #433). Done before the workloads' AgentRuntimes reconcile."""
    kubectl(
        "patch", "configmap", "authbridge-config", "-n", namespace, "--type", "merge",
        "-p", f'{{"data":{{"KEYCLOAK_REALM":"{realm}"}}}}',
    )


def _deploy_workloads() -> None:
    kubectl_apply(AGENT_CONFIGMAPS)  # authbridge-config + authproxy-routes first
    _set_namespace_realm(NAMESPACE, TEST_REALM)  # ...then pin the realm before pods register
    kubectl_apply(TOOL_MANIFEST)
    kubectl_apply(AGENT_MANIFEST)


def _delete_workloads() -> None:
    # Delete the deployment manifests (incl. AgentRuntimes) so the operator de-registers the
    # clients. ConfigMaps + realm + users + captured .rego are left in place (spec step 6).
    for manifest in (AGENT_MANIFEST, TOOL_MANIFEST):
        try:
            kubectl_delete(manifest)
        except Exception as exc:  # best-effort teardown — never mask the test result
            log.warning("teardown: delete %s failed: %s", manifest.name, exc)


def _poll(check, *, desc: str, timeout: float = 180.0, interval: float = 3.0):
    """Poll ``check()`` until it returns truthy (returning that value), or fail after ``timeout``."""
    import time

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = check()
            if last:
                return last
        except Exception as exc:  # transient during rollout — keep polling
            last = exc
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for: {desc} (last={last!r})")


def _pod_type_label(namespace: str, app_selector: str) -> str | None:
    out = kubectl(
        "get", "pods", "-n", namespace, "-l", app_selector,
        "-o", "jsonpath={.items[0].metadata.labels.kagenti\\.io/type}",
    )
    return out.strip() or None


def _agentcard_present(namespace: str, name: str) -> bool:
    out = kubectl(
        "get", "agentcard", name, "-n", namespace, "--ignore-not-found",
        "-o", "jsonpath={.metadata.name}",
    )
    return out.strip() == name


def _tool_answers_tools_list(namespace: str) -> bool:
    """Port-forward the tool Service and confirm its MCP ``tools/list`` returns tools."""
    with port_forward(
        f"svc/{scn.TOOL_WORKLOAD}", namespace=namespace, local_port=19090, remote_port=9090
    ) as base:
        resp = requests.post(
            f"{base}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json"},
            timeout=5,
        )
        resp.raise_for_status()
        return bool((resp.json().get("result") or {}).get("tools"))


def _wait_for_workloads(admin) -> None:
    """Block until both workloads are Ready, the operator has registered their Keycloak clients and
    applied ``kagenti.io/type`` labels, the AgentCard CR exists, and the tool answers ``tools/list``
    (spec step 2)."""
    kubectl_rollout_status(f"deployment/{scn.TOOL_WORKLOAD}", namespace=NAMESPACE)
    kubectl_rollout_status(f"deployment/{scn.AGENT_WORKLOAD}", namespace=NAMESPACE)

    _poll(
        lambda: resolve_client_id(admin, TEST_REALM, f"{NAMESPACE}/{scn.TOOL_WORKLOAD}"),
        desc=f"operator registers client {NAMESPACE}/{scn.TOOL_WORKLOAD}",
    )
    _poll(
        lambda: resolve_client_id(admin, TEST_REALM, f"{NAMESPACE}/{scn.AGENT_WORKLOAD}"),
        desc=f"operator registers client {NAMESPACE}/{scn.AGENT_WORKLOAD}",
    )
    _poll(
        lambda: _pod_type_label(NAMESPACE, f"app={scn.TOOL_WORKLOAD}") == "tool",
        desc=f"kagenti.io/type=tool on {scn.TOOL_WORKLOAD} pod",
    )
    _poll(
        lambda: _pod_type_label(NAMESPACE, f"app.kubernetes.io/name={scn.AGENT_WORKLOAD}") == "agent",
        desc=f"kagenti.io/type=agent on {scn.AGENT_WORKLOAD} pod",
    )
    _poll(
        lambda: _agentcard_present(NAMESPACE, scn.AGENT_WORKLOAD),
        desc=f"AgentCard CR {scn.AGENT_WORKLOAD} present",
    )
    _poll(lambda: _tool_answers_tools_list(NAMESPACE), desc="tool answers tools/list")


# ======================================================================================
# Onboarding trigger + Rego capture
# ======================================================================================


def _trigger_onboard(base_url: str, client_id: str) -> None:
    """``POST /apply/service/{client_id}`` against a variant's Controller; assert 200."""
    resp = requests.post(f"{base_url}/apply/service/{client_id}", timeout=120)
    assert resp.status_code == 200, (
        f"onboard {client_id!r} at {base_url}: HTTP {resp.status_code} — {resp.text[:500]}"
    )


def _capture_rego(variant: str, rego_dir: Path) -> None:
    """``kubectl cp`` the variant stack's inbound + outbound Rego from its OPA-writer pod."""
    pod = os.environ.get(OPA_POD_ENV[variant]) or resolve_pod(OPA_SELECTOR[variant], namespace=OPA_NAMESPACE)
    for filename in (INBOUND_REGO, OUTBOUND_REGO):
        kubectl_cp(
            pod, f"{OPA_REGO_PATH.rstrip('/')}/{filename}", rego_dir / filename, namespace=OPA_NAMESPACE
        )


# ======================================================================================
# Session fixture — deploy, provision, onboard both variants, capture Rego
# ======================================================================================


@pytest.fixture(scope="session")
def pipeline() -> dict[str, dict]:
    """Deploy the demo workloads once, provision users/roles once, then onboard **tool then agent**
    against each variant's in-cluster AIAC stack, capturing each variant's ``.rego`` to
    ``rego_out_uc1/<variant>/``. Yields ``{variant: {"rego_dir": Path}}``. Teardown deletes the demo
    workloads (operator de-registers clients); realm/users/rego are left in place."""
    require_env("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")

    admin = _connect_admin()
    provision_realm_and_users(admin, TEST_REALM)  # BEFORE onboarding (PRB reads the role universe)

    results: dict[str, dict] = {}
    try:
        _deploy_workloads()
        _wait_for_workloads(admin)

        tool_id = resolve_client_id(admin, TEST_REALM, f"{NAMESPACE}/{scn.TOOL_WORKLOAD}")
        agent_id = resolve_client_id(admin, TEST_REALM, f"{NAMESPACE}/{scn.AGENT_WORKLOAD}")

        for variant in VARIANTS:
            base_url = VARIANT_URL[variant]
            _trigger_onboard(base_url, tool_id)  # tool first — its scopes must exist for the agent
            _trigger_onboard(base_url, agent_id)  # then agent — PRB reads the scope universe

            rego_dir = REGO_BASE / variant
            rego_dir.mkdir(parents=True, exist_ok=True)
            _capture_rego(variant, rego_dir)
            results[variant] = {"rego_dir": rego_dir}
            log.info("variant %s: rego captured to %s", variant, rego_dir)

        yield results
    finally:
        _delete_workloads()


# ======================================================================================
# Expected-verdict oracle (pure functions over the scenario_uc1 truth table)
# ======================================================================================

_INBOUND_SOURCES = {role for role, _ in scn.INBOUND_PAIRS}  # user-roles reaching some agent scope
_OUTBOUND_SUBJECT = set(scn.OUTBOUND_SUBJECT_PAIRS)  # (user-role, tool-scope) the subject may reach


def expected_inbound(subject: str) -> bool:
    """A user may call the agent iff their realm role sources some agent scope (``INBOUND_PAIRS``)."""
    return scn.USERS[subject] in _INBOUND_SOURCES


def expected_outbound(subject: str, function_name: str) -> bool:
    """User gate only: a user's call to a tool function is allowed iff the subject is entitled to that
    scope (``OUTBOUND_SUBJECT_PAIRS``). The agent->tool gate is degenerate under UC-1 (not probed)."""
    return (scn.USERS[subject], function_name) in _OUTBOUND_SUBJECT


# --- Grant-set extraction (semantic-equivalence oracle, re-derived from the generated Rego) ---


def _opa_dump(rego: Path, ref: str) -> object:
    """Return the value of a Rego data ref (a map/list, not a boolean) via ``opa eval``."""
    return opa_eval([rego], ref, {})


def outbound_subject_grants(rego_dir: Path) -> set[tuple[str, str]]:
    """User->tool grant set from the outbound Rego's ``outbound_subject_role_scopes`` map."""
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


_TRUTH: dict[str, set[tuple[str, str]]] = {
    "inbound": set(scn.INBOUND_PAIRS),
    "outbound_subject": set(scn.OUTBOUND_SUBJECT_PAIRS),
}
_GRANTS = {"inbound": inbound_grants, "outbound_subject": outbound_subject_grants}


# ======================================================================================
# Tests — opa eval the truth table (verdicts computed from scenario_uc1)
# ======================================================================================


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(pipeline: dict[str, dict], variant: str, subject: str) -> None:
    """Inbound gate allows a user iff their role may reach some (discovered) agent scope."""
    rego = pipeline[variant]["rego_dir"] / INBOUND_REGO
    allowed = opa_eval([rego], "data.authz.github_agent.inbound.allow", {"subject": subject})
    assert allowed == expected_inbound(subject), f"{variant} / {subject}"


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("function_name", list(scn.TOOL_SCOPES))
def test_outbound(pipeline: dict[str, dict], variant: str, subject: str, function_name: str) -> None:
    """Outbound user gate (via ``probe_uc1.rego``) allows a subject's call to a tool function iff the
    subject is entitled to that full discovered scope name (exact match; agent gate not probed)."""
    rego = pipeline[variant]["rego_dir"] / OUTBOUND_REGO
    allowed = opa_eval(
        [rego, HERE / "probe_uc1.rego"],
        "data.probe.outbound.allow",
        {"subject": subject, "function_name": function_name},
    )
    assert allowed == expected_outbound(subject, function_name), f"{variant} / {subject} / {function_name}"


@pytest.mark.parametrize("variant", VARIANTS)
def test_no_tool_rego(pipeline: dict[str, dict], variant: str) -> None:
    """The pipeline emits no tool model — exactly the two agent files, no ``github_tool.*.rego``."""
    rego_dir = pipeline[variant]["rego_dir"]
    assert not list(rego_dir.glob("github_tool*.rego")), "unexpected tool Rego emitted"
    for filename in (INBOUND_REGO, OUTBOUND_REGO):
        assert (rego_dir / filename).exists(), f"missing {filename}"


# ======================================================================================
# Semantic-equivalence tests — grant sets re-derived from the generated Rego
# ======================================================================================


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("gate", list(_TRUTH))
def test_grant_set_matches_truth_table(pipeline: dict[str, dict], variant: str, gate: str) -> None:
    """Each variant's grant set (re-derived from its Rego) equals the ``scenario_uc1`` truth table —
    catching verdict-neutral over/under-grants the coarse allow/deny oracle cannot see."""
    got = _GRANTS[gate](pipeline[variant]["rego_dir"])
    assert got == _TRUTH[gate], f"{variant} {gate}: missing={_TRUTH[gate] - got} extra={got - _TRUTH[gate]}"


@pytest.mark.parametrize("gate", list(_TRUTH))
def test_variants_are_semantically_equivalent(pipeline: dict[str, dict], gate: str) -> None:
    """The two policy variants describe the same access model, so their Rego must yield the same
    grant set (compared as order-independent sets; text/name-ordering may differ)."""
    explicit = _GRANTS[gate](pipeline["explicit"]["rego_dir"])
    abstract = _GRANTS[gate](pipeline["abstract"]["rego_dir"])
    assert explicit == abstract, (
        f"{gate}: variants diverge — only-explicit={explicit - abstract} only-abstract={abstract - explicit}"
    )
