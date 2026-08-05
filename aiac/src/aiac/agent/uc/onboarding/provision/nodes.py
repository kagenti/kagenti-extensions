"""Nodes for the Service Provision sub-agent (UC1).

All nodes are **non-LLM**. Graph:

    START -> classify_service -> [analyze_agent | analyze_tool] -> provision_service -> END

IdP access is via the **idp-library** `Configuration` (the `_config` seam), never the IdP
service directly. Kubernetes access is via the `kube` seam module (`list_pods`,
`read_service`, `list_agentcards`), which retries internally. Any upstream failure surfaces
as an `HTTPException(502, ...)` whose message names the workload and the specific
missing/invalid label — actionable, never silent.
"""

from fastapi import HTTPException

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import ServiceType
from aiac.shared.upstream import run_upstream

from .kube import list_agentcards, list_pods, read_service
from .state import OnboardingProvisionState
from .types import RoleDefinition, ScopeDefinition, ServiceProvision

_TYPE_LABEL = "rossoctl.io/type"
_MCP_LABEL = "protocol.rossoctl.io/mcp"


# --------------------------------------------------------------------------- #
# Seams (patched in unit tests)                                                #
# --------------------------------------------------------------------------- #
def _config() -> Configuration:
    return Configuration.for_default_realm()


def _discovery_token(service_id: str) -> str:
    """Mint a tool-audienced discovery bearer token via the idp-library `Configuration` seam
    (never Keycloak directly). The config service holds the admin creds and does the minting."""
    return _config().mint_discovery_token(service_id)


# (connect, read) timeouts for the MCP discovery probe — an unreachable/hanging tool must not
# block the onboarding request indefinitely (there was previously no timeout).
_MCP_TIMEOUT = (5, 30)


def _mcp_tools_list(endpoint: str, token: str | None = None) -> list[dict]:
    """POST a JSON-RPC `tools/list` to an MCP endpoint and return the tool manifest list.
    Each tool is a dict with `name` and (optional) `description`. When `token` is provided it is
    sent as an `Authorization: Bearer` header (the tool's MCP endpoint is fronted by an AuthBridge
    sidecar that validates inbound JWTs). Bounded transport retries are applied here so callers
    just map the final failure to a 502."""
    import requests

    def _do():
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.post(
            endpoint,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=headers,
            timeout=_MCP_TIMEOUT,
        )
        resp.raise_for_status()
        return (resp.json().get("result") or {}).get("tools", [])

    return run_upstream(_do)


def _select_pod(pods, workload_name: str):
    """The pod owned by ``workload_name``: a Deployment's ReplicaSet (name prefix
    ``{workload}-``), or a StatefulSet / Sandbox whose name equals ``workload``."""
    for pod in pods:
        for owner in getattr(pod.metadata, "owner_references", None) or []:
            if owner.kind == "ReplicaSet" and owner.name.startswith(f"{workload_name}-"):
                return pod
            if owner.kind in ("StatefulSet", "Sandbox") and owner.name == workload_name:
                return pod
    return None


# --------------------------------------------------------------------------- #
# Nodes                                                                        #
# --------------------------------------------------------------------------- #
def classify_service(state: OnboardingProvisionState) -> dict:
    """Resolve identity and determine service type from the operator's `rossoctl.io/type`
    pod label (authoritative — not the entity_id format)."""
    service_id = state.trigger.entity_id

    try:
        service = _config().get_service(service_id)
    except Exception as e:
        raise HTTPException(502, f"IdP config unavailable resolving service {service_id!r}: {e}")

    name = service.name or ""
    if "/" not in name:
        raise HTTPException(
            502,
            f"client.name {name!r} for service {service_id!r} has no '/': "
            "namespace/workload_name unrecoverable",
        )
    namespace, workload_name = name.split("/", 1)

    try:
        pods = list_pods(namespace)
    except Exception as e:
        raise HTTPException(502, f"Kubernetes pod LIST failed in namespace {namespace!r}: {e}")

    pod = _select_pod(pods, workload_name)
    if pod is None:
        raise HTTPException(
            502, f"no pod owned by workload {workload_name!r} in namespace {namespace!r}"
        )

    label = (getattr(pod.metadata, "labels", None) or {}).get(_TYPE_LABEL)
    try:
        service_type = ServiceType((label or "").capitalize())
    except ValueError:
        raise HTTPException(
            502,
            f"workload {workload_name!r}: {_TYPE_LABEL} label missing or invalid "
            f"(got {label!r}, expected 'agent' or 'tool')",
        )

    return {
        "service_id": service_id,
        "namespace": namespace,
        "workload_name": workload_name,
        "service_type": service_type,
    }


