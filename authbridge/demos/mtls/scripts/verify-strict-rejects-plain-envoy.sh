#!/bin/bash
# Verify a strict envoy-sidecar callee closes plain HTTP connections.
# Same contract as verify-strict-rejects-plain.sh, just hits the
# envoy-variant callee.
#
# Usage: verify-strict-rejects-plain-envoy.sh <namespace> <callee>

set -euo pipefail

NAMESPACE=${1:?usage: verify-strict-rejects-plain-envoy.sh <namespace> <callee>}
CALLEE=${2:?}

echo "[*] Hitting $CALLEE with plain HTTP from a non-mesh client — expect rejection"
if kubectl -n "$NAMESPACE" run --rm -i --restart=Never \
     --image=curlimages/curl:latest \
     --command "verify-strict-envoy-$$" -- \
     curl -sS --max-time 10 \
          "http://${CALLEE}.${NAMESPACE}.svc.cluster.local:8080/" >/dev/null 2>&1; then
  echo "ERROR: strict envoy-sidecar callee unexpectedly served plaintext." >&2
  echo "       Either tls_inspector isn't routing the connection to the" >&2
  echo "       TLS chain, or a raw_buffer chain is still present." >&2
  exit 1
fi
echo "  Connection rejected as expected."
echo
echo "================================================================"
echo " Strict (envoy) verified — plaintext rejected at filter chain match"
echo "================================================================"
