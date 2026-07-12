"""Nodes for the Service Provision sub-agent (UC1).

All nodes are **non-LLM**. Graph:

    START -> classify_service -> [analyze_agent | analyze_tool] -> provision_service -> END

IdP access is via the **idp-library** `Configuration` (the `_config` seam), never the IdP
service directly. Kubernetes access is via the `_core_v1` / `_custom_objects` seams, which
lazily import the `kubernetes` client so unit tests (which patch the seams) never need it
installed. Any upstream failure surfaces as an `HTTPException(502, ...)` whose message names
the workload and the specific missing/invalid label — actionable, never silent.
"""

import os

from fastapi import HTTPException
from tenacity import Retrying, stop_after_attempt, wait_exponential

from aiac.idp.configuration.api import Configuration
from aiac.idp.configuration.models import ServiceType

from .state import OnboardingProvisionState
from .types import RoleDefinition, ScopeDefinition, ServiceProvision

_TYPE_LABEL = "kagenti.io/type"
_MCP_LABEL = "protocol.kagenti.io/mcp"
_AGENTCARD_GROUP = "agent.kagenti.dev"
_AGENTCARD_VERSION = "v1alpha1"
_AGENTCARD_PLURAL = "agentcards"


# --------------------------------------------------------------------------- #
# Seams (patched in unit tests)                                                #
# --------------------------------------------------------------------------- #
def _config() -> Configuration:
    return Configuration.for_realm(os.getenv("KEYCLOAK_REALM", ""))


def _core_v1():
    """CoreV1Api client (pods, services). Lazily imports kubernetes so tests that patch
    this seam never require the package."""
    from kubernetes import client, config

    _load_kube_config(config)
    return client.CoreV1Api()


def _custom_objects():
    """CustomObjectsApi client (AgentCard CRs). Lazily imports kubernetes."""
    from kubernetes import client, config

    _load_kube_config(config)
    return client.CustomObjectsApi()


def _load_kube_config(config) -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


def _mcp_tools_list(endpoint: str) -> list[dict]:
    """POST a JSON-RPC `tools/list` to an MCP endpoint and return the tool manifest list.
    Each tool is a dict with `name` and (optional) `description`."""
    import requests

    resp = requests.post(
        endpoint,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return (resp.json().get("result") or {}).get("tools", [])


def _run_upstream(fn):
    """Run an upstream call with bounded retries (UPSTREAM_MAX_RETRIES), reraising the last
    error so the caller can convert it to a 502."""
    retryer = Retrying(
        stop=stop_after_attempt(int(os.getenv("UPSTREAM_MAX_RETRIES", "3"))),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retryer(fn)


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
    """Resolve identity and determine service type from the operator's `kagenti.io/type`
    pod label (authoritative — not the entity_id format)."""
    service_id = state.trigger.entity_id

    try:
        service = _run_upstream(lambda: _config().get_service(service_id))
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
        pods = _run_upstream(lambda: _core_v1().list_namespaced_pod(namespace).items)
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
    """Derive an agent's roles + scopes from its AgentCard CR (non-LLM). Falls back to a
    default access scope for legacy deployments with no AgentCard."""
    namespace, workload = state.namespace, state.workload_name
    role = RoleDefinition(name=f"{workload}.agent", description="Agent role")

    try:
        resp = _run_upstream(
            lambda: _custom_objects().list_namespaced_custom_object(
                group=_AGENTCARD_GROUP,
                version=_AGENTCARD_VERSION,
                namespace=namespace,
                plural=_AGENTCARD_PLURAL,
            )
        )
    except Exception as e:
        raise HTTPException(502, f"Kubernetes AgentCard LIST failed in namespace {namespace!r}: {e}")

    card = next(
        (c for c in resp.get("items", []) if (c.get("metadata") or {}).get("name") == workload),
        None,
    )
    if card is None:
        provision = ServiceProvision(
            roles=[role],
            scopes=[ScopeDefinition(name=f"{workload}.access", description="Default access scope")],
            reasoning="partial: no AgentCard found, default scope assigned",
        )
        return {"service_provision": provision}

    skills = (card.get("spec") or {}).get("skills", [])
    scopes = [
        ScopeDefinition(name=f"{workload}.{s['name']}", description=s.get("description", ""))
        for s in skills
    ]
    provision = ServiceProvision(
        roles=[role],
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
        svc = _run_upstream(lambda: _core_v1().read_namespaced_service(workload, namespace))
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

    port = svc.spec.ports[0].port
    endpoint = f"http://{workload}.{namespace}.svc.cluster.local:{port}/mcp"

    try:
        tools = _run_upstream(lambda: _mcp_tools_list(endpoint))
    except Exception as e:
        raise HTTPException(502, f"MCP tools/list failed at {endpoint}: {e}")

    scopes = [
        ScopeDefinition(name=f"{workload}.{t['name']}", description=t.get("description", ""))
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
            _run_upstream(lambda role=role: config.create_service_role(service_id, role))
        for scope in provision.scopes:
            _run_upstream(lambda scope=scope: config.create_service_scope(service_id, scope))
        service = _run_upstream(lambda: config.get_service(service_id))
        _run_upstream(lambda: config.set_service_type(service, state.service_type))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"IdP Configuration Service unavailable provisioning {service_id!r}: {e}")

    return {"service_provision": provision, "service_type": state.service_type}
