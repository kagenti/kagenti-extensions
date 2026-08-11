# Expectation card — `git_issue_agent`

*Filled BEFORE the run (WS-1 matrix). Fleet re-attached with the uniform parser
chain (`6e8b0cca`), app image unchanged.*

## Identity

| Field | Value |
|---|---|
| App | `git-issue-agent` (fleet.conf) |
| Entry protocol | `a2a` |
| `SELF_ID` | `git-issue-agent` |
| Image ref | `docker.io/library/git_issue_agent-otel:latest` |
| Running image ID | `sha256:037c86cc0c60d7854aed29c2c36d9e18ab94f642d7e0c912555a30c771acf5d8` · sidecar `sha256:692a668d6ac5` |
| LLM wiring | `TASK_MODEL_ID=openai/qwen2.5:7b` via `host.containers.internal:11434`; **`MCP_URL=""` — run LLM-only** |

Framework axis (STUDY §10.10.2 row 3): **crewai / litellm** / Starlette / httpx.
The framework axis is what this app proves; its `github_tool` is Go (mcp-go), a
documented instrumentation exception, and defaults to an external TLS
GitHub-Copilot-MCP needing auth — so the MCP leg is deliberately not exercised.

## Expected entities

| kind | natural_key |
|---|---|
| client | `client:<driver-pod-ip>` |
| agent | `agent:git-issue-agent` |
| llm | `llm:host.containers.internal/qwen2.5:7b` |

## Expected interactions per turn

| leg | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| client → agent | inbound a2a | `agent_request` / `agent_response` | 1 | no |
| agent → llm | outbound inference | `llm_chat_prompt` / `llm_completion` | 1..k | no |

Totals: **2..(1+k)**, all visible, **0 hidden** — third no-MCP control. crewai
issues multiple LLM calls per request, so k > 1 is expected and unpinned.

Forest: `entry=1`, callee `agent:git-issue-agent`, 0 orphans,
anchors == interactions, 0 dup. Total roots 1 (no sidecarred callee).

### `EXPECT_KINDS`

```bash
EXPECT_KINDS='agent_request=1,agent_response=1'
```

## Expected NO-interaction legs

| leg | why absent |
|---|---|
| agent → github_tool (MCP) | not wired this run (`MCP_URL=""`); and the tool is Go — Python auto-instrumentation cannot cover it (documented exception) |

## Known quirks

- Shim carries `HOME`/`UV_CACHE_DIR` under `/tmp` — this image ships a
  root-owned `/app/.cache/uv` that UID 1001 cannot write (STUDY §10.10.2).

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 / ys (driven by Claude) |
| Harness + N | `concurrency-test-interactions.sh`, N=6, `EXPECT_KINDS='agent_request=1,agent_response=1'` |
| Trace ids | `00da5fa473c6…`, `ce5cc07a4c63…`, `4de42e19b646…`, `421b5b69ae7e…`, `60b8d98c4389…`, `a03a0bd3811b…` |
| Harness summary | `CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6` — every trace `entry=1 roots=1 orphan=0 anchors=3 dup=0` |
| Observed | 3 interactions/turn = 1 entry + 2 LLM calls (k=2, within the card's unpinned range). **0 hidden** — third no-MCP control. |
| Deviations | none |
| Verdict | **PASS** |
