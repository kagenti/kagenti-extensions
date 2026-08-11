#!/usr/bin/env bash
# deploy-fleet.sh — plug & play. Stand up the adapted app fleet from fleet.yaml so
# the whole system produces correct per-request lineage forests, in one command.
# (fleet-read.py validates the catalog and emits the rows — a malformed stanza
# refuses to deploy; requires python3 + PyYAML.)
#
# For each catalog app it: (1) gets the base image (pull ghcr or podman-build local)
# and kind-loads it, (2) builds the propagate-only OTEL shim on top (build-otel-shim.sh),
# (3) applies any per-app overlay fix, (4) deploys app+sidecar (attach-lineage.sh) —
# TOOLS BEFORE AGENTS so an agent's MCP_URL resolves.
#
# Usage:
#   ./deploy-fleet.sh                 # whole fleet
#   ./deploy-fleet.sh slack-tool slack-researcher   # only these (deps: deploy tools too)
#   SKIP_BUILD=1 ./deploy-fleet.sh    # skip image prep, just (re)apply manifests
#
# Env:
#   NAMESPACE       target namespace (default team1)
#   WORKSPACE_ROOT  dir holding the sibling clones (default: auto — 4 levels up).
#                   `local:` images are built from <WORKSPACE_ROOT>/<path>.
#   SKIP_BUILD=1    don't pull/build images or shims (deploy-only)
#   SKIP_OLLAMA=1   don't attempt the Marvin model alias
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/container-runtime.sh"
NAMESPACE="${NAMESPACE:-team1}"
# lineage-adapter -> demos -> authbridge -> kagenti-extensions-snp -> workspace root
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
CATALOG="${SCRIPT_DIR}/fleet.yaml"
SKIP_BUILD="${SKIP_BUILD:-0}"
SELECT=("$@")
command -v python3 >/dev/null || { echo "error: python3 not on PATH (fleet-read.py needs it)" >&2; exit 1; }

log() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }
selected() {  # $1=name -> 0 if in SELECT (or SELECT empty)
  [ "${#SELECT[@]}" -eq 0 ] && return 0
  local n; for n in "${SELECT[@]}"; do [ "$n" = "$1" ] && return 0; done; return 1
}
base_name_of() {  # image spec -> base image name
  case "$1" in
    ghcr:*)  echo "${1#ghcr:}" ;;
    local:*) local p="${1#local:}"; echo "${p##*/}" ;;
    kit:*)   local p="${1#kit:}"; echo "${p##*/}" ;;
    pull:*)  local p="${1#pull:}"; echo "${p%%:*}" ;;   # pull:redis:7 -> redis
    *) echo "ERR"; return 1 ;;
  esac
}

# read validated catalog rows (12 pipe fields — fleet-read.py's private interface)
rows=()
while IFS= read -r line; do
  rows+=("$line")
done < <(python3 "${SCRIPT_DIR}/fleet-read.py" "$CATALOG")

# ---- Marvin model alias (best-effort) ----
if [ "${SKIP_OLLAMA:-0}" != "1" ] && command -v ollama >/dev/null 2>&1; then
  ollama cp qwen2.5:7b gpt-4o-mini >/dev/null 2>&1 || true
fi

# ---- phase 1: prepare images (unique base image per row; overlay per row) ----
if [ "$SKIP_BUILD" != "1" ]; then
  declare -a built=()
  for line in "${rows[@]}"; do
    IFS='|' read -r name role image entrypoint self_id port svcport env overlay _ _ _ <<< "$line"
    selected "$name" || continue
    bn="$(base_name_of "$image")"
    # raw rows deploy a stock image untouched — pull + load, NO shim, NO overlay
    if [ "$role" = "raw" ]; then
      if ! printf '%s\n' "${built[@]:-}" | grep -qx "$bn"; then
        ref="docker.io/library/${image#pull:}"
        log "prepare raw image: $ref"
        "$CONTAINER_TOOL" pull "$ref"
        kind_load "$ref"
        built+=("$bn")
      fi
      continue
    fi
    if ! printf '%s\n' "${built[@]:-}" | grep -qx "$bn"; then
      log "prepare base image: $bn ($image)"
      case "$image" in
        ghcr:*)
          ref="ghcr.io/kagenti/agent-examples/${bn}:latest"
          "$CONTAINER_TOOL" pull "$ref"
          "$CONTAINER_TOOL" tag "$ref" "docker.io/library/${bn}:latest"
          kind_load "docker.io/library/${bn}:latest"
          ;;
        local:*|kit:*)
          # local: resolves beside this repo (the umbrella/workspace layout);
          # kit: resolves inside this directory — app sources that SHIP with
          # the kit (e.g. probe-app) and need no sibling clone.
          case "$image" in
            kit:*) ctx="${SCRIPT_DIR}/${image#kit:}" ;;
            *)     ctx="${WORKSPACE_ROOT}/${image#local:}" ;;
          esac
          [ -f "${ctx}/Dockerfile" ] || { echo "!! no Dockerfile at ${ctx}"; exit 1; }
          "$CONTAINER_TOOL" build -f "${ctx}/Dockerfile" -t "${bn}:latest" "${ctx}"
          "$CONTAINER_TOOL" tag "${bn}:latest" "docker.io/library/${bn}:latest"
          kind_load "docker.io/library/${bn}:latest"
          ;;
      esac
      "${SCRIPT_DIR}/build-otel-shim.sh" "${bn}:latest"
      built+=("$bn")
    fi
    # per-app overlay fix on top of the shim
    if [ "$overlay" != "-" ] && [ -n "$overlay" ]; then
      log "apply overlay $overlay onto ${bn}-otel"
      "$CONTAINER_TOOL" build -f "${SCRIPT_DIR}/${overlay}" -t "${bn}-otel:latest" "${SCRIPT_DIR}/$(dirname "$overlay")"
      "$CONTAINER_TOOL" tag "${bn}-otel:latest" "docker.io/library/${bn}-otel:latest"
      kind_load "docker.io/library/${bn}-otel:latest"
    fi
  done
