"""PDP Policy Writer (OPA) — always-on Custom Resource writer.

Renders the two fixed-package Rego strings via ``rego.py`` and writes them into
a per-agent ``AuthorizationPolicy`` Custom Resource (``agent.rossoctl.dev/
v1alpha1``, one CR per agent) on the live Kubernetes API. ``bundle-service``
(operator repo) composes those CRs into per-pod OPA bundles that AuthBridge polls.

The CR write is **always active** — it is never gated by an env var. Setting
``POLICY_WRITER_DUMP_REGO`` truthy *additionally* dumps the same rego to
``REGO_OUTPUT_DIR`` for local inspection; the toggle defaults off and never
disables, replaces, or gates the CR write.
"""

import os
import shutil
from pathlib import Path

from fastapi import FastAPI
from kubernetes import client, config
from kubernetes.client import ApiException
from starlette.responses import JSONResponse, Response

from aiac.pdp.service.policy.opa.rego import (
    generate_inbound_rego,
    generate_outbound_rego,
    identity_ref,
)
from aiac.policy.model.models import AgentPolicyModel, PolicyModel

# --------------------------------------------------------------------------- #
# CR coordinates & write identity — code constants, never env vars (Q6, Q8a). #
# --------------------------------------------------------------------------- #
_GROUP = "agent.rossoctl.dev"
_VERSION = "v1alpha1"
_PLURAL = "authorizationpolicies"
_MANAGED_BY_LABEL = {"app.kubernetes.io/managed-by": "aiac-pdp-policy-writer"}
_FIELD_MANAGER = "aiac-pdp-policy-writer"
# Selects only CRs this writer owns (used by delete-all and the label filter).
_MANAGED_BY_SELECTOR = "app.kubernetes.io/managed-by=aiac-pdp-policy-writer"

# Truthy spellings for the additive rego-dump toggle.
_TRUTHY = {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Kubernetes client — constructed at startup (incluster -> kubeconfig fallback)#
# --------------------------------------------------------------------------- #
def _load_kube_config() -> None:
    """Load in-cluster config, falling back to a local kubeconfig.

    Both failing (e.g. a unit-test / CI host with neither) is non-fatal: the
    client is still constructed and API calls surface as 502/503 until real
    config exists. This keeps the module importable everywhere.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        try:
            config.load_kube_config()
        except config.ConfigException:
            pass


_load_kube_config()
_api = client.CustomObjectsApi()


# --------------------------------------------------------------------------- #
# Env-derived config — read at call time so tests can toggle it per request.   #
# --------------------------------------------------------------------------- #
def get_output_dir() -> Path:
    """Local rego-dump destination (only consulted when the dump toggle is on)."""
    return Path(os.environ.get("REGO_OUTPUT_DIR", "/rego"))


def _dump_enabled() -> bool:
    """True when ``POLICY_WRITER_DUMP_REGO`` is truthy.

    Off by default. This gates the *additive* local-debug dump only — never the
    CR write.
    """
    return os.environ.get("POLICY_WRITER_DUMP_REGO", "").strip().lower() in _TRUTHY


def _platform_clients() -> tuple[str, ...]:
    """Platform source clients for the inbound generator's bypass rules (Q5).

    ``PLATFORM_SOURCE_CLIENTS`` comma-split, blanks dropped; unset or all-blank
    falls back to ``("rossoctl",)`` (dropping the bypass would deny end-user
    traffic, which carries the platform client).
    """
    raw = os.environ.get("PLATFORM_SOURCE_CLIENTS")
    if raw is None:
        return ("rossoctl",)
    clients = tuple(c.strip() for c in raw.split(",") if c.strip())
    return clients or ("rossoctl",)


# --------------------------------------------------------------------------- #
# CR body + write ops                                                          #
# --------------------------------------------------------------------------- #
def _build_cr(model: AgentPolicyModel) -> dict:
    """Build the per-agent ``AuthorizationPolicy`` CR body (Q6a).

    ``metadata.name`` / ``.namespace`` come from ``identity_ref(agent_id)``;
    ``spec.clientID`` is the DNS-label-safe ``name`` — display / print-column
    only, since bundle-service matches on name+namespace, never ``clientID``.
    Raises ``ValueError`` (via ``identity_ref``) on a malformed ``agent_id``.
    """
    namespace, name = identity_ref(model.agent_id)
    return {
        "apiVersion": f"{_GROUP}/{_VERSION}",
        "kind": "AuthorizationPolicy",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(_MANAGED_BY_LABEL),
        },
        "spec": {
            "scope": "client",
            "clientID": name,
            "policies": [
                {
                    "path": "inbound/request.rego",
                    "content": generate_inbound_rego(
                        model, platform_clients=_platform_clients()
                    ),
                },
                {
                    "path": "outbound/request.rego",
                    "content": generate_outbound_rego(model),
                },
            ],
        },
    }


def _dump_cr(namespace: str, name: str, body: dict) -> None:
    """Additive local dump: write each policy under ``<out>/<ns>/<name>/<path>``.

    Mirrors the CR ``policies[].path`` so on-disk output equals CR content. Any
    ``OSError`` propagates (mapped to 502 upstream) — a broken debug mount should
    surface, not silently drop files.
    """
    base = get_output_dir() / namespace / name
    for policy in body["spec"]["policies"]:
        dest = base / policy["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(policy["content"])


def _upsert_agent(model: AgentPolicyModel) -> None:
    """Server-side-apply the agent's CR (idempotent), then dump if enabled (Q6b)."""
    namespace, name = identity_ref(model.agent_id)
    body = _build_cr(model)
    _api.patch_namespaced_custom_object(
        group=_GROUP,
        version=_VERSION,
        namespace=namespace,
        plural=_PLURAL,
        name=name,
        body=body,
        field_manager=_FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )
    if _dump_enabled():
        _dump_cr(namespace, name, body)


def _delete_agent(agent_id: str) -> None:
    """Delete the agent's CR (idempotent: k8s 404 == success), then dump-clear (Q6c)."""
    namespace, name = identity_ref(agent_id)
    try:
        _api.delete_namespaced_custom_object(
            group=_GROUP,
            version=_VERSION,
            namespace=namespace,
            plural=_PLURAL,
            name=name,
        )
    except ApiException as e:
        if e.status != 404:
            raise
    if _dump_enabled():
        shutil.rmtree(get_output_dir() / namespace / name, ignore_errors=True)


def _delete_all() -> None:
    """Delete every CR carrying the managed-by label, cluster-wide (Q6c).

    A per-item 404 (a concurrent delete race) is tolerated; other API failures
    propagate (mapped to 502). If the dump is on, clear the dumped tree too.
    """
    listing = _api.list_cluster_custom_object(
        _GROUP, _VERSION, _PLURAL, label_selector=_MANAGED_BY_SELECTOR
    )
    for item in listing.get("items", []):
        meta = item["metadata"]
        try:
            _api.delete_namespaced_custom_object(
                group=_GROUP,
                version=_VERSION,
                namespace=meta["namespace"],
                plural=_PLURAL,
                name=meta["name"],
            )
        except ApiException as e:
            if e.status != 404:
                raise
    if _dump_enabled():
        # Clear the dumped tree's contents without removing the mount point itself.
        for child in get_output_dir().glob("*"):
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)


