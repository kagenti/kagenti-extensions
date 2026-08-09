#!/usr/bin/env bash
# Stamp the Kagenti UI *backend* (the FastAPI service the browser UI talks to)
# with the propagate-only OTEL shim, so a real browser chat mints ONE trace and
# every downstream hop the backend makes carries its traceparent — the sidecar
# then captures a correctly-rooted forest in Data Governance. This is the "real
# UI" entry path (the in-cluster simulated-user driver is the acceptance path).
#
# Why the backend needs its own script instead of build-otel-shim.sh:
#
#   1. The backend image is a PLATFORM image, not an agent-example. Its runtime
#      layer is python:3.x-slim built in a throwaway `uv venv` builder stage —
#      it has NEITHER `uv` NOR `pip` (a uv-made venv is pip-less). The shim's
#      `RUN uv pip install` therefore needs the uv that Dockerfile.otel-shim now
#      copies in itself (COPY --from=ghcr.io/astral-sh/uv). VENV_PYTHON is the
#      usual /app/.venv/bin/python; the app runs as uid=999 (NOT the 1001
#      agent-examples default) — confirmed with `id` in the base image.
#
#   2. Agent-examples are launched by attach-lineage.sh, which wraps the command
#      with `opentelemetry-instrument --traces_exporter none` at the Deployment
#      level. The backend is deployed by the kagenti operator/Helm from its
#      baked-in CMD, so we bake the SAME wrapper into the stamped image instead
#      (prepended onto the base image's own Cmd). Deploying is then a pure image
#      swap — no Deployment `command:` patch to keep in sync.
#
# `--traces_exporter none`: the shim EXPORTS NOTHING (DG stays sidecar-only);
# only the W3C traceparent header flows. See DESIGN.md / the two-span plan.
#
# Usage:
#   ./stamp-ui-backend.sh [base-image] [wrapper-tag]
#
# Defaults:
#   base-image   ghcr.io/kagenti/kagenti/backend:latest
#   wrapper-tag  <base-name>-otel:latest  (e.g. backend-otel:latest)
#
# After it runs, point the backend Deployment at the stamped image, e.g.:
#   kubectl -n kagenti-system set image deploy/kagenti-ui-backend \
#     <container>=docker.io/library/backend-otel:latest
# (confirm the real Deployment/container names on the cluster first).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "${SCRIPT_DIR}/container-runtime.sh"

BASE_IMAGE="${1:-ghcr.io/kagenti/kagenti/backend:latest}"
base_short="${BASE_IMAGE##*/}"; base_name="${base_short%%:*}"
WRAPPER_TAG="${2:-${base_name}-otel:latest}"

# Fixed for the kagenti backend image (verified by inspecting the image):
VENV_PYTHON="/app/.venv/bin/python"
APP_UID="999"

command -v python3 >/dev/null || { echo "error: python3 not on PATH (needed to build the wrapped Cmd)" >&2; exit 1; }

# 0) Ensure the base image is present locally (the cluster/agent-examples flow
#    usually has it pulled already; pull if not). `image inspect` is the
#    existence check both docker and podman understand.
if ! "$CONTAINER_TOOL" image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
  echo ">> pulling ${BASE_IMAGE}"
  "$CONTAINER_TOOL" pull "${BASE_IMAGE}"
fi

# 1) Install the propagate-only instrumentors via the SHARED shim.
INSTRUMENTED="${base_name}-otel-instrumented:latest"
echo ">> [1/3] building instrumentor layer ${INSTRUMENTED} FROM ${BASE_IMAGE} (uid=${APP_UID}, ${CONTAINER_TOOL})"
"$CONTAINER_TOOL" build -f "${SCRIPT_DIR}/Dockerfile.otel-shim" \
  --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
  --build-arg "VENV_PYTHON=${VENV_PYTHON}" \
  --build-arg "APP_UID=${APP_UID}" \
  -t "${INSTRUMENTED}" "${SCRIPT_DIR}"

