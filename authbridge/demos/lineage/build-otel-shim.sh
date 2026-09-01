#!/usr/bin/env bash
# build-otel-shim.sh — bake the propagate-only OTel shim onto a Python app
# image and load it into kind. Only the base image changes per app: the
# interpreter and uid:gid are DETECTED from the image, and the app's command is
# never touched — the shim activates through the environment
# (LINEAGE_PROPAGATE=1, see Dockerfile.otel-shim).
#
# Usage:
#   ./build-otel-shim.sh <base-image> [wrapper-tag] [venv-python] [app-uid[:gid]]
#   venv-python / app-uid are escape hatches for when detection picks wrong;
#   an explicit value is used as-is.
#
# Env:
#   FORCE_BAKE=1     override the refuse-to-bake interlock
#   SELF_ACTIVATE=1  bake LINEAGE_PROPAGATE=1 INTO the image — for workloads
#                    whose Deployment you cannot edit. Default images stay
#                    inert and are activated by the Deployment env.
#   NO_KIND_LOAD=1   build + attest only (CI / offline)
#
# Every probe runs the (unaudited) app image with --network=none.
# container-runtime.sh picks podman or docker (override: CONTAINER_TOOL).
#
# Structure: main() at the bottom is the pipeline; each phase is a function
# and a failed phase exits (3 = image refused, 4 = attestation failed).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/container-runtime.sh"

