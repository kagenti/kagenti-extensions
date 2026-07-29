# Expectation card — `wiki_memory_tool`

*T9 post-cutover re-run (2026-07-29): live cluster moved to the sidecar interactions algorithm (`INTERACTIONS_ALGORITHM=sidecar`, legs schema, migration 0009, branch `feat/interactions-sidecar-algorithm`). Expectations carried over unchanged from `wiki_memory_tool-2026-07-29.md` — the point is reproducing the same numbers on the legs schema.*

*Filled BEFORE the run (WS-1 validation matrix, correctness slice #2). First use
of `concurrency-test-mcp-interactions.sh` on the two-span pipeline, and the
folded-in "daylight look" at the drifted MCP framing on the newer app images.*

## Identity

| Field | Value |
|---|---|
| App | `wiki-mcp` (front, MCP entry) → `wiki-service` (backend, FastAPI) — fleet.conf lines 28–29 |
| Entry protocol | `mcp` (streamable-http, `/mcp`) |
| `SELF_ID` | `wiki-mcp` |
| Image ref | `docker.io/library/wiki_memory_tool-otel:latest` (both services) |
| Running image ID (recorded at run time) | front `sha256:bd6496be7d4ee973aeae43f4cb4f670c5ded1dda168f59cf9d15777503d7e68f` · sidecar `sha256:692a668d6ac50403a239472f61a5c8e2ebc8b7b294ef4220fb0408566b8148c8` |
| LLM wiring | none — no model in this path |

Framework axis (STUDY §10.10.2 row 6): FastMCP front / FastAPI backend, httpx
between them, Python 3.14, git subprocess for persistence.

## Expected entities (kind + natural key)

| kind | natural_key | note |
|---|---|---|
| client | `client:<driver-pod-ip>` | anonymous caller (no JWT); ip varies per run |
| tool | `tool:wiki-mcp` | the entry entity (callee of the tools/call root) |
| agent | `agent:wiki-mcp` | **expected duplicate identity**: the bodyless `/mcp` legs (SSE open / teardown) classify as inbound http, whose callee kind is `agent`, so the same pod appears as both `tool:` and `agent:` (template documents this) |
| service | `wiki-service.team1.svc.cluster.local:8000` | front → backend REST hop |

## Expected interactions per turn

An MCP session is **structurally multi-root**: each entry HTTP exchange carries
the caller's traceparent and derives its own dangling-parent root. `roots == 1`
cannot hold; acceptance is *exactly one root is the tools/call*.

| leg (caller → callee) | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| client → tool (`initialize`) | inbound mcp | `mcp_lifecycle_request` / `mcp_lifecycle_result` | 1 | yes |
| client → tool (`notifications/initialized`) | inbound mcp | `mcp_lifecycle_request` / `mcp_lifecycle_result` | 1 | yes |
| client → tool (`tools/call`) | inbound mcp | `tool_call_arguments` / `tool_call_result` | 1 | **no** |
| client → agent (SSE open / teardown, bodyless on `/mcp`) | inbound http | `mcp_lifecycle_request` / `mcp_lifecycle_result` | 1..k | yes |
| wiki-mcp → wiki-service | outbound http | generic REST — no MCP framing | 1 | no |

Totals per turn: **4..(3+k) interactions**, of which exactly **1 is the
tools/call** and the rest are lifecycle/plumbing. `tools/list` is **not**
expected: the harness's driver calls the tool directly rather than browsing.

### `EXPECT_KINDS` line (derived content-kind counts)

Only the tools/call pair is deterministic — the number of bodyless SSE legs
depends on how the client closes the stream, so lifecycle kinds stay unlisted.

```bash
EXPECT_KINDS='tool_call_arguments=1,tool_call_result=1'
```

## Expected NO-interaction legs (capture gaps — expected, not failures)

| leg | why absent |
|---|---|
| wiki-service → git | local subprocess, not an HTTP hop — no traceparent, same class as the `claude_agent` CLI exception (STUDY §10.10.2) |

## Known quirks

- Backend needs `JWT_SECRET_KEY` + a writable `WIKI_ROOT` (fleet.conf sets both).
- Shim image carries `UV_NO_CACHE=1` + `HOME=/tmp` (this base pre-seeds a
  root-owned uv cache).
- Driver pod runs the app's own image for its `mcp` SDK.

## Open question this run must answer (daylight look)

The newer upstream app images reportedly shifted the MCP framing — the concern
recorded post-meeting is whether `mcp.tool` still arrives populated on the
sidecar span, since tool-echo identity was only ever proven offline. **Record
what the wire actually carries**: `mcp.method`, `mcp.tool` on the tools/call
anchor span. A missing `mcp.tool` does not change the derived kinds (which key
on `mcp.method`), but it is a wire-contract observation for Igor.

## Results

| Field | Value |
|---|---|
| Date / operator | 2026-07-29 (T9 post-cutover) / ys (driven by Claude) |
| DG image | `d841c3999683` built from lab-data-governance `feat/interactions-sidecar-algorithm` @ `e909438` (legs schema, migration head `0009_interaction_legs`, `INTERACTIONS_ALGORITHM=sidecar`) |
| Harness + N | `concurrency-test-mcp-interactions.sh`, N=6, SETTLE=45, TOOL=wiki_query, `EXPECT_KINDS='tool_call_arguments=1,tool_call_result=1'` |
| Trace ids | `3f9a08a9cd6c…`, `e52d11706d03…`, `ae8d7a2174b3…`, `55cc0e1d1144…`, `114c5bc658f7…`, `60c48f15e597…` |
| Harness summary | `CLEAN TURNS: 6/6   DISTINCT TRACES: 6/6` — every trace `ix=8 roots=7 callroots=1 orphan=0 anchors=8 dup=0`, callee `tool:wiki-mcp` |
| Deviations | none — identical to pre-cutover. Legs-model note: `interaction_spans` for `55cc0e1d1144…` is exactly 8 anchor(`request`) + 8 connector(`response`) rows; **no NULL-`leg_type` echo connectors derive live** — consistent with the known open item that tool-echo identity is offline-proven only (new app images shifted MCP framing); the NULL path is covered by `test_sidecar_write_integration.py`. App image `sha256:bd6496be7d…`. |
| Verdict | **PASS** |
