#!/usr/bin/env bash
# Two-span interaction-level concurrency test for an MCP-ENTRY app (a tool
# served over MCP streamable-http). Companion to concurrency-test-interactions.sh
# (A2A entry); the driving half is ported from the retired one-span
# concurrency-test-mcp.sh, whose assertions grepped span attributes
# (`lineage.hop.kind`) the two-span plugin no longer emits.
#
# Fires N CONCURRENT MCP tools/call turns at the tool, each session with a
# DISTINCT caller-minted W3C traceparent (so the driver owns each turn's
# trace_id), then asserts per trace_id in the DG Postgres.
#
# ROOTS — deliberate difference from the A2A harness: an A2A turn is ONE entry
# POST, so exactly-one-root holds there. An MCP session is SEVERAL entry HTTP
# exchanges (initialize, notifications/initialized, tools/call, and usually a
# bodyless GET stream-open / DELETE teardown), all carrying the same
# caller-minted traceparent — so EACH derives a dangling-parent ROOT
# interaction (the derivation never guesses parents; wire contract). Asserting
# roots==1 is structurally impossible here. Per trace this harness asserts:
#   * the trace derived at least one interaction  (turn was observed)
#   * EXACTLY ONE root is the tools/call — derived request content kind
#     'tool_call_arguments' — and ITS callee is tool:<SELF_ID>
#   * ZERO orphans — every non-root interaction's parent is in the same trace
#   * #interactions == #anchor interaction_spans, and no anchor span maps to
#     more than one interaction  (no double-counted exchange / splice regression)
#   * the N traces are all DISTINCT
#   * optionally, when EXPECT_KINDS is set: per-trace DERIVED content-kind
#     counts match every listed kind exactly
# The TOTAL root count per trace is asserted against EXPECT_ROOTS when set
# (fill from the app's expectation card), only reported when unset;
# lifecycle/discovery volume is pinned via EXPECT_KINDS.
# Target: N/N clean.
#
# Usage:
#   SELF_ID=wiki-mcp \
#   MCP_URL=http://wiki-mcp.team1.svc.cluster.local:8000/mcp \
#   TOOL=wiki_query DRIVER_IMAGE=docker.io/library/wiki_memory_tool-otel:latest \
#   ./concurrency-test-mcp-interactions.sh
#
# Env:
#   SELF_ID   (required) the tool's lineage self_id — the derived callee
#             natural key of the tools/call root is tool:<SELF_ID>.
#   MCP_URL   (required) in-cluster streamable-http URL of the tool (…/mcp).
#   TOOL      MCP tool name to call (default wiki_query)
#   TOOL_ARGS JSON object template for the call arguments; {TOKEN} is
#             substituted per turn (default the wiki_query shape)
#   DRIVER_IMAGE image with the `mcp` SDK for the driver pod (default the wiki
#             -otel image; must be kind-loaded)
#   DRIVER_PYTHON interpreter path inside DRIVER_IMAGE (default /app/.venv/bin/python)
#   N         concurrent turns (default 6)
#   NAMESPACE tool namespace (default team1)
#   SETTLE    seconds to wait for spans to derive (default 25)
#   DRIVER_POD in-cluster driver pod name (default mcp-lineage-driver)
#   DG_NS     data-governance namespace (default data-governance)
#   EXPECT_KINDS optional "kind=count,kind=count" — same semantics as in
#             concurrency-test-interactions.sh: exact per-trace DERIVED
#             content-kind counts for the LISTED kinds only, one entry per
#             interaction leg. Bodyless exchanges (SSE open / teardown) DO
#             count — they derive an interaction and a kind, just no payload.
#             Fill from the app's expectation card
#             (validation/TEMPLATE-expectation-card.md).
#   EXPECT_ROOTS optional exact per-trace root count (from the app's
#             expectation card). Asserted when set; only reported when unset.
set -euo pipefail

