#!/usr/bin/env bash
# probe-cross-validate.sh — the CROSS-SESSION flow: data at rest between traces.
#
# Trace A (POST /stash/{tag} at probe-front): front mints deterministic A2A
# bytes and sends them to probe-back's /echo over the visible hop; back
# persists the raw bytes to BOTH stores lineage cannot see — a shared-PVC file
# and redis (RESP, port-excluded from the sidecar redirect).
#
# Trace B (LATER, POST /replay/{tag}): front reads both stores mid-flow and
# USES the data — re-sends the exact bytes to back's /echo, so trace B captures
# a payload byte-identical to trace A's.
#
# Assertions (the probe ASSERTS, including absences):
#   (a) app-level round-trips: stash sha == echo sha == replay file sha ==
#       replay redis sha, stores_match true (write path and read path are
#       DIFFERENT pods — two redis clients, one shared PVC).
#   (b) each trace derives a clean forest of exactly 2 interactions, 1 root —
#       and the two traces are DISCONNECTED trees (no parent link exists or
#       can exist across trace ids). Today's honest derivation.
#   (c) the invisible hops derive NOTHING: zero spans / interactions involving
#       redis in either trace; the file I/O has no wire at all.
#   (d) THE HOOK: DG payloads are content-addressed (interaction_payloads is
#       keyed by sha256 of canonical content). Because trace B's read bytes
#       equal trace A's written bytes, the two disconnected traces reference
#       the SAME content hash. The SQL below joins the traces on payload hash
#       — the seed of a future data-at-rest / cross-session lineage
#       workstream (see probe-app/README note).
#
# Env: TARGET (probe-front svc), SETTLE (30), NAMESPACE (team1),
#   DRIVER_POD (lineage-driver2), DG_NS (data-governance).
set -euo pipefail

TARGET="${TARGET:-probe-front.team1.svc.cluster.local:8080}"
SETTLE="${SETTLE:-30}"
NAMESPACE="${NAMESPACE:-team1}"
DRIVER_POD="${DRIVER_POD:-lineage-driver2}"
DG_NS="${DG_NS:-data-governance}"

# ---- ensure an in-cluster driver pod exists (recreate if not Running) ----
phase="$(kubectl get pod -n "$NAMESPACE" "$DRIVER_POD" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
if [ -n "$phase" ] && [ "$phase" != "Running" ]; then
  echo ">> driver pod $DRIVER_POD is $phase — recreating"
  kubectl delete pod -n "$NAMESPACE" "$DRIVER_POD" --wait=true >/dev/null
fi
if ! kubectl get pod -n "$NAMESPACE" "$DRIVER_POD" >/dev/null 2>&1; then
  echo ">> creating driver pod $DRIVER_POD in $NAMESPACE"
  kubectl run "$DRIVER_POD" -n "$NAMESPACE" --image=curlimages/curl:latest \
    --image-pull-policy=IfNotPresent --restart=Never \
    --labels="kagenti.io/inject=disabled" --command -- sleep infinity >/dev/null
  kubectl wait -n "$NAMESPACE" --for=condition=Ready "pod/$DRIVER_POD" --timeout=90s
fi

tok="C$(openssl rand -hex 3)"
tidA=$(openssl rand -hex 16); sidA=$(openssl rand -hex 8)
tidB=$(openssl rand -hex 16); sidB=$(openssl rand -hex 8)
body="{\"jsonrpc\":\"2.0\",\"id\":\"$tok\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"m-$tok\",\"parts\":[{\"kind\":\"text\",\"text\":\"cross-session probe $tok\"}]}}}"

echo ">> trace A (stash $tok, ${tidA:0:12}) then trace B (replay, ${tidB:0:12})"
kubectl exec -n "$NAMESPACE" "$DRIVER_POD" -- sh -c "
set -e
curl -sf -o /tmp/stash-$tok.json --max-time 60 -X POST http://$TARGET/stash/$tok \
  -H 'Content-Type: application/json' -H 'traceparent: 00-$tidA-$sidA-01' -d '$body'
sleep 2
curl -sf -o /tmp/replay-$tok.json --max-time 60 -X POST http://$TARGET/replay/$tok \
  -H 'Content-Type: application/json' -H 'traceparent: 00-$tidB-$sidB-01' -d '$body'
echo stash:;  cat /tmp/stash-$tok.json;  echo
echo replay:; cat /tmp/replay-$tok.json; echo"

