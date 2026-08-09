# container-runtime.sh — shared container-runtime detection + kind image loading.
# Sourced (not executed) by build-otel-shim.sh / deploy-fleet.sh / stamp-ui-backend.sh.
#
# CONTAINER_TOOL: env override -> podman if on PATH -> docker. Podman is checked
# FIRST deliberately: on podman hosts a `docker` CLI is often a compat client to
# the podman socket (docker info reports the podman version), and picking it
# would route `kind load docker-image` at a podman-provider cluster — the exact
# breakage this helper exists to avoid. Docker-only hosts (e.g. WSL2) fall
# through to docker. Same env var name as lab-data-governance's
# deploy/build-and-load.sh, so one setting steers both kits.
#
# kind_load <image-ref>:
#   docker -> `kind load docker-image` (the direct path; works on Linux/WSL2).
#   podman -> save + `kind load image-archive` under KIND_EXPERIMENTAL_PROVIDER=podman
#             (the docker daemon is off and `kind load docker-image` misbehaves
#             under podman v5 — see RUNBOOK.md gotchas).
#
# KIND_CLUSTER: target kind cluster name (default kagenti).

CONTAINER_TOOL="${CONTAINER_TOOL:-}"
if [ -z "$CONTAINER_TOOL" ]; then
  if command -v podman >/dev/null 2>&1; then CONTAINER_TOOL=podman
  elif command -v docker >/dev/null 2>&1; then CONTAINER_TOOL=docker
  else echo "error: neither docker nor podman on PATH (set CONTAINER_TOOL)" >&2; exit 1; fi
fi
KIND_CLUSTER="${KIND_CLUSTER:-kagenti}"

kind_load() {
  local ref="$1"
  if [ "$CONTAINER_TOOL" = "podman" ]; then
    local base="${ref##*/}"
    local tar="/tmp/${base//:/-}.tar"
    podman save -o "$tar" "$ref"
    KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "$tar" --name "$KIND_CLUSTER"
  else
    kind load docker-image "$ref" --name "$KIND_CLUSTER"
  fi
}
