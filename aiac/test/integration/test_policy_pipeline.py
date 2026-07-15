"""End-to-end policy-pipeline integration test — the generated Rego is the artifact under test.

Parametrized pytest suite for the fixed ``github-agent`` scenario (spec:
``docs/specs/integration-test/policy-pipeline.md``). A single session fixture drives the
whole identity->policy pipeline with nothing mocked: it provisions a live Keycloak realm, spawns the
IdP Configuration, Policy Store, and OPA Policy Writer services as ``uvicorn`` subprocesses, runs the
real Policy Rules Builder (real LLM) to map roles->scopes, then the real Policy Computation Engine to
build the ``PolicyModel`` and push ``.rego`` files to the OPA filesystem stub. It does this twice —
once for the explicit ``policy.md`` and once for the abstract one — and leaves both Rego sets on disk
under ``rego_out/<variant>/``.

Each test then evaluates the generated Rego with the standalone ``opa`` binary and asserts the verdict
against the scenario's role->access truth table (``scenario.py``). A wrong LLM/PCE mapping fails the
exact ``variant / subject[ / function_name]`` cell. The outbound gate is driven by ``function_name``
(reformatted to exercise the soft match) through ``probe.rego``; the inbound gate queries the real
inbound ``allow`` directly.

This is the pytest replacement for the former write-only ``policy_pipeline.py`` launcher; its helpers
were ported here verbatim.

Run (needs KEYCLOAK_URL + admin creds + LLM_* exported, ``opa`` on PATH; realm defaults to aiac-e2e):
    .venv/bin/pytest test/integration/test_policy_pipeline.py -m integration -v
Without ``-m integration`` the suite is skipped; without ``opa`` each node skips at runtime.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.integration

HERE = Path(__file__).resolve().parent  # test/integration/
REPO_ROOT = HERE.parents[1]  # -> aiac/
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(REPO_ROOT))  # so ``import test.integration.*`` resolves
sys.path.insert(0, str(SRC))  # so ``import aiac.*`` resolves

from test.integration import scenario as scn  # noqa: E402
from test.integration.launcher import (  # noqa: E402
    Service,
    require_env,
    running_services,
)

# --- Resolve config + set env BEFORE importing aiac (the libraries read env at import time) ---
TEST_REALM = os.environ.setdefault("AIAC_TEST_REALM", scn.REALM_DEFAULT)
os.environ["KEYCLOAK_REALM"] = TEST_REALM  # the PCE reads back the realm we provision (single source of truth)
os.environ.setdefault("AIAC_PDP_CONFIG_URL", "http://127.0.0.1:7071")
os.environ.setdefault("AIAC_POLICY_STORE_URL", "http://127.0.0.1:7074")
os.environ.setdefault("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")
os.environ.setdefault("AIAC_POLICY_FILE", str(HERE / "policy.explicit.md"))  # overridden per variant
os.environ.setdefault("KEYCLOAK_ADMIN_REALM", "master")  # inherited by the IdP subprocess

from keycloak import KeycloakAdmin  # noqa: E402
from keycloak.exceptions import KeycloakError  # noqa: E402

from aiac.agent.policy_rules_builder.graph import build_role_rules, build_scope_rules  # noqa: E402
from aiac.idp.configuration.api import Configuration  # noqa: E402
from aiac.idp.configuration.models import Role, Scope  # noqa: E402
from aiac.policy.computation.engine import compute_and_apply  # noqa: E402
from aiac.policy.model.models import PolicyRule  # noqa: E402

log = logging.getLogger(__name__)

VARIANTS = ("explicit", "abstract")


# ======================================================================================
# Ported helpers (verbatim from the former policy_pipeline.py launcher)
# ======================================================================================


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    parts = urlsplit(url)
    return parts.hostname or "127.0.0.1", parts.port or default_port


def _connect_admin() -> KeycloakAdmin:
    """Connect to the admin realm so the launcher can create/delete the test realm."""
    creds = require_env("KEYCLOAK_URL", "KEYCLOAK_ADMIN_USERNAME", "KEYCLOAK_ADMIN_PASSWORD")
    admin_realm = os.environ["KEYCLOAK_ADMIN_REALM"]
    return KeycloakAdmin(
        server_url=creds["KEYCLOAK_URL"],
        realm_name=admin_realm,
        user_realm_name=admin_realm,
        username=creds["KEYCLOAK_ADMIN_USERNAME"],
        password=creds["KEYCLOAK_ADMIN_PASSWORD"],
    )


def provision_keycloak_admin(admin: KeycloakAdmin, test_realm: str) -> None:
    """Provision the realm via ``python-keycloak`` (idempotent: delete-if-exists, then create).

    Driven entirely off ``scenario.py``: creates the realm, every ``scn.USER_ROLES`` realm role
    (``developer`` / ``tester`` / ``devops``), every ``scn.USERS`` user (with role assignments), and
    the ``github-agent`` / ``github-tool`` clients. The agent client enables a service account so
    its client roles can be assigned to it later.
    """
    try:
        admin.delete_realm(test_realm)
    except KeycloakError:
        pass  # realm absent — nothing to delete
    admin.create_realm({"realm": test_realm, "enabled": True})
    admin.change_current_realm(test_realm)

    for name, description in scn.USER_ROLES.items():
        # aiac.managed marker required: the IdP service only populates actorIds (member usernames)
        # for managed roles, and the PCE needs actorIds to build the subject_roles map in the APM.
        admin.create_realm_role(
            {"name": name, "description": description, "attributes": {"aiac.managed": ["true"]}},
            skip_exists=True,
        )

    for username, role_name in scn.USERS.items():
        user_id = admin.create_user({"username": username, "enabled": True}, exist_ok=True)
        admin.set_user_password(user_id, scn.USER_PASSWORD, temporary=False)
        admin.assign_realm_roles(user_id, [admin.get_realm_role(role_name)])

    def _client(client_id: str, description: str) -> dict:
        return {
            "clientId": client_id,
            "enabled": True,
            "description": description,
            "protocol": "openid-connect",
            "publicClient": False,  # confidential — required for a service account
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
        }

    admin.create_client(_client(scn.AGENT_ID, scn.AGENT_DESCRIPTION), skip_exists=True)
    admin.create_client(_client(scn.TOOL_ID, scn.TOOL_DESCRIPTION), skip_exists=True)


def provision_via_config(config: Configuration) -> None:
    """Provision client roles + scopes and their service mappings through the aiac IdP library.

    This is the real product surface the PCE reads back: it creates the agent/tool scopes and the
    agent client roles, then maps scopes->services and client-roles->agent so ``get_services_by_*``
    and ``get_service().roles/.scopes`` resolve. NOT idempotent — call exactly once per realm.
    """
    agent_scopes = {name: config.create_scope(name, desc) for name, desc in scn.AGENT_SCOPES.items()}
    tool_scopes = {name: config.create_scope(name, desc) for name, desc in scn.TOOL_SCOPES.items()}
    agent_roles = {name: config.create_role(name, desc) for name, desc in scn.AGENT_ROLES.items()}

    services = {svc.serviceId: svc for svc in config.get_services()}
    agent_svc, tool_svc = services[scn.AGENT_ID], services[scn.TOOL_ID]

    for scope in agent_scopes.values():
        config.map_scope_to_service(agent_svc, scope)
    for scope in tool_scopes.values():
        config.map_scope_to_service(tool_svc, scope)
    for role in agent_roles.values():
        config.map_role_to_service(agent_svc, role)

    # Type the services via the canonical ``client.type`` attribute using the IdP setter. The IdP no
    # longer infers type from the description or clientId shape (``_build_service`` leaves it to the
    # ``client.type`` attribute). The Agent tag makes the PCE build the agent model; the Tool
    # tag makes it omit the tool model. Without this the PCE builds an empty model — nothing is
    # stored and no rego is written.
    config.set_service_type(agent_svc, "Agent")
    config.set_service_type(tool_svc, "Tool")


def _read_back(config: Configuration) -> tuple[dict[str, Role], dict[str, Scope]]:
    """Read roles + scopes back through the IdP library (carrying real ids + descriptions).

    Scopes are sourced from each service's scope list (not the standalone get_scopes()),
    so that scope.serviceId is populated — a required input for the PCE's SPM routing.
    """
    roles = {r.name: r for r in config.get_roles()}
    scopes: dict[str, Scope] = {}
    for svc in config.get_services():
        for s in svc.scopes:
            scopes.setdefault(s.name, s)  # first owner wins; each scope has exactly one owner
    return roles, scopes


def orchestrate_prb(roles: dict[str, Role], scopes: dict[str, Scope]) -> list[PolicyRule]:
    """Proto-UC1: run the three PRB mappings against the real LLM and concatenate the rules."""
    user_roles = [roles[name] for name in scn.USER_ROLES]
    agent_scopes = [scopes[name] for name in scn.AGENT_SCOPES]
    tool_scopes = [scopes[name] for name in scn.TOOL_SCOPES]
    agent_roles = [roles[name] for name in scn.AGENT_ROLES]

    rules: list[PolicyRule] = []
    for agent_scope in agent_scopes:  # (a) user role -> agent scope
        rules += build_scope_rules(user_roles, agent_scope)
    for tool_scope in tool_scopes:  # (b) user role -> tool scope
        rules += build_scope_rules(user_roles, tool_scope)
    for agent_role in agent_roles:  # (c) agent role -> tool scopes
        rules += build_role_rules(agent_role, tool_scopes)
    return rules


# ======================================================================================
# OPA evaluation
# ======================================================================================


def opa_bin() -> str:
    """Path to the ``opa`` binary, or skip the calling test if it cannot be found."""
    found = os.environ.get("OPA_BIN") or shutil.which("opa")
    if not found:
        pytest.skip("opa binary not found (set OPA_BIN or add opa to PATH)")
    return found


def opa_eval(rego_paths: list[Path], query: str, input_doc: dict) -> bool:
    """Evaluate ``query`` against the given Rego file(s) with ``input_doc`` on stdin; return the
    boolean result. Raises (via ``check=True``) if OPA rejects the Rego or the query errors."""
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


# ======================================================================================
# Expected-verdict oracle (pure functions over the scenario truth table)
# ======================================================================================

_INBOUND_SOURCES = {role for role, _ in scn.INBOUND_PAIRS}  # user-roles that may reach some agent scope
_OUTBOUND_SUBJECT = set(scn.OUTBOUND_SUBJECT_PAIRS)  # (user-role, tool-scope) the subject may reach
_AGENT_REACHABLE = {scope for _, scope in scn.OUTBOUND_PAIRS}  # tool-scopes some agent role reaches


def expected_inbound(subject: str) -> bool:
    """A user may call the agent iff their realm role appears as a source in ``INBOUND_PAIRS``."""
    return scn.USERS[subject] in _INBOUND_SOURCES


def expected_outbound(subject: str, scope: str) -> bool:
    """A user's call resolves to a tool scope iff the subject is entitled to it
    (``OUTBOUND_SUBJECT_PAIRS``) *and* some agent role is entitled to it (``OUTBOUND_PAIRS``)."""
    return (scn.USERS[subject], scope) in _OUTBOUND_SUBJECT and scope in _AGENT_REACHABLE


def reformat_function_name(scope: str) -> str:
    """Render a tool scope as a differently-cased/separated ``function_name`` to exercise the
    probe's token soft-match: ``source-read`` -> ``Source.Read``."""
    return ".".join(part.capitalize() for part in scope.split("-"))


