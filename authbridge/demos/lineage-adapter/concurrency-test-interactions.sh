#!/usr/bin/env bash
# WS-E E2: two-span interaction-level concurrency test.
#
# The sibling concurrency-test.sh was written for the OLD one-span plugin: it
# pairs `<SELF_ID>.inbound` spans to `.llm`/`.mcp` outbound spans by the
# removed `lineage.hop.kind` attribute. The two-span rewrite (WS-A) emits
# request+response span PAIRS with lineage.role/direction/exchange.id and the
# derivation (WS-B) reconciles them into the data-governance `interactions`
# forest — so acceptance is asserted at the INTERACTION level, not on span
# names.
#
# This harness fires N concurrent A2A turns at a stamped agent, each with a
# DISTINCT caller-minted W3C traceparent (so the driver owns each turn's
# trace_id), then asserts per trace_id in the DG Postgres:
#   * the trace derived at least one interaction  (turn was observed)
#   * EXACTLY ONE root is the ENTRY exchange — derived request kind
#     `agent_request` — and ITS callee is agent:<SELF_ID>
#
#     NOT `roots == 1`: that holds only when a single sidecar observes the
#     turn. When callee services are sidecarred too, each of their inbound
#     exchanges derives its OWN dangling-parent root, because apps propagate
#     their own OTel context rather than the sidecar's span (phantom-root
#     design, wire contract — the derivation never guesses parents). Verified
#     against pre-WS-1 data: on 2026-07-21 a reservation turn observed by one
#     sidecar derived 33 interactions / 1 root, while a turn observed by both
#     service and tool derived 22 / 11. The total root count is asserted
#     against EXPECT_ROOTS when set (fill it from the app's expectation card);
#     without it the count is only reported — plus one unconditional guard:
#   * the forest never COLLAPSES — a multi-interaction trace where EVERY
#     interaction is a root means zero parent links survived, i.e. the splice
#     is broken end-to-end. The 89cfbedf relaxation (dropping roots==1) left
#     this failure mode invisible: orphans counts only NON-root rows with
#     missing parents, so total collapse was vacuously "0 orphans" and passed.
#   * ZERO orphans — every non-root interaction's parent is in the same trace
#   * #interactions == #anchor interaction_spans, and no anchor span maps to
#     more than one interaction  (no double-counted exchange / splice regression)
#   * the N traces are all DISTINCT
#   * optionally, when EXPECT_KINDS is set: per-trace DERIVED content-kind
#     counts match every listed kind exactly
# Target: N/N clean forests.
#
# Usage:
#   SELF_ID=weather-service \
#   TARGET=weather-service.team1.svc.cluster.local:8080 \
#   ./concurrency-test-interactions.sh
#
# Env:
#   SELF_ID   (required) the agent's lineage self_id — the derived callee
#             natural key is agent:<SELF_ID> for the entry interaction.
#   TARGET    (required) in-cluster host:port of the agent A2A Service (POST /).
#   N         concurrent turns (default 6)
#   PROMPT    user text; {TOKEN} is substituted per turn (default weather ask)
#   NAMESPACE agent namespace (default team1)
#   SETTLE    seconds to wait for spans to derive (default 25)
#   DRIVER_POD in-cluster curl pod name (default lineage-driver2)
#   DG_NS     data-governance namespace (default data-governance)
#   EXPECT_KINDS optional "kind=count,kind=count" (e.g.
#             "tool_call_arguments=1,mcp_lifecycle_request=2"). Per trace, read
#             the derived kinds from the interactions API and count one entry
#             per interaction LEG (request + response), then require each
#             LISTED kind to match EXACTLY. Unlisted kinds are ignored — leave
#             nondeterministic ones (llm_*) unpinned. Counts are leg counts, so
#             a bodyless exchange (SSE open / teardown) DOES count: it derives
#             an interaction and a kind even though it stores no payload. Fill
#             the value from the app's expectation card
#             (validation/TEMPLATE-expectation-card.md).
#   EXPECT_ROOTS optional exact per-trace root count (from the app's
#             expectation card). Asserted when set; only reported when unset.
set -euo pipefail

SELF_ID="${SELF_ID:?set SELF_ID}"
TARGET="${TARGET:?set TARGET (host:port of agent Service)}"
N="${N:-6}"
PROMPT="${PROMPT:-what is the weather in Tokyo? ref {TOKEN}}"
NAMESPACE="${NAMESPACE:-team1}"
SETTLE="${SETTLE:-25}"
DRIVER_POD="${DRIVER_POD:-lineage-driver2}"
DG_NS="${DG_NS:-data-governance}"
EXPECT_KINDS="${EXPECT_KINDS:-}"
EXPECT_ROOTS="${EXPECT_ROOTS:-}"

