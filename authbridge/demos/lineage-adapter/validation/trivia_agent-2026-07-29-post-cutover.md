# Expectation card — `trivia_agent`

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `trivia_agent-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

*Filled BEFORE the run (WS-1 validation matrix, correctness slice #1). This is
the first exercise of the **shim path** on the two-span pipeline: the two live
apps validated earlier (weather, reservation) propagate `traceparent` natively,
so the propagate-only OTel shim has never been proven end-to-end against the
two-span producer + `sidecar_interactions` derivation.*

## Identity

| Field | Value |
|---|---|
| App | `trivia-agent` (fleet.conf line 32) |
| Entry protocol | `a2a` |
| `SELF_ID` | `weather-service`-style self id: `trivia-agent` |
| Image ref | `docker.io/library/trivia_agent-otel:latest` |
| Running image ID (recorded at run time) | agent `sha256:8cf1958274167d4dcf1b221c895ad58b68b19e17d73be41c12f7b707c430548e` · sidecar `sha256:692a668d6ac50403a239472f61a5c8e2ebc8b7b294ef4220fb0408566b8148c8` |
| LLM wiring | `LLM_API_BASE=http://host.containers.internal:11434/v1`, `LLM_MODEL=qwen2.5:7b`, `LLM_API_KEY=ollama` |

Framework axis (STUDY §10.10.2 row 0): raw OpenAI SDK / Starlette / httpx.
LLM-only — no MCP tool leg, so **no lifecycle/discovery noise is expected at
all**. That makes this card the cleanest possible control for WS-1: if
`mcp_lifecycle_*` or `tool_discovery_*` appears here, the classification is
over-reaching.

## Expected entities (kind + natural key)

| kind | natural_key | note |
|---|---|---|
| client | `client:<driver-pod-ip>` | anonymous caller (no JWT); ip varies per run |
| agent | `agent:trivia-agent` | the entry entity |
| llm | `llm:host.containers.internal/qwen2.5:7b` | ollama on the macOS host |

No tool/service entities — nothing downstream but the LLM.

## Expected interactions per turn

| leg (caller → callee) | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| client → agent (entry) | inbound a2a | `agent_request` / `agent_response` | 1 | no |
| agent → llm | outbound inference | `llm_chat_prompt` / `llm_completion` | 1..k | no |

Totals per turn: **2..(1+k) interactions = all visible + 0 hidden.**
Expected k=1 for a single-shot trivia answer; >1 only if the app loops.

Forest shape per trace: exactly **1 root** (callee `agent:trivia-agent`),
0 orphans, anchors == interactions, 0 duplicates.

### `EXPECT_KINDS` line (payload-kind counts)

`llm_*` left unlisted (nondeterministic if the agent loops); the entry pair is
pinned exactly.

```bash
EXPECT_KINDS='agent_request=1,agent_response=1'
```

## Expected NO-interaction legs (capture gaps — expected, not failures)

| leg | why absent |
|---|---|
| — | none; the only outbound hop (ollama) is plaintext HTTP through the sidecar |

## Known quirks

- Shim image carries `HOME=/tmp` + `UV_NO_CACHE=1` (STUDY §10.10.2 — this base
  image pre-seeds a root-owned uv cache).
- `imagePullPolicy: Always` on `:latest` applies to the app image; the running
  IDs above are the drift guard for this run.

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-interactions.sh`, N=6, `EXPECT_KINDS='agent_request=1,agent_response=1'` |
| Trace ids | `9079b8f6215c…`, `62b620d234a6…`, `51b5c6293e87…`, `3a294ce53817…`, `a8a472d47cf9…`, `d52f6d234244…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` — every trace `ix=2 entry=1 roots=1 orphan=0 anchors=2 dup=0` |
| Deviations | none. Identical structure to the pre-cutover run; app image unchanged (`sha256:8cf1958274…`, sidecar `692a668d…`). WS-1 control re-held on the legs schema: API kinds for `a8a472d47cf9…` = `agent_request=1, agent_response=1, llm_chat_prompt=1, llm_completion=1` — zero `mcp_lifecycle_*`/`tool_discovery_*`. |
| Verdict | **PASS** |
