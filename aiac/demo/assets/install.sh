#!/usr/bin/env bash
# Idempotent installer for the demo/assets workloads (github-tool, github-agent) into a
# rossoctl/Kind cluster. See INSTALL.md for the manual steps this automates and the
# non-obvious invariants (MCP service label, async Keycloak registration, etc).
#
# This script does NOT wait for Keycloak client registration — that needs Keycloak
# credentials this script has no business holding. It belongs to whatever use-case demo
# consumes the client (e.g. the UC-1 onboarding demo's 00-prereqs.py). Do not "fix" that
# omission here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CLUSTER_NAME="${CLUSTER_NAME:-rossoctl}"
NAMESPACE="${NAMESPACE:-team1}"
# Tags must carry the localhost/ prefix to match the Deployment manifests' image refs
# (image: localhost/github-*:latest, imagePullPolicy: IfNotPresent). docker does not auto-prefix
# built tags, so a bare github-*:latest would load into the kind node under a different repository
# name and the IfNotPresent pods would try to pull localhost/github-*:latest → ImagePullBackOff.
TOOL_IMAGE="${TOOL_IMAGE:-localhost/github-tool:latest}"
AGENT_IMAGE="${AGENT_IMAGE:-localhost/github-agent:latest}"

DO_TOOL=1
DO_AGENT=1
REBUILD=0

for arg in "$@"; do
  case "$arg" in
    --agent-only) DO_TOOL=0 ;;
    --tool-only) DO_AGENT=0 ;;
    --rebuild) REBUILD=1 ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--agent-only|--tool-only] [--rebuild]" >&2
      exit 1
      ;;
  esac
done

log() { echo "[install.sh] $*" >&2; }

detect_runtime() {
  if command -v podman >/dev/null 2>&1; then
    echo podman
  elif command -v docker >/dev/null 2>&1; then
    echo docker
  else
    log "ERROR: neither podman nor docker found on PATH."
    exit 1
  fi
}

RUNTIME="${CONTAINER_RUNTIME:-$(detect_runtime)}"

preflight() {
  local missing=0
  for bin in kubectl kind "$RUNTIME"; do
    if ! command -v "$bin" >/dev/null 2>&1; then
      log "ERROR: required binary '$bin' not found on PATH."
      missing=1
    fi
  done
  [ "$missing" -eq 0 ] || exit 1

  if ! kubectl cluster-info >/dev/null 2>&1; then
    log "ERROR: kubectl cannot reach a cluster. Is your kubeconfig pointing at '$CLUSTER_NAME'?"
    exit 1
  fi

  if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
    log "ERROR: namespace '$NAMESPACE' does not exist."
    log "This script does not create cluster-owned resources — run the Rossoctl installer first."
    exit 1
  fi

  if ! kubectl get crd agentruntimes.agent.rossoctl.dev >/dev/null 2>&1; then
    log "ERROR: AgentRuntime CRD not found. Is the rossoctl-operator installed?"
    exit 1
  fi
}

image_exists() {
  "$RUNTIME" image exists "$1" >/dev/null 2>&1 || "$RUNTIME" image inspect "$1" >/dev/null 2>&1
}

load_image_to_kind() {
  local image="$1"
  # `kind load docker-image` shells out to the docker binary and does not work with podman;
  # for podman, save to an archive and use `kind load image-archive` instead.
  if [ "$RUNTIME" = "podman" ]; then
    local tar_file
    tar_file="$(mktemp "${TMPDIR:-/tmp}/install-image.XXXXXX")"
    trap 'rm -f "$tar_file"' RETURN
    "$RUNTIME" save "$image" -o "$tar_file"
    kind load image-archive "$tar_file" --name "$CLUSTER_NAME"
  else
    kind load docker-image "$image" --name "$CLUSTER_NAME"
  fi
}

build_and_load() {
  local image="$1" context="$2"
  if [ "$REBUILD" -eq 0 ] && image_exists "$image"; then
    log "Image '$image' already present locally, skipping build (pass --rebuild to force)."
  else
    log "Building '$image' from $context"
    "$RUNTIME" build -t "$image" "$context"
  fi
  log "Loading '$image' into kind cluster '$CLUSTER_NAME'"
  load_image_to_kind "$image"
}

install_tool() {
  local dir="$SCRIPT_DIR/tools/github_tool"
  build_and_load "$TOOL_IMAGE" "$dir"
  log "Applying tool manifests"
  kubectl apply -n "$NAMESPACE" -f "$dir/k8s/github-tool-deployment.yaml"
  kubectl rollout status -n "$NAMESPACE" deployment/github-tool
}

install_agent() {
  local dir="$SCRIPT_DIR/agents/github_agent"
  build_and_load "$AGENT_IMAGE" "$dir"
  log "Applying agent configmaps"
  kubectl apply -n "$NAMESPACE" -f "$dir/k8s/configmaps.yaml"
  log "Applying agent manifests"
  kubectl apply -n "$NAMESPACE" -f "$dir/k8s/github-agent-deployment.yaml"
  kubectl rollout status -n "$NAMESPACE" deployment/github-agent
}

preflight

[ "$DO_TOOL" -eq 1 ] && install_tool
[ "$DO_AGENT" -eq 1 ] && install_agent

log "Done."