parse_args() {
  BASE_IMAGE="${1:?usage: build-otel-shim.sh <base-image> [wrapper-tag] [venv-python] [app-uid[:gid]]}"
  # default wrapper tag: <base name>-otel:latest
  local base_short base_name
  base_short="${BASE_IMAGE##*/}"; base_name="${base_short%%:*}"
  WRAPPER_TAG="${2:-${base_name}-otel:latest}"
  case "${WRAPPER_TAG##*/}" in *:*) ;; *) WRAPPER_TAG="${WRAPPER_TAG}:latest" ;; esac
  VENV_PYTHON="${3:-}"
  APP_UID="${4:-}"
  APP_GID="${APP_UID#*:}"; [ "$APP_GID" = "$APP_UID" ] && APP_GID=""
  APP_UID="${APP_UID%%:*}"
  FORCE_BAKE="${FORCE_BAKE:-0}"
  SELF_ACTIVATE="${SELF_ACTIVATE:-0}"
  NO_KIND_LOAD="${NO_KIND_LOAD:-0}"

  # a bare name resolves under docker.io/library/
  case "$BASE_IMAGE" in
    */*) base_ref="$BASE_IMAGE" ;;
    *)   base_ref="docker.io/library/${BASE_IMAGE}" ;;
  esac
}

# The two per-image build inputs are IN the image — probe it, never transcribe.
runs_python() {  # $1 = candidate interpreter path/name
  "$CONTAINER_TOOL" run --rm --network=none --entrypoint "$1" "$base_ref" -c 'import sys' >/dev/null 2>&1
}

detect_python() {  # sets VENV_PYTHON (validates it when given explicitly)
  if [ -z "$VENV_PYTHON" ]; then
    # The env the image declares, then the common venv layouts, then PATH.
    local candidates virtual_env c
    candidates=()
    virtual_env="$("$CONTAINER_TOOL" inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$base_ref" \
      | sed -n 's/^VIRTUAL_ENV=//p' | head -1)"
    [ -n "$virtual_env" ] && candidates+=("${virtual_env}/bin/python")
    candidates+=(/app/.venv/bin/python /opt/venv/bin/python python3)
    for c in "${candidates[@]}"; do
      if runs_python "$c"; then VENV_PYTHON="$c"; break; fi
    done
    if [ -z "$VENV_PYTHON" ]; then
      echo "REFUSING to bake ${base_ref}: no runnable Python found." >&2
      echo "  Probed: ${candidates[*]}" >&2
      echo "  The image is outside the shim envelope (non-Python, or an unusual layout)." >&2
      echo "  -> pass the interpreter explicitly as arg 3, or attach the sidecar only:" >&2
      echo "     DEPLOY=<deployment> ./sidecar-patch.sh   (still captures every HTTP hop," >&2
      echo "     but pairing under concurrency needs the app to propagate on its own)" >&2
      exit 3
    fi
    echo ">> detected app python: ${VENV_PYTHON}"
  elif ! runs_python "$VENV_PYTHON"; then
    echo "REFUSING to bake ${base_ref}: no runnable Python at ${VENV_PYTHON} (explicit arg)." >&2
    exit 3
  fi
}

detect_user() {  # sets APP_UID and APP_GID (either may be given explicitly)
  local config_user_full=""
  if [ -z "$APP_UID" ]; then
    # Exactly the user the base declared (root when empty); a named user is
    # resolved to its uid inside the image.
    local config_user
    config_user_full="$("$CONTAINER_TOOL" inspect --format '{{.Config.User}}' "$base_ref")"
    config_user="${config_user_full%%:*}"
    case "$config_user" in
      "")       APP_UID=0 ;;
      *[!0-9]*) APP_UID="$("$CONTAINER_TOOL" run --rm --network=none --entrypoint id "$base_ref" -u 2>/dev/null)" || {
                  echo "REFUSING to bake ${base_ref}: cannot resolve user '${config_user}' to a uid" >&2
                  echo "  (no 'id' binary in the image?) -> pass the uid explicitly as arg 4." >&2
                  exit 3
                } ;;
      *)        APP_UID="$config_user" ;;
    esac
    echo ">> detected app user: uid=${APP_UID} (Config.User='${config_user_full:-<root>}')"
  fi
  if [ -z "$APP_GID" ]; then
    # An explicit `USER uid:gid`, else the primary gid the runtime gives that
    # user (0 without a passwd entry — which is what the base ran as).
    case "$config_user_full" in
      *:*) APP_GID="${config_user_full#*:}" ;;
      *)   APP_GID="$("$CONTAINER_TOOL" run --rm --network=none --entrypoint id "$base_ref" -g 2>/dev/null)" || APP_GID=0 ;;
    esac
    echo ">> detected app group: gid=${APP_GID}"
  fi
}

# The interlock asks exactly "would wrapping DOUBLE-instrument?": is any of the
# seven instrumentors this shim installs already present? Not the
# `opentelemetry.instrumentation` namespace (a transitive dep of anything
# OTel-adjacent, no library instrumentation in it) and not a dormant SDK
# (a2a-sdk ships one on every stock agent) — neither is a refusal signal. An
# app that activates its SDK in code is not statically detectable.
refuse_already_instrumented() {
  [ "$FORCE_BAKE" = "1" ] && return 0
  # keep in sync with Dockerfile.otel-shim's install RUN (all but -distro)
  local already
  if already=$("$CONTAINER_TOOL" run --rm --network=none --entrypoint "$VENV_PYTHON" "$base_ref" -c '
import importlib.util as u
mods = ["starlette", "asgi", "fastapi", "httpx", "requests", "aiohttp_client", "threading"]
found = [m for m in mods if u.find_spec("opentelemetry.instrumentation." + m)]
print(",".join(found))
raise SystemExit(0 if found else 1)' 2>/dev/null); then
    echo "REFUSING to bake ${base_ref}: it already instruments ${already}" >&2
    echo "  Those are instrumentors this shim installs, so wrapping would stack a" >&2
    echo "  second one on the same library (an -otel image, or an app that bundles" >&2
    echo "  its own instrumentation)." >&2
    echo "  -> if this is a stock app image, point me at the un-shimmed base." >&2
    echo "  -> FORCE_BAKE=1 overrides if you know the wrap is safe." >&2
    exit 3
  fi
  # An already-baked -otel image carries the hook; refuse to bake it twice.
  if "$CONTAINER_TOOL" run --rm --network=none --entrypoint "$VENV_PYTHON" "$base_ref" -c '
import importlib.util as u
raise SystemExit(0 if u.find_spec("_lineage_propagate") else 1)' >/dev/null 2>&1; then
    echo "REFUSING to bake ${base_ref}: it already carries the lineage propagate hook" >&2
    echo "  (an already-baked -otel image). -> point me at the un-shimmed base." >&2
    echo "  -> FORCE_BAKE=1 overrides if you know the wrap is safe." >&2
    exit 3
  fi
}

build_image() {
  echo ">> building shim ${WRAPPER_TAG} FROM ${base_ref} (${CONTAINER_TOOL}, python=${VENV_PYTHON}, uid=${APP_UID})"
  "$CONTAINER_TOOL" build -f "${SCRIPT_DIR}/Dockerfile.otel-shim" \
    --build-arg "BASE_IMAGE=${base_ref}" \
    --build-arg "VENV_PYTHON=${VENV_PYTHON}" \
    --build-arg "APP_UID=${APP_UID}" \
    --build-arg "APP_GID=${APP_GID}" \
    -t "${WRAPPER_TAG}" "${SCRIPT_DIR}"
}

# Attestation: the bake proves itself before anything is loaded. Gate off, not
# one opentelemetry module may load; gate on, the hook ran, the exporter
# selection is explicit, and the propagator injects a traceparent.
verify_inert() {
  if ! "$CONTAINER_TOOL" run --rm --network=none --entrypoint "$VENV_PYTHON" "$WRAPPER_TAG" -c '
import sys
loaded = sorted(m for m in sys.modules if m.startswith("opentelemetry"))
raise SystemExit("gate off, yet otel loaded: %s" % loaded if loaded else 0)'; then
    echo "ATTESTATION FAILED for ${WRAPPER_TAG}: image is not inert with the gate off." >&2
    exit 4
  fi
}

verify_propagates() {
  if ! "$CONTAINER_TOOL" run --rm --network=none -e LINEAGE_PROPAGATE=1 --entrypoint "$VENV_PYTHON" "$WRAPPER_TAG" -c '
import os, sys
assert "opentelemetry.instrumentation.auto_instrumentation" in sys.modules, "hook did not run"
assert os.environ.get("OTEL_TRACES_EXPORTER") is not None, "exporter selection not pinned"
from opentelemetry import trace
from opentelemetry.propagate import inject
with trace.get_tracer("attest").start_as_current_span("attest"):
    carrier = {}
    inject(carrier)
assert "traceparent" in carrier, "propagator injects nothing: %r" % carrier'; then
    echo "ATTESTATION FAILED for ${WRAPPER_TAG}: gate on, but the hook did not come up." >&2
    exit 4
  fi
}

self_activate() {  # optional: bake the activation in
  [ "$SELF_ACTIVATE" = "1" ] || return 0
  echo ">> baking LINEAGE_PROPAGATE=1 into ${WRAPPER_TAG} (SELF_ACTIVATE=1)"
  printf 'FROM %s\nENV LINEAGE_PROPAGATE=1\n' "$WRAPPER_TAG" \
    | "$CONTAINER_TOOL" build -t "$WRAPPER_TAG" -
}

publish() {
  # A bare tag is aliased under docker.io/library/ so kind resolves it;
  # a registry-qualified one is used as is.
  local alias_ref
  case "$WRAPPER_TAG" in
    */*) alias_ref="$WRAPPER_TAG" ;;
    *)   alias_ref="docker.io/library/${WRAPPER_TAG}"
         "$CONTAINER_TOOL" tag "${WRAPPER_TAG}" "${alias_ref}" ;;
  esac

  if [ "$NO_KIND_LOAD" = "1" ]; then
    echo ">> built + attested ${alias_ref} (kind load skipped: NO_KIND_LOAD=1)"
  else
    kind_load "${alias_ref}"
    echo ">> loaded ${alias_ref} into kind cluster ${KIND_CLUSTER_NAME}"
  fi
  if [ "$SELF_ACTIVATE" = "1" ]; then
    echo ">> NOTE: this image is SELF-ACTIVATING (LINEAGE_PROPAGATE=1 baked in) — any"
    echo ">>       deployment of it propagates. Meant for workloads whose Deployment you"
    echo ">>       cannot edit; everywhere else prefer the inert default."
  else
    echo ">> NOTE: the -otel image is INERT — it runs exactly like its base until a"
    echo ">>       Deployment sets LINEAGE_PROPAGATE=1 in the app container's env"
    echo ">>       (attach-lineage.sh does this). Deploy it without that env and you"
    echo ">>       simply get the base image's behavior: no propagation, and the trace"
    echo ">>       fragments at this pod, visibly (lineage.parent.source=wire)."
  fi
}

main() {
  parse_args "$@"               # args + env → globals
  detect_python                 # the app's interpreter, probed (or arg 3)
  detect_user                   # uid:gid, probed (or arg 4)
  refuse_already_instrumented   # would double-instrument, or already baked → exit 3
  build_image                   # Dockerfile.otel-shim with the detected build-args
  verify_inert                  # gate off: nothing OTel loads → else exit 4
  verify_propagates             # gate on: hook up, traceparent injected → else exit 4
  self_activate                 # SELF_ACTIVATE=1 only
  publish                       # alias, kind-load, print the NOTE
}
main "$@"
