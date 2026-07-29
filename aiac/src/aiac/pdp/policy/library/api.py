"""HTTP client for the PDP Policy Writer (OPA) REST API.

Module-level functions wrapping ``{AIAC_PDP_POLICY_URL}/policy...`` endpoints.
The PDP Policy Writer operates on a Kubernetes CR, not a Keycloak realm, so
none of these functions take or send a ``realm`` parameter.
"""

import os
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from aiac.policy.model.models import AgentPolicyModel, PolicyModel

load_dotenv(Path(__file__).resolve().parent / ".env")


def _base_url() -> str:
    return os.getenv("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")


def _check(resp: requests.Response) -> None:
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


def apply_policy(model: PolicyModel) -> None:
    _check(requests.post(f"{_base_url()}/policy", json=model.model_dump()))


# ``agent_id`` is the Keycloak clientId (``{ns}/{name}`` or a SPIFFE URI), so it can carry
# slashes and other reserved characters. Encode it as a single, inert path segment
# (``safe=""`` also escapes ``/``) so it cannot alter the request target — closes the
# partial-SSRF vector and keeps the id from splitting into extra path segments.
def apply_agent_policy(agent_id: str, model: AgentPolicyModel) -> None:
    _check(
        requests.post(
            f"{_base_url()}/policy/agents/{quote(agent_id, safe='')}", json=model.model_dump()
        )
    )


def delete_agent_policy(agent_id: str) -> None:
    _check(requests.delete(f"{_base_url()}/policy/agents/{quote(agent_id, safe='')}"))


def delete_policy() -> None:
    _check(requests.delete(f"{_base_url()}/policy"))
