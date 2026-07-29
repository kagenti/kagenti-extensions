# Expectation card — `a2a_currency_converter`

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `a2a_currency_converter-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

*Filled BEFORE the run (WS-1 matrix). Run against the fleet re-attached with the
uniform parser chain (`6e8b0cca`), app images unchanged.*

## Identity

| Field | Value |
|---|---|
| App | `a2a-currency-converter` (fleet.conf) |
| Entry protocol | `a2a` |
| `SELF_ID` | `a2a-currency-converter` |
| Image ref | `docker.io/library/a2a_currency_converter-otel:latest` |
| Running image ID | `sha256:4c4d4a55b062a7a550d908617af2de1e7b20a0430013eb613323b8a343ca69e6` · sidecar `sha256:692a668d6ac5` |
| LLM wiring | `qwen2.5:7b` via `host.containers.internal:11434` |

Framework axis (STUDY §10.10.2 row 1): LangGraph / Starlette / direct httpx +
langchain-openai.

## Expected entities

| kind | natural_key | note |
|---|---|---|
| client | `client:<driver-pod-ip>` | anonymous caller |
| agent | `agent:a2a-currency-converter` | entry entity |
| llm | `llm:host.containers.internal/qwen2.5:7b` | |

No tool entity: this agent's only external call is an HTTPS rate lookup, which
the sidecar cannot see (below).

## Expected interactions per turn

| leg | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| client → agent | inbound a2a | `agent_request` / `agent_response` | 1 | no |
| agent → llm | outbound inference | `llm_chat_prompt` / `llm_completion` | 1..k | no |

Totals: **2..(1+k) interactions**, all visible, **0 hidden** — like trivia, a
second control that WS-1's noise classification does not over-reach on an app
with no MCP. LangGraph's agentic loop makes k variable.

Forest: `entry=1`, callee `agent:a2a-currency-converter`, 0 orphans,
anchors == interactions, 0 duplicate anchors. Total roots expected 1 (no
sidecarred callee).

### `EXPECT_KINDS`

```bash
EXPECT_KINDS='agent_request=1,agent_response=1'
```

## Expected NO-interaction legs

| leg | why absent |
|---|---|
| agent → `api.frankfurter.dev` (or equivalent rate API) | **HTTPS — TLS passthrough**; the sidecar never sees plaintext. Contract-documented capture gap, and the named follow-up (SNI observer) is deliberately out of scope. |

## Known quirks

- `APP_ENTRYPOINT=python -m app` (STUDY §10.10.2 row 1).

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-interactions.sh`, N=6, `EXPECT_KINDS='agent_request=1,agent_response=1'` |
| Trace ids | `db20fa70f757…`, `3223f12deb04…`, `d3142e92585a…`, `65cb3929c16d…`, `c3890c41daee…`, `551b1b70b1be…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` — every trace `ix=3 entry=1 roots=1 orphan=0 anchors=3 dup=0` |
| Deviations | ix=3 vs 4 pre-cutover: one fewer LLM loop iteration (`llm_*` is 1..k on this card — nondeterministic loop depth, not a pipeline change). Control held: kinds for `551b1b70b1be…` = `agent_request=1, agent_response=1, llm_chat_prompt=2, llm_completion=2`, zero infra noise. App image `sha256:4c4d4a55b0…`. |
| Verdict | **PASS** |
