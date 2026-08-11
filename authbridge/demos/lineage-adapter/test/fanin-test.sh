#!/usr/bin/env bash
# Same-trace fan-in concurrency test — the exact-pairing proof for the
# tracestate stamp (wire contract v1.2+; single-channel since v1.5). This
# harness originally demonstrated the wire-observer blind spot before the
# stamp existed; both modes now expect N/N.
#
# Topology (deploy first — see header of agent-examples/a2a/fanin_probe/):
#   driver -> fanin-agent (mid-chain: holds HOLD_MS, then calls downstream)
#          -> fanin-echo  (leaf)
#
# Two modes, same N concurrent tagged requests, differing ONLY in trace ids:
#   MODE=distinct  every request gets its OWN trace id (the cross-trace shape).
#                  Expect N/N: each fanin-agent outbound span is parented on
#                  the inbound span carrying the SAME tag.
#   MODE=shared    every request carries ONE trace id with distinct parent
#                  span ids — the shape an upstream orchestrator's sidecar
#                  produces when it fans out to the same agent within one
#                  turn. The app holds all inbounds open (HOLD_MS) so every
#                  outbound fires while all N inbounds are in flight. Trace
#                  membership cannot disambiguate here — only the tracestate
#                  stamp couriered through the app's shim can. Expect N/N;
#                  a collapse toward ~1/N means the stamp chain is broken.
#
# Ground truth is the tag in url.path: the sidecar records url.path on every
# request span (no parser dependency), so for each outbound request span of
# SELF_ID we compare its tag to the tag of the inbound request span it was
# parented on (spans.parent_id -> trace-keyed map result).
#
# Usage:
#   ./fanin-test.sh                  # runs distinct, then shared
#   MODE=shared N=6 ./fanin-test.sh
#
# Env: SELF_ID (default fanin-agent), TARGET (default
#   fanin-agent.team1.svc.cluster.local:8080), N (6), MODE (both),
#   NAMESPACE (team1), SETTLE (25), DRIVER_POD (lineage-driver3),
#   DG_NS (data-governance).
set -euo pipefail

SELF_ID="${SELF_ID:-fanin-agent}"
TARGET="${TARGET:-fanin-agent.team1.svc.cluster.local:8080}"
N="${N:-6}"
MODE="${MODE:-both}"
NAMESPACE="${NAMESPACE:-team1}"
SETTLE="${SETTLE:-25}"
DRIVER_POD="${DRIVER_POD:-lineage-driver3}"
DG_NS="${DG_NS:-data-governance}"

if ! kubectl get pod -n "$NAMESPACE" "$DRIVER_POD" >/dev/null 2>&1; then
  echo ">> creating driver pod $DRIVER_POD in $NAMESPACE"
  kubectl run "$DRIVER_POD" -n "$NAMESPACE" --image=curlimages/curl:latest \
    --image-pull-policy=IfNotPresent --restart=Never \
    --labels="kagenti.io/inject=disabled" --command -- sleep infinity >/dev/null
  kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/$DRIVER_POD" --timeout=90s
fi

psql() { kubectl exec -n "$DG_NS" data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAF $'\t' -c "$1"; }

