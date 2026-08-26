# HITL pause mode — local demo

A walkthrough of `on_exceed: pause` with a human at the terminal.
**No Kubernetes required** — this demo runs on your laptop with Docker
(Redis), Go (approver), and the shipping `authbridge-proxy` binary as
a forward proxy that `curl` points at explicitly. With no inbound A2A
session to tag the request, the plugin uses its
`default_session_fallback: true` path to bucket counters under
`"default"`; cluster deployments inherit a real session ID instead.

For the cluster-based version, see [`README.md`](README.md).

## What you'll see

Four moving parts on one laptop:

- **Redis** (Docker) — where session-budget persists per-session counters.
- **Ollama** — a real local LLM behind an OpenAI-compatible endpoint,
  so `inference-parser` sees real `usage.total_tokens` on each response.
- **`authbridge-proxy`** — the shipping binary, wired as a forward
  proxy with `inference-parser` + `session-budget` in the outbound
  pipeline. `session-budget` is configured with `max_calls: 2` and
  `on_exceed: pause`.
- **`approver.go`** — a ~130-line stdlib HTTP server on `:9099` that
  prints each pause request and reads `a`/`d` from stdin.

`curl` (through `HTTP_PROXY=127.0.0.1:47601`) drives the LLM. The first
two chat completions pass instantly. The third breaches the call
budget: the plugin blocks the request, POSTs to the approver, and only
resumes (or rejects with 403) once the operator decides.

## How it fits together

The outbound pipeline is an in-process function chain with no network hop between
the proxy and its plugins:

```text
                                 ┌────────────────────────────────────┐
                                 │  authbridge-proxy   (:47601)       │
                                 │                                    │
   curl ──HTTP_PROXY──▶ forward ─┼─▶ outbound pipeline                │
                       proxy     │     ├─ session-budget    (plugin)  │
                                 │     └─ inference-parser  (plugin)  │
                                 │            │        ▲              │
                                 │            │        │ HGETALL /    │
                                 │            │        │ HINCRBY      │
                                 │            ▼        │              │
                                 │     ┌────────────────────┐         │
                                 │     │  Redis :6379       │         │
                                 │     │  session-budget:*  │         │
                                 │     └────────────────────┘         │
                                 │  on breach │      ▲                │
                                 │  HTTP POST │      │ {"action":...} │
                                 │            ▼      │                │
                                 │        ┌────────────────┐          │
                                 │        │ approver.go    │          │
                                 │        │    (:9099)     │          │
                                 │        └────────────────┘          │
                                 │  under budget, OR approver said    │
                                 │  approve: proxy forwards           │
                                 │            │                       │
                                 └────────────┼───────────────────────┘
                                              ▼
                                          Ollama :11434
```

## Prerequisites

- `docker` (for Redis)
- `ollama serve` running with any small chat model pulled
  (`ollama pull llama3.2:latest` works). Confirm the OpenAI endpoint:

  ```bash
  curl -s http://localhost:11434/v1/models | jq -r '.data[].id'
  ```

- Go toolchain matching `authbridge/cmd/authbridge-proxy/go.mod` for
  building the proxy binary.

## Setup

### Start Redis

```bash
docker run --rm -d --name sb-demo-redis -p 6379:6379 redis:7-alpine
docker exec sb-demo-redis redis-cli PING  # expect PONG
```

### Build the proxy binary (once)

`session-budget` is opt-in via build tag (it links go-redis into the
binary). Build it in-tree from the `cmd/authbridge-proxy` module:

```bash
cd authbridge/cmd/authbridge-proxy
go build -tags include_plugin_sessionbudget -o authbridge-proxy .
```

This produces `authbridge/cmd/authbridge-proxy/authbridge-proxy` —
that's the binary the rest of this doc invokes.

### The config

`authbridge/demos/session-budget/local/config.yaml`:

```yaml
mode: proxy-sidecar
listener:
  roles: [forward]                      # egress-only (no inbound listener)
  forward_proxy_addr: "127.0.0.1:47601"
  session_api_addr: "127.0.0.1:47604"
stats:
  address: "127.0.0.1:47602"
pipeline:
  outbound:
    plugins:
      # Order matters: RunResponseFrame dispatches in REVERSE declaration
      # order, so session-budget (which reads Inference.TotalTokens) must
      # be declared BEFORE inference-parser (which populates it).
      - name: session-budget
        config:
          redis_url: "redis://localhost:6379"
          max_calls: 2
          max_tokens: 1000000            # effectively unlimited; forces the
                                         # plugin to track tokens in Redis so
                                         # the watch pane shows real spend
          on_exceed: "pause"
          pause_webhook: "http://localhost:9099"
          pause_timeout: "120s"
          pause_timeout_action: "deny"
          pause_grace_period: "1ms"      # tiny grace so every over-budget call re-prompts
          session_ttl_seconds: 300
          default_session_fallback: true # single-workload demo: pool
                                         # sessionless egress into 'default'
      - inference-parser
```

`inference-parser` reads `usage.total_tokens` on OpenAI-compatible
responses, `session-budget` counts every classified inference call
against `max_calls` AND accumulates `total_tokens` into a running
`tokens` counter, and Redis persists both under
`session-budget:<session-id>`. This demo has no inbound A2A session,
so `default_session_fallback: true` pools all sessionless egress into
`session-budget:default`. The flag is off by default and should stay
off in multi-tenant deployments — one caller exhausting the shared
bucket denies all others.

