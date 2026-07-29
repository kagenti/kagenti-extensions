# Expectation card — `weather-service` + `reservation-service` (WS-1 regression)

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

## Results — Run A (as deployed, a2a-only inbound chain)

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 / ys (driven by Claude) |
| Running image IDs | weather-service `ghcr…weather_service@sha256:184f3298d158` · reservation-service `sha256:bb4d0646157b` · reservation-tool `sha256:331d6c08afca` · sidecar `sha256:692a668d6ac5` — **identical across runs A and B** (no image drift; run B changed only the parser chain) |
| weather-service | **6/6 clean, 6 distinct, 19 ix each** — reproduces the E2 record exactly |
| reservation-service | **6/6 clean, 6 distinct**, 32 ix (one turn 61) — structural invariants hold; see the roots finding below |
| Verdict | **PASS** — WS-1 changed labels, not derivations |

## Results — Run B (after re-attach with the uniform parser chain, `6e8b0cca`)

| Field | Value |
|---|---|
| weather-service | not re-attached — **`weather-service` is not in `fleet.conf`**; it is attached by a different mechanism (`authbridge-config-weather-service`), so the kit fix does not reach it. Harmless here: its entry is A2A and its MCP is outbound, both already parsed. Recorded as a coverage gap. |
| reservation-service | **6/6 clean, 6 distinct**, 32 ix (one turn 48) — same counts as run A |
| kinds delta vs run A | **counts identical, labels improved.** 9 exchanges moved from anonymous `http` to framed `mcp`: `tools/call` 1→2, `initialize` 3→6, `notifications/initialized` 3→6, `tools/list` 2→4, anonymous `http` 21→12. The new half is the **tool's own inbound view** of each call, previously unparsed. |
| Verdict | **PASS** |

```
RUN A  89d985a93e98…                     RUN B  778c07d67ffd…
  21 x http  -            lifecycle        12 x http  -            lifecycle
   3 x mcp   initialize                     6 x mcp   initialize
   3 x mcp   notifications/initialized      6 x mcp   notifications/initialized
   2 x mcp   tools/list   discovery         4 x mcp   tools/list   discovery
   1 x mcp   tools/call   TOOL CALL         2 x mcp   tools/call   TOOL CALL
   1 x a2a   -            entry             1 x a2a   -            entry
   1 x inference          llm               1 x inference          llm
  = 32 interactions                        = 32 interactions
```

### Structural finding: `roots == 1` was a single-sidecar assumption

Run A initially scored **0/6**: 16 roots per trace where the E2 record said 1.
This is **not** a WS-1 regression — it is a topology change, proven against
pre-WS-1 data already in Postgres (2026-07-21, a week before WS-1 existed):

| trace (2026-07-21) | sidecars reporting | interactions | roots |
|---|---|---|---|
| `3b363f28…` | reservation-service only | 33 | **1** |
| `1aca33e0…` | service **and** tool | 22 | **11** |

When the callee is sidecarred too, each of its inbound exchanges derives its
own dangling-parent root: apps propagate their own OTel context, not the
sidecar's span, so the wire parent is a span DG never stores (phantom-root
design — the derivation never guesses parents, per the wire contract). At E2
only the agent side was sidecarred, so `roots == 1` happened to hold; the
current fleet sidecars both sides, so it cannot.

Fix (general, matching what `concurrency-test-mcp-interactions.sh` already
did): assert **exactly one root is the ENTRY exchange** — derived request kind
`agent_request`, callee `agent:<SELF_ID>` — and *report* the total root count
for the card. Both harnesses now share one acceptance model, and it holds
across both topologies: weather (single sidecar) 6/6 with `entry=1 roots=1`,
reservation (dual sidecar) 6/6 with `entry=1 roots=16`.