# ---- ensure an in-cluster driver pod exists (IfNotPresent: works offline) ----
# A pod that exists but isn't Running (e.g. left in Unknown/Failed by a node
# restart) can't be exec'd into — recreate it rather than crash on exec.
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

# ---- build N concurrent tagged requests, each with its own traceparent ----
declare -a TIDS TOKS
script=$'set -e\n'
for i in $(seq 0 $((N-1))); do
  tok="T$(openssl rand -hex 3)"
  tid=$(openssl rand -hex 16)
  sid=$(openssl rand -hex 8)
  TIDS[$i]="$tid"; TOKS[$i]="$tok"
  text="${PROMPT//\{TOKEN\}/$tok}"
  body="{\"jsonrpc\":\"2.0\",\"id\":\"$i\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"m$i\",\"parts\":[{\"kind\":\"text\",\"text\":\"$text\"}]}}}"
  line="curl -s -o /dev/null -w 'req $tok -> HTTP %{http_code}\\n' -X POST http://$TARGET/ "
  line+="-H 'Content-Type: application/json' -H 'traceparent: 00-$tid-$sid-01' -d '$body' &"
  script+="$line"$'\n'
done
script+='wait'$'\n'

echo ">> firing $N concurrent turns at $TARGET (self_id=$SELF_ID)"
echo "$script" | kubectl exec -i -n "$NAMESPACE" "$DRIVER_POD" -- sh
echo ">> waiting ${SETTLE}s for spans to derive into interactions..."
sleep "$SETTLE"

psql() { kubectl exec -n "$DG_NS" data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAF $'\t' -c "$1"; }

# Derived content kinds come from the interactions API — see dg-api.sh for why
# `interaction_payloads.content_kind` is not a sound substitute.
. "$(dirname "${BASH_SOURCE[0]}")/dg-api.sh"

echo ""
echo "=== per-turn interaction forest (self_id=$SELF_ID, N=$N) ==="
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
  # The ENTRY exchange is identified by its derived request kind, and the total
  # root count is reported rather than asserted — see the header for why
  # roots==1 is a single-sidecar assumption, not an invariant.
  read -r entryroots callee_root <<<"$(api_roots_of_kind "$tid" agent_request)"
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
  # Roots: exact when the card pins it; always guard against total collapse
  # (nix>1 with every interaction a root = zero parent links = broken splice —
  # invisible to the orphans count, which only sees non-root rows).
  rmiss=""
  if [ -n "$EXPECT_ROOTS" ] && [ "${roots:-0}" != "$EXPECT_ROOTS" ]; then
    rmiss=" roots=${roots:-?}(want=$EXPECT_ROOTS)"
  fi
  if [ "${nix:-0}" -gt 1 ] && [ "${roots:-0}" = "${nix:-0}" ]; then
    rmiss="$rmiss forest-collapsed(roots==ix==$nix)"
  fi
  ok="FAIL"
  if [ "${nix:-0}" -ge 1 ] && [ "$entryroots" = "1" ] && [ "$orphans" = "0" ] \
     && [ "$nix" = "$nanchor" ] && [ "$dupanchor" = "0" ] \
     && [ "$callee_root" = "agent:$SELF_ID" ] && [ -z "$kmiss" ] && [ -z "$rmiss" ]; then
    ok="OK"; pass=$((pass+1))
  fi
  printf '%-8s trace=%s ix=%-3s entry=%s roots=%-3s orphan=%s anchors=%-3s dup=%s callee=%-24s [%s]\n' \
    "$tok" "${tid:0:12}" "${nix:-0}" "${entryroots:-?}" "${roots:-?}" "${orphans:-?}" "${nanchor:-?}" "${dupanchor:-?}" "${callee_root:-<none>}" "$ok"
  if [ -n "$kmiss" ]; then echo "         kind mismatch:$kmiss"; fi
  if [ -n "$rmiss" ]; then echo "         root mismatch:$rmiss"; fi
done

# distinct traces
distinct=$(printf '%s\n' "${TIDS[@]}" | sort -u | wc -l | tr -d ' ')
echo "-----------------------------------------"
echo "CLEAN FORESTS: ${pass}/${N}   DISTINCT TRACES: ${distinct}/${N}"
[ "$pass" = "$N" ] && [ "$distinct" = "$N" ]