# 2) Read the base image's own Cmd and prepend the opentelemetry-instrument
#    wrapper, producing an exec-form JSON array for a thin CMD-override layer.
#    An ENTRYPOINT-based image cannot be wrapped this way (the wrapper would
#    land in the args, not the command) — refuse rather than mis-wrap.
BASE_ENTRYPOINT_JSON="$("$CONTAINER_TOOL" inspect --format '{{json .Config.Entrypoint}}' "${BASE_IMAGE}")"
case "${BASE_ENTRYPOINT_JSON}" in
  null|'""'|'[]') ;;
  *)
    echo "ERROR: ${BASE_IMAGE} declares an ENTRYPOINT (${BASE_ENTRYPOINT_JSON})." >&2
    echo "       CMD-prepend wrapping would corrupt it. Wrap the entrypoint instead" >&2
    echo "       (see attach-lineage.sh) or clear the ENTRYPOINT in an overlay." >&2
    exit 1;;
esac
BASE_CMD_JSON="$("$CONTAINER_TOOL" inspect --format '{{json .Config.Cmd}}' "${BASE_IMAGE}")"
WRAPPED_CMD="$(
  BASE_CMD_JSON="${BASE_CMD_JSON}" python3 - <<'PY'
import json, os, sys
base = json.loads(os.environ["BASE_CMD_JSON"] or "null")
if not base:
    # Never fabricate a command for an image we don't know: a guessed CMD
    # fails later as an unrelated-looking pod crash. Refuse loudly instead.
    sys.stderr.write(
        "ERROR: base image declares no CMD; refusing to invent one.\n"
        "       Inspect the image and wrap its real command explicitly.\n"
    )
    sys.exit(1)
# Disable ALL THREE signal exporters, not just traces: the distro defaults
# metrics+logs to OTLP, and we install only instrumentors (no OTLP exporter),
# so `--traces_exporter none` alone throws RuntimeError 'otlp_proto_grpc not
# found' at startup. Matches attach-lineage.sh's agent-examples wrapper.
wrapped = [
    "opentelemetry-instrument",
    "--traces_exporter", "none",
    "--metrics_exporter", "none",
    "--logs_exporter", "none",
] + base
sys.stdout.write(json.dumps(wrapped))
PY
)"
echo ">> [2/3] wrapping Cmd: ${WRAPPED_CMD}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
cat > "${WORK_DIR}/Dockerfile.wrap" <<EOF
# GENERATED by stamp-ui-backend.sh — do not hand-edit; re-run.
FROM ${INSTRUMENTED}
# opentelemetry-instrument is installed into the venv, which is already on PATH
# (the backend image sets ENV PATH=/app/.venv/bin:\$PATH).
# Belt-and-suspenders: even if the CMD is overridden at deploy time, these keep
# the app from trying to reach a (non-existent) OTLP exporter. DG is sidecar-only.
ENV OTEL_TRACES_EXPORTER=none \\
    OTEL_METRICS_EXPORTER=none \\
    OTEL_LOGS_EXPORTER=none
CMD ${WRAPPED_CMD}
EOF
"$CONTAINER_TOOL" build -f "${WORK_DIR}/Dockerfile.wrap" -t "${WRAPPER_TAG}" "${WORK_DIR}"

# 3) Load into the kind cluster, aliased under docker.io/library so containerd
#    resolves the bare name (kind_load handles the docker-vs-podman mechanics).
wrapper_name="${WRAPPER_TAG%%:*}"; wrapper_ver="${WRAPPER_TAG##*:}"
alias_ref="docker.io/library/${wrapper_name}:${wrapper_ver}"
"$CONTAINER_TOOL" tag "${WRAPPER_TAG}" "${alias_ref}"
echo ">> [3/3] loading ${alias_ref} into kind cluster ${KIND_CLUSTER}"
kind_load "${alias_ref}"

cat <<EOF

Done. Stamped image: ${alias_ref}

Next: point the UI backend Deployment at it (confirm the real names first):
  kubectl -n kagenti-system get deploy | grep -i backend
  kubectl -n kagenti-system set image deploy/<backend-deploy> <container>=${alias_ref}

Then run one browser chat and confirm in DG/Phoenix that the turn is a single,
correctly-rooted trace (the backend mints the entry span).
EOF