echo ">> waiting ${SETTLE}s for spans to derive into interactions..."
sleep "$SETTLE"

psql() { kubectl exec -n "$DG_NS" data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAF $'\t' -c "$1"; }
jfield() {  # $1=file $2=key -> first string/number value of "key" in the driver-side json
  kubectl exec -n "$NAMESPACE" "$DRIVER_POD" -- sh -c \
    "sed -n 's/.*\"$2\":\"\\{0,1\\}\\([a-z0-9]*\\).*/\\1/p' /tmp/$1-$tok.json" | head -1
}

overall=0
fail() { echo "FAIL: $*"; overall=1; }

# -- (a) app-level round-trips --
s_sha=$(jfield stash stashed_sha256); s_echo=$(jfield stash sha256)
r_file=$(jfield replay file_sha256);  r_redis=$(jfield replay redis_sha256)
r_match=$(jfield replay stores_match)
echo ""
echo "app: stash=$s_sha echo=${s_echo:0:12}.. file=${r_file:0:12}.. redis=${r_redis:0:12}.. match=$r_match"
[ -n "$s_sha" ] && [ "$s_sha" = "$s_echo" ] || fail "stash bytes != echo capture"
[ "$s_sha" = "$r_file" ]  || fail "file read-back differs from stashed bytes"
[ "$s_sha" = "$r_redis" ] || fail "redis read-back differs from stashed bytes"
[ "$r_match" = "true" ]   || fail "replay stores_match != true"

# -- (b) two clean, DISCONNECTED trees --
for t in "$tidA" "$tidB"; do
  read -r nix roots orphans <<<"$(psql "
    WITH t AS (SELECT * FROM interactions WHERE trace_id='$t')
    SELECT (SELECT count(*) FROM t),
           (SELECT count(*) FROM t WHERE parent_interaction_id IS NULL),
           (SELECT count(*) FROM t c WHERE c.parent_interaction_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM t p WHERE p.id=c.parent_interaction_id))
  " | tr '\t' ' ')"
  echo "trace ${t:0:12}: ix=$nix roots=$roots orphans=$orphans (want 2/1/0)"
  [ "$nix" = "2" ] && [ "$roots" = "1" ] && [ "$orphans" = "0" ] || fail "trace ${t:0:12} shape"
done

# -- (c) the invisible hops derive NOTHING --
nredis=$(psql "SELECT
    (SELECT count(*) FROM spans WHERE trace_id IN ('$tidA','$tidB')
       AND (attributes->>'lineage.peer.host' ILIKE '%redis%' OR attributes->>'url.path' ILIKE '%6379%'))
  + (SELECT count(*) FROM interactions i JOIN entities e ON e.id=i.callee_entity_id
       WHERE i.trace_id IN ('$tidA','$tidB') AND e.natural_key ILIKE '%redis%')")
echo "redis-derived rows across both traces: $nredis (want 0 — RESP hop is invisible BY DESIGN)"
[ "$nredis" = "0" ] || fail "redis leaked into lineage"

# -- (d) THE HOOK: join the two disconnected traces on content hash --
shared=$(psql "
  SELECT l.payload_hash, count(DISTINCT i.trace_id)
  FROM interaction_legs l
  JOIN interactions i ON i.id = l.interaction_id
  JOIN interaction_payloads p ON p.content_hash = l.payload_hash
  WHERE i.trace_id IN ('$tidA','$tidB') AND l.payload_hash IS NOT NULL
    AND p.content::text LIKE '%cross-session stash $tok%'
  GROUP BY l.payload_hash
  HAVING count(DISTINCT i.trace_id) = 2")
echo ""
echo "cross-trace content-hash join (stash bytes present in BOTH traces):"
if [ -n "$shared" ]; then
  echo "$shared" | while IFS=$'\t' read -r h n; do echo "  hash ${h:0:16}.. seen in $n traces"; done
else
  fail "no shared content hash between trace A and trace B"
fi

echo ""
echo "==============================================================="
echo "cross-session: (a) write/read round-trips file+redis across pods"
echo "               (b) two clean trees, DISCONNECTED (today's honest truth)"
echo "               (c) redis + file hops derive NOTHING (asserted absences)"
echo "               (d) content-addressed payloads LINK the traces by hash"
if [ "$overall" = "0" ]; then echo "CROSS-SESSION VALIDATED ✔"; else echo "VALIDATION FAILED"; exit 1; fi