# --- Grant-set extraction (semantic-equivalence oracle) -------------------------------------
#
# The two policy variants describe the SAME access model, so the PRB must derive the same set of
# grants from each (Rego text/ordering may differ — the grant set may not). ``orchestrate_prb``
# returns all three mappings concatenated; the four name spaces are disjoint, so each PolicyRule
# is classified into its gate by ``(role.name, scope.name)`` membership.

_USER_ROLE_NAMES = set(scn.USER_ROLES)
_AGENT_ROLE_NAMES = set(scn.AGENT_ROLES)
_AGENT_SCOPE_NAMES = set(scn.AGENT_SCOPES)
_TOOL_SCOPE_NAMES = set(scn.TOOL_SCOPES)


def grant_sets(rules: list[PolicyRule]) -> dict[str, set[tuple[str, str]]]:
    """Classify a flat PRB rule list into the three gate grant sets, each a set of
    ``(role_name, scope_name)`` pairs: ``inbound`` (user role -> agent scope),
    ``outbound_subject`` (user role -> tool scope), ``outbound_target`` (agent role -> tool scope)."""
    sets: dict[str, set[tuple[str, str]]] = {"inbound": set(), "outbound_subject": set(), "outbound_target": set()}
    for r in rules:
        pair = (r.role.name, r.scope.name)
        if r.role.name in _USER_ROLE_NAMES and r.scope.name in _AGENT_SCOPE_NAMES:
            sets["inbound"].add(pair)
        elif r.role.name in _USER_ROLE_NAMES and r.scope.name in _TOOL_SCOPE_NAMES:
            sets["outbound_subject"].add(pair)
        elif r.role.name in _AGENT_ROLE_NAMES and r.scope.name in _TOOL_SCOPE_NAMES:
            sets["outbound_target"].add(pair)
    return sets


