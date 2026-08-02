#!/bin/bash
# Verify a permissive envoy-sidecar callee serves plaintext on the
# same port. Same contract as verify-permissive.sh, just hits the
# envoy-variant callee.
#
# Usage: verify-permissive-envoy.sh <namespace> <callee>

set -euo pipefail

NAMESPACE=${1:?usage: verify-permissive-envoy.sh <namespace> <callee>}
CALLEE=${2:?}

echo "[*] Hitting $CALLEE with plain HTTP from a non-mesh client"
out=$(kubectl -n "$NAMESPACE" run --rm -i --restart=Never \
        --image=curlimages/curl:latest \
        --command "verify-permissive-envoy-$$" -- \
        curl -sS --max-time 10 \
             "http://${CALLEE}.${NAMESPACE}.svc.cluster.local:8080/" 2>&1) || {
  echo "ERROR: plaintext request to permissive callee failed:" >&2
  echo "$out" >&2
  exit 1
}
echo "  Response: $out"
echo
echo "==================================================================="
echo " Permissive (envoy) verified — raw_buffer chain serves plaintext"
echo "==================================================================="
