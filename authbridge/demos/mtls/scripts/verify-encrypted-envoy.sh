#!/bin/bash
# Verify mTLS is active on the envoy-sidecar variant.
#
# We can't grep "mTLS enabled" out of authbridge-proxy logs the way
# verify-encrypted.sh does — in envoy-sidecar mode mTLS is terminated
# by Envoy, not by authbridge. Three-pronged check:
#
#   1. The active envoy-config CM has tls_inspector + SVID paths
#      (proves the swap landed).
#   2. The spiffe Provider mirror has written /opt/svid*.pem on each
#      pod (proves SPIRE registration is alive).
#   3. The caller can reach the callee through the sidecars (proves
#      the wiring works end-to-end).
#
# The "encryption on the wire" proof comes from
# verify-strict-rejects-plain-envoy.sh — a strict listener that
# rejects plaintext is observably enforcing TLS.
#
# Usage: verify-encrypted-envoy.sh <namespace> <caller> <callee>

set -euo pipefail

NAMESPACE=${1:?usage: verify-encrypted-envoy.sh <namespace> <caller> <callee>}
CALLER=${2:?}
CALLEE=${3:?}

echo "[*] Confirming the active envoy-config has TLS termination wired in"
cm=$(kubectl -n "$NAMESPACE" get cm envoy-config-mtls-active -o jsonpath='{.data.envoy\.yaml}' 2>/dev/null || true)
if [[ -z "$cm" ]]; then
  echo "  ERROR: envoy-config-mtls-active not found in $NAMESPACE — run swap-envoy-config.sh first" >&2
  exit 1
fi
for pat in 'tls_inspector' 'DownstreamTlsContext' '/opt/svid.pem' '/opt/svid_bundle.pem'; do
  if ! echo "$cm" | grep -q "$pat"; then
    echo "  ERROR: envoy-config-mtls-active missing pattern: $pat" >&2
    exit 1
  fi
done
echo "  envoy-config: tls_inspector + DownstreamTlsContext + SVID paths present"

echo
echo "[*] Confirming spiffe Provider mirror wrote SVIDs to /opt on each pod"
for agent in "$CALLER" "$CALLEE"; do
  # Filter to a Running, fully-Ready pod so we don't accidentally
  # exec into a Terminating one mid-rollout.
  pod=$(kubectl -n "$NAMESPACE" get pod \
          -l app.kubernetes.io/name="$agent" \
          --field-selector=status.phase=Running \
          -o 'jsonpath={range .items[?(@.status.containerStatuses[0].ready==true)]}{.metadata.name}{"\n"}{end}' \
          | head -1)
  if [[ -z "$pod" ]]; then
    echo "  ERROR: no Ready pod found for $agent in $NAMESPACE" >&2
    exit 1
  fi
  files=$(kubectl -n "$NAMESPACE" exec "$pod" -c envoy-proxy -- /bin/sh -c \
            'ls /opt/svid.pem /opt/svid_key.pem /opt/svid_bundle.pem 2>/dev/null | wc -l' 2>/dev/null \
          || echo 0)
  if [[ "$files" -lt 3 ]]; then
    echo "  ERROR: $pod ($agent): only $files of 3 SVID files present in /opt/" >&2
    echo "         spiffe-helper or in-process Provider didn't fetch from SPIRE." >&2
    exit 1
  fi
  echo "  $agent: 3/3 SVID files present in /opt/"
done

echo
echo "[*] Triggering caller → callee request through the sidecars"
out=$(kubectl -n "$NAMESPACE" exec deploy/"$CALLER" -c demo-app -- \
        curl -sS --max-time 10 \
             "http://${CALLEE}.${NAMESPACE}.svc.cluster.local:8080/" 2>&1) || {
  echo "ERROR: request failed:" >&2
  echo "$out" >&2
  exit 1
}
echo "  Response: $out"

echo
echo "============================================="
echo " mTLS verified — caller → callee through TLS-"
echo " enabled Envoy listeners on both ends"
echo "============================================="
