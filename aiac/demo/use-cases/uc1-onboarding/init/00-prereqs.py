#!/usr/bin/env python3
"""Verify (and, where safe, install) everything the UC-1 onboarding demo needs before ``make
setup`` touches Keycloak. Two classes of check, per the handoff:

  1. VERIFY ONLY, else abort with a pointer to the installer: cluster reachable, the
     ``agentruntimes``/``agentcards`` CRDs, Keycloak reachable, namespace ``team1``, SPIRE.
  2. VERIFY AND INSTALL IF ABSENT: the four AIAC images (build + kind load + apply, in dependency
     order), then the demo workloads via ``demo/assets/install.sh`` (not reimplemented here).

Then the real readiness condition: poll Keycloak until both the ``team1/github-agent`` and
``team1/github-tool`` clients exist (registration is async — "rollout complete" is not "ready to
onboard"), and assert the tool Service carries the ``protocol.rossoctl.io/mcp`` LABEL (the handoff's
own spec text calls it an annotation; it is not).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import scenario as scn
from _lib import Config, abort, kubectl, kubectl_get_json, kubectl_rollout_status, load_config, note, ok, rule, say

HERE = Path(__file__).resolve().parent
AIAC_ROOT = HERE.parents[3]  # demo/use-cases/uc1-onboarding/init/ -> aiac/
ASSETS_DIR = HERE.parents[2] / "assets"  # demo/assets/

AIAC_NAMESPACE = "aiac-system"
AIAC_IMAGES = [
    ("localhost/aiac-pdp-config:local", "src/aiac/idp/service/configuration/keycloak/Dockerfile", "src/aiac/idp/service/configuration/keycloak"),
    ("localhost/aiac-pdp-policy-opa:local", "src/aiac/pdp/service/policy/opa/Dockerfile", "src"),
    ("localhost/aiac-policy-model-store:local", "src/aiac/policy/model_store/service/Dockerfile", "src"),
    ("localhost/aiac-agent:local", "src/aiac/agent/controller/Dockerfile", "src"),
]
AIAC_MANIFESTS = ["pdp-interface-deployment.yaml", "policy-model-store-statefulset.yaml", "agent-deployment.yaml"]


# --- class 1: verify only ---------------------------------------------------------------------


def verify_cluster_reachable() -> None:
    try:
        kubectl("cluster-info", timeout=15)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        abort("kubectl cannot reach a cluster. Set up a rossoctl/Kind cluster first (see the Rossoctl installer).")
    ok("cluster reachable")


def verify_crds() -> None:
    for crd in ("agentruntimes.agent.rossoctl.dev", "agentcards.agent.rossoctl.dev"):
        try:
            kubectl("get", "crd", crd, timeout=15)
        except subprocess.CalledProcessError:
            abort(f"CRD {crd!r} not found — is the rossoctl-operator installed?")
    ok("agentruntimes/agentcards CRDs present")


def verify_namespace(namespace: str) -> None:
    try:
        kubectl("get", "namespace", namespace, timeout=15)
    except subprocess.CalledProcessError:
        abort(f"namespace {namespace!r} does not exist — run the Rossoctl installer first.")
    ok(f"namespace {namespace!r} exists")


def verify_spire() -> None:
    out = kubectl(
        "get", "pods", "-A", "-l", "app.kubernetes.io/name=agent,app.kubernetes.io/instance=spire",
        "-o", "jsonpath={.items[*].status.phase}", timeout=15,
    )
    if not out.split() or any(phase != "Running" for phase in out.split()):
        abort(f"SPIRE agent not Running (got phases: {out or '<none>'}) — is SPIRE installed?")
    ok("SPIRE agent Running")


def verify_keycloak(cfg: Config) -> None:
    import requests

    try:
        resp = requests.get(f"{cfg.keycloak_url}/realms/master/.well-known/openid-configuration", timeout=10)
    except requests.RequestException as exc:
        abort(f"Keycloak unreachable at {cfg.keycloak_url}: {exc}")
    if resp.status_code != 200:
        abort(f"Keycloak at {cfg.keycloak_url} returned HTTP {resp.status_code}")
    ok(f"Keycloak reachable at {cfg.keycloak_url}")


# --- class 2: verify and install if absent ------------------------------------------------------


def _image_present(image: str) -> bool:
    for runtime in ("podman", "docker"):
        try:
            subprocess.run([runtime, "image", "inspect", image], capture_output=True, timeout=15, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False


def _container_runtime() -> str:
    for runtime in ("podman", "docker"):
        if subprocess.run(["which", runtime], capture_output=True).returncode == 0:
            return runtime
    abort("neither podman nor docker found on PATH")


def ensure_aiac_deployed(cfg: Config) -> None:
    try:
        deployments = kubectl_get_json("deployments", namespace=AIAC_NAMESPACE)
        names = {d["metadata"]["name"] for d in deployments.get("items", [])}
    except subprocess.CalledProcessError:
        names = set()

    if {"aiac-interface", "aiac-agent"} <= names:
        note(f"AIAC stack already deployed in {AIAC_NAMESPACE!r} — skipping build/apply")
        return

    note("AIAC stack not found — building images, loading into kind, and applying manifests")
    runtime = _container_runtime()
    cluster_name = "rossoctl"

    for image, dockerfile, context in AIAC_IMAGES:
        if _image_present(image):
            note(f"{image} already present locally, skipping build")
        else:
            subprocess.run([runtime, "build", "-f", dockerfile, "-t", image, context], cwd=AIAC_ROOT, check=True)
        subprocess.run(["kind", "load", "docker-image", image, "--name", cluster_name], check=True)

    for manifest in AIAC_MANIFESTS:
        kubectl("apply", "-f", str(AIAC_ROOT / "k8s" / manifest))

    kubectl("wait", "deployment/aiac-interface", "-n", AIAC_NAMESPACE, "--for=condition=Available", "--timeout=120s")
    kubectl("wait", "statefulset/aiac-policy-model-store", "-n", AIAC_NAMESPACE, "--for=jsonpath={.status.readyReplicas}=1", "--timeout=120s")
    kubectl("wait", "deployment/aiac-agent", "-n", AIAC_NAMESPACE, "--for=condition=Available", "--timeout=120s")
    ok("AIAC stack deployed")


def ensure_workloads_deployed(namespace: str) -> None:
    try:
        deployments = kubectl_get_json("deployments", namespace=namespace)
        names = {d["metadata"]["name"] for d in deployments.get("items", [])}
    except subprocess.CalledProcessError:
        names = set()

    if {scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD} <= names:
        note(f"demo workloads already deployed in {namespace!r} — skipping install.sh")
        # A Deployment object existing is not the same as it being available; a partial or failed
        # prior install would otherwise skip repair and fail later at client registration. Wait for
        # both to roll out so "already deployed" also means "actually up".
        for workload in (scn.AGENT_WORKLOAD, scn.TOOL_WORKLOAD):
            kubectl_rollout_status(f"deployment/{workload}", namespace=namespace)
        return

    note("demo workloads not found — running demo/assets/install.sh")
    subprocess.run(["bash", str(ASSETS_DIR / "install.sh")], env={**os.environ, "NAMESPACE": namespace}, check=True)
    ok("demo workloads deployed")


# --- the real readiness condition: async Keycloak client registration -------------------------


def wait_for_client_registration(cfg: Config, timeout: float = 180.0) -> None:
    from _lib import connect_admin

    admin = connect_admin(cfg)
    admin.change_current_realm(cfg.realm)
    wanted = {f"{cfg.namespace}/{scn.AGENT_WORKLOAD}", f"{cfg.namespace}/{scn.TOOL_WORKLOAD}"}
    deadline = time.time() + timeout
    while time.time() < deadline:
        names = {c.get("name") for c in admin.get_clients()}
        if wanted <= names:
            ok(f"both clients registered: {sorted(wanted)}")
            return
        missing = wanted - names
        note(f"waiting on Keycloak client registration (missing: {sorted(missing)}) ...")
        time.sleep(5)
    abort(f"clients {sorted(wanted - names)} never registered within {timeout}s — check the operator's webhook logs.")


def verify_mcp_label(namespace: str) -> None:
    label = kubectl(
        "get", "service", scn.TOOL_WORKLOAD, "-n", namespace,
        "-o", "jsonpath={.metadata.labels.protocol\\.rossoctl\\.io/mcp}",
    ).strip()
    if label != "true":
        abort(
            f"Service {scn.TOOL_WORKLOAD!r} in {namespace!r} is missing the "
            f"protocol.rossoctl.io/mcp='true' LABEL (found: {label!r}) — UC-1's analyze_tool will "
            f"502 during onboarding. See demo/assets/INSTALL.md."
        )
    ok(f"Service {scn.TOOL_WORKLOAD!r} carries protocol.rossoctl.io/mcp='true'")


def main() -> None:
    cfg = load_config()

    say("1", "4", "Verify: cluster + CRDs + namespace + SPIRE + Keycloak")
    verify_cluster_reachable()
    verify_crds()
    verify_namespace(cfg.namespace)
    verify_spire()
    verify_keycloak(cfg)

    say("2", "4", "Verify/install: AIAC stack")
    ensure_aiac_deployed(cfg)

    say("3", "4", "Verify/install: demo workloads (github-agent, github-tool)")
    ensure_workloads_deployed(cfg.namespace)

    say("4", "4", "Wait: Keycloak client registration + MCP service label")
    wait_for_client_registration(cfg)
    verify_mcp_label(cfg.namespace)

    rule()
    print("All prerequisites satisfied.")


if __name__ == "__main__":
    main()
