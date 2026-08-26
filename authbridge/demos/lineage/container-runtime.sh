# shellcheck shell=bash
# container-runtime.sh — shared container-runtime detection + kind image loading.
# Sourced (not executed), so it carries no shebang: build-otel-shim.sh sources it,
# and so may any script that needs to put an image into the kind cluster.
#
# CONTAINER_TOOL: env override -> podman if on PATH -> docker. Podman is checked
# FIRST deliberately: on podman hosts a `docker` CLI is often a compat client to
# the podman socket (docker info reports the podman version), and picking it
# would route `kind load docker-image` at a podman-provider cluster — the exact
# breakage this helper exists to avoid. Docker-only hosts (e.g. WSL2) fall
# through to docker.
#
# kind_load <image-ref>:
#   docker -> `kind load docker-image` (the direct path; works on Linux/WSL2).
#   podman -> save + `kind load image-archive` under KIND_EXPERIMENTAL_PROVIDER=podman
#             (the docker daemon is off and `kind load docker-image` misbehaves
#             under podman v5 — see README.md "Troubleshooting").
#
# KIND_CLUSTER_NAME: target kind cluster name (default rossoctl).

CONTAINER_TOOL="${CONTAINER_TOOL:-}"
if [ -z "$CONTAINER_TOOL" ]; then
  if command -v podman >/dev/null 2>&1; then CONTAINER_TOOL=podman
  elif command -v docker >/dev/null 2>&1; then CONTAINER_TOOL=docker
  else
    echo "error: neither docker nor podman on PATH (set CONTAINER_TOOL)" >&2
    # `return`, not `exit`: this file is sourced, and the sourcing script's
    # `set -e` turns the failed `.` into its own exit.
    return 1
  fi
fi
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-rossoctl}"

kind_load() {
  local ref="$1"
  if [ "$CONTAINER_TOOL" = "podman" ]; then
    local tar rc=0
    tar="$(mktemp "${TMPDIR:-/tmp}/kind-load.XXXXXX")"
    podman save -o "$tar" "$ref" \
      && KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "$tar" --name "$KIND_CLUSTER_NAME" \
      || rc=$?
    rm -f "$tar"   # on success and on failure alike; no trap, nothing lingers
    return "$rc"
  else
    kind load docker-image "$ref" --name "$KIND_CLUSTER_NAME"
  fi
}
