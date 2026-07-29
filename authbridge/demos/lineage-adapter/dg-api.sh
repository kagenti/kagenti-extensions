#!/usr/bin/env bash
# Shared read helpers for the data-governance interactions API.
#
# WHY THIS FILE EXISTS
# --------------------
# Content kinds (`agent_request`, `tool_call_arguments`, `mcp_lifecycle_*`, …)
# are a READ-TIME derivation: the DG `sidecar_interactions` processor stores
# facts, and `derive.py` (`classify()` + `_KIND_TABLE`) re-applies the
# vocabulary to each interaction's anchor span when the API is read. The API is
# therefore the only faithful source for them — asserting kinds any other way
# forks the vocabulary and drifts the moment `_KIND_TABLE` changes.
#
# In particular, do NOT count `interaction_payloads.content_kind` rows: payloads
# are content-addressed (hash is the key), so when an agent relays a body
# verbatim — e.g. trivia-agent returning the model's answer unchanged — the A2A
# response leg and the LLM completion leg are the SAME bytes and collapse into
# ONE payload row carrying ONE kind. Per-kind row counts then depend on which
# insert won the race, and a leg can report 0 or 2 where the truth is 1.
# (Live evidence: validation/trivia_agent-2026-07-29.md.)
#
# The helpers run inside the API pod so the kit keeps its single `kubectl exec`
# access path — no host gateway URL, no jq/python requirement on the operator's
# machine.
#
# Env: DG_NS (data-governance namespace; default `data-governance`).

_dg_api_py() {
  # $1 = trace id, $2 = python program reading `doc` (interactions) and
  # `entities` (natural_key by entity id).
  kubectl exec -n "${DG_NS:-data-governance}" deploy/data-governance-ui -- \
    python -c '
import collections, json, sys, urllib.request


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path) as fh:
        return json.load(fh)


tid = sys.argv[1]
doc = get("/api/traces/%s/interactions" % tid)["interactions"]
entities = {e["id"]: e["natural_key"] for e in get("/api/traces/%s/entities" % tid)["entities"]}
exec(sys.argv[2])
' "$1" "$2"
}

# Derived content kinds for one trace, as `kind=count` lines. One entry per
# interaction LEG (request + response) — matching what the DG UI shows. Bodyless
# exchanges (SSE open / teardown) count: they derive an interaction and a kind
# even though they store no payload.
api_kinds() {
  _dg_api_py "$1" '
counts = collections.Counter()
for row in doc:
    kinds = row.get("kinds") or {}
    for field in ("request_content_kind", "response_content_kind"):
        if kinds.get(field):
            counts[kinds[field]] += 1
for kind, n in sorted(counts.items()):
    print("%s=%d" % (kind, n))
'
}

# Roots of one trace whose request kind is $2, as `<count> <callee_natural_key>`
# (natural key of the first such root, or `-` when there is none). Used by the
# MCP-entry harness: an MCP session is multi-root by design, so acceptance is
# "exactly one root is the tools/call", identified by its DERIVED kind.
api_roots_of_kind() {
  _dg_api_py "$1" '
roots = [r for r in doc
         if r.get("parent_interaction_id") is None
         and (r.get("kinds") or {}).get("request_content_kind") == "'"$2"'"]
print("%d %s" % (len(roots),
                 entities.get(roots[0]["callee_entity_id"], "-") if roots else "-"))
'
}
