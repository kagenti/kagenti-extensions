# Expectation card — `slack_researcher` → `slack_tool`

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `slack_pair-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

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
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-interactions.sh`, N=6, SETTLE=60 (SELF_ID=slack-researcher) |
| Trace ids | `d3ac933d0c36…`, `efe84dcfa8b3…`, `24238cb1c607…`, `b8386ddf6858…`, `deb971b6c582…`, `6855cf583160…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` — every trace `ix=20 entry=1 roots=7 orphan=0 anchors=20 dup=0`, one trace spanning both sidecarred services |
| Deviations | none — 20 ix / roots=7, identical to pre-cutover. WS-1 UI re-checked in the browser on `6855cf583160…`: flow view hides 10 infrastructure interactions by default, `?showInfra=1` round-trips (toggle writes it, fresh load honors it), and the new `?flat=1` mirrors the Flat view checkbox both directions, rendering per-leg rows (independent request/response status). Masthead shows APP_VERSION `e909438`. Researcher image `sha256:5eeef34a42…`. |
| Verdict | **PASS** |
