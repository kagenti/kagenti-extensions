"""Generate github-agent Rego by driving the live PDP Policy Writer (OPA) stub.

Standalone (NOT pytest, NOT CI). Launches the stub as a uvicorn subprocess writing to a known
local dir, applies a PolicyModel through the PDP policy library, shuts the service down, and
prints the output dir. Inspect the .rego files by hand.

The subprocess lifecycle is shared with the 5.3 launcher via ``test.integration.launcher``, and
the fixed scenario (the same canonical github-agent worked example) lives in
``test.integration.scenario`` so the two launchers cannot drift.

Run:
    .venv/bin/python test/pdp/policy/generate_rego.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # -> aiac/
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(REPO_ROOT))  # so ``import test.integration.*`` resolves
sys.path.insert(0, str(SRC))  # so ``import aiac.*`` resolves

from aiac.idp.configuration.models import Role, Scope  # noqa: E402
from aiac.policy.model.models import AgentPolicyModel, PolicyModel, PolicyRule  # noqa: E402
from test.integration import scenario as scn  # noqa: E402
from test.integration.launcher import (  # noqa: E402
    Service,
    print_rego_dir,
    resolve_output_dir,
    running_services,
)

PORT = int(os.environ.get("PORT", "7072"))
BASE_URL = f"http://127.0.0.1:{PORT}"
OUTPUT_DIR = resolve_output_dir(Path(__file__).parent / "rego_out")


def _roles() -> dict[str, Role]:
    """Synthesize a Role per scenario role name (ids stable as ``role-<name>``)."""
    names = list(scn.USER_ROLES) + list(scn.AGENT_ROLES)
    return {name: Role(id=f"role-{name}", name=name, composite=False) for name in names}


def _scopes() -> dict[str, Scope]:
    """Synthesize a Scope per scenario scope name (ids stable as ``scope-<name>``)."""
    names = list(scn.AGENT_SCOPES) + list(scn.TOOL_SCOPES)
    return {name: Scope(id=f"scope-{name}", name=name) for name in names}


def build_model() -> PolicyModel:
    role, scope = _roles(), _scopes()

    def rules(pairs: list[tuple[str, str]]) -> list[PolicyRule]:
        return [PolicyRule(role=role[r], scope=scope[s]) for r, s in pairs]

    agent = AgentPolicyModel(
        agent_id=scn.AGENT_ID,
        agent_roles=[role[name] for name in scn.AGENT_ROLES],
        agent_scopes=[scope[name] for name in scn.AGENT_SCOPES],
        source_roles={},
        subject_roles={user: [role[role_name]] for user, role_name in scn.USERS.items()},
        target_allow_scopes={scn.TOOL_ID: [scope[name] for name in scn.TOOL_SCOPES]},
        inbound_subject_allow_rules=rules(scn.INBOUND_PAIRS),
        outbound_target_allow_rules=rules(scn.OUTBOUND_PAIRS),
        outbound_subject_allow_rules=rules(scn.OUTBOUND_SUBJECT_PAIRS),
    )
    return PolicyModel(agents=[agent])


def main() -> None:
    os.environ["AIAC_PDP_POLICY_URL"] = BASE_URL  # consumed by the library
    from aiac.pdp.policy.library.api import apply_policy  # import after env is set

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    opa = Service(
        "aiac.pdp.service.policy.opa.main:app",
        port=PORT,
        env={"REGO_OUTPUT_DIR": str(OUTPUT_DIR)},
    )
    with running_services([opa], src=SRC):
        apply_policy(build_model())

    print_rego_dir(OUTPUT_DIR)


if __name__ == "__main__":
    main()