_TRUTH: dict[str, set[tuple[str, str]]] = {
    "inbound": set(scn.INBOUND_PAIRS),
    "outbound_subject": set(scn.OUTBOUND_SUBJECT_PAIRS),
    "outbound_target": set(scn.OUTBOUND_PAIRS),
}


# ======================================================================================
# Session fixture — the one-time pipeline run (both policy variants)
# ======================================================================================


@pytest.fixture(scope="session")
def pipeline() -> dict[str, dict]:
    """Provision Keycloak once, then run the real PRB+PCE pipeline for each policy variant, leaving
    ``.rego`` on disk under ``rego_out/<variant>/``. Returns
    ``{variant: {"rego_dir": Path, "rules": list[PolicyRule]}}`` — the rules are the PRB's grant set,
    captured so the equivalence/truth-table assertions can compare grants directly (not Rego text)."""
    require_env(
        "KEYCLOAK_URL",
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
    )

    admin = _connect_admin()
    provision_keycloak_admin(admin, TEST_REALM)

    idp_host, idp_port = _host_port(os.environ["AIAC_PDP_CONFIG_URL"], 7071)
    store_host, store_port = _host_port(os.environ["AIAC_POLICY_STORE_URL"], 7074)
    opa_host, opa_port = _host_port(os.environ["AIAC_PDP_POLICY_URL"], 7072)

    idp = Service("aiac.idp.service.configuration.keycloak.main:app", port=idp_port, host=idp_host)
    results: dict[str, dict] = {}

    # IdP stays up across both variants; the store/opa pair is restarted per variant so each variant
    # writes into a fresh store (compute_and_apply merges onto the existing model with override=False).
    with running_services([idp], src=SRC):
        config = Configuration.for_realm(TEST_REALM)
        provision_via_config(config)  # exactly once — not idempotent
        roles, scopes = _read_back(config)

        agent_slug = scn.AGENT_ID.replace("-", "_")  # github-agent -> github_agent
        for variant in VARIANTS:
            rego_dir = HERE / "rego_out" / variant
            rego_dir.mkdir(parents=True, exist_ok=True)
            # Clear any rego left by a previous run so the assertions below always verify freshly
            # generated policy — never a stale artifact that would let a broken pipeline pass green.
            for stale in rego_dir.glob("*.rego"):
                stale.unlink()
            db_path = Path(tempfile.mkdtemp(prefix=f"aiac-store-{variant}-")) / "policy_model.db"
            os.environ["AIAC_POLICY_FILE"] = str(HERE / f"policy.{variant}.md")  # read per PRB call
            log.info("variant %s: policy=%s rego_dir=%s", variant, os.environ["AIAC_POLICY_FILE"], rego_dir)

            store = Service(
                "aiac.policy.store.service.main:app",
                port=store_port,
                host=store_host,
                env={"SERVICEPOLICY_DB_PATH": str(db_path)},
            )
            opa = Service(
                "aiac.pdp.service.policy.opa.main:app",
                port=opa_port,
                host=opa_host,
                env={"REGO_OUTPUT_DIR": str(rego_dir)},
            )
            with running_services([store, opa], src=SRC):
                rules = orchestrate_prb(roles, scopes)
                compute_and_apply(rules, override=False)
            # compute_and_apply is fire-and-forget: it swallows every dependency error and logs it,
            # so a failed IdP/store/PDP interaction (or an empty derived agent set) silently writes no
            # rego. Assert the agent's rego actually landed here at setup, so such a failure surfaces
            # as one clear error instead of cryptic "no such file" OPA failures across every test.
            expected = [rego_dir / f"{agent_slug}.inbound.rego", rego_dir / f"{agent_slug}.outbound.rego"]
            missing = [p.name for p in expected if not p.is_file()]
            if missing:
                raise RuntimeError(
                    f"variant {variant!r}: compute_and_apply produced no {missing} in {rego_dir} "
                    f"(PRB returned {len(rules)} rule(s)); the pipeline failed silently — "
                    f"check the compute_and_apply logs above for a swallowed exception."
                )
            results[variant] = {"rego_dir": rego_dir, "rules": rules}

    yield results


