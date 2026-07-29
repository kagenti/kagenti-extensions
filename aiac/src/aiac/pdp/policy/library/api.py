"""HTTP client for the PDP Policy Writer (OPA) REST API.

Module-level functions wrapping ``{AIAC_PDP_POLICY_URL}/policy...`` endpoints.
The PDP Policy Writer operates on a Kubernetes CR, not a Keycloak realm, so
none of these functions take or send a ``realm`` parameter.
"""

import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from aiac.policy.model.models import AgentPolicyModel, PolicyModel

load_dotenv(Path(__file__).resolve().parent / ".env")

# ``quote(..., safe="")`` emits only unreserved characters (``A-Za-z0-9-._~``) and ``%XX`` escapes.
# Asserting the encoded value against this set before it is spliced into a request URL proves it is
# a single, inert path segment — no scheme, host, ``/`` or ``..`` can be injected (closes the
# partial-SSRF vector). The fullmatch barrier is what CodeQL recognises; ``quote`` alone does not.
_URL_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~%-]+")


def _base_url() -> str:
    return os.getenv("AIAC_PDP_POLICY_URL", "http://127.0.0.1:7072")


def _agent_id_segment(agent_id: str) -> str:
    """URL-encode ``agent_id`` as a single, validated path segment.

    ``agent_id`` is the Keycloak clientId (``{ns}/{name}`` or a SPIFFE URI), so it can carry
    slashes and other reserved characters; ``safe=""`` escapes them all. The fullmatch check
    then guarantees the result cannot alter the request target.
    """
    segment = quote(agent_id, safe="")
    if not _URL_SEGMENT_RE.fullmatch(segment):
        raise ValueError(f"agent_id {agent_id!r} does not yield a safe URL path segment")
    return segment


def _check(resp: requests.Response) -> None:
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")


def apply_policy(model: PolicyModel) -> None:
    _check(requests.post(f"{_base_url()}/policy", json=model.model_dump()))


def apply_agent_policy(agent_id: str, model: AgentPolicyModel) -> None:
    _check(
        requests.post(
            f"{_base_url()}/policy/agents/{_agent_id_segment(agent_id)}", json=model.model_dump()
        )
    )


def delete_agent_policy(agent_id: str) -> None:
    _check(requests.delete(f"{_base_url()}/policy/agents/{_agent_id_segment(agent_id)}"))


def delete_policy() -> None:
    _check(requests.delete(f"{_base_url()}/policy"))