fi

# ---- phase 2: deploy (raw infra, then tools, then agents) ----
# raw rows: a stock image as-is — no shim, no sidecar, no lineage. The honest
# statement "this pod is invisible to lineage today" as a deployment shape.
deploy_raw() {
  IFS='|' read -r name role image entrypoint self_id port svcport env overlay _ _ _ <<< "$1"
  local ref="docker.io/library/${image#pull:}"
  log "deploy raw: $name (image=$ref, no shim, no sidecar)"
  cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: Service
metadata:
  name: ${name}
  namespace: ${NAMESPACE}
  labels: { app.kubernetes.io/name: ${name} }
spec:
  ports: [{ name: tcp, port: ${svcport}, protocol: TCP, targetPort: ${port} }]
  selector: { app.kubernetes.io/name: ${name} }
  type: ClusterIP
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${name}
  namespace: ${NAMESPACE}
  labels: { app.kubernetes.io/name: ${name} }
spec:
  replicas: 1
  selector:
    matchLabels: { app.kubernetes.io/name: ${name} }
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${name}
        kagenti.io/inject: disabled
    spec:
      containers:
        - name: ${name}
          image: ${ref}
          imagePullPolicy: IfNotPresent
          ports: [{ containerPort: ${port} }]
          readinessProbe:
            tcpSocket: { port: ${port} }
            initialDelaySeconds: 2
            periodSeconds: 5
EOF
  kubectl rollout status -n "$NAMESPACE" "deploy/$name" --timeout=180s
}

deploy_row() {
  IFS='|' read -r name role image entrypoint self_id port svcport env overlay exclude pvc_name pvc_mount <<< "$1"
  local bn; bn="$(base_name_of "$image")"
  log "deploy $role: $name (self_id=$self_id, image=${bn}-otel)"
  NAME="$name" KAGENTI_TYPE="$role" \
  IMAGE="docker.io/library/${bn}-otel:latest" \
  APP_PORT="$port" SVC_PORT="$svcport" \
  APP_ENTRYPOINT="$entrypoint" SELF_ID="$self_id" \
  ENV_VARS="$env" NAMESPACE="$NAMESPACE" \
  OUTBOUND_PORTS_EXCLUDE="$exclude" \
  PVC_NAME="$pvc_name" PVC_MOUNT="$pvc_mount" \
  "${SCRIPT_DIR}/attach-lineage.sh" | kubectl apply -f -
  # A failed restart must fail the deploy: swallowing it lets the following
  # rollout-status green-light STALE pods (fleet says "deployed", runs old
  # images). First-ever apply has no pods to restart — that (and only that)
  # case is tolerated explicitly.
  if ! kubectl rollout restart -n "$NAMESPACE" "deploy/$name"; then
    echo "ERROR: rollout restart failed for deploy/$name" >&2
    exit 1
  fi
  kubectl rollout status -n "$NAMESPACE" "deploy/$name" --timeout=180s
}

for want in raw tool agent; do
  for line in "${rows[@]}"; do
    IFS='|' read -r name role _ <<< "$line"
    selected "$name" || continue
    [ "$role" = "$want" ] || continue
    if [ "$want" = "raw" ]; then deploy_raw "$line"; else deploy_row "$line"; fi
  done
done

log "fleet deployed to namespace $NAMESPACE. Verify with:  ./verify-fleet.sh"