run_mode() {
  local mode="$1"
  local -a TIDS TOKS
  local shared_tid=""
  [ "$mode" = "shared" ] && shared_tid=$(openssl rand -hex 16)

  local script=$'set -e\n'
  for i in $(seq 0 $((N-1))); do
    local tok tid sid
    tok="T$(openssl rand -hex 3)"
    if [ "$mode" = "shared" ]; then tid="$shared_tid"; else tid=$(openssl rand -hex 16); fi
    sid=$(openssl rand -hex 8)
    TIDS[$i]="$tid"; TOKS[$i]="$tok"
    local body="{\"jsonrpc\":\"2.0\",\"id\":\"$i\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"m$i\",\"parts\":[{\"kind\":\"text\",\"text\":\"fan-in probe $tok\"}]}}}"
    script+="curl -s -o /dev/null -w 'req $tok trace ${tid:0:8} -> HTTP %{http_code}\\n' -X POST http://$TARGET/probe/$tok -H 'Content-Type: application/json' -H 'traceparent: 00-$tid-$sid-01' -d '$body' &"$'\n'
  done
  script+='wait'$'\n'

  echo ""
  echo "###############################################################"
  echo "## MODE=$mode : firing $N concurrent requests at $TARGET"
  if [ "$mode" = "shared" ]; then
    echo "## ONE trace id ($shared_tid) for all $N — orchestrator fan-out shape"
  else
    echo "## $N distinct trace ids — the proven cross-trace shape"
  fi
  echo "###############################################################"
  echo "$script" | kubectl exec -i -n "$NAMESPACE" "$DRIVER_POD" -- sh
  echo ">> waiting ${SETTLE}s for spans to arrive..."
  sleep "$SETTLE"

  local tid_list
  tid_list=$(printf "'%s'," "${TIDS[@]}"); tid_list="${tid_list%,}"

  # Every outbound request span of SELF_ID in these traces, with the tag it
  # carries vs the tag of the inbound request span it was parented on.
  local rows
  rows=$(psql "
    SELECT
      substring(o.trace_id for 8),
      COALESCE(substring(o.attributes->>'url.path' from 'T[0-9a-f]{6}'), '?'),
      COALESCE(substring(i.attributes->>'url.path' from 'T[0-9a-f]{6}'), '<dangling>'),
      COALESCE(substring(o.parent_id for 8), '<none>')
    FROM spans o
    LEFT JOIN spans i
      ON i.trace_id = o.trace_id AND i.span_id = o.parent_id
    WHERE o.trace_id IN ($tid_list)
      AND o.attributes->>'lineage.self.id' = '$SELF_ID'
      AND o.attributes->>'lineage.role' = 'request'
      AND o.attributes->>'lineage.direction' = 'outbound'
    ORDER BY o.started_at")

  echo ""
  echo "=== $SELF_ID outbound spans: which inbound did the sidecar credit? ==="
  printf '%-10s %-10s %-14s %-10s %s\n' "trace" "out-tag" "credited-in" "parent" "verdict"
  local ok=0 total=0
  while IFS=$'\t' read -r trace outtag intag parent; do
    [ -z "$trace" ] && continue
    total=$((total+1))
    local verdict="MISATTRIBUTED"
    if [ "$outtag" = "$intag" ]; then verdict="OK"; ok=$((ok+1)); fi
    printf '%-10s %-10s %-14s %-10s %s\n' "$trace" "$outtag" "$intag" "$parent" "$verdict"
  done <<<"$rows"

  echo "---------------------------------------------------------------"
  echo "MODE=$mode : CORRECT PAIRING ${ok}/${total}  (fired $N)"
  if [ "$mode" = "distinct" ]; then
    echo "expected: $N/$N — distinct traces; in-process context propagation is exact"
  else
    echo "expected: $N/$N — one trace, N in-flight inbounds; only the"
    echo "          tracestate stamp can pair each outbound to its exact"
    echo "          inbound. A collapse toward 1/$N means the stamp chain"
    echo "          (sidecar re-stamp -> app shim courier) is broken."
  fi
  if [ "$mode" = "distinct" ]; then RESULT_DISTINCT="${ok}/${total}"; else RESULT_SHARED="${ok}/${total}"; fi
}

RESULT_DISTINCT=""; RESULT_SHARED=""
case "$MODE" in
  both)     run_mode distinct; run_mode shared ;;
  distinct) run_mode distinct ;;
  shared)   run_mode shared ;;
  *) echo "MODE must be distinct|shared|both" >&2; exit 2 ;;
esac

echo ""
echo "==============================================================="
echo "SUMMARY"
[ -n "$RESULT_DISTINCT" ] && echo "  distinct traces (proven shape):   $RESULT_DISTINCT correct"
[ -n "$RESULT_SHARED" ]   && echo "  shared trace (fan-in blind spot): $RESULT_SHARED correct"
echo "==============================================================="
