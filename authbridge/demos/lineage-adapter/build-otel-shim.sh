#!/usr/bin/env bash
# Build the GENERALIZED propagate-only OTEL shim on top of any app image and
# load it into the kind cluster. Only the base image changes per app.
#
# Usage:
#   ./build-otel-shim.sh <base-image> [wrapper-tag] [venv-python] [app-uid]
#
# Examples:
#   # app image already loaded as docker.io/library/a2a_currency_converter:latest
#   ./build-otel-shim.sh a2a_currency_converter:latest
#   # -> builds+loads docker.io/library/a2a_currency_converter-otel:latest
#
# Runtime-agnostic: container-runtime.sh picks docker or podman (override with
# CONTAINER_TOOL) and kind_load does the right load per runtime. BRACE
# "${x}:latest" — zsh applies a :l modifier to $x:latest (this script is bash
# so it's fine, but keep the habit).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/container-runtime.sh"

BASE_IMAGE="${1:?usage: build-otel-shim.sh <base-image> [wrapper-tag] [venv-python] [app-uid]}"
# derive default wrapper tag: strip registry/path + tag, append -otel
base_short="${BASE_IMAGE##*/}"; base_name="${base_short%%:*}"
WRAPPER_TAG="${2:-${base_name}-otel:latest}"
VENV_PYTHON="${3:-/app/.venv/bin/python}"
APP_UID="${4:-1001}"

# Normalize the base image to the docker.io/library/ name kind resolves against,
# unless a registry was already given.
case "$BASE_IMAGE" in
  */*) base_ref="$BASE_IMAGE" ;;
  *)   base_ref="docker.io/library/${BASE_IMAGE}" ;;
esac

# ---- refuse-to-bake interlock (R-universal-adoption) ----
# Baking the shim is safe ONLY for an in-envelope image that is not already
# auto-instrumented. That judgment must not depend on a human writing the
# fleet row correctly, so probe the image itself — sufficient, because no
# entrypoint can activate packages the image lacks.
#
# What the probes can and cannot decide (measured across the fleet):
#   - a runnable venv python IS the envelope test — the weather pair (pip
#     layout, self-instrumenting) correctly lands in sidecar-only here.
#   - `opentelemetry.instrumentation` present = auto-instrumentation already
#     baked (an -otel image, or an app bundling instrumentors) — a wrap on
#     top would double-instrument; refuse.
#   - bare SDK/exporter presence is NOT a refusal signal: a2a-sdk ships both
#     as dormant transitive deps on every stock agent, and the wrap is proven
#     safe on them (exporters stay off; the shim never sets an endpoint).
#     An in-envelope app that ACTIVATES its SDK in code is not statically
#     detectable — that runtime probe is the named follow-up in the PLAN.
# FORCE_BAKE=1 overrides, turning the implicit human judgment into an
# explicit, greppable one.
FORCE_BAKE="${FORCE_BAKE:-0}"
if [ "$FORCE_BAKE" != "1" ]; then
  if ! probe_out=$("$CONTAINER_TOOL" run --rm --entrypoint "$VENV_PYTHON" "$base_ref" -c 'import sys' 2>&1); then
    echo "REFUSING to bake ${base_ref}: no runnable Python at ${VENV_PYTHON}." >&2
    echo "  The image is outside the shim envelope (non-Python, or a different venv path)." >&2
    echo "  -> pass the right venv-python arg, or attach the sidecar only:" >&2
    echo "     DEPLOY=<name> ./sidecar-patch.sh   (lineage still captures every HTTP hop)" >&2
    echo "  -> FORCE_BAKE=1 overrides. Probe said: ${probe_out}" >&2
    exit 3
  fi
  if "$CONTAINER_TOOL" run --rm --entrypoint "$VENV_PYTHON" "$base_ref" -c 'import opentelemetry.instrumentation' >/dev/null 2>&1; then
    echo "REFUSING to bake ${base_ref}: auto-instrumentation is already baked in" >&2
    echo "  (opentelemetry.instrumentation importable — an -otel image, or an app that" >&2
    echo "  bundles instrumentors). Wrapping again would double-instrument." >&2
    echo "  -> if this is a stock app image, point me at the un-shimmed base." >&2
    echo "  -> FORCE_BAKE=1 overrides if you know the wrap is safe." >&2
    exit 3
  fi
fi

echo ">> building shim ${WRAPPER_TAG} FROM ${base_ref} (${CONTAINER_TOOL})"
"$CONTAINER_TOOL" build -f "${SCRIPT_DIR}/Dockerfile.otel-shim" \
  --build-arg "BASE_IMAGE=${base_ref}" \
  --build-arg "VENV_PYTHON=${VENV_PYTHON}" \
  --build-arg "APP_UID=${APP_UID}" \
  -t "${WRAPPER_TAG}" "${SCRIPT_DIR}"

# alias under docker.io/library so containerd resolves the bare name in manifests
wrapper_name="${WRAPPER_TAG%%:*}"; wrapper_ver="${WRAPPER_TAG##*:}"
alias_ref="docker.io/library/${wrapper_name}:${wrapper_ver}"
"$CONTAINER_TOOL" tag "${WRAPPER_TAG}" "${alias_ref}"

kind_load "${alias_ref}"
echo ">> loaded ${alias_ref} into kind cluster ${KIND_CLUSTER}"
