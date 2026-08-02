#!/bin/bash
# Verify that a permissive callee serves both TLS and plaintext on
# the same port. Two-pronged check:
#
#   1. callee's authbridge log says `mTLS enabled mode=permissive` —
#      distinguishes "permissive correctly applied" from "mTLS off
#      entirely" (a disabled callee would also serve the plaintext
#      request below, so the plaintext-served check alone isn't
#      enough evidence the permissive mode is on).
#   2. plain HTTP from a non-mesh client (no authbridge sidecar, no
#      SPIRE) reaches the demo-app — proves the raw_buffer / byte-peek
#      path is open in addition to the TLS chain.
#
# Usage: verify-permissive.sh <namespace> <callee>

set -euo pipefail

NAMESPACE=${1:?usage: verify-permissive.sh <namespace> <callee>}
CALLEE=${2:?}

echo "[*] Confirming $CALLEE has mTLS enabled in permissive mode"
log_line=$(kubectl -n "$NAMESPACE" logs deploy/"$CALLEE" -c authbridge-proxy --tail=200 2>/dev/null \
             | grep '"mTLS enabled"' | head -1 || true)
if [[ -z "$log_line" ]]; then
  echo "  ERROR: $CALLEE does not show 'mTLS enabled' in its authbridge logs." >&2
  echo "         The callee is running with mTLS off, so the plaintext-served" >&2
  echo "         test below would pass even without permissive being applied." >&2
  exit 1
fi
mode=$(echo "$log_line" | sed -n 's/.*mode=\([a-z]*\).*/\1/p')
if [[ "$mode" != "permissive" ]]; then
  printf "  ERROR: %s mTLS mode is %q, want permissive.\n" "$CALLEE" "$mode" >&2
  exit 1
fi
echo "  $CALLEE: mTLS enabled, mode=permissive"

echo
echo "[*] Hitting $CALLEE with plain HTTP from a non-mesh client"
out=$(kubectl -n "$NAMESPACE" run --rm -i --restart=Never \
        --image=curlimages/curl:latest \
        --command "verify-permissive-$$" -- \
        curl -sS --max-time 10 \
             "http://${CALLEE}.${NAMESPACE}.svc.cluster.local:8080/healthz" 2>&1) || {
  echo "ERROR: plaintext request to permissive callee failed:" >&2
  echo "$out" >&2
  exit 1
}
echo "  Response: $out"
echo
echo "============================================================"
echo " Permissive verified — mTLS on (mode=permissive), plaintext"
echo " callers served on the same port"
echo "============================================================"