# ======================================================================================
# Tests
# ======================================================================================


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("subject", list(scn.USERS))
def test_inbound(pipeline: dict[str, dict], variant: str, subject: str) -> None:
    """The generated inbound gate allows a user iff their role may reach some agent scope."""
    rego = pipeline[variant]["rego_dir"] / "github_agent.inbound.rego"
    allowed = opa_eval([rego], "data.authz.github_agent.inbound.allow", {"subject": subject})
    assert allowed == expected_inbound(subject)


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("subject", list(scn.USERS))
@pytest.mark.parametrize("scope", list(scn.TOOL_SCOPES))
def test_outbound(pipeline: dict[str, dict], variant: str, subject: str, scope: str) -> None:
    """The generated outbound gate (via the probe) allows a subject's call to a tool operation iff
    both the subject and some agent role are entitled to that operation's scope."""
    rego = pipeline[variant]["rego_dir"] / "github_agent.outbound.rego"
    fn = reformat_function_name(scope)  # soft-match rendering, e.g. source-read -> Source.Read
    allowed = opa_eval(
        [rego, HERE / "probe.rego"],
        "data.probe.outbound.allow",
        {"subject": subject, "target": scn.TOOL_ID, "function_name": fn},
    )
    assert allowed == expected_outbound(subject, scope)