SELF_ID="${SELF_ID:?set SELF_ID}"
MCP_URL="${MCP_URL:?set MCP_URL (streamable-http URL, …/mcp)}"
TOOL="${TOOL:-wiki_query}"
DEFAULT_ARGS='{"topic_id":"{TOKEN}","query":"find {TOKEN}"}'
TOOL_ARGS="${TOOL_ARGS:-$DEFAULT_ARGS}"
DRIVER_IMAGE="${DRIVER_IMAGE:-docker.io/library/wiki_memory_tool-otel:latest}"
DRIVER_PYTHON="${DRIVER_PYTHON:-/app/.venv/bin/python}"
N="${N:-6}"
NAMESPACE="${NAMESPACE:-team1}"
SETTLE="${SETTLE:-25}"
DRIVER_POD="${DRIVER_POD:-mcp-lineage-driver}"
DG_NS="${DG_NS:-data-governance}"
EXPECT_KINDS="${EXPECT_KINDS:-}"
EXPECT_ROOTS="${EXPECT_ROOTS:-}"

# ---- ensure a driver pod with the mcp SDK exists (IfNotPresent: works offline) ----
if ! kubectl get pod -n "$NAMESPACE" "$DRIVER_POD" >/dev/null 2>&1; then
  echo ">> creating driver pod $DRIVER_POD in $NAMESPACE"
  kubectl run "$DRIVER_POD" -n "$NAMESPACE" --image="$DRIVER_IMAGE" \
    --image-pull-policy=IfNotPresent --restart=Never \
    --labels="kagenti.io/inject=disabled" --command -- sleep infinity >/dev/null
  kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/$DRIVER_POD" --timeout=90s
fi

# ---- python MCP driver: N concurrent sessions, one traceparent each ----
cat > /tmp/mcp_driver.py <<'PYEOF'
import asyncio, json, sys
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
URL, TOOL, TMPL = sys.argv[1], sys.argv[2], sys.argv[3]
reqs = [a.split(",") for a in sys.argv[4:]]   # tid,sid,token
async def one(tid, sid, token):
    tp = f"00-{tid}-{sid}-01"
    args = json.loads(TMPL.replace("{TOKEN}", token))
    try:
        async with streamablehttp_client(url=URL, headers={"traceparent": tp}) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.call_tool(TOOL, args)
                print(f"{token} -> ok")
    except Exception as e:
        print(f"{token} -> {type(e).__name__}: {str(e)[:70]}")
async def main(): await asyncio.gather(*[one(t, s, tok) for (t, s, tok) in reqs])
asyncio.run(main())
PYEOF
kubectl cp /tmp/mcp_driver.py "${NAMESPACE}/${DRIVER_POD}:/tmp/mcp_driver.py" >/dev/null

# ---- host-mint N distinct traceparents, one tagged turn each ----
declare -a TIDS TOKS
args=""
for i in $(seq 0 $((N-1))); do
  tok="T$(openssl rand -hex 3)"
  tid=$(openssl rand -hex 16)
  sid=$(openssl rand -hex 8)
  TIDS[$i]="$tid"; TOKS[$i]="$tok"
  args="$args ${tid},${sid},${tok}"
done

echo ">> firing $N concurrent MCP '$TOOL' turns at $MCP_URL (self_id=$SELF_ID)"
kubectl exec -i -n "$NAMESPACE" "$DRIVER_POD" -- \
  "$DRIVER_PYTHON" /tmp/mcp_driver.py "$MCP_URL" "$TOOL" "$TOOL_ARGS" $args
echo ">> waiting ${SETTLE}s for spans to derive into interactions..."
sleep "$SETTLE"

psql() { kubectl exec -n "$DG_NS" data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAF $'\t' -c "$1"; }

# Derived content kinds come from the interactions API — see dg-api.sh for why
# `interaction_payloads.content_kind` is not a sound substitute.
. "$(dirname "${BASH_SOURCE[0]}")/dg-api.sh"

