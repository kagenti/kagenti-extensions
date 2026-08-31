#!/usr/bin/env bash
# Build the GENERALIZED propagate-only OTEL shim on top of any Python app image
# and load it into the kind cluster. Normally only the base image changes per
# app: the two build inputs a human used to transcribe (the app's python, the
# app's UID) are DETECTED from the image itself, and the app's command is not
# an input at all — the shim activates through the environment
# (LINEAGE_PROPAGATE=1, see Dockerfile.otel-shim), never by rewriting CMD.
#
# Usage:
#   ./build-otel-shim.sh <base-image> [wrapper-tag] [venv-python] [app-uid[:gid]]
#
#   venv-python / app-uid are ESCAPE HATCHES: pass them only when detection
#   picks the wrong interpreter or user (e.g. an image carrying two pythons).
#   An explicit value is used as-is, unprobed except by the interlock.
#
#   Every probe runs the (unaudited) app image with --network=none: the probes
#   need neither network nor a writable filesystem.
#
# Env:
#   FORCE_BAKE=1     override the refuse-to-bake interlock (see below)
#   SELF_ACTIVATE=1  bake LINEAGE_PROPAGATE=1 INTO the image, for workloads
#                    whose Deployment you cannot edit (operator/Helm-owned):
#                    pointing the owner at the stamped image is then the whole
#                    change. Default images stay inert and are activated by
#                    the Deployment env (attach-lineage.sh).
#   NO_KIND_LOAD=1   build + attest only; skip the kind cluster load (CI /
#                    offline use)
#
# Examples:
#   # app image already loaded as docker.io/library/a2a_currency_converter:latest
#   ./build-otel-shim.sh a2a_currency_converter:latest
#   # -> builds+attests+loads docker.io/library/a2a_currency_converter-otel:latest
#
# Runtime-agnostic: container-runtime.sh picks docker or podman (override with
# CONTAINER_TOOL) and kind_load does the right load per runtime.
#
# Structure: main() at the bottom is the pipeline — each phase is a function,
# each phase owns the globals it sets, and a failed phase exits the script
# (3 = image refused / outside the envelope, 4 = attestation failed).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/container-runtime.sh"

