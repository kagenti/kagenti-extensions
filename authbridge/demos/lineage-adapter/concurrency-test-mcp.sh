#!/usr/bin/env bash
# GENERALIZED concurrency test for a 2-service MCP tool chain (front MCP server →
# backend HTTP service), e.g. #6 wiki_memory_tool. Companion to concurrency-test.sh
# (which drives A2A agents). Here the entry protocol is MCP streamable-http, so an
# in-cluster Python MCP client drives it.
#
# Fires N CONCURRENT MCP tool calls at the FRONT, each in its own session with a
# DISTINCT W3C traceparent. Then checks, per request trace, that BOTH a front
# outbound hop (front→backend) AND a backend inbound span landed in that SAME
# trace — i.e. the traceparent propagated FastMCP-inbound → httpx-outbound →
# FastAPI-inbound without collapsing under concurrency. Metric: matches / N (N/N).
#
# Bodies are not used here (a generic REST hop carries no parsed input.value);
# correlation is by trace_id, which is exactly what propagation must preserve.
#
# Usage:
#   FRONT=wiki-mcp BACKEND=wiki-service \
#   MCP_URL=http://wiki-mcp.team1.svc.cluster.local:8000/mcp \
#   TOOL=wiki_query DRIVER_IMAGE=docker.io/library/wiki_memory_tool-otel:latest \
#   lineage-sidecar/concurrency-test-mcp.sh
#
# Variables:
#   FRONT     (required) self_id of the MCP front (span names <FRONT>.inbound/.http).
#   BACKEND   (required) self_id of the backend HTTP service (span name <BACKEND>.inbound).
#   MCP_URL   (required) in-cluster streamable-http URL of the front (…/mcp).
#   TOOL      MCP tool name to call (default wiki_query).
#   DRIVER_IMAGE  image with the `mcp` SDK for the driver pod (default the wiki -otel image).
#   N         concurrent requests (default 6).
#   TOKENS    space-separated tags, one per request (default 6 planets).
#   NAMESPACE default team1.  WINDOW default '3 minutes'.  SETTLE default 12.
set -euo pipefail

FRONT="${FRONT:?set FRONT}"
BACKEND="${BACKEND:?set BACKEND}"
MCP_URL="${MCP_URL:?set MCP_URL}"
TOOL="${TOOL:-wiki_query}"
DRIVER_IMAGE="${DRIVER_IMAGE:-docker.io/library/wiki_memory_tool-otel:latest}"
N="${N:-6}"
TOKENS="${TOKENS:-MERCURY VENUS MARS JUPITER SATURN NEPTUNE URANUS PLUTO}"
NAMESPACE="${NAMESPACE:-team1}"
WINDOW="${WINDOW:-3 minutes}"
SETTLE="${SETTLE:-12}"
DRIVER_POD="mcp-lineage-driver"
toks=($TOKENS)

# ---- ensure driver pod (needs the mcp SDK) ----
if ! kubectl get pod -n "$NAMESPACE" "$DRIVER_POD" >/dev/null 2>&1; then
  kubectl run "$DRIVER_POD" -n "$NAMESPACE" --image="$DRIVER_IMAGE" \
    --image-pull-policy=IfNotPresent --restart=Never \
    --labels="kagenti.io/inject=disabled" --command -- sleep infinity >/dev/null
  kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/$DRIVER_POD" --timeout=90s
fi

# ---- python MCP driver: N concurrent sessions, one traceparent each ----
cat > /tmp/mcp_driver.py <<'PYEOF'
import asyncio, sys
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
URL = sys.argv[1]; TOOL = sys.argv[2]
reqs = [a.split(",") for a in sys.argv[3:]]   # tid,sid,token
async def one(tid, sid, token):
    tp = f"00-{tid}-{sid}-01"
    try:
        async with streamablehttp_client(url=URL, headers={"traceparent": tp}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.call_tool(TOOL, {"topic_id": token, "query": f"find {token}"})
                print(f"{token} -> ok")
    except Exception as e:
        print(f"{token} -> {type(e).__name__}: {str(e)[:70]}")
async def main(): await asyncio.gather(*[one(t, s, tok) for (t, s, tok) in reqs])
asyncio.run(main())
PYEOF
kubectl cp /tmp/mcp_driver.py "${NAMESPACE}/${DRIVER_POD}:/tmp/mcp_driver.py" >/dev/null

# host-generate distinct traceparents; remember token->trace
args=""; declare_traces=""
tokens_used=""
trace_list=""
for i in $(seq 0 $((N-1))); do
  tok="${toks[$i]}"; tid=$(openssl rand -hex 16); sid=$(openssl rand -hex 8)
  args="$args ${tid},${sid},${tok}"
  trace_list="${trace_list}${tok} ${tid}"$'\n'
  tokens_used="$tokens_used $tok"
done

echo ">> firing $N concurrent MCP '$TOOL' calls at $MCP_URL (front=$FRONT backend=$BACKEND)"
kubectl exec -i -n "$NAMESPACE" "$DRIVER_POD" -- /app/.venv/bin/python /tmp/mcp_driver.py "$MCP_URL" "$TOOL" $args

echo ">> waiting ${SETTLE}s for spans to reach DG..."
sleep "$SETTLE"

psql() { kubectl exec -n data-governance data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAc "$1"; }

echo ""
echo "=== 2-service trace propagation (front=$FRONT backend=$BACKEND, N=$N) ==="
match=0
while read -r tok tid; do
  [ -z "$tok" ] && continue
  front_out=$(psql "select count(*) from spans where service_name='authbridge' and name like '${FRONT}.%' and (attributes->>'lineage.hop.kind')='agent_to_service' and trace_id='${tid}';")
  back_in=$(psql "select count(*) from spans where service_name='authbridge' and name='${BACKEND}.inbound' and trace_id='${tid}';")
  ok="MISMATCH"
  if [ "${front_out:-0}" -ge 1 ] && [ "${back_in:-0}" -ge 1 ]; then ok="OK"; match=$((match+1)); fi
  printf '%-10s trace=%s  front_out=%s backend_in=%s [%s]\n' "$tok" "$tid" "${front_out:-0}" "${back_in:-0}" "$ok"
done <<< "$trace_list"
echo "-----------------------------------------"
echo "CORRECT 2-SERVICE PROPAGATION: ${match}/${N}"
