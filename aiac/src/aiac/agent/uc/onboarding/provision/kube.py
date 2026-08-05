"""Kubernetes access seam for the Service Provision sub-agent (UC1).

Owns the `kubernetes` client seams (`_core_v1`, `_custom_objects`, `_load_kube_config`) and
exposes the small set of read operations the provision nodes need. Each operation wraps its
client call in ``run_upstream`` so transient API failures are retried at the transport
boundary — the nodes call these plainly and only map the final failure to
``HTTPException(502)``. Unit tests patch the ``_core_v1`` / ``_custom_objects`` seams here.
"""

from kubernetes import client, config

from aiac.shared.upstream import run_upstream

_AGENTCARD_GROUP = "agent.rossoctl.dev"
_AGENTCARD_VERSION = "v1alpha1"
_AGENTCARD_PLURAL = "agentcards"


# --------------------------------------------------------------------------- #
# Seams (patched in unit tests)                                                #
# --------------------------------------------------------------------------- #
def _load_kube_config() -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


def _core_v1():
    """CoreV1Api client (pods, services)."""
    _load_kube_config()
    return client.CoreV1Api()


def _custom_objects():
    """CustomObjectsApi client (AgentCard CRs)."""
    _load_kube_config()
    return client.CustomObjectsApi()


# --------------------------------------------------------------------------- #
# Retrying operations                                                          #
# --------------------------------------------------------------------------- #
def list_pods(namespace: str | None):
    """Pods in ``namespace`` (the ``.items`` list), with bounded transport retries."""
    return run_upstream(lambda: _core_v1().list_namespaced_pod(namespace).items)


def read_service(name: str | None, namespace: str | None):
    """A single Service by name, with bounded transport retries."""
    return run_upstream(lambda: _core_v1().read_namespaced_service(name, namespace))


def list_agentcards(namespace: str | None) -> dict:
    """List AgentCard CRs in ``namespace`` (raw dict response), with bounded transport retries."""
    return run_upstream(
        lambda: _custom_objects().list_namespaced_custom_object(
            group=_AGENTCARD_GROUP,
            version=_AGENTCARD_VERSION,
            namespace=namespace,
            plural=_AGENTCARD_PLURAL,
        )
    )
