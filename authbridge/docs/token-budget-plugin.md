# token-budget Plugin

Enforces per-session lifetime budgets on tokens, inference calls, and
wall-clock duration. Runs in the outbound pipeline alongside `inference-parser`.
Uses Redis for cross-pod durable counters; evaluates limits from a local
cache with zero I/O on the hot path. Returns HTTP 403 on breach (lifetime
cap is permanently exhausted; retrying won't help).

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
| `session_ttl_seconds` | no | 7200 | Redis key TTL; should be >= `max_duration_seconds` |
| `refresh_interval` | no | "5s" | How often to sync local cache from Redis |
| `redis_unavailable` | no | "fail_open" | `fail_open` or `fail_closed` when Redis is unreachable |

At least one of `max_tokens`, `max_calls`, or `max_duration_seconds` must be > 0.

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
    "limit_tokens": 50000,
    "limit_calls": 100
  }
}
```

## Redis Key Schema

```
token-budget:<session-id>  (Hash, TTL = session_ttl_seconds)
  tokens      int   cumulative TotalTokens
  calls       int   inference call count
  started_at  unix  first-call timestamp (set-if-not-exists)
```

## Failure Modes

| Scenario | Behavior |
|----------|----------|
| Redis down at startup | Pod fails to start (`Init` error) |
| Redis fails mid-session | Local cache continues enforcing; writes dropped silently |
| Pod restarts | First request passes (cold cache); refresh picks up Redis counters within one interval |
| `fail_closed` + outage | All inference requests denied until Redis recovers |

## Testing

```bash
cd authbridge/authlib
go test ./plugins/tokenbudget/... -v -count=1
```

No external dependencies — tests use in-memory stores.