echo ""
echo "=== per-turn interaction forest, MCP entry (self_id=$SELF_ID, N=$N) ==="
pass=0
for i in $(seq 0 $((N-1))); do
  tid="${TIDS[$i]}"; tok="${TOKS[$i]}"
  read -r nix roots orphans nanchor dupanchor <<<"$(psql "
    WITH t AS (SELECT * FROM interactions WHERE trace_id='$tid')
    SELECT
      (SELECT count(*) FROM t),
      (SELECT count(*) FROM t WHERE parent_interaction_id IS NULL),
      (SELECT count(*) FROM t c WHERE c.parent_interaction_id IS NOT NULL
         AND NOT EXISTS (SELECT 1 FROM t p WHERE p.id=c.parent_interaction_id)),
      (SELECT count(*) FROM interaction_spans WHERE trace_id='$tid' AND role='anchor'),
      (SELECT count(*) FROM (SELECT span_id FROM interaction_spans
         WHERE trace_id='$tid' AND role='anchor'
         GROUP BY span_id HAVING count(DISTINCT interaction_id)>1) d)
  " | tr '\t' ' ')"
  # the tools/call root is identified by its DERIVED request kind, not by a
  # payload row (dg-api.sh explains why payload kinds are not 1:1 with legs)
  read -r callroots callee_call <<<"$(api_roots_of_kind "$tid" tool_call_arguments)"
  # optional EXPECT_KINDS: exact per-trace derived content-kind counts
  kmiss=""
  if [ -n "$EXPECT_KINDS" ]; then
    kcounts=$(api_kinds "$tid")
    for pair in ${EXPECT_KINDS//,/ }; do
      kind="${pair%%=*}"; want="${pair#*=}"
      have=$(printf '%s\n' "$kcounts" | awk -F= -v k="$kind" '$1==k{print $2}')
      [ "${have:-0}" = "$want" ] || kmiss="$kmiss $kind=${have:-0}(want=$want)"
    done
  fi
  # optional EXPECT_ROOTS: exact per-trace root count from the expectation
  # card. No collapse guard here — a sidecar-less driver means every tool
  # inbound legitimately roots itself (roots==ix is this harness's shape).
  rmiss=""
  if [ -n "$EXPECT_ROOTS" ] && [ "${roots:-0}" != "$EXPECT_ROOTS" ]; then
    rmiss=" roots=${roots:-?}(want=$EXPECT_ROOTS)"
  fi
  ok="FAIL"
  if [ "${nix:-0}" -ge 1 ] && [ "$orphans" = "0" ] \
     && [ "$nix" = "$nanchor" ] && [ "$dupanchor" = "0" ] \
     && [ "$callroots" = "1" ] && [ "$callee_call" = "tool:$SELF_ID" ] \
     && [ -z "$kmiss" ] && [ -z "$rmiss" ]; then
    ok="OK"; pass=$((pass+1))
  fi
  printf '%-8s trace=%s ix=%-3s roots=%-2s callroots=%s orphan=%s anchors=%-3s dup=%s callee=%-20s [%s]\n' \
    "$tok" "${tid:0:12}" "${nix:-0}" "${roots:-?}" "${callroots:-?}" "${orphans:-?}" "${nanchor:-?}" "${dupanchor:-?}" "${callee_call:-<none>}" "$ok"
  if [ -n "$kmiss" ]; then echo "         kind mismatch:$kmiss"; fi
  if [ -n "$rmiss" ]; then echo "         root mismatch:$rmiss"; fi
done

# distinct traces
distinct=$(printf '%s\n' "${TIDS[@]}" | sort -u | wc -l | tr -d ' ')
echo "-----------------------------------------"
echo "CLEAN TURNS: ${pass}/${N}   DISTINCT TRACES: ${distinct}/${N}"
[ "$pass" = "$N" ] && [ "$distinct" = "$N" ]
