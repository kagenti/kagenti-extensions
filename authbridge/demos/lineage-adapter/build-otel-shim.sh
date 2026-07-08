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
# Notes (see RUNBOOK.md / DESIGN.md): podman build + `kind load image-archive`
# because the docker daemon is off and `kind load docker-image` misbehaves under
# podman. BRACE "${x}:latest" — zsh applies a :l modifier to $x:latest (this
# script is bash so it's fine, but keep the habit).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

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

echo ">> building shim ${WRAPPER_TAG} FROM ${base_ref}"
podman build -f "${SCRIPT_DIR}/Dockerfile.otel-shim" \
  --build-arg "BASE_IMAGE=${base_ref}" \
  --build-arg "VENV_PYTHON=${VENV_PYTHON}" \
  --build-arg "APP_UID=${APP_UID}" \
  -t "${WRAPPER_TAG}" "${SCRIPT_DIR}"

# alias under docker.io/library so containerd resolves the bare name in manifests
wrapper_name="${WRAPPER_TAG%%:*}"; wrapper_ver="${WRAPPER_TAG##*:}"
alias_ref="docker.io/library/${wrapper_name}:${wrapper_ver}"
podman tag "${WRAPPER_TAG}" "${alias_ref}"

tar="/tmp/${wrapper_name}-otel.tar"
podman save -o "${tar}" "${alias_ref}"
KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive "${tar}" --name kagenti
echo ">> loaded ${alias_ref} into kind cluster kagenti"
