# Expectation card — `trivia_agent`

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
| Date / operator | 2026-07-29 / ys (driven by Claude) |
| Harness + N | `concurrency-test-interactions.sh`, N=6, `EXPECT_KINDS='agent_request=1,agent_response=1'` |
| Trace ids | `b6739951b918…`, `7e1c86405173…`, `91b76ab67eac…`, `12809d755a2f…`, `85dad58d98bf…`, `7b4967a63efe…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` |
| Deviations | none in the pipeline. Structure matched the card exactly on the first run (2 interactions, 1 root, 0 orphans, anchors == interactions, 0 duplicate anchors, 6/6 distinct traces), and **no `mcp_lifecycle_*` / `tool_discovery_*` appeared** — the WS-1 control held: an LLM-only app produces no infrastructure noise. The first run nonetheless reported `0/6`, from a **defect in the harness**, now fixed (below). |
| Verdict | **PASS** |

### Harness defect found and fixed by this run (kit fix, not a pipeline finding)

The first run failed `EXPECT_KINDS` with `agent_response=0` on three traces and
`=2` on the other three — never 1, and summing to exactly 6 across 6 traces.

Cause: the check counted `interaction_payloads.content_kind` rows joined by
content hash. Payloads are **content-addressed**, and trivia-agent relays the
model's answer to the caller **unchanged** — so within one trace the A2A
response leg and the LLM completion leg are byte-identical, collapse into ONE
payload row, and that row carries ONE kind, whichever insert won:

```
230a5e59…|7d3e85df12|41a913a298   ← entry a2a   } same response hash
230a5e59…|423c887021|41a913a298   ← llm call    }

41a913a298 | agent_response | 187 | "Great! Here's an astronomy trivia question…"
6d769acd7d | llm_completion | 231 | "Great! Here's an astronomy trivia question…"
```

The derivation was correct throughout — the interactions API reported
`a2a: agent_request → agent_response` and `inference: llm_chat_prompt →
llm_completion` on **every** trace, including the ones the old check scored 0.

Fix (general, not a special case): `EXPECT_KINDS` now counts **derived kinds
per interaction leg**, read from the interactions API via the shared
`dg-api.sh` helpers — the same derivation the DG UI shows. The MCP harness's
tools/call root identification, which keyed on the same payload kind, moved to
the derived kind too. Re-run: **6/6**.

**Live evidence of relay/echo identity.** This is the echo phenomenon named in
the open items, observed live on the LLM leg: the same bytes legitimately play
two roles. Worth flagging to Igor as a schema-level observation (no action —
migration 0004's tables are the fixed interface): `content_kind` is stored as a
property of the *content*, but it is really a property of the content's *use*.
Nothing downstream depends on it — the UI and the API classify from anchor-span
attributes — so the collapse is cosmetic today, and only misleads a consumer
that reads payload kinds directly, as this harness did.
