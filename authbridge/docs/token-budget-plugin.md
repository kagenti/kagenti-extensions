# token-budget Plugin

Enforces per-session lifetime budgets on tokens, inference calls, and
wall-clock duration. Supports `observe` (shadow — log without blocking) and
`deny` (return 403) modes. Uses Redis for cross-pod durable counters;
evaluates limits from a local cache with zero I/O on the hot path.

A "session" maps to the AuthBridge session ID (typically one A2A conversation
or agent task invocation).

## Build Tag

This plugin is **opt-IN**. Build with `-tags include_plugin_tokenbudget`
to include it (and its `storage/redis` dependency) in the binary:

```bash
cd authbridge
docker build -f cmd/authbridge-proxy/Dockerfile \
  --build-arg GO_BUILD_TAGS="include_plugin_tokenbudget" \
  -t authbridge:latest .
```

Without the tag, neither token-budget nor go-redis are linked.

The same build tag works for the envoy-sidecar image:

```bash
docker build -f cmd/authbridge-envoy/Dockerfile \
  --build-arg GO_BUILD_TAGS="include_plugin_tokenbudget" \
  -t authbridge-envoy:latest .
```

## Configuration

```yaml
pipeline:
  outbound:
    plugins:
      - name: token-exchange
        config: { ... }
      - name: token-budget
        config:
          redis_url: "redis://valkey.infra.svc:6379"
          max_tokens: 50000
          max_calls: 100
          max_duration_seconds: 1800
      - name: inference-parser
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `redis_url` | yes | — | Redis/Valkey connection URL |
| `max_tokens` | no | 0 | Cumulative token ceiling per session (0 = no limit) |
| `max_calls` | no | 0 | Max inference calls per session (0 = no limit) |
| `max_duration_seconds` | no | 0 | Wall-clock session lifetime in seconds (0 = no limit) |
| `on_exceed` | no | "deny" | `deny` (block with 403) or `observe` (shadow — log but continue) |
| `session_ttl_seconds` | no | 7200 | Redis key TTL; should be >= `max_duration_seconds` |
| `refresh_interval` | no | "5s" | How often to sync local cache from Redis |
| `redis_unavailable` | no | "fail_open" | Only `fail_open` supported (stale cache retained on failure). `fail_closed` reserved. |

At least one of `max_tokens`, `max_calls`, or `max_duration_seconds` must be > 0.

**Local Redis/Valkey** for development: `docker run -d --name valkey -p 6379:6379 valkey/valkey:latest`.
Use `redis://localhost:6379` or `redis://host.docker.internal:6379` (container mode).

## Shadow Mode

Set `on_exceed: "observe"` to run the plugin in shadow mode. The plugin
still accumulates counters and evaluates limits, but instead of blocking
requests it logs a WARN and continues the pipeline. Use this to calibrate
limits under real workloads before enabling enforcement.

Rollout workflow:
1. Deploy with `on_exceed: "observe"` and conservative limits
2. Monitor logs for `"budget exceeded (shadow mode)"` entries
3. Adjust `max_tokens` / `max_calls` / `max_duration_seconds` based on observed patterns
4. Flip to `on_exceed: "deny"` when confident in the thresholds

## Pipeline Position

Must be declared **before** `inference-parser` in the outbound plugin list.
The response path runs in reverse order, so inference-parser finalizes token
counts first, then token-budget reads them. Both plugins implement
`StreamingResponder` so they work with streaming SSE responses (Ollama,
LiteLLM, OpenAI) and buffered JSON responses alike.

## Response Format (403)

```json
{
  "error": "budget.exceeded",
  "message": "token limit reached: 50200/50000",
  "details": {
    "spent_tokens": 50200,
    "spent_calls": 42,
    "token_limit": 50000,
    "call_limit": 100,
    "duration_seconds": 1205,
    "duration_limit": 1800
  }
}
```

`duration_seconds` and `duration_limit` are included only when `max_duration_seconds` is configured.

## Redis Key Schema

```text
token-budget:<session-id>  (Hash, TTL = session_ttl_seconds)
  tokens      int   cumulative TotalTokens
  calls       int   inference call count
  started_at  unix  first-call timestamp (set-if-not-exists)
```

## Failure Modes

| Scenario | Behavior |
|----------|----------|
| Redis down at startup | `Init` succeeds (no connectivity check); enforcement fail-open until first refresh populates cache |
| Redis fails mid-session | Local cache continues enforcing; writes dropped silently |
| Pod restarts | First request passes (cold cache); refresh picks up Redis counters within one interval |
| Provider returns no usage data | `max_tokens` not enforced; `max_calls` and `max_duration_seconds` still work |

**Fail-open guarantee:** The plugin never blocks requests due to its own infrastructure failures. Redis unavailability degrades enforcement (local cache only, no cross-pod consistency) but never causes false denials.

**Note on token counting:** Token accumulation requires the LLM provider to
return `usage` (prompt/completion token counts) in responses. Providers that
omit usage from streaming chunks (e.g. Anthropic via LiteLLM) will show
`promptTokens=0` in inference-parser logs — `max_tokens` enforcement won't
trigger for these providers, but `max_calls` and `max_duration_seconds` still
apply. Ollama, OpenAI, and Azure OpenAI include usage in streaming responses
and work fully.

## Testing

```bash
cd authbridge/authlib
go test ./plugins/tokenbudget/... -v -count=1
```

No external dependencies — tests use in-memory stores.