def _run_write(op) -> Response:
    """Run a write op, mapping failures to HTTP responses.

    - ``ValueError`` — a malformed / namespace-less ``agent_id`` from
      ``identity_ref`` — maps to **400** (its message names the bad id).
    - ``ApiException`` — a Kubernetes API failure — maps to **502**.
    - ``OSError`` — the additive rego dump's filesystem write — maps to **502**.

    400 is reserved for a malformed ``agent_id``; 502 strictly for Kubernetes
    API failures and the additive dump. The two are never conflated.
    """
    try:
        op()
        return Response(status_code=204)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except ApiException as e:
        return JSONResponse(status_code=502, content={"error": str(e)})
    except OSError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
app = FastAPI()


@app.post("/policy", status_code=204)
def upsert_policy(policy: PolicyModel):
    def _op():
        # A malformed agent_id aborts the batch with a 400 naming it; agents
        # already applied before that point stay written (no rollback). The
        # batch is also unbounded in size. This partial-write semantic is
        # acceptable for the current caller (store-derived, pre-validated ids;
        # SSA is idempotent so a retry re-applies the whole set) and is tracked
        # for hardening in s-and-p-team/cortex#143.
        for agent in policy.agents:
            _upsert_agent(agent)

    return _run_write(_op)


@app.post("/policy/agents/{agent_id}", status_code=204)
def upsert_agent(agent_id: str, model: AgentPolicyModel):
    # The ``{agent_id}`` path segment is intentionally ignored: the request
    # body is authoritative. ``_upsert_agent`` derives namespace/name from
    # ``model.agent_id`` (via ``identity_ref``), the single source of truth, so
    # a mismatched or placeholder URL segment (e.g. ``/policy/agents/ignored``)
    # never affects which CR is written. The segment is kept only to give the
    # route a RESTful shape.
    return _run_write(lambda: _upsert_agent(model))


@app.delete("/policy/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: str):
    return _run_write(lambda: _delete_agent(agent_id))


@app.delete("/policy", status_code=204)
def delete_all():
    return _run_write(_delete_all)


@app.get("/health")
def health():
    # A bounded cluster-wide list proves the API is reachable and the CRD is
    # served. An empty list is success; any failure (unreachable API, RBAC
    # forbidden) is 503. The dump dir is not part of this signal.
    try:
        _api.list_cluster_custom_object(_GROUP, _VERSION, _PLURAL, limit=1)
        return {"status": "ok"}
    except Exception as e:
        # Any failure — unreachable API, RBAC-forbidden, CRD not served — means
        # the writer cannot serve, so it is reported as unavailable.
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "error": str(e)},
        )
