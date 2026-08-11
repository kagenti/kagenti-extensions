# Lineage Adapter — make ANY Kagenti app sing in harmony

This demo turns arbitrary, uninstrumented Kagenti apps into sources of a
**correct per-request execution forest** in Data Governance (DG), using the
lineage sidecar (AuthBridge envoy-sidecar mode) in this repo — with **zero app
source changes** and minimum friction.

It is the generalization of `../weather-agent/`: instead of one hand-wired agent,
a **catalog** (`fleet.yaml`) links each existing app to its adaptation, and one
command stands the whole fleet up.

- **Start here — stock app to lineage pod, no script-reading:** [`QUICKSTART.md`](QUICKSTART.md)
- **Why the sidecar alone isn't enough, and what the shim does:** [`docs/DESIGN.md`](docs/DESIGN.md)
- **Step-by-step for a single app:** [`docs/RUNBOOK.md`](docs/RUNBOOK.md)
- **No cluster yet:** [`docs/CLUSTER-FROM-ZERO-windows.md`](docs/CLUSTER-FROM-ZERO-windows.md)
- **Cluster running, no lineage yet:** [`docs/LINEAGE-FROM-ZERO-windows.md`](docs/LINEAGE-FROM-ZERO-windows.md)
  — collector + DG service + sidecar on the stock weather pair, end to end (WSL2/docker)
- **The consumer side** (what DG derives from the spans): the sibling
  `lab-data-governance` repo; the wire between producer and consumer is
  `docs/sidecar-wire-contract.md` there, and the DG UI lives at
  `http://dg.localtest.me:8080/ui/traces` (RUNBOOK §5 says what to expect).

## The two things we add to an app

1. **A propagate-only OpenTelemetry shim** (`lib/Dockerfile.otel-shim`). Layered on the
   app image, it auto-instruments the app's HTTP libs to **extract** the inbound
   `traceparent` and **inject** it on outbound calls — and **exports nothing**
   (so DG stays sidecar-only). This is the in-process context propagation the
   sidecar fundamentally cannot do from outside (see DESIGN §1).
2. **The lineage sidecar** (`proxy-init` initContainer + `envoy-proxy` sidecar,
   envoy-sidecar mode, `capture_io: true`, no auth/SPIRE). Captures every hop with
   bodies and, thanks to (1), re-parents each outbound under the correct inbound.

Result: under concurrent load, DG reconstructs each request as its own connected
trace — agent → llm, agent → tool → downstream — instead of collapsing them.

## The link: `fleet.yaml`

One stanza per app maps an **existing** Kagenti app (its ghcr image or local
build) to **what we add** (shim + sidecar) and **how it's wired** (LLM repoint,
MCP_URL to its tool, `attach:` knobs). Plug & play app #8 = add one stanza.
Keys are documented in the file header; `lib/fleet-read.py` schema-checks every
stanza — a typo refuses to deploy instead of silently mis-adapting.

## Files

The deploy surface is three files; everything else proves or documents.

| File | Role |
|---|---|
| `lineage` | **The one door.** `deploy · verify · adopt · stamp-backend · on/off/status · shim · gen` — every subcommand dispatches into `lib/`. |
| `fleet.yaml` | **The catalog** — existing app ↔ adaptation ↔ wiring (`env:` for the app, `attach:` for the deploy layer). Schema-checked on every read. |
| `QUICKSTART.md` | Stock app → lineage-included pod without reading any script source. |
| `lib/` | The machinery: `attach-lineage.sh` (the ONE generator — full manifest, `EMIT=cm`, `EMIT=patch`), `Dockerfile.otel-shim` + `build-otel-shim.sh` (shim bake, with the refuse-to-bake interlock), `sidecar-patch.sh` (= `lineage adopt`), `stamp-ui-backend.sh`, `deploy-fleet.sh`, `lineage-switch.sh`, `container-runtime.sh`, `fleet-read.py`, `overlays/`. |
| `test/` | Everything that proves it: `verify-fleet.sh` (= `lineage verify`, the harmony table), the two interaction harnesses, `fanin-test.sh`, the probe (`probe-app/` + `probe-validate.sh` + `probe-cross-validate.sh` + the agent playbook `probe-lineage-validate.md`), `dg-api.sh`, and `validation/` expectation cards. |
| `docs/` | The deep docs: `RUNBOOK.md` (per-app recipe), `DESIGN.md` (why the shim), the two FROM-ZERO walkthroughs. |

## Quick start

Prereqs (see QUICKSTART / docs/RUNBOOK.md): a Kind cluster `kagenti` (podman) with the
Kagenti platform, this repo's `authbridge-envoy` + `proxy-init` images loaded,
the DG pod running and fed by the patched collector, the `envoy-config`
ConfigMap in `team1`, host Ollama (`qwen2.5:7b`) reachable at
`host.containers.internal:11434`, and `python3` + PyYAML. The `agent-examples`
clone must sit beside this repo (for `local:` builds).

```bash
cd authbridge/demos/lineage-adapter
./lineage deploy           # stand up the whole adapted fleet (one command)
./lineage verify           # -> harmony table, target 6/6 each
```

Deploy or test a subset:
```bash
./lineage deploy slack-tool slack-researcher   # a cross-service chain
./lineage verify slack-researcher
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
and asserts BOTH sides of it (`test/probe-validate.sh` capability 4, and all of
`test/probe-cross-validate.sh`):

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

`test/probe-cross-validate.sh` runs trace A (`/stash/{tag}`: front sends exact bytes
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
