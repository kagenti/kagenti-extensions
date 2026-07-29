# Expectation card — `wiki_memory_tool`

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
| Date / operator | 2026-07-29 / ys (driven by Claude) |
| Harness + N | `concurrency-test-mcp-interactions.sh`, N=6, `EXPECT_KINDS='tool_call_arguments=1,tool_call_result=1'` |
| Trace ids | `fcc916558787…`, `7f82839e6ff4…`, `cf6e11643fda…`, `488ca066769a…`, `ca69e47a7d68…`, `c9825bf9d3a8…` |
| Harness summary | `CLEAN TURNS: 6/6   DISTINCT TRACES: 6/6` — every trace: exactly 1 tools/call root, callee `tool:wiki-mcp` |
| Root counts per trace | 7 roots of 8 interactions (multi-root by design: each entry exchange carries the caller's traceparent and derives its own dangling-parent root) |
| `mcp.tool` on the wire | **present** — `mcp.method=tools/call`, `mcp.tool=wiki_query` on the anchor span |
| Deviations | 8 interactions vs the card's "4..(3+k)" — the card omitted `tools/list`, which the MCP SDK client issues during session setup, and the front→backend REST hop appears twice (outbound at wiki-mcp, inbound at wiki-service) rather than once. Both are correct captures the card under-counted; no pipeline deviation. `http.method` absent on all request spans (pre-existing producer/contract deviation, already on record). |
| Verdict | **PASS** (after the config fix below) |

### Config defect found and fixed by this run (kit fix)

**First run: 0/6.** All 8 exchanges recorded with correct structure (0 orphans,
0 duplicate anchors, 6/6 distinct traces), but every one was labelled plain
`http` with no `mcp.method`, no `mcp.tool` and no body — so the `wiki_query`
tools/call was indistinguishable from connection plumbing, and **WS-1 would
have hidden the real tool call as infrastructure**.

Cause: `attach-lineage.sh` hardcoded a **direction-specific parser chain** —
`a2a-parser` inbound, `mcp-parser` + `inference-parser` outbound. Every app
validated until now has an **A2A** entry, and MCP was only ever seen on the
*caller's outbound* side (`weather-tool` runs with no sidecar at all), so an
MCP **entry** had never been exercised. wiki-mcp receives MCP inbound, where no
MCP parser was listening.

Fix: the **same** chain in both directions (`a2a-parser`, `mcp-parser`,
`inference-parser`, `lineage-telemetry`). An app's entry protocol is not
knowable from the attach side, so a direction-specific chain silently
mislabels whatever it wasn't given. Uniform is safe because the parsers are
content-gated and mutually exclusive — `a2a-parser` claims only the A2A
JSON-RPC prefixes (`message/*`, `tasks/*`), `mcp-parser` takes the other
JSON-RPC methods, `inference-parser` matches only the OpenAI completion paths;
that guard exists upstream precisely so the two JSON-RPC parsers can coexist.

Re-attached with `SKIP_BUILD=1` (app image unchanged — `bd6496be7d4e`, so the
before/after differs only by parser chain). Re-run: **6/6**.

Derived kinds after the fix, one full trace:

```
mcp   initialize                mcp_lifecycle_request  -> mcp_lifecycle_result
http  (bodyless /mcp)           mcp_lifecycle_request  -> mcp_lifecycle_result
mcp   notifications/initialized mcp_lifecycle_request  -> mcp_lifecycle_result
mcp   tools/call                tool_call_arguments    -> tool_call_result      ← the signal
http  wiki-mcp → wiki-service   (generic REST, no framing)
http  → wiki-service            (generic REST, no framing)
mcp   tools/list                tool_discovery_request -> tool_discovery_result
http  (bodyless /mcp)           mcp_lifecycle_request  -> mcp_lifecycle_result
```

**Answers a named open item.** The post-meeting worry was that the newer app
images had drifted the MCP framing (`mcp.tool` possibly empty). They have not:
`mcp.tool=wiki_query` arrives populated. The apparent drift was entirely this
config bug.

**Scope note for the remaining matrix.** Every tool pod in the fleet was
attached with the a2a-only inbound chain, so each tool's own inbound view of
its MCP traffic was mislabelled the same way. Agent-entry apps are unaffected
in their *entry* classification (A2A inbound was always parsed), and the
caller-side MCP was always captured outbound — which is why the earlier
validations passed. Re-attaching the fleet is therefore expected to *improve*
kinds on tool pods without changing interaction counts.
