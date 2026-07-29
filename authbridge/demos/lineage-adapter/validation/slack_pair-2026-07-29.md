# Expectation card — `slack_researcher` → `slack_tool`

*WS-1 matrix, cross-service pair. Run against the fleet re-attached with the
uniform parser chain (`6e8b0cca`), app images unchanged.*

> **Process deviation, recorded honestly:** for this app the harness was run
> **before** the card was written, so the "expected" column below would be
> hindsight. The expectations are therefore stated as *what the STUDY §10.10.2
> row 4 record and the pipeline's rules predict*, and the observed values are
> reported separately; where the card would have been guessing, it says so.
> Cards for the remaining apps are written before their runs, as the workflow
> requires.

## Identity

| Field | Value |
|---|---|
| Apps | `slack-researcher` (a2a entry) → `slack-tool` (MCP tool) — both sidecarred |
| Entry protocol | `a2a` |
| `SELF_ID` | `slack-researcher` |
| Running image IDs | researcher `sha256:5eeef34a42fe` · tool `sha256:db321ff4e59a` · sidecar `sha256:692a668d6ac5` |
| LLM wiring | `qwen2.5:7b` via `host.containers.internal:11434` |

Framework axis (STUDY §10.10.2 row 4): **ag2/autogen**, the scenario that
forced the `-threading` instrumentor into the shim (LLM call dispatched via
`run_in_executor`, contextvars lost across the thread boundary). This run
re-proves that fix still holds on the two-span pipeline.

## Predicted shape (from the rules, not from the run)

| leg | dir/protocol | req / resp content_kind | hidden? |
|---|---|---|---|
| client → agent | inbound a2a | `agent_request` / `agent_response` | no |
| agent → llm | outbound inference | `llm_chat_prompt` / `llm_completion` | no |
| agent → tool (`tools/call`) | outbound mcp | `tool_call_arguments` / `tool_call_result` | **no** |
| tool inbound (same call) | inbound mcp | `tool_call_arguments` / `tool_call_result` | **no** |
| lifecycle + discovery, both sides | mcp / bodyless http | `mcp_lifecycle_*`, `tool_discovery_*` | yes |

Structural invariant asserted: `entry=1` with callee `agent:slack-researcher`,
0 orphans, anchors == interactions, 0 duplicate anchors. Total roots > 1
expected (dual-sidecar topology — see the regression card).

### `EXPECT_KINDS`

```bash
EXPECT_KINDS='agent_request=1,agent_response=1'
```

LLM count is ag2-loop dependent and MCP session count follows it, so only the
entry pair is pinned.

## Expected NO-interaction legs

| leg | why absent |
|---|---|
| slack-tool → api.slack.com | HTTPS + the tool runs on a fake `SLACK_BOT_TOKEN`; no real Slack call leaves the cluster |

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 / ys (driven by Claude) |
| Harness + N | `concurrency-test-interactions.sh`, N=6 |
| Trace ids | `3fb6b5438010…`, `b7305362cf36…`, `cbb38a8b9280…`, `5bae3998e611…`, `3cc0b946160c…`, `c0c8651dd496…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` — every trace `entry=1 roots=7 orphan=0 anchors=20 dup=0`, identical across all six |
| Cross-service | **one trace spans both services** — spans carry `lineage.self.id` of `slack-researcher` *and* `slack-tool`. Context propagation across the MCP boundary holds on the two-span pipeline. |
| Observed kinds (per turn, 20 interactions) | 7 × `inference llm_chat_prompt` · 4 × bodyless `http` lifecycle · 2 × `mcp initialize` · 2 × `mcp notifications/initialized` · 2 × `mcp tools/list` (discovery) · **2 × `mcp tools/call`** · 1 × `a2a agent_request`. **10 visible / 10 hidden.** |
| Deviations | none. The doubled `tools/call` is the parser fix working on an agent→tool chain: the caller's outbound view and the callee's inbound view of the same call. Before `6e8b0cca` the second one was anonymous `http` and would have been hidden as infrastructure. |
| Verdict | **PASS** |
