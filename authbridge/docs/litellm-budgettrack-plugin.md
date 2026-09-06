# litellm-budget-track Plugin

Design document for the `litellm-budget-track` AuthBridge pipeline plugin.

**Issue:** https://github.com/rossoctl/rossoctl/issues/2177

## Overview

The `litellm-budget-track` plugin provides daily budget enforcement for AI agents
proxied through LiteLLM. It reads the `x-litellm-response-cost` header from
upstream LiteLLM responses, accumulates per-day spending, and rejects requests
with HTTP 429 when the configured daily budget is exceeded.

## Use Case

When running AI agents through Cortex (local budget proxy), each agent needs
a spending cap. LiteLLM returns the cost of each completion in the
`x-litellm-response-cost` response header. This plugin reads that header in the
AuthBridge pipeline that carries the LLM traffic (see [Pipeline placement](#pipeline-placement)),
tracks cumulative daily spend in a JSON ledger file, and blocks further requests
once the budget is exhausted.

## Architecture

```
Agent → cortex.py → AuthBridge (litellm-budget-track) → LiteLLM upstream
                                    │
                                    ├── OnRequest: check if budget exceeded → 429
                                    └── OnResponse: read x-litellm-response-cost, accumulate
                                           │
                                           └── spend-authbridge.json (daily ledger)
```

The plugin hooks:
- `OnRequest` — pre-flight budget check (reject if over limit).
- `OnResponseFrame` — post-flight cost accounting for **every** response. Because the
  plugin is a `StreamingResponder`, in-tree listeners route all responses through this
  hook — a buffered `application/json` body as a single terminal frame, a streamed
  `text/event-stream` body frame-by-frame — and `pipeline.RunResponse` skips the plugin
  unconditionally. (`OnResponse` remains only as a fallback for a hypothetical listener
  that calls it but never `OnResponseFrame`; no in-tree listener does.)

### Cost source (what the terminal frame charges)

The cost is settled **once**, on the terminal frame, from one of two sources:

- **Response header** — `x-litellm-response-cost`, falling back to the pre-discount
  `-original` variant. Used whenever the header carries a usable positive cost. A
  header of `0` on a non-streamed response is a genuine free call (cache hit / error)
  and is charged `0` — it is **not** re-priced from usage.
- **Parsed token usage × configured rates** — used only when the cost header is
  **absent**, or the response is `text/event-stream` (LiteLLM always reports `0` in the
  header for streams, e.g. Claude Code's `/v1/messages`). The plugin sums the token
  usage from the terminal SSE events and prices each **prompt-cache tier separately**:
  uncached input × `input_cost_per_token`, cache writes × `cache_write_cost_per_token`,
  cache reads × `cache_read_cost_per_token`, output × `output_cost_per_token`. Without
  any input rate a streamed response contributes `0`.

  **Cache tiers matter.** Providers charge a premium to *write* a cache entry and a
  steep discount to *read* one, so two requests with identical prompt-token counts can
  differ ~10× in price. If `cache_write_cost_per_token` / `cache_read_cost_per_token`
  are unset they default to `input_cost_per_token` (flat pricing), which **overstates
  cache-heavy traffic like Claude Code by up to ~10×** and would trip the 429 that much
  earlier. Set the two cache rates to your provider's real prices for accurate budgets.
  (This only affects the usage-fallback path; when LiteLLM's `x-litellm-response-cost`
  header is present it already accounts for cache tiers and wins.)

> **envoy-sidecar note.** Because the plugin declares `ReadsBody`, the extproc listener
> requests `ResponseBodyMode: BUFFERED` — so on the envoy-sidecar path the SSE body is
> buffered (capped at that listener's 1 MB `maxBodySize`) and "frame-by-frame" means
> re-parsed from the buffered body, not incrementally as events arrive. The proxy
> (forward/reverse) listeners stream frame-by-frame as normal.

## Files

| File | Purpose |
|------|---------|
| `authbridge/authlib/plugins/litellm_budgettrack/plugin.go` | Plugin implementation |
| `authbridge/cmd/authbridge-proxy/plugins_litellm_budgettrack.go` | Registration (build-tag gated) |

## Plugin Configuration

In the AuthBridge `config.yaml` pipeline section:

```yaml
pipeline:
  inbound:
    plugins:
      - name: litellm-budget-track
        config:
          spend_file: /etc/cortex/spend-authbridge.json
          max_budget: 5.00
```

### Pipeline placement

The plugin is direction-agnostic — place it in whichever pipeline carries the LLM
traffic: **`inbound`** when fronting the LLM endpoint (a reverse proxy the LLM requests
arrive at, as shown above), **`outbound`** when hosting the agent via
`rossoctl authbridge exec -- <agent>`, whose forward proxy runs the outbound pipeline on
the agent's own egress to LiteLLM. In an `authbridge exec` setup nothing reaches the
inbound pipeline, so a plugin left under `inbound:` there records `$0` — use `outbound:`.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `spend_file` | string | yes | Path to the JSON ledger file (created if missing) |
| `max_budget` | float | yes | Daily budget in USD (must be > 0) |
| `input_cost_per_token` | float | no | USD per **uncached** input token; prices streamed responses (whose header cost is 0) from parsed usage |
| `output_cost_per_token` | float | no | USD per output/completion token; prices streamed responses from parsed usage |
| `cache_write_cost_per_token` | float | no | USD per cache-write (creation) input token; defaults to `input_cost_per_token` when unset |
| `cache_read_cost_per_token` | float | no | USD per cache-read input token; defaults to `input_cost_per_token` when unset |

## Ledger Format

The spend file (`spend-authbridge.json`) is a simple JSON object:

```json
{
  "date": "2026-07-09",
  "total_spend": 0.0234,
  "total_calls": 12
}
```

- Resets automatically at midnight UTC (when `date` doesn't match today)
- Written atomically after each response (no rotation needed)
- Safe for single-process use (mutex-protected in-memory + file sync)

## Behavior

### OnRequest (pre-flight check)

1. Lock mutex
2. If `date` != today UTC → reset ledger to zero
3. If `total_spend >= max_budget` → return HTTP 429 with body:
   ```
   Cortex ExceededTokenBudget: daily spend $X.XXXX exceeds budget $Y.YY. Reset at midnight UTC.
   ```
4. Otherwise → continue pipeline

### OnResponse (cost accumulation)

1. Read the cost from the **response** headers (`pctx.ResponseHeaders`):
   `X-Litellm-Response-Cost`, falling back to `X-Litellm-Response-Cost-Original`
   when the bare header is absent. The bare (effective, post-discount) header is
   present on OpenAI `/v1/chat/completions` responses; the Anthropic `/v1/messages`
   endpoint used by Claude Code — and newer LiteLLM releases — emit only the
   pre-discount `-original` variant.
2. If missing or non-positive → continue (no cost to track)
3. Lock mutex, reset if new day
4. Add cost to `total_spend`, increment `total_calls`
5. Write ledger to disk
6. Continue pipeline

## Build

The plugin is included by default in `authbridge-proxy` builds. To exclude:

```bash
go build -tags exclude_plugin_litellm_budgettrack ./cmd/authbridge-proxy/
```

The registration file uses the standard build-tag pattern:

```go
//go:build !exclude_plugin_litellm_budgettrack

package main

import _ "github.com/rossoctl/cortex/authbridge/authlib/plugins/litellm_budgettrack"
```

## Integration with Cortex

Cortex generates the AuthBridge config on startup, embedding the plugin
in the inbound pipeline with the correct `spend_file` path and `max_budget`
from the CLI flags:

```bash
# rossoctlx.py start --budget 5.00
# → generates config.yaml with litellm-budget-track plugin
#   spend_file = ~/.config/cortex/spend-authbridge.json
#   max_budget = 5.00
```

The plugin complements cortex.py's own budget tracking (which reads
the same `x-litellm-response-cost` header on direct HTTP requests). For
CONNECT-tunneled traffic that flows through AuthBridge's TLS bridge,
the plugin is the only cost-tracking mechanism.

## Testing

```bash
cd authbridge/authlib/plugins/litellm_budgettrack

# Run the plugin in a test pipeline
go test -v ./...

# Or build authbridge-proxy with the plugin and test end-to-end:
cd authbridge
go build ./cmd/authbridge-proxy/
./authbridge-proxy --config test-config.yaml
# Send requests with x-litellm-response-cost header in responses
```

## Future Work

- Per-agent budget tracking (separate ledger files per agent identity)
- Budget alerts at configurable thresholds (e.g., 80% warning)
- Weekly/monthly budget periods (not just daily)
- Integration with cortex control API for real-time budget queries
