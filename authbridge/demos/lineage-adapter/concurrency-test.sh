#!/usr/bin/env bash
# GENERALIZED concurrency test — the whole point of the exercise (STUDY §10.7/10.8).
#
# Fires N CONCURRENT A2A requests at an in-cluster agent, each with:
#   (a) a DISTINCT W3C traceparent  00-<32hex unique>-<16hex>-01  (the caller
#       roots the trace — required; the sidecar does not seed it, see §10.9), and
#   (b) a DISTINCT token embedded in the prompt text, so each captured hop's
#       input.value reveals which request it belongs to.
# Traffic comes from an IN-CLUSTER driver pod (NOT kubectl port-forward, which
# uses pod-loopback and bypasses the sidecar inbound listener — §10.7).
#
# Then queries DG's Postgres for the pairing: for each request's token, does the
# outbound hop (agent_to_llm / agent_to_tool) that carries that token share the
# trace of the INBOUND that carries the same token? Metric: matches / N. Target N/N.
#
# Usage:
#   SELF_ID=trivia-agent TARGET=trivia-agent.team1.svc.cluster.local:8080 \
#   PROMPT='Ask one trivia question about the planet {TOKEN} only.' \
#   lineage-sidecar/concurrency-test.sh
#
# Variables:
#   SELF_ID   (required) the lineage self_id — span names are <SELF_ID>.inbound / .llm / .mcp*.
#   TARGET    (required) in-cluster host:port of the agent Service (A2A endpoint at /).
#   N         number of concurrent requests (default 6).
#   PROMPT    prompt template; '{TOKEN}' is replaced by each request's unique token.
#   TOKENS    space-separated token list (default 6 planets). Must be >= N, verbatim-safe.
#   NAMESPACE default team1.
#   WINDOW    DG lookback window for the pairing query (default '3 minutes').
#   SETTLE    seconds to wait for spans to reach DG before querying (default 12).
set -euo pipefail

SELF_ID="${SELF_ID:?set SELF_ID}"
TARGET="${TARGET:?set TARGET (host:port of agent Service)}"
N="${N:-6}"
PROMPT="${PROMPT:-Ask one trivia question about the planet {TOKEN} only.}"
TOKENS="${TOKENS:-MERCURY VENUS MARS JUPITER SATURN NEPTUNE URANUS PLUTO}"
NAMESPACE="${NAMESPACE:-team1}"
WINDOW="${WINDOW:-3 minutes}"
SETTLE="${SETTLE:-12}"
DRIVER_POD="lineage-driver"

toks=($TOKENS)
if [ "${#toks[@]}" -lt "$N" ]; then echo "need >= $N tokens" >&2; exit 1; fi

# ---- ensure an in-cluster driver pod exists ----
if ! kubectl get pod -n "$NAMESPACE" "$DRIVER_POD" >/dev/null 2>&1; then
  echo ">> creating driver pod $DRIVER_POD in $NAMESPACE"
  kubectl run "$DRIVER_POD" -n "$NAMESPACE" --image=curlimages/curl:latest \
    --restart=Never --labels="kagenti.io/inject=disabled" \
    --command -- sleep infinity >/dev/null
  kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/$DRIVER_POD" --timeout=90s
fi

# ---- build a shell script that fires N concurrent tagged requests ----
# traceparents generated on the host (guaranteed unique randomness).
script=$'set -e\n'
for i in $(seq 0 $((N-1))); do
  tok="${toks[$i]}"
  tid=$(openssl rand -hex 16)
  sid=$(openssl rand -hex 8)
  text="${PROMPT//\{TOKEN\}/$tok}"
  body="{\"jsonrpc\":\"2.0\",\"id\":\"$i\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"m$i\",\"parts\":[{\"kind\":\"text\",\"text\":\"$text\"}]}}}"
  # each request backgrounded => concurrent in flight
  line="curl -s -o /dev/null -w 'req $tok -> HTTP %{http_code}\\n' -X POST http://$TARGET/ "
  line+="-H 'Content-Type: application/json' "
  line+="-H 'traceparent: 00-$tid-$sid-01' "
  line+="-d '$body' &"
  script+="$line"$'\n'
done
script+='wait'$'\n'

echo ">> firing $N concurrent requests at $TARGET (self_id=$SELF_ID)"
echo "$script" | kubectl exec -i -n "$NAMESPACE" "$DRIVER_POD" -- sh

echo ">> waiting ${SETTLE}s for spans to reach DG..."
sleep "$SETTLE"

# ---- pairing analysis via DG Postgres ----
psql() { kubectl exec -n data-governance data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAF $'\t' -c "$1"; }

# inbound: trace_id + input.value; outbound: trace_id + input.value for llm/tool hops
# translate TAB/newline/CR -> space so each DB row stays on ONE output line
# (multi-line input/output.value would otherwise break the line-oriented join).
inbound=$(psql "select trace_id, translate(coalesce(attributes->>'input.value',''),chr(9)||chr(10)||chr(13),'   ') from spans where service_name='authbridge' and name='${SELF_ID}.inbound' and started_at > now() - interval '${WINDOW}';")
outbound=$(psql "select trace_id, translate(coalesce(attributes->>'input.value','')||' '||coalesce(attributes->>'output.value',''),chr(9)||chr(10)||chr(13),'   ') from spans where service_name='authbridge' and name like '${SELF_ID}.%' and (attributes->>'lineage.hop.kind') in ('agent_to_llm','agent_to_tool') and started_at > now() - interval '${WINDOW}';")

echo ""
echo "=== pairing (self_id=$SELF_ID, N=$N) ==="
match=0
for i in $(seq 0 $((N-1))); do
  tok="${toks[$i]}"
  # inbound trace(s) whose payload carries this token
  in_trace=$(printf '%s\n' "$inbound" | grep -F "$tok" | head -1 | cut -f1)
  # outbound trace(s) whose payload carries this token
  out_traces=$(printf '%s\n' "$outbound" | grep -F "$tok" | cut -f1 | sort -u | tr '\n' ',' | sed 's/,$//')
  ok="MISMATCH"
  if [ -n "$in_trace" ] && printf '%s\n' "$outbound" | grep -F "$tok" | cut -f1 | grep -qx "$in_trace"; then
    ok="OK"; match=$((match+1))
  fi
  printf '%-10s inbound_trace=%-34s outbound_trace(s)=%-34s [%s]\n' \
    "$tok" "${in_trace:-<none>}" "${out_traces:-<none>}" "$ok"
done
echo "-----------------------------------------"
echo "CORRECT PAIRING: ${match}/${N}"
