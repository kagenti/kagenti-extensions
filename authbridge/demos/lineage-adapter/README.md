# Lineage Adapter — make ANY Kagenti app sing in harmony

This demo turns arbitrary, uninstrumented Kagenti apps into sources of a
**correct per-request execution forest** in Data Governance (DG), using the
lineage sidecar (AuthBridge envoy-sidecar mode) in this repo — with **zero app
source changes** and minimum friction.

It is the generalization of `../weather-agent/`: instead of one hand-wired agent,
a **catalog** (`fleet.conf`) links each existing app to its adaptation, and one
command stands the whole fleet up.

- **Why the sidecar alone isn't enough, and what the shim does:** [`DESIGN.md`](DESIGN.md)
- **Step-by-step for a single app:** [`RUNBOOK.md`](RUNBOOK.md)
- **No cluster yet:** [`CLUSTER-FROM-ZERO-windows.md`](CLUSTER-FROM-ZERO-windows.md)
- **Cluster running, no lineage yet:** [`LINEAGE-FROM-ZERO-windows.md`](LINEAGE-FROM-ZERO-windows.md)
  — collector + DG service + sidecar on the stock weather pair, end to end (WSL2/docker)
- **The consumer side** (what DG derives from the spans): the sibling
  `lab-data-governance` repo; the wire between producer and consumer is
  `docs/sidecar-wire-contract.md` there, and the DG UI lives at
  `http://dg.localtest.me:8080/ui/traces` (RUNBOOK §5 says what to expect).

## The two things we add to an app

1. **A propagate-only OpenTelemetry shim** (`Dockerfile.otel-shim`). Layered on the
   app image, it auto-instruments the app's HTTP libs to **extract** the inbound
   `traceparent` and **inject** it on outbound calls — and **exports nothing**
   (so DG stays sidecar-only). This is the in-process context propagation the
   sidecar fundamentally cannot do from outside (see DESIGN §1).
2. **The lineage sidecar** (`proxy-init` initContainer + `envoy-proxy` sidecar,
   envoy-sidecar mode, `capture_io: true`, no auth/SPIRE). Captures every hop with
   bodies and, thanks to (1), re-parents each outbound under the correct inbound.

Result: under concurrent load, DG reconstructs each request as its own connected
trace — agent → llm, agent → tool → downstream — instead of collapsing them.

## The link: `fleet.conf`

One line per app maps an **existing** Kagenti app (its ghcr image or local build)
to **what we add** (shim + sidecar) and **how it's wired** (LLM repoint, MCP_URL
to its tool). Plug & play app #8 = add one line. Columns are documented in the
file header.

## Files

| File | Role |
|---|---|
| `fleet.conf` | **The catalog** — existing app ↔ adaptation ↔ wiring. |
| `deploy-fleet.sh` | Plug & play: reads the catalog → pull/build image → `build-otel-shim.sh` → overlay → `attach-lineage.sh`. Tools before agents. |
| `verify-fleet.sh` | Drives every entry point under concurrency → the **harmony table** (app → N/N). |
| `Dockerfile.otel-shim` | The propagate-only shim (all mainstream instrumentors + threading). |
| `build-otel-shim.sh` | Build the shim on an app image + kind-load it. |
| `attach-lineage.sh` | Emit a full Deployment+Service+lineage-ConfigMap (app + sidecar). |
| `sidecar-patch.sh` | Attach the sidecar to an EXISTING (operator-managed) Deployment — for natively-instrumented apps that need no shim (weather pair, UI-imported apps). |
| `probe-app/` + `probe-validate.sh` | The **all-capabilities probe**: one shipped app (front = LLM + external HTTP/HTTPS legs + threaded A2A fan-out, back = held same-trace fan-in → MCP tool + LLM + the `/echo` cross-session writer, tool = MCP leaf) and the one-command validation of concurrent traces, thread propagation, exact inbound→outbound pairing, external-egress presence AND absence, and tool identity echo. |
| `probe-cross-validate.sh` | The **cross-session probe**: trace A stashes bytes at rest (shared PVC file + redis), a later trace B reads and re-sends them; asserts the two disconnected trees, the invisible-hop absences, and the content-hash join (see below). |
| `probe-lineage-validate.md` | **Agent-runnable validation playbook**: prereqs → deploy → both validators → exact expected shapes, triage guide, and the report format. Hand this file to an agent; it needs nothing else. |
| `container-runtime.sh` | Shared docker/podman auto-detection + `kind_load` (sourced by the build scripts; override with `CONTAINER_TOOL`). |
| `concurrency-test.sh` / `concurrency-test-mcp.sh` | The A2A / MCP concurrency verifiers. |
| `overlays/` | Per-app upstream-defect fixes applied on top of the shim (not part of the method). |

## Quick start

