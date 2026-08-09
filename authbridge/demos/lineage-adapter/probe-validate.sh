#!/usr/bin/env bash
# probe-validate.sh — ONE run over the lineage-probe topology validating every
# lineage capability at once (deploy first: ./deploy-fleet.sh probe-tool
# probe-back probe-front):
#
#   (1) concurrent traces      N (default 2) simultaneous turns, distinct
#                              caller-minted traceparents -> N distinct
#                              single-rooted forests, zero orphans.
#   (2) thread propagation     probe-front fans out through a ThreadPoolExecutor
#                              and probe-back calls tool+LLM through another;
#                              pairing can only survive if the shim carries
#                              context across both hand-offs.
#   (3) inbound->outbound      probe-back holds its LEGS same-trace inbounds
#       exact pairing          open together, then emits 2 outbounds each
#                              (MCP tools/call + real LLM chat). Every outbound
#                              must parent on exactly the inbound whose tag it
#                              carries — trace membership cannot fake this.
#
# Ground truth: the tag in url.path (inbounds, tool outbounds) and in
# input.value (LLM outbounds). Derived-kind coverage is asserted through the
# interactions API (dg-api.sh): agent_request / llm_chat_prompt /
# tool_call_arguments per trace.
#
# Env: TARGET (probe-front svc), N (2), LEGS (3), SETTLE (30), NAMESPACE
#   (team1), DRIVER_POD (lineage-driver2), DG_NS (data-governance).
set -euo pipefail

TARGET="${TARGET:-probe-front.team1.svc.cluster.local:8080}"
N="${N:-2}"
LEGS="${LEGS:-3}"
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

# ---- fire N concurrent turns, each with its own trace id and tag ----
declare -a TIDS TOKS
script=$'set -e\n'
for i in $(seq 0 $((N-1))); do
  tok="T$(openssl rand -hex 3)"
  tid=$(openssl rand -hex 16)
  sid=$(openssl rand -hex 8)
  TIDS[$i]="$tid"; TOKS[$i]="$tok"
  body="{\"jsonrpc\":\"2.0\",\"id\":\"$tok\",\"method\":\"message/send\",\"params\":{\"message\":{\"role\":\"user\",\"messageId\":\"m-$tok\",\"parts\":[{\"kind\":\"text\",\"text\":\"all-capability probe $tok\"}]}}}"
  script+="curl -s -o /dev/null -w 'req $tok trace ${tid:0:8} -> HTTP %{http_code}\\n' --max-time 180 -X POST http://$TARGET/probe/$tok -H 'Content-Type: application/json' -H 'traceparent: 00-$tid-$sid-01' -d '$body' &"$'\n'
done
script+='wait'$'\n'

echo ">> firing $N concurrent probe turns at $TARGET (legs=$LEGS each)"
echo "$script" | kubectl exec -i -n "$NAMESPACE" "$DRIVER_POD" -- sh
echo ">> waiting ${SETTLE}s for spans to derive into interactions..."
sleep "$SETTLE"

psql() { kubectl exec -n "$DG_NS" data-governance-postgres-0 -- \
  psql -U data_governance -d data_governance -tAF $'\t' -c "$1"; }

. "$(dirname "${BASH_SOURCE[0]}")/dg-api.sh"

