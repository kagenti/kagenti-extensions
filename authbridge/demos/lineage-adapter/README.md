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
- **The consumer side** (what DG derives from the spans, and how to read its UI):
  `docs/REVIEWER-QUICKSTART.md` in the sibling `lab-data-governance` repo; the wire
  between producer and consumer is `docs/sidecar-wire-contract.md` there.

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

## Out-of-envelope (documented exceptions)

The Python auto-instrumentation covers ASGI/Starlette/FastAPI servers + httpx/
requests/aiohttp clients. It does **not** cover: outbound via a **subprocess**
(`claude_agent` CLI; wiki's local `git` — no network hop), or **non-Python**
services (`github_tool`, Go). These are excluded from the catalog by design.