Prereqs (see DESIGN / RUNBOOK): a Kind cluster `kagenti` (podman) with the Kagenti
platform, this repo's `authbridge-envoy` + `proxy-init` images loaded, the DG pod
running and fed by the patched collector, the `envoy-config` ConfigMap in `team1`,
and host Ollama (`qwen2.5:7b`) reachable at `host.containers.internal:11434`. The
`agent-examples` clone must sit beside this repo (for `local:` builds).

```bash
cd authbridge/demos/lineage-adapter
./deploy-fleet.sh          # stand up the whole adapted fleet (one command)
./verify-fleet.sh          # -> harmony table, target 6/6 each
```

Deploy or test a subset:
```bash
./deploy-fleet.sh slack-tool slack-researcher   # a cross-service chain
./verify-fleet.sh slack-researcher
```

## The fleet, in harmony

```
   caller (supplies traceparent)
        │  A2A / MCP
        ▼
   ┌─────────────────────────── namespace team1 ───────────────────────────┐
   │  every pod = app(+propagate-only shim)  ⊕  lineage sidecar             │
   │                                                                        │
   │  trivia-agent ─▶ Ollama                                                │
   │  a2a-currency-converter ─▶ Ollama (+ Frankfurter TLS)                  │
   │  a2a-contact-extractor ─▶ Ollama                                       │
   │  git-issue-agent ─▶ Ollama                                             │
   │  slack-researcher ─▶ Ollama  ─▶ slack-tool (MCP)                       │
   │  reservation-service ─▶ Ollama  ─▶ reservation-tool (MCP)              │
   │  wiki-mcp (FastMCP) ─▶ wiki-service (FastAPI ─▶ git)                   │
   └───────────────────────────────┬────────────────────────────────────────┘
                                    │  sidecar spans (bodies), one trace/request
                                    ▼
                     otel-collector ─▶ Data Governance  ─▶ correct execution forest
```

Coverage across the catalog: servers Starlette + FastAPI; clients httpx / requests
/ aiohttp; frameworks raw / LangGraph / Marvin / crewai / ag2; topologies single
LLM hop, agent→REST, agent→MCP-tool (cross-service), 2-service MCP→HTTP.

## Edges of visibility (probed, asserted — never left unmentioned)

The probe topology deliberately walks the boundary of what the sidecar can see,
and asserts BOTH sides of it (`probe-validate.sh` capability 4, and all of
`probe-cross-validate.sh`):

- **External plaintext HTTP** (probe-front → `http://httpbin.org`): visible.
  Derives exactly one interaction, callee `service:httpbin.org`.
- **The same call over HTTPS**: invisible. TLS passthrough — the sidecar's
  documented gap (SNI observer is the named follow-up). Asserted as *exactly
  one* httpbin interaction per trace: 0 = the visible leg broke, 2 = the gap
  closed silently and the assertion must be updated deliberately.
- **Redis (RESP, a real non-HTTP datastore)**: invisible *by configuration*.
  The sidecar's Envoy listener is an HTTP connection manager; raw RESP through
  the redirect would break, so the client pods carry
  `OUTBOUND_PORTS_EXCLUDE=6379` — that bypass IS the honest statement "this
  hop is invisible to lineage today". Asserted: the app-level round-trip
  works, and zero redis spans/interactions derive.
- **File I/O on a shared PVC**: invisible — no wire exists at all.

### The cross-session hook (seed of a data-at-rest lineage workstream)

`probe-cross-validate.sh` runs trace A (`/stash/{tag}`: front sends exact bytes
to back's `/echo`; back persists them to the PVC file AND redis) and a later
trace B (`/replay/{tag}`: front reads both stores and re-sends the bytes over
the visible hop). Today this honestly derives **two disconnected trees** — the
derivation must not guess a link. But DG payload storage is content-addressed
(`interaction_payloads` is keyed by sha256 of canonical content), so the two
traces reference the **same content hash**, and this join finds the link:

```sql
SELECT l.payload_hash, count(DISTINCT i.trace_id)
FROM interaction_legs l
JOIN interactions i ON i.id = l.interaction_id
WHERE i.trace_id IN ('<traceA>', '<traceB>') AND l.payload_hash IS NOT NULL
GROUP BY l.payload_hash
HAVING count(DISTINCT i.trace_id) = 2;
```

That query is the seed of a future *data-at-rest / cross-session lineage*
workstream: "trace B read the bytes trace A wrote" becomes derivable evidence
the moment both sides of a store pass through any payload-captured hop — no
new instrumentation, no guessing, just the content address. Generalizing it
(store-side identity, time ordering, provenance confidence) is future work,
deliberately NOT wired into today's derivation.

## Out-of-envelope (documented exceptions)

The Python auto-instrumentation covers ASGI/Starlette/FastAPI servers + httpx/
requests/aiohttp clients. It does **not** cover: outbound via a **subprocess**
(`claude_agent` CLI; wiki's local `git` — no network hop), or **non-Python**
services (`github_tool`, Go). These are excluded from the catalog by design.