overall=0
for i in $(seq 0 $((N-1))); do
  tid="${TIDS[$i]}"; tok="${TOKS[$i]}"
  echo ""
  echo "=== trace $tok (${tid:0:12}) ==="

  # -- (1) forest sanity: interactions, single entry root, no orphans --
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
  read -r entryroots entry_callee <<<"$(api_roots_of_kind "$tid" agent_request)"
  printf 'forest: ix=%s roots=%s entry-roots=%s orphans=%s anchors=%s dup=%s callee=%s\n' \
    "${nix:-0}" "${roots:-?}" "${entryroots:-?}" "${orphans:-?}" "${nanchor:-?}" "${dupanchor:-?}" "${entry_callee:-?}"

  # -- (2)+(3) exact pairing at BOTH pods: every outbound request span's parent
  # must be the inbound request span carrying the SAME tag. Tag of a span =
  # leg tag (Txxxxxx-legN) if present anywhere, else the bare turn tag.
  rows=$(psql "
    SELECT
      o.attributes->>'lineage.self.id',
      COALESCE(
        substring(o.attributes->>'url.path'    from 'T[0-9a-f]{6}-leg[0-9]+'),
        substring(o.attributes->>'input.value' from 'T[0-9a-f]{6}-leg[0-9]+'),
        substring(o.attributes->>'url.path'    from 'T[0-9a-f]{6}'),
        substring(o.attributes->>'input.value' from 'T[0-9a-f]{6}'), '?'),
      COALESCE(
        substring(i.attributes->>'url.path'    from 'T[0-9a-f]{6}-leg[0-9]+'),
        substring(i.attributes->>'url.path'    from 'T[0-9a-f]{6}'), '<dangling>'),
      COALESCE(substring(o.attributes->>'url.path' for 24), '?')
    FROM spans o
    LEFT JOIN spans i
      ON i.trace_id = o.trace_id AND i.span_id = o.parent_id
    WHERE o.trace_id='$tid'
      AND o.attributes->>'lineage.role' = 'request'
      AND o.attributes->>'lineage.direction' = 'outbound'
    ORDER BY o.seq")
  ok=0; total=0
  printf '%-12s %-14s %-14s %-26s %s\n' "pod" "out-tag" "credited-in" "out-path" "verdict"
  while IFS=$'\t' read -r pod outtag intag opath; do
    [ -z "$pod" ] && continue
    total=$((total+1))
    v="MISATTRIBUTED"
    # A leg-tagged outbound at back must credit the SAME leg's inbound
    # (outtag == intag). At front the leg tag is minted BY the pod itself,
    # so its legs credit the bare-turn-tag inbound (outtag == intag-legN).
    case "$outtag" in
      "$intag"|"$intag"-leg*) v="OK"; ok=$((ok+1)) ;;
    esac
    printf '%-12s %-14s %-14s %-26s %s\n' "$pod" "$outtag" "$intag" "$opath" "$v"
  done <<<"$rows"

  # -- derived kind coverage (tools AND LLM present, correctly classified) --
  kinds=$(api_kinds "$tid")
  want_agent=$((1 + LEGS)); want_llm=$((1 + LEGS)); want_tool=$LEGS
  k_agent=$(printf '%s\n' "$kinds" | awk -F= '$1=="agent_request"{print $2}')
  k_llm=$(printf '%s\n' "$kinds" | awk -F= '$1=="llm_chat_prompt"{print $2}')
  k_tool=$(printf '%s\n' "$kinds" | awk -F= '$1=="tool_call_arguments"{print $2}')
  printf 'kinds: agent_request=%s(want %s) llm_chat_prompt=%s(want %s) tool_call_arguments=%s(want %s)\n' \
    "${k_agent:-0}" "$want_agent" "${k_llm:-0}" "$want_llm" "${k_tool:-0}" "$want_tool"

  # per-trace verdict: 1 entry root, clean forest, all outbounds paired,
  # expected outbound count (front: 1 llm + LEGS a2a; back: 2*LEGS), kinds exact
  want_out=$((1 + LEGS + 2*LEGS))
  if [ "${entryroots:-0}" = "1" ] && [ "${entry_callee:-}" = "agent:probe-front" ] \
     && [ "${orphans:-1}" = "0" ] && [ "${nix:-0}" = "${nanchor:-x}" ] && [ "${dupanchor:-1}" = "0" ] \
     && [ "$ok" = "$total" ] && [ "$total" = "$want_out" ] \
     && [ "${k_agent:-0}" = "$want_agent" ] && [ "${k_llm:-0}" = "$want_llm" ] && [ "${k_tool:-0}" = "$want_tool" ]; then
    echo "trace $tok: CLEAN (pairing $ok/$total)"
  else
    echo "trace $tok: FAIL (pairing $ok/$total, want $want_out outbounds)"
    overall=1
  fi
done

distinct=$(printf '%s\n' "${TIDS[@]}" | sort -u | wc -l | tr -d ' ')
echo ""
echo "==============================================================="
echo "capabilities: (1) $N concurrent traces [distinct=$distinct/$N]"
echo "              (2) thread propagation at front AND back"
echo "              (3) same-trace fan-in exact pairing (tool + LLM)"
[ "$distinct" = "$N" ] || overall=1
if [ "$overall" = "0" ]; then echo "ALL CAPABILITIES VALIDATED ✔"; else echo "VALIDATION FAILED"; exit 1; fi
