# Design — why the shim exists and why it's enough

Condensed rationale for the lineage adapter. The full teaching document (stage
history, every experiment, per-scenario results) lives outside this repo in the
project's `docs/STUDY.md`; this is the self-contained summary.

## 1. The sidecar can capture, but cannot correlate

The lineage sidecar (AuthBridge, envoy-sidecar mode) sits at the pod's network
boundary and sees every HTTP hop with parsed bodies. But it lives **outside** the
app's execution context: it has no idea which internal coroutine/task issued which
outbound call. To attribute an outbound call to the inbound request that caused
it, a token must travel **with the execution scope through the app** — exactly
what the W3C `traceparent` trace-id is for. Only code running *inside* the
request's context can copy it from the inbound request onto outbound requests.

The plugin has a fallback for a missing `traceparent` — re-parent an outbound hop
under "this agent's current inbound span" (`agentCurrentInbound`). That is a
**single-in-flight heuristic**: correct only when the agent handles one request at
a time. Under concurrency it collapses — with 6 concurrent requests, all 6
outbound calls pile onto whichever inbound updated the process-wide pointer last
(**1/6**, empirically). Conclusion: **in-process `traceparent` propagation is
mandatory and the sidecar cannot do it from outside.**

## 2. The fix: a propagate-only OTEL shim (export nothing)

OpenTelemetry ships generic auto-instrumentation for the mainstream Python HTTP
libraries. `Dockerfile.otel-shim` layers them on the app image and runs it under
`opentelemetry-instrument --traces_exporter none …`:

- **server side** (`starlette`/`asgi`/`fastapi`) — extract the inbound
  `traceparent`, set the active context.
- **client side** (`httpx`/`requests`/`aiohttp-client`) — inject `traceparent` on
  outbound calls.
- **`threading`** — copy the active context across `Thread.start` /
  `ThreadPoolExecutor.submit`. **Load-bearing**: frameworks like ag2/autogen run
  the LLM call in a worker thread (`run_in_executor`); without this the context is
  lost at the thread boundary and propagation silently breaks back to 1/N.
- **export nothing** — a real SDK TracerProvider generates valid non-zero
  trace/span ids, but there is **no exporter**, so no app spans reach DG.

Zero app source changes; `opentelemetry-instrument` auto-activates whichever libs
the app actually imports, so one image recipe fits every in-envelope app.

### 2a. Why `threading` is load-bearing (and why it's correct, not a hack)

The OTEL "context" is a `contextvars` value holding the currently **active span**
(→ its `trace_id`/`span_id`). It flows automatically **within one async task**,
but **not across a thread boundary**: a bare `threading.Thread`, a
`ThreadPoolExecutor`, or `loop.run_in_executor` starts the worker with an **empty**
context. Frameworks like ag2/autogen and crewai run the *synchronous* LLM client
in exactly such a worker (to avoid blocking the event loop). So without help, the
worker has no active span → the client instrumentation (httpx/requests) emits no
`traceparent` (or a fresh root) → the sidecar can't match that outbound to the
inbound request → it falls back to the single-in-flight `agentCurrentInbound`
heuristic → under concurrency all outbounds collapse onto one inbound (**1/N**).

The `threading` instrumentor does **not** blindly stamp one traceparent on every
thread. It **snapshots the parent's active context at the instant of
`.start()`/`.submit()`** and **re-activates that snapshot inside the child for the
duration of its work, then detaches it**:

```
Thread.start  (parent):  thread._otel_context = context.get_current()
Thread.run    (worker):  token = context.attach(thread._otel_context)
                         try: <child work> finally: context.detach(token)
ThreadPoolExecutor.submit: capture at submit; attach in the worker; detach after.
```

Note it propagates the **Context**, not a header — the `traceparent` header is
generated later by the HTTP-client instrumentation, which reads
`context.get_current()` at call time. Correctness follows from the snapshot being
**per-submit** and **torn down on exit**:

- A worker thread a handler dispatches to *is doing that request's work* (the
  thread is just an implementation detail), so re-activating the request's context
  there is semantically exact.
- Two concurrent requests snapshot two **different** contexts → their workers carry
  **different** `trace_id`s (this is what turns 1/N into N/N).
- `detach` in `finally` means a pooled worker reused for the next task does not
  inherit a stale context.

**Caveats:** it only helps a thread spawned *from within* the request's active
context (a long-lived thread started at app boot snapshots an empty context and
carries nothing — that needs explicit propagation); and it covers threads/timers/
`ThreadPoolExecutor` only — **not** `multiprocessing` or `subprocess` (the latter
being the documented git/CLI exceptions in §4).

## 3. Two problems, one artifact

- **Problem 1 (correlation):** the sidecar needs `traceparent` propagated through
  the app. → the shim provides it.
- **Problem 2 (pollution):** an app's own spans (e.g. an OpenInference-instrumented
  LangGraph agent) would mix into DG. → the shim exports nothing, and we don't set
  `OTEL_EXPORTER_OTLP_ENDPOINT`, so the app's spans are created but never exported.
  **DG stays sidecar-only with no global collector filter** (verified on an
  already-instrumented app).

Same artifact solves both, and needs no OTEL knowledge from the app developer.

### 3a. What happens to the app's own telemetry

Nothing — the do-no-harm invariant. **`--traces_exporter none` silences only
the shim's own auto-instrumentation; app-owned export is untouched.** The
mechanism: the shim never sets `OTEL_EXPORTER_OTLP_ENDPOINT`, so
instrumentation riding the shim-bootstrapped SDK has nowhere to send (§3's
pollution case) — while an app that configures its **own** exporter in code
keeps exporting exactly as before. Kagenti apps gate that exporter on
`OTEL_EXPORTER_OTLP_ENDPOINT`, which the platform injects into every agent it
deploys (kagenti backend `DEFAULT_ENV_VARS`); weather-service under the
sidecar still ships its 78 app spans per trace — the live proof. Keeping such
export flowing takes one routing step — `OUTBOUND_PORTS_EXCLUDE` on the export
port — recipe in `RUNBOOK.md`. And by decision the shim stays
**propagation-only**: no APP_TELEMETRY modes, no bundled exporters, no export
management. Whether an app exports is the app's (and the platform env's)
business; the shim guarantees only that it does not interfere.

## 4. Precondition and exceptions

**Envelope:** the app uses a mainstream Python HTTP server (ASGI/Starlette/FastAPI)
and a mainstream client (httpx/requests/aiohttp), and **the entry caller supplies
the initial `traceparent`** (the natural distributed-tracing model — the first
caller roots the trace, each hop propagates). The sidecar does not seed the root
for an untraced entry; that gap is deliberately out of scope here.

**Documented exceptions (excluded from `fleet.yaml`):**
- Outbound via a **subprocess** — `claude_agent` (CLI), or a **local** subprocess
  with no network hop such as wiki's `git` (its HTTP legs are still covered).
- **Non-Python** services — `github_tool` (Go).

## 5. Verification method

Drive N **concurrent** requests from **inside the cluster** (not `kubectl
port-forward` — loopback bypasses the sidecar's inbound listener), each with a
distinct `traceparent` and an identifying tag. Then query DG's Postgres: for each
request, does the outbound hop carrying its tag share the trace of the inbound
carrying the same tag? Metric: matches / N, target **N/N**.
`concurrency-test-interactions.sh` (A2A) and `concurrency-test-mcp-interactions.sh`
(MCP) implement this at the derived-interaction level; `verify-fleet.sh` runs it
across the whole fleet.