def analyze_agent(state: OnboardingProvisionState) -> dict:
    """Derive an agent's roles + scopes from its AgentCard CR (non-LLM).

    The operator fetches the agent's A2A card and syncs it onto the CR's ``status.card``; each skill
    there carries a machine ``id`` (a stable identifier, e.g. ``source_operations``) plus a display
    ``name`` (which may contain spaces). Scope names are built from the skill ``id`` so they are
    usable Keycloak scope names, and each skill also gets a **per-skill operator role** mirroring the
    scope (same name + description): the role's description is what the PRB capability-match reads to
    confine and grant the agent's outbound access on a domain basis. Falls back to a default access
    scope + a default operator role when there is no AgentCard CR (legacy deployments) or the CR has
    no synced skills yet."""
    namespace, workload = state.namespace, state.workload_name

    try:
        resp = list_agentcards(namespace)
    except Exception as e:
        raise HTTPException(502, f"Kubernetes AgentCard LIST failed in namespace {namespace!r}: {e}")

    # Link the card to the workload by its ``spec.targetRef`` (the Deployment it describes), since the
    # operator names the CR after the Deployment (e.g. ``<workload>-deployment-card``), not the
    # workload. Fall back to ``metadata.name == workload`` for hand-authored/legacy cards.
    def _targets_workload(c: dict) -> bool:
        target = ((c.get("spec") or {}).get("targetRef") or {}).get("name")
        return target == workload or (c.get("metadata") or {}).get("name") == workload

    card = next((c for c in resp.get("items", []) if _targets_workload(c)), None)
    skills = (((card or {}).get("status") or {}).get("card") or {}).get("skills", [])
    if not skills:
        provision = ServiceProvision(
            roles=[RoleDefinition(name=f"{workload}.access", description="Default access scope")],
            scopes=[ScopeDefinition(name=f"{workload}.access", description="Default access scope")],
            reasoning=(
                "partial: no AgentCard found, default scope assigned"
                if card is None
                else "partial: AgentCard has no synced skills, default scope assigned"
            ),
        )
        return {"service_provision": provision}

    # One operator role per skill, mirroring the scope (same name + description). The role name ==
    # scope name is fine — a realm role and a client scope are distinct Keycloak objects. The role's
    # description drives the PRB capability-match (see generic_policy.md).
    def _skill_key(s: dict) -> str:
        key = s.get("id") or s.get("name")
        if not key:
            raise HTTPException(
                502,
                f"AgentCard for workload {workload!r} in namespace {namespace!r} has a skill "
                f"with neither 'id' nor 'name'; cannot derive a scope/role name (skill: {s!r})",
            )
        return key

    scopes = [
        ScopeDefinition(name=f"{workload}.{_skill_key(s)}", description=s.get("description", ""))
        for s in skills
    ]
    roles = [
        RoleDefinition(name=f"{workload}.{_skill_key(s)}", description=s.get("description", ""))
        for s in skills
    ]
    provision = ServiceProvision(
        roles=roles,
        scopes=scopes,
        reasoning=f"derived from AgentCard: {len(skills)} skills",
    )
    return {"service_provision": provision}


def analyze_tool(state: OnboardingProvisionState) -> dict:
    """Discover a tool's scopes from its MCP `tools/list` manifest (non-LLM). Endpoint is
    resolved via the hybrid Keycloak->K8s strategy (issue 6.2): identity from `classify_service`,
    reachable endpoint from the K8s Service."""
    namespace, workload = state.namespace, state.workload_name

    try:
        svc = read_service(workload, namespace)
    except Exception as e:
        raise HTTPException(
            502, f"Kubernetes Service GET failed for {workload!r} in namespace {namespace!r}: {e}"
        )

    labels = getattr(svc.metadata, "labels", None) or {}
    if _MCP_LABEL not in labels:
        raise HTTPException(
            502,
            f"Service {workload!r} in namespace {namespace!r} is missing the {_MCP_LABEL!r} "
            "label (deploy-time prerequisite for MCP tool discovery)",
        )

    ports = getattr(svc.spec, "ports", None) or []
    if not ports:
        raise HTTPException(
            502,
            f"Service {workload!r} in namespace {namespace!r} exposes no ports; "
            "cannot resolve an MCP endpoint",
        )
    port = ports[0].port
    endpoint = f"http://{workload}.{namespace}.svc.cluster.local:{port}/mcp"

    # The MCP endpoint is fronted by the tool's AuthBridge sidecar, which validates inbound JWTs
    # against the tool's own clientId as the audience. Mint a tool-audienced discovery token first;
    # a failure here surfaces as an actionable 502 rather than a downstream 401.
    try:
        token = _discovery_token(state.service_id)
    except Exception as e:
        raise HTTPException(
            502, f"discovery token minting failed for service {state.service_id!r}: {e}"
        )

    try:
        tools = _mcp_tools_list(endpoint, token=token)
    except Exception as e:
        raise HTTPException(502, f"MCP tools/list failed at {endpoint}: {e}")

    def _tool_name(t: dict) -> str:
        name = t.get("name")
        if not name:
            raise HTTPException(
                502,
                f"MCP tools/list at {endpoint} returned a tool with no 'name'; "
                f"cannot derive a scope name (tool: {t!r})",
            )
        return name

    scopes = [
        ScopeDefinition(name=f"{workload}.{_tool_name(t)}", description=t.get("description", ""))
        for t in tools
    ]
    provision = ServiceProvision(
        roles=[],
        scopes=scopes,
        reasoning=f"derived from MCP manifest: {len(tools)} tools",
    )
    return {"service_provision": provision}


def provision_service(state: OnboardingProvisionState) -> dict:
    """Write the derived roles + scopes into the IdP (idempotent create-or-get + map) and
    persist the discovered service type onto the Keycloak client, via the idp-library.
    Returns the `ServiceProvision` + `service_type` to the Orchestrator."""
    config = _config()
    provision = state.service_provision
    service_id = state.service_id

    try:
        for role in provision.roles:
            config.create_service_role(service_id, role)
        for scope in provision.scopes:
            config.create_service_scope(service_id, scope)
        service = config.get_service(service_id)
        config.set_service_type(service, state.service_type)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"IdP Configuration Service unavailable provisioning {service_id!r}: {e}")

    return {"service_provision": provision, "service_type": state.service_type}