## Reset between runs

`session-budget` persists per-session counters in Redis, so **the
second run of the demo starts already over-budget** unless you flush.
Every time you're about to demo (or re-demo) the pause path, reset
first:

```bash
docker exec sb-demo-redis redis-cli FLUSHALL   # zero the counters
```

The approver is stateless — leave it running between runs. Only kill
it if you want to switch its mode (interactive ↔ `--auto-approve` ↔
`--auto-deny`). `pkill -f approver` doesn't always match the binary
that `go run` execs; if the new approver reports `address already in
use`, free the port directly:

```bash
pids=$(lsof -ti :9099 -sTCP:LISTEN); [ -n "$pids" ] && kill $pids
```

Optional — confirm the counter is actually zero before you start:

```bash
docker exec sb-demo-redis redis-cli HGETALL session-budget:default
# empty output = fresh session, ready to demo
```

### Optional 4th pane — watch the counter live

Add a fourth terminal that shows the Redis counter updating in real time:

```bash
watch -n 0.5 'docker exec sb-demo-redis redis-cli HGETALL session-budget:default'
```

## Run it — three terminals

### Terminal 1 — the approver

Launch it in **interactive mode** (no flags) — the walkthrough below
depends on you typing `a`/`d` at the prompt:

```bash
cd authbridge/demos/session-budget/local
go run ./approver.go
```

Expected:

```text
approver listening on 127.0.0.1:9099 (auto-approve=false, auto-deny=false)
```

### Terminal 2 — the proxy

From the repo root:

```bash
./authbridge/cmd/authbridge-proxy/authbridge-proxy \
  -config ./authbridge/demos/session-budget/local/config.yaml
```

Expected (relevant lines):

```text
level=INFO msg="HTTP server listening" name=forward-proxy addr=127.0.0.1:47601
level=INFO msg="authbridge-proxy starting" mode=proxy-sidecar
```

### Terminal 3 — drive it with curl

```bash
for i in 1 2 3; do
  echo "=== call #$i ==="
  curl -sS -x http://127.0.0.1:47601 \
    -H "Content-Type: application/json" \
    http://localhost:11434/v1/chat/completions \
    -d "{\"model\":\"llama3.2:latest\",\"messages\":[
          {\"role\":\"user\",\"content\":\"say hi in 3 words (call $i)\"}
        ]}" \
    -w "\nhttp_status=%{http_code}\n"
done
```

What happens:

- **Call #1 and #2** — pass instantly. Terminal 3 sees `200` and the
  LLM's reply. Terminal 2 logs `inference-parser: response ...
  promptTokens=... completionTokens=...`. If you added the 4th watch
  pane, it shows both `calls` and `tokens` incrementing in Redis; the
  `session-budget` call counter is now at `2/2`.
- **Call #3** — hangs. Terminal 2 logs `budget exceeded, requesting
  approval reason="call limit reached: 2/2"`. Terminal 1 prints:

  ```text
  ─── pause request ───
    session: "default"
    reason:  "call limit reached: 2/2"
    calls:   2 / 2
    tokens:  87 / 1000000        # tokens value will vary with the model
    [a]pprove / [d]eny (Enter = approve):
  ```

  Type `a` (or just Enter) → curl in Terminal 3 completes with `200`.
  Type `d` → curl gets `403` with body:

  ```json
  {"error":"budget.exceeded",
   "message":"call limit reached: 2/2 (approval denied)",
   "plugin":"session-budget",
   "details":{"call_limit":2,"spent_calls":2,"spent_tokens":87,"token_limit":1000000}}
  ```

  (`spent_tokens` will vary with the model, same as the prompt above.)

  Every subsequent over-budget call re-prompts (the 1ms grace window
  is effectively off), so you can approve one, deny the next, and see
  both outcomes in a single run.

## Auto modes for CI

The approver has `--auto-approve` and `--auto-deny` flags. They let you
smoke-test the wire without a human present. Stop any prior approver
first (see "Reset between runs" for the `lsof` one-liner) — a
backgrounded auto-mode approver will exit silently if `:9099` is
already bound.

```bash
# Auto-deny: every over-budget call returns 403.
go run ./approver.go --auto-deny &
# then run the curl loop above — calls 1-2 pass, calls 3+ return 403.

# Auto-approve: every over-budget call is waved through.
go run ./approver.go --auto-approve &
# then run the curl loop — all calls return 200.

# Stop the approver when done — kill $! only reaps the `go run` parent
# and leaves the compiled server bound to :9099, so free the port directly:
pids=$(lsof -ti :9099 -sTCP:LISTEN); [ -n "$pids" ] && kill $pids
```

## Cleanup

```bash
# pkill -f approver may miss the go-run child (see "Reset between runs");
# freeing :9099 directly is more reliable.
pids=$(lsof -ti :9099 -sTCP:LISTEN); [ -n "$pids" ] && kill $pids
pkill -f authbridge-proxy
docker rm -f sb-demo-redis
```

If you want to keep Redis running for another demo pass, skip the
`docker rm` and just re-run the "Reset between runs" flush.
