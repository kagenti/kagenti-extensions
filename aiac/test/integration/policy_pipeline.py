"""Drive the whole identity->policy pipeline end-to-end and leave the Rego on disk to eyeball.

Standalone launcher (NOT pytest, NOT CI, NOT ``@pytest.mark.integration``) for the fixed
``github-agent`` scenario. It provisions a live Keycloak realm, spawns the IdP Configuration,
Policy Store, and OPA Policy Writer services as ``uvicorn`` subprocesses, runs the real Policy
Rules Builder (real LLM) to map roles->scopes, then the real Policy Computation Engine to build
the ``PolicyModel`` and push it to the OPA filesystem stub. Nothing is mocked; the only shortcut is
that OPA writes ``.rego`` files instead of patching a Kubernetes CR (same stub as the 5.2 launcher).

Write-only: no read-back, no assertions. The realm is left in place and the ``.rego`` files are
left on disk for a human to compare against the package shapes in
``inception/requirements/components/pdp-policy-writer-opa.md``.

Spec:  inception/requirements/integration-test/policy-pipeline.md
Issue: inception/issues/testing/5.3-policy-pipeline-integration-test.md

Run (with KEYCLOAK_URL + admin creds + LLM_* exported; realm defaults to aiac-e2e):
    .venv/bin/python test/integration/policy_pipeline.py
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent  # test/integration/
REPO_ROOT = HERE.parents[1]  # -> aiac/
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(REPO_ROOT))  # so ``import test.integration.*`` resolves
sys.path.insert(0, str(SRC))  # so ``import aiac.*`` resolves

from test.integration import scenario as scn  # noqa: E402
from test.integration.launcher import (  # noqa: E402
    Service,
    print_rego_dir,
    require_env,
    resolve_output_dir,
    running_services,
)

# --- Resolve config + set env BEFORE importing aiac (the libraries read env at import time) ---
TEST_REALM = os.environ.setdefault("AIAC_TEST_REALM", scn.REALM_DEFAULT)
os.environ["AIAC_REALM"] = TEST_REALM  # the PCE reads back the realm we provision
os.environ.setdefault("AIAC_PDP_CONFIG_URL", "http://127.0.0.1:7071")
os.environ.setdefault("AIAC_POLICY_STORE_URL", "http://127.0.0.1:7074")
os.environ.setdefault("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")
os.environ.setdefault("AIAC_POLICY_FILE", str(HERE / "policy.explicit.md"))
os.environ.setdefault("KEYCLOAK_ADMIN_REALM", "master")  # inherited by the IdP subprocess

from keycloak import KeycloakAdmin  # noqa: E402
from keycloak.exceptions import KeycloakError  # noqa: E402

from aiac.agent.policy_rules_builder.graph import build_role_rules, build_scope_rules  # noqa: E402
from aiac.idp.configuration.api import Configuration  # noqa: E402
from aiac.idp.configuration.models import Role, Scope  # noqa: E402
from aiac.policy.computation.engine import compute_and_apply  # noqa: E402
from aiac.policy.model.models import PolicyRule  # noqa: E402


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


# Keycloak caps realm-role and client descriptions at 255 chars. These four scenario descriptions
# exceed it, so the launcher provisions a <=255 rendering that preserves the meaning (the client
# ones keep the "Agent" / "Tool" keyword the IdP type inference needs). scenario.py keeps the
# verbatim text as the source of truth; the PRB reads the shortened developer / tester text back
# from Keycloak.
_KEYCLOAK_DESCRIPTIONS: dict[str, str] = {
    "developer": (
        "Developer — an engineering user who needs read and write access to source repository "
        "contents and read access to the issue tracker, but does not modify issues. Resolves to "
        "source read, source write, and issues read."
    ),
    "tester": (
        "Tester — a quality-assurance user who needs full read and write access to issues (to "
        "file, triage, and update reports) but does not touch source repository contents. "
        "Resolves to issues read and issues write."
    ),
    scn.AGENT_ID: (
        "GitHub Agent — an autonomous agent acting on a user's GitHub repositories and issue "
        "tracker, delegating each operation to the github-tool. Source: `source-helper` / "
        "`source-access`; issues: `issues-helper` / `issues-access`."
    ),
    scn.TOOL_ID: (
        "GitHub Tool — a capability provider exposing fine-grained operations against GitHub: read "
        "source (`source-read`), write source (`source-write`), read issues (`issues-read`), write "
        "issues (`issues-write`). It performs the actual GitHub calls."
    ),
}


def _kc_desc(name: str, full: str) -> str:
    """Keycloak-storable (<=255 char) description for ``name``: the shortened rendering when the
    verbatim ``full`` description is over Keycloak's limit, else ``full`` unchanged."""
    desc = _KEYCLOAK_DESCRIPTIONS.get(name, full)
    if len(desc) > 255:
        raise ValueError(f"Keycloak description for {name!r} is {len(desc)} chars (>255)")
    return desc


