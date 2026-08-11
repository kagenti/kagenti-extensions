# Expectation card — `a2a_contact_extractor`

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `a2a_contact_extractor-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

*Filled BEFORE the run (WS-1 matrix). Fleet re-attached with the uniform parser
chain (`6e8b0cca`), app image unchanged.*

## Identity

| Field | Value |
|---|---|
| App | `a2a-contact-extractor` (fleet.conf) |
| Entry protocol | `a2a` |
| `SELF_ID` | `a2a-contact-extractor` |
| Image ref | `docker.io/library/a2a_contact_extractor-otel:latest` (with the `Dockerfile.contact-extractor-fix` overlay) |
| Running image ID | `sha256:41cf5a6c461c6b694325bca5df3937763101e580e1ed4cd2d0d70df4f42feaa2` · sidecar `sha256:692a668d6ac5` |
| LLM wiring | ollama via `OPENAI_BASE_URL`, model alias **`gpt-4o-mini`** (present in the host ollama: `gpt-4o-mini:latest`, a copy of qwen2.5:7b) |

Framework axis (STUDY §10.10.2 row 2): **Marvin / pydantic-ai** / Starlette /
httpx-SDK.

## Expected entities

| kind | natural_key |
|---|---|
| client | `client:<driver-pod-ip>` |
| agent | `agent:a2a-contact-extractor` |
| llm | `llm:host.containers.internal/gpt-4o-mini` — note the **alias**, not `qwen2.5:7b`; the entity key follows the model name on the wire |

## Expected interactions per turn

| leg | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| client → agent | inbound a2a | `agent_request` / `agent_response` | 1 | no |
| agent → llm | outbound inference | `llm_chat_prompt` / `llm_completion` | 1..k | no |

Totals: **2..(1+k)**, all visible, **0 hidden** — fourth no-MCP control.

Forest: `entry=1`, callee `agent:a2a-contact-extractor`, 0 orphans,
anchors == interactions, 0 dup. Total roots 1.

**Known capture nuance (STUDY row 2):** Marvin streams its outputs, and streamed
response bodies are not parsed — so `llm_completion` payload rows may be absent
even though the interaction and its kind exist. This is exactly why
`EXPECT_KINDS` counts derived kinds per leg rather than payload rows
(`cdc9ea38`); under the old payload-row check this app would have mis-scored.

### `EXPECT_KINDS`

```bash
EXPECT_KINDS='agent_request=1,agent_response=1'
```

## Expected NO-interaction legs

| leg | why absent |
|---|---|
| — | none; the only outbound hop is the LLM, plaintext through the sidecar |

## Known quirks

- Marvin's `MARVIN_AGENT_MODEL` is a strict Literal enum → the model must be
  called `gpt-4o-mini` (aliased in ollama), not `qwen2.5:7b`.
- Prebuilt image's marvin 3.2.7 crashes on its pinned pydantic-ai 1.106.0 →
  overlay pins `pydantic-ai==1.20.0`.

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-interactions.sh`, N=6, SETTLE=60, `EXPECT_KINDS='agent_request=1,agent_response=1'` |
| Trace ids | `afe9ec1697cb…`, `88f4ee0237c7…`, `ac83bad1c3c6…`, `7c5c33db014a…`, `dad4fc458559…`, `2ed15c2904d6…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` — every trace `ix=5 entry=1 roots=1 orphan=0 anchors=5 dup=0` |
| Deviations | none — ix=5 inside the 5–7 Marvin-loop envelope. Control held: kinds for `dad4fc458559…` = `agent_request=1, agent_response=1, llm_chat_prompt=4, llm_completion=4`, zero infra noise. App image `sha256:41cf5a6c46…`. |
| Verdict | **PASS** |
