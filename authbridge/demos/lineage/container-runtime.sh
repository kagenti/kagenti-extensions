# shellcheck shell=bash
# container-runtime.sh — sourced by build-otel-shim.sh: picks the container
# engine (CONTAINER_TOOL) and loads an image into kind the way that engine
# needs (kind_load). Everything else uses $CONTAINER_TOOL directly.
# KIND_CLUSTER_NAME: target cluster (default rossoctl).

# Podman is checked FIRST: on podman hosts `docker` is often a compat client,
# and `kind load docker-image` through it is exactly the breakage to avoid.
container_tool() {
  [ -n "${CONTAINER_TOOL:-}" ] && return 0
  if command -v podman >/dev/null 2>&1; then CONTAINER_TOOL=podman
  elif command -v docker >/dev/null 2>&1; then CONTAINER_TOOL=docker
  else
    echo "error: neither docker nor podman on PATH (set CONTAINER_TOOL)" >&2
    return 1
  fi
}

# `kind load docker-image` misbehaves under podman v5; save + image-archive.
kind_load_podman() {
  local ref="$1" tar rc=0
  tar="$(mktemp "${TMPDIR:-/tmp}/kind-load.XXXXXX")"
  podman save -o "$tar" "$ref" \
    && KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "$tar" --name "$KIND_CLUSTER_NAME" \
    || rc=$?
  rm -f "$tar"   # on success and failure alike
  return "$rc"
}

kind_load_docker() {
  kind load docker-image "$1" --name "$KIND_CLUSTER_NAME"
}

kind_load() { "kind_load_${CONTAINER_TOOL}" "$@"; }

# `return`, not `exit`: sourced — the caller's `set -e` handles it.
container_tool || return 1
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-rossoctl}"
