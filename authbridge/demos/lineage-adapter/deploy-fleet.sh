#!/usr/bin/env bash
# deploy-fleet.sh — plug & play. Stand up the adapted app fleet from fleet.conf so
# the whole system produces correct per-request lineage forests, in one command.
#
# For each catalog row it: (1) gets the base image (pull ghcr or podman-build local)
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
NAMESPACE="${NAMESPACE:-team1}"
# lineage-adapter -> demos -> authbridge -> kagenti-extensions-snp -> workspace root
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "$SCRIPT_DIR/../../../.." && pwd)}"
CATALOG="${SCRIPT_DIR}/fleet.conf"
SKIP_BUILD="${SKIP_BUILD:-0}"
SELECT=("$@")

log() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }
selected() {  # $1=name -> 0 if in SELECT (or SELECT empty)
  [ "${#SELECT[@]}" -eq 0 ] && return 0
  local n; for n in "${SELECT[@]}"; do [ "$n" = "$1" ] && return 0; done; return 1
}
base_name_of() {  # image spec -> base image name
  case "$1" in
    ghcr:*)  echo "${1#ghcr:}" ;;
    local:*) local p="${1#local:}"; echo "${p##*/}" ;;
    *) echo "ERR"; return 1 ;;
  esac
}

# read catalog rows (skip comments/blank)
rows=()
while IFS= read -r line; do
  [ -z "$line" ] && continue
  case "$line" in \#*) continue ;; esac
  rows+=("$line")
done < "$CATALOG"

# ---- Marvin model alias (best-effort) ----
if [ "${SKIP_OLLAMA:-0}" != "1" ] && command -v ollama >/dev/null 2>&1; then
  ollama cp qwen2.5:7b gpt-4o-mini >/dev/null 2>&1 || true
fi

# ---- phase 1: prepare images (unique base image per row; overlay per row) ----
if [ "$SKIP_BUILD" != "1" ]; then
  declare -a built=()
  for line in "${rows[@]}"; do
    IFS='|' read -r name role image entrypoint self_id port svcport env overlay <<< "$line"
    selected "$name" || continue
    bn="$(base_name_of "$image")"
    if ! printf '%s\n' "${built[@]:-}" | grep -qx "$bn"; then
      log "prepare base image: $bn ($image)"
      case "$image" in
        ghcr:*)
          ref="ghcr.io/kagenti/agent-examples/${bn}:latest"
          podman pull "$ref"
          podman tag "$ref" "docker.io/library/${bn}:latest"
          podman save -o "/tmp/${bn}.tar" "docker.io/library/${bn}:latest"
          KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "/tmp/${bn}.tar" --name kagenti
          ;;
        local:*)
          ctx="${WORKSPACE_ROOT}/${image#local:}"
          [ -f "${ctx}/Dockerfile" ] || { echo "!! no Dockerfile at ${ctx}"; exit 1; }
          podman build -f "${ctx}/Dockerfile" -t "${bn}:latest" "${ctx}"
          podman tag "${bn}:latest" "docker.io/library/${bn}:latest"
          podman save -o "/tmp/${bn}.tar" "docker.io/library/${bn}:latest"
          KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "/tmp/${bn}.tar" --name kagenti
          ;;
      esac
      "${SCRIPT_DIR}/build-otel-shim.sh" "${bn}:latest"
      built+=("$bn")
    fi
    # per-app overlay fix on top of the shim
    if [ "$overlay" != "-" ] && [ -n "$overlay" ]; then
      log "apply overlay $overlay onto ${bn}-otel"
      podman build -f "${SCRIPT_DIR}/${overlay}" -t "${bn}-otel:latest" "${SCRIPT_DIR}/$(dirname "$overlay")"
      podman tag "${bn}-otel:latest" "docker.io/library/${bn}-otel:latest"
      podman save -o "/tmp/${bn}-otel.tar" "docker.io/library/${bn}-otel:latest"
      KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "/tmp/${bn}-otel.tar" --name kagenti
    fi
  done
fi

# ---- phase 2: deploy (tools first, then agents) ----
deploy_row() {
  IFS='|' read -r name role image entrypoint self_id port svcport env overlay <<< "$1"
  local bn; bn="$(base_name_of "$image")"
  log "deploy $role: $name (self_id=$self_id, image=${bn}-otel)"
  NAME="$name" KAGENTI_TYPE="$role" \
  IMAGE="docker.io/library/${bn}-otel:latest" \
  APP_PORT="$port" SVC_PORT="$svcport" \
  APP_ENTRYPOINT="$entrypoint" SELF_ID="$self_id" \
  ENV_VARS="$env" NAMESPACE="$NAMESPACE" \
  "${SCRIPT_DIR}/attach-lineage.sh" | kubectl apply -f -
  kubectl rollout restart -n "$NAMESPACE" "deploy/$name" >/dev/null 2>&1 || true
  kubectl rollout status -n "$NAMESPACE" "deploy/$name" --timeout=180s
}

for want in tool agent; do
  for line in "${rows[@]}"; do
    IFS='|' read -r name role _ <<< "$line"
    selected "$name" || continue
    [ "$role" = "$want" ] || continue
    deploy_row "$line"
  done
done

log "fleet deployed to namespace $NAMESPACE. Verify with:  ./verify-fleet.sh"
