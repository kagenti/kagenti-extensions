# Expectation card — `weather-service` + `reservation-service` (WS-1 regression)

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `regression-weather-reservation-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

*Filled BEFORE the run (WS-1 validation matrix, correctness slice #3). These two
apps were validated live at E2 (2026-07-21) on the pre-WS-1 build. Re-running
them answers one question: **did WS-1 change labels only, or did it change
derivations?** Counts and forest structure must be unchanged; only the `kinds`
object is new.*

Two runs, deliberately separated so two independent changes don't confound:

- **Run A — as currently deployed** (a2a-only inbound parser chain, i.e. the
  same sidecar config the E2 records were taken under). Isolates WS-1.
- **Run B — after re-attaching with the fixed uniform parser chain**
  (`6e8b0cca`). Isolates the parser fix. Counts must stay as in run A; kinds on
  **tool pods** may improve, because a tool's own inbound MCP was previously
  unparsed.

## Identity

| Field | Value |
|---|---|
| Apps | `weather-service` (a2a entry, MCP tool `weather-tool-mcp`, **tool has no sidecar**) · `reservation-service` → `reservation-tool` (a2a entry, both sidecarred) |
| `SELF_ID` | `weather-service` · `reservation-service` |
| Image refs | upstream `:latest` — record running IDs at run time (`imagePullPolicy: Always`) |
| LLM wiring | both: `qwen2.5:7b` via `host.containers.internal:11434` |

## Prior record to reproduce (E2, 2026-07-21 — PLAN-two-span-delivery.md)

| app | forests | interactions per turn |
|---|---|---|
| weather-service | 6/6 clean, 6 distinct traces | **19 each** (fixed shape) |
| reservation-service | 6/6 clean, 6 distinct traces | **variable 33–108** (agentic loop depth) — the invariant is structural: 1 root, 0 orphans, anchors == interactions, 0 duplicate anchors |

Reservation's count is *expected* to vary between runs; asserting a fixed
number there would be asserting the model's mood, not the pipeline.

## Expected interactions per turn — weather (the fixed-shape one)

Observed today on both the E3 reference trace `cde6dbb8…` and a fresh turn
`e67126ab…`, identically:

| leg | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| client → agent | inbound a2a | `agent_request` / `agent_response` | 1 | no |
| agent → llm | outbound inference | `llm_chat_prompt` / `llm_completion` | 2 | no |
| agent → tool (`tools/call`) | outbound mcp | `tool_call_arguments` / `tool_call_result` | 1 | **no** |
| agent → tool (`initialize`) | outbound mcp | `mcp_lifecycle_*` | 3 | yes |
| agent → tool (`notifications/initialized`) | outbound mcp | `mcp_lifecycle_*` | 3 | yes |
| agent → tool (`tools/list`) | outbound mcp | `tool_discovery_*` | 3 | yes |
| agent → tool (bodyless `/mcp`) | outbound http | `mcp_lifecycle_*` | 6 | yes |

Totals: **19 = 4 visible + 15 hidden.**

### `EXPECT_KINDS` lines

Pinned: only what is deterministic. LLM loop depth drives the number of MCP
sessions, so lifecycle/discovery counts stay unlisted — pinning them would
assert the model's behaviour, not the pipeline's.

```bash
# weather-service
EXPECT_KINDS='agent_request=1,agent_response=1,tool_call_arguments=1,tool_call_result=1'
# reservation-service (variable depth; only the entry pair is deterministic)
EXPECT_KINDS='agent_request=1,agent_response=1'
```

## Expected NO-interaction legs

| leg | why absent |
|---|---|
| weather-service → weather-tool inbound view | `weather-tool` runs with **no sidecar** (1/1) — only the caller's outbound side is captured. Expected, and the reason MCP-entry was never exercised before wiki. |

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-interactions.sh`, N=6 each. weather: SETTLE=50, `EXPECT_KINDS='agent_request=1,agent_response=1,tool_call_arguments=1,tool_call_result=1'`; reservation: SETTLE=60, kinds unpinned (loop-variable) |
| Trace ids | weather `46be877a7afa…`, `c67129a75ede…`, `cac84e2ad582…`, `8a17507effe2…`, `f4b7e178caa1…`, `5267d0fbadc8…` · reservation `6af7045aac72…`, `8c2bae84d582…`, `4e1bdb4d6db7…`, `3e8019c42f54…`, `08b88fa009a1…`, `1087f703a748…` |
| Harness summary | weather `CLEAN FORESTS: 6/6` — every trace `ix=19 entry=1 roots=1 orphan=0 anchors=19 dup=0` · reservation `CLEAN FORESTS: 6/6` — every trace `ix=22 entry=1 roots=11 orphan=0 anchors=22 dup=0` |
| Deviations | weather: none — 19 ix/turn exactly as pre-cutover, and the **id-preservation claim is proven**: the re-derived interaction ids of pre-cutover trace `f6b86f1d7619…` are set-identical to the snapshot taken before the truncate (uuid5 over `{trace_id}/{exchange_id}` coincides with upstream's, as claimed). reservation: ix=22 roots=11 vs the morning's 32/16 — below the morning range but exactly the dual-sidecar 22/11 shape recorded in the harness header from 2026-07-21; loop depth is the acknowledged variable, the structural invariants (entry=1, 0 orphans, anchors == interactions, 0 dups) held 6/6. Images: weather agent `ghcr…weather_service@sha256:184f3298d1…`, reservation agent `sha256:bb4d064615…`, sidecar `692a668d…` — unchanged from the morning run. |
| Verdict | **PASS** |
