#!/bin/bash
# Populate the demo's `envoy-config-mtls-active` ConfigMap from one of
# the per-mode variants (envoy-config-mtls-permissive /
# envoy-config-mtls-strict). The hand-crafted demo deployments mount
# `envoy-config-mtls-active` directly — there's no shared
# namespace `envoy-config` to mutate, and no operator reconciliation
# loop to fight.
#
# Usage:
#   swap-envoy-config.sh <namespace> permissive|strict
#   swap-envoy-config.sh <namespace> clean      # delete the active CM

set -euo pipefail

NAMESPACE=${1:?usage: swap-envoy-config.sh <namespace> permissive|strict|clean}
ACTION=${2:?usage: swap-envoy-config.sh <namespace> permissive|strict|clean}

HERE=$(dirname "$0")

case "$ACTION" in
  permissive|strict)
    SRC_CM="envoy-config-mtls-${ACTION}"

    # Apply both variant CMs (idempotent) and the runtime CM.
    kubectl apply -f "$HERE/../k8s/envoy-config-mtls.yaml"
    kubectl apply -f "$HERE/../k8s/authbridge-runtime-mtls.yaml"

    # Build the active CM from the chosen variant.
    YAML=$(kubectl -n "$NAMESPACE" get cm "$SRC_CM" -o jsonpath='{.data.envoy\.yaml}')
    if [[ -z "$YAML" ]]; then
      echo "ERROR: $SRC_CM has no envoy.yaml entry — did the apply succeed?" >&2
      exit 1
    fi

    kubectl -n "$NAMESPACE" create configmap envoy-config-mtls-active \
      --from-literal=envoy.yaml="$YAML" \
      --dry-run=client -o yaml | \
      kubectl -n "$NAMESPACE" apply -f - >/dev/null

    echo "[*] envoy-config-mtls-active populated from $SRC_CM (mode: $ACTION)"
    ;;

  clean)
    kubectl -n "$NAMESPACE" delete cm \
      envoy-config-mtls-active envoy-config-mtls-permissive envoy-config-mtls-strict \
      authbridge-runtime-mtls --ignore-not-found
    echo "[*] demo CMs removed"
    ;;

  *)
    echo "ERROR: action must be permissive | strict | clean, got: $ACTION" >&2
    exit 1
    ;;
esac