parse_args() {
  BASE_IMAGE="${1:?usage: build-otel-shim.sh <base-image> [wrapper-tag] [venv-python] [app-uid[:gid]]}"
  # derive default wrapper tag: strip registry/path + tag, append -otel
  local base_short base_name
  base_short="${BASE_IMAGE##*/}"; base_name="${base_short%%:*}"
  WRAPPER_TAG="${2:-${base_name}-otel:latest}"
  # a tag is required downstream (the alias split in publish() keys on it)
  case "${WRAPPER_TAG##*/}" in *:*) ;; *) WRAPPER_TAG="${WRAPPER_TAG}:latest" ;; esac
  VENV_PYTHON="${3:-}"
  APP_UID="${4:-}"
  APP_GID="${APP_UID#*:}"; [ "$APP_GID" = "$APP_UID" ] && APP_GID=""
  APP_UID="${APP_UID%%:*}"
  FORCE_BAKE="${FORCE_BAKE:-0}"
  SELF_ACTIVATE="${SELF_ACTIVATE:-0}"
  NO_KIND_LOAD="${NO_KIND_LOAD:-0}"

  # Normalize the base image to the docker.io/library/ name kind resolves
  # against, unless a registry was already given.
  case "$BASE_IMAGE" in
    */*) base_ref="$BASE_IMAGE" ;;
    *)   base_ref="docker.io/library/${BASE_IMAGE}" ;;
  esac
}

# ---- detect the two per-image build inputs ----
# The refuse-to-bake principle applied to the bake's own inputs: everything
# needed is IN the image, so probe it instead of trusting a transcription.
runs_python() {  # $1 = candidate interpreter path/name
  "$CONTAINER_TOOL" run --rm --network=none --entrypoint "$1" "$base_ref" -c 'import sys' >/dev/null 2>&1
}

detect_python() {  # sets VENV_PYTHON (validates it when given explicitly)
  if [ -z "$VENV_PYTHON" ]; then
    # Ordered candidates: the environment the image itself declares first, then
    # the common venv layouts, then a python3 on PATH (pip/poetry/distro-python
    # images — squarely in the envelope, no venv at all).
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
    # Restore exactly the user the base image declared — including root (empty
    # Config.User) — instead of assuming an ecosystem convention. A named user
    # is resolved to its numeric uid inside the image itself.
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
    # The gid the base actually ran with: an explicit `USER uid:gid`, else the
    # primary gid the runtime gives the declared user (0 when the user has no
    # passwd entry — which is what the base image ran as, so it is reproduced).
    case "$config_user_full" in
      *:*) APP_GID="${config_user_full#*:}" ;;
      *)   APP_GID="$("$CONTAINER_TOOL" run --rm --network=none --entrypoint id "$base_ref" -g 2>/dev/null)" || APP_GID=0 ;;
    esac
    echo ">> detected app group: gid=${APP_GID}"
  fi
}

# ---- refuse-to-bake interlock ----
# Baking the shim is safe ONLY for an in-envelope image that is not already
# auto-instrumented. That judgment must not depend on a human getting a config
# entry right, so probe the image itself — sufficient, because no entrypoint
# can activate packages the image lacks.
#
# What the probes can and cannot decide (measured across a mixed set of
# example agent and tool images):
#   - a runnable python IS the envelope test — a self-instrumenting app
#     correctly lands in sidecar-only here.
#   - the refusal test is "would wrapping DOUBLE-instrument?", so it asks
#     exactly that: is any of the instrumentors this shim installs already
#     present? Testing for the `opentelemetry.instrumentation` package instead
#     is far too broad — that namespace exists whenever the *base*
#     `opentelemetry-instrumentation` package is installed, which arrives as a
#     transitive dependency of anything OTel-adjacent and brings no library
#     instrumentation with it. An app carrying only the base package needs the
#     shim and must not be refused.
#   - bare SDK/exporter presence is NOT a refusal signal: a2a-sdk ships both
#     as dormant transitive deps on every stock agent, and the wrap is proven
#     safe on them (exporters stay off; the shim never sets an endpoint).
#     An in-envelope app that ACTIVATES its SDK in code is not statically
#     detectable; deciding that needs a runtime probe, which this does not do.
# FORCE_BAKE=1 overrides, turning the implicit human judgment into an
# explicit, greppable one.
interlock() {
  [ "$FORCE_BAKE" = "1" ] && return 0
  # The seven this shim installs; presence of ANY means a wrap would stack a
  # second instrumentor on the same library. Keep this list in sync with the
  # instrumentor packages in Dockerfile.otel-shim's install RUN (everything
  # there except opentelemetry-distro, which is the SDK, not an instrumentor).
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
  # The hook itself must not be double-baked: a second bake would leave two
  # .pth entries importing the same module (harmless but wrong) and marks the
  # input as an -otel image, not a base. An app's own sitecustomize is NOT
  # probed — the .pth hook coexists with sitecustomize; only a same-named
  # module conflicts.
  if "$CONTAINER_TOOL" run --rm --network=none --entrypoint "$VENV_PYTHON" "$base_ref" -c '
import importlib.util as u
raise SystemExit(0 if u.find_spec("_lineage_propagate") else 1)' >/dev/null 2>&1; then
    echo "REFUSING to bake ${base_ref}: it already carries the lineage propagate hook" >&2
    echo "  (an already-baked -otel image). -> point me at the un-shimmed base." >&2
    echo "  -> FORCE_BAKE=1 overrides if you know the wrap is safe." >&2
    exit 3
  fi
}

bake() {
  echo ">> building shim ${WRAPPER_TAG} FROM ${base_ref} (${CONTAINER_TOOL}, python=${VENV_PYTHON}, uid=${APP_UID})"
  "$CONTAINER_TOOL" build -f "${SCRIPT_DIR}/Dockerfile.otel-shim" \
    --build-arg "BASE_IMAGE=${base_ref}" \
    --build-arg "VENV_PYTHON=${VENV_PYTHON}" \
    --build-arg "APP_UID=${APP_UID}" \
    --build-arg "APP_GID=${APP_GID}" \
    -t "${WRAPPER_TAG}" "${SCRIPT_DIR}"
}

# ---- post-bake attestation ----
# The bake proves itself before anything is loaded or deployed. Two probes on
# the image just built:
#   inert  — gate off: not a single opentelemetry module may load. The -otel
#            image must behave exactly like its base until activated.
#   active — gate on: the hook must have initialized the SDK, the exporter
#            selection must be explicit (the hook's `none` default, or a
#            value the base image itself declares — an explicit env value
#            wins over the hook by design), and the propagator must actually
#            inject a traceparent. This is the in-process half of
#            propagation proven end-to-end, per image, at build time.
attest_inert() {
  if ! "$CONTAINER_TOOL" run --rm --network=none --entrypoint "$VENV_PYTHON" "$WRAPPER_TAG" -c '
import sys
loaded = sorted(m for m in sys.modules if m.startswith("opentelemetry"))
raise SystemExit("gate off, yet otel loaded: %s" % loaded if loaded else 0)'; then
    echo "ATTESTATION FAILED for ${WRAPPER_TAG}: image is not inert with the gate off." >&2
    exit 4
  fi
}

attest_active() {
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
  # alias under docker.io/library so containerd resolves the bare name in
  # manifests (split on the LAST colon: a registry with a port has one earlier)
  local wrapper_name wrapper_ver alias_ref
  wrapper_name="${WRAPPER_TAG%:*}"; wrapper_ver="${WRAPPER_TAG##*:}"
  alias_ref="docker.io/library/${wrapper_name}:${wrapper_ver}"
  "$CONTAINER_TOOL" tag "${WRAPPER_TAG}" "${alias_ref}"

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
  parse_args "$@"   # args + env -> globals; derive wrapper tag, normalize refs
  detect_python     # probe the image for its interpreter (or validate arg 3)
  detect_user       # probe uid:gid from Config.User / in-image id
  interlock         # refuse if already instrumented or already baked (exit 3)
  bake              # build Dockerfile.otel-shim with the detected build-args
  attest_inert      # gate off: not one otel module may load (exit 4)
  attest_active     # gate on: hook up, exporter explicit, traceparent injected (exit 4)
  self_activate     # SELF_ACTIVATE=1 only: bake the gate var into the image
  publish           # alias under docker.io/library, kind-load, print the honest NOTE
}
main "$@"
