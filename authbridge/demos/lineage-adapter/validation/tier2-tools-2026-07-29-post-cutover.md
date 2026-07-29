# Expectation card — Tier-2 standalone tools (`slack_tool`, `reservation_tool`)

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `tier2-tools-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

*Filled BEFORE the runs (WS-1 matrix, Tier-2 slice). Both tools were previously
exercised only as **callees of an agent**; here each is driven **directly as an
MCP entry**, the topology the parser fix (`6e8b0cca`) made observable. Fleet
re-attached with the uniform chain, app images unchanged.*

## Identity

| Field | slack_tool | reservation_tool |
|---|---|---|
| `SELF_ID` | `slack-tool` | `reservation-tool` |
| Entry protocol | `mcp` (streamable-http `/mcp`) | `mcp` |
| Tool called | `get_channels` (no args) | `search_restaurants` |
| Running image ID | `sha256:db321ff4e59a` | `sha256:331d6c08afca` |
| Sidecar | `sha256:692a668d6ac5` | same |
| LLM wiring | none | none |

## Expected entities (each)

| kind | natural_key | note |
|---|---|---|
| client | `client:<driver-pod-ip>` | anonymous caller |
| tool | `tool:<self_id>` | callee of the tools/call root |
| agent | `agent:<self_id>` | expected duplicate identity — bodyless `/mcp` legs classify as inbound http whose callee kind is `agent` (template documents this) |

## Expected interactions per turn

Same structure as wiki (MCP entry): the session is **multi-root by design** —
each entry exchange carries the caller's traceparent and derives its own
dangling-parent root. Acceptance: exactly one root is the tools/call.

| leg | dir/protocol | req / resp content_kind | hidden? |
|---|---|---|---|
| `initialize` | inbound mcp | `mcp_lifecycle_*` | yes |
| `notifications/initialized` | inbound mcp | `mcp_lifecycle_*` | yes |
| `tools/list` | inbound mcp | `tool_discovery_*` | yes |
| **`tools/call`** | inbound mcp | `tool_call_arguments` / `tool_call_result` | **no** |
| bodyless `/mcp` (SSE open / teardown) | inbound http | `mcp_lifecycle_*` | yes |

Predicted total ≈ **6–8 interactions**, of which exactly **1 is signal**. Under
WS-1 the flow view should therefore show a single row by default — the strongest
demonstration of the noise filter in the matrix, and the case that would have
been *empty* before the parser fix.

### `EXPECT_KINDS` (both)

```bash
EXPECT_KINDS='tool_call_arguments=1,tool_call_result=1'
```

Lifecycle counts depend on how the MCP client closes the stream, so they stay
unlisted.

## Expected NO-interaction legs

| leg | why absent |
|---|---|
| slack-tool → api.slack.com | HTTPS, and the tool runs on a fake `SLACK_BOT_TOKEN` — no real call leaves the cluster |
| reservation-tool → storage | in-process fixture data, no HTTP hop |

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-mcp-interactions.sh`, N=6, SETTLE=45 each. slack-tool: TOOL=get_channels; reservation-tool: TOOL=search_restaurants, args `{"cuisine":"italian"}`; both `EXPECT_KINDS='tool_call_arguments=1,tool_call_result=1'` |
| Trace ids | slack-tool `890d13651a1b…`, `c9338fd8b6db…`, `0642ebc89039…`, `2222b0831b55…`, `253d28f1f630…`, `cf78d31e7e2a…` · reservation-tool `ff5ad4fd1db2…`, `36373bec932c…`, `8f04995ae0fc…`, `1ccdb034cfdc…`, `8d8c2608b59c…`, `b40d694356f8…` |
| Harness summary | slack-tool `CLEAN TURNS: 6/6` — every trace `ix=5 roots=5 callroots=1 orphan=0 anchors=5 dup=0`, callee `tool:slack-tool` · reservation-tool `CLEAN TURNS: 6/6` — every trace `ix=5 roots=5 callroots=1 orphan=0 anchors=5 dup=0`, callee `tool:reservation-tool` |
| Deviations | slack-tool ix=5 vs the morning's 6: one fewer bodyless `/mcp` leg (kinds for `2222b0831b55…` = `mcp_lifecycle_request=4, mcp_lifecycle_result=4, tool_call_arguments=1, tool_call_result=1`) — the same client-side session-close variability the morning card documented for reservation-tool; lifecycle counts are deliberately unpinned. reservation-tool: identical to the morning run. Images slack-tool `sha256:db321ff4e5…`, reservation-tool `sha256:331d6c08af…` — unchanged. |
| Verdict | **PASS** |
