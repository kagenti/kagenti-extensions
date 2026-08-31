# shellcheck shell=bash
# container-runtime.sh — shared container-runtime detection + kind image loading.
# Sourced (not executed), so it carries no shebang: build-otel-shim.sh sources it,
# and so may any script that needs to put an image into the kind cluster.
#
# Layout: one backend function per engine for the single operation whose
# mechanics differ (kind_load), a selector (container_tool) that resolves
# CONTAINER_TOOL, and a dispatcher. Everything else the demo runs is
# engine-agnostic and uses $CONTAINER_TOOL directly (build/run/inspect —
# identical CLI on both engines).
#
# KIND_CLUSTER_NAME: target kind cluster name (default rossoctl).

# CONTAINER_TOOL: env override -> podman if on PATH -> docker. Podman is checked
# FIRST deliberately: on podman hosts a `docker` CLI is often a compat client to
# the podman socket (docker info reports the podman version), and picking it
# would route `kind load docker-image` at a podman-provider cluster — the exact
# breakage this helper exists to avoid. Docker-only hosts (e.g. WSL2) fall
# through to docker.
container_tool() {
  [ -n "${CONTAINER_TOOL:-}" ] && return 0
  if command -v podman >/dev/null 2>&1; then CONTAINER_TOOL=podman
  elif command -v docker >/dev/null 2>&1; then CONTAINER_TOOL=docker
  else
    echo "error: neither docker nor podman on PATH (set CONTAINER_TOOL)" >&2
    return 1
  fi
}

# save + `kind load image-archive` under KIND_EXPERIMENTAL_PROVIDER=podman:
# the docker daemon is off and `kind load docker-image` misbehaves under
# podman v5 — see README.md "Troubleshooting".
kind_load_podman() {
  local ref="$1" tar rc=0
  tar="$(mktemp "${TMPDIR:-/tmp}/kind-load.XXXXXX")"
  podman save -o "$tar" "$ref" \
    && KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "$tar" --name "$KIND_CLUSTER_NAME" \
    || rc=$?
  rm -f "$tar"   # on success and on failure alike; no trap, nothing lingers
  return "$rc"
}

# The direct path; works on Linux/WSL2.
kind_load_docker() {
  kind load docker-image "$1" --name "$KIND_CLUSTER_NAME"
}

kind_load() { "kind_load_${CONTAINER_TOOL}" "$@"; }

# `return`, not `exit`, on failure: this file is sourced, and the sourcing
# script's `set -e` turns the failed `.` into its own exit.
container_tool || return 1
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-rossoctl}"