@pytest.mark.parametrize("variant", VARIANTS)
def test_outbound_unknown_target_denied(pipeline: dict[str, dict], variant: str) -> None:
    """An otherwise-allowed call to an unknown target is denied (target not in target_scopes)."""
    rego = pipeline[variant]["rego_dir"] / "github_agent.outbound.rego"
    allowed = opa_eval(
        [rego, HERE / "probe.rego"],
        "data.probe.outbound.allow",
        {"subject": "dev-user", "target": "unknown-tool", "function_name": "Source.Read"},
    )
    assert allowed is False


@pytest.mark.parametrize("variant", VARIANTS)
def test_outbound_soft_match_not_overbroad(pipeline: dict[str, dict], variant: str) -> None:
    """A function name whose tokens match no scope is denied — guards against soft-match over-match."""
    rego = pipeline[variant]["rego_dir"] / "github_agent.outbound.rego"
    allowed = opa_eval(
        [rego, HERE / "probe.rego"],
        "data.probe.outbound.allow",
        {"subject": "dev-user", "target": scn.TOOL_ID, "function_name": "delete_everything"},
    )
    assert allowed is False


# ======================================================================================
# Semantic-equivalence tests — the two policy variants must yield the SAME grant set
# ======================================================================================


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("gate", list(_TRUTH))
def test_grant_set_matches_truth_table(pipeline: dict[str, dict], variant: str, gate: str) -> None:
    """Each variant's PRB grant set for each gate equals the scenario truth table. Catches both
    under-grants (a missing pair) and over-grants (an unsupported pair) that the coarse allow/deny
    oracle above cannot see."""
    got = grant_sets(pipeline[variant]["rules"])[gate]
    assert got == _TRUTH[gate], f"{variant} {gate}: missing={_TRUTH[gate] - got} extra={got - _TRUTH[gate]}"


@pytest.mark.parametrize("gate", list(_TRUTH))
def test_variants_are_semantically_equivalent(pipeline: dict[str, dict], gate: str) -> None:
    """The explicit and abstract variants describe the same access model, so the PRB must derive the
    same grant set from each. Compared as order-independent sets (Rego text/ordering may differ)."""
    explicit = grant_sets(pipeline["explicit"]["rules"])[gate]
    abstract = grant_sets(pipeline["abstract"]["rules"])[gate]
    assert explicit == abstract, (
        f"{gate}: variants diverge — only-explicit={explicit - abstract} only-abstract={abstract - explicit}"
    )