def provision_keycloak_admin(admin: KeycloakAdmin, test_realm: str) -> None:
    """Provision the realm via ``python-keycloak`` (idempotent: delete-if-exists, then create).

    Creates the realm, the ``developer`` / ``tester`` realm roles, the ``dev-user`` / ``test-user``
    users (with role assignments), and the ``github-agent`` / ``github-tool`` clients. The agent
    client enables a service account so its client roles can be assigned to it later.
    """
    try:
        admin.delete_realm(test_realm)
    except KeycloakError:
        pass  # realm absent — nothing to delete
    admin.create_realm({"realm": test_realm, "enabled": True})
    admin.change_current_realm(test_realm)

    for name, description in scn.USER_ROLES.items():
        admin.create_realm_role({"name": name, "description": _kc_desc(name, description)}, skip_exists=True)

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

    admin.create_client(_client(scn.AGENT_ID, _kc_desc(scn.AGENT_ID, scn.AGENT_DESCRIPTION)), skip_exists=True)
    admin.create_client(_client(scn.TOOL_ID, _kc_desc(scn.TOOL_ID, scn.TOOL_DESCRIPTION)), skip_exists=True)


def provision_via_config(config: Configuration) -> None:
    """Provision client roles + scopes and their service mappings through the aiac IdP library.

    This is the real product surface the PCE reads back: it creates the agent/tool scopes and the
    agent client roles, then maps scopes->services and client-roles->agent so ``get_services_by_*``
    and ``get_service().roles/.scopes`` resolve.
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


def _read_back(config: Configuration) -> tuple[dict[str, Role], dict[str, Scope]]:
    """Read roles + scopes back through the IdP library (carrying real ids + descriptions)."""
    roles = {r.name: r for r in config.get_roles()}
    scopes = {s.name: s for s in config.get_scopes()}
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Fail fast on inputs that have no safe default (the PRB LLM has none either).
    require_env(
        "KEYCLOAK_URL",
        "KEYCLOAK_ADMIN_USERNAME",
        "KEYCLOAK_ADMIN_PASSWORD",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_API_KEY",
    )

    output_dir = resolve_output_dir(HERE / "rego_out")
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = os.environ.get("AGENTPOLICY_DB_PATH") or str(
        Path(tempfile.mkdtemp(prefix="aiac-store-")) / "policy_model.db"
    )

    admin = _connect_admin()
    provision_keycloak_admin(admin, TEST_REALM)

    idp_host, idp_port = _host_port(os.environ["AIAC_PDP_CONFIG_URL"], 7071)
    store_host, store_port = _host_port(os.environ["AIAC_POLICY_STORE_URL"], 7074)
    opa_host, opa_port = _host_port(os.environ["AIAC_PDP_POLICY_URL"], 7072)
    services = [
        Service("aiac.idp.service.configuration.keycloak.main:app", port=idp_port, host=idp_host),
        Service(
            "aiac.policy.store.service.main:app",
            port=store_port,
            host=store_host,
            env={"AGENTPOLICY_DB_PATH": db_path},
        ),
        Service(
            "aiac.pdp.service.policy.opa.main:app",
            port=opa_port,
            host=opa_host,
            env={"REGO_OUTPUT_DIR": str(output_dir)},
        ),
    ]

    with running_services(services, src=SRC):
        config = Configuration.for_realm(TEST_REALM)
        provision_via_config(config)
        roles, scopes = _read_back(config)
        rules = orchestrate_prb(roles, scopes)
        compute_and_apply(rules, override=False)

    print_rego_dir(output_dir)


if __name__ == "__main__":
    main()
