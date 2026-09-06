# `tool-prune` plugin

Removes unused tool definitions from outbound inference requests.

A Claude Code turn carries the full tool manifest on every request — tens of
thousands of tokens of JSON schema, billed each time, largely for tools the
agent will never call in a given deployment. The manifest is assembled by the
client, so the proxy is the only place to trim it without changing every client.

The verdict is entirely configuration. `remove` names the tools to drop; there
is no learning, no state and no storage dependency. `abctl tools scan` proposes
a list, but the plugin only ever does what it was told.

## Configuration

```yaml
pipeline:
  outbound:
    plugins:
      - inference-parser
      - mcp-parser
      - name: tool-prune
        # on_error defaults to enforce; the empty remove list is what gates the
        # plugin. Set observe when you want a projection instead — see below.
        config:
          remove: [NotebookEdit, ScheduleWakeup, TaskOutput]
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `remove` | `[]string` | `[]` | Tool names to delete. Names absent from a given request are ignored. |
| `paths` | `[]string` | `/v1/chat/completions`, `/v1/completions`, `/v1/messages` | Request paths to act on, matched exactly or by suffix. |

**Placement matters.** `tool-prune` requires `inference-parser` earlier in the
chain, and because it rewrites the request body it must sit *after* every
body-reading plugin — readers have to see the original bytes. `pipeline.New`
enforces both and fails at startup rather than misbehaving quietly.

It declares `WritesRequestBody` only, never `WritesResponseBody`, so responses
still stream incrementally. See
[`plugin-reference.md`](./plugin-reference.md#capability-fields).

## Turning it on

**The empty `remove` list is the off switch.** With no tool named the plugin does
nothing, whatever the policy, so filling the list is the single act that enables
it:

```sh
abctl tools scan --write ~/.cortex/config.yaml
```

The config is hot-reloaded, so no restart. A reload does rebuild the plugin and
therefore **resets its counters** — the same as a process restart.

### Measure instead of enforce, when you want to

`on_error: observe` turns the plugin into a projection: it computes exactly what
it would remove and counts it, while every byte on the wire stays untouched.
Nothing in the plugin differs between the modes — under observe `SetBody` is a
no-op on bytes and leaves `BodyMutated()` false, which is how it knows which
counter to increment.

Two occasions worth it:

- **Sizing the change** before it affects anything: read `bytes removed` and
  `tokens saved / request`, decide, then remove the line.
- **Clearing the plugin of suspicion.** If requests start failing and you are
  not sure whether this is the cause, set `observe` and watch: the bytes are then
  provably unmodified, so a failure that persists is not this plugin. That is
  faster than reasoning about it, and it costs no configuration.

## Reading the metrics

`abctl`'s plugin detail pane shows a `Metrics:` section (source:
`GET /v1/pipeline`):

```text
Metrics:
  requests seen                      2  count
  requests pruned                    2  count
  tools removed                     22  count
  bytes removed                 57,136  bytes
  bytes removed / request       28,568  bytes
  tokens saved: cache write     13,044  tokens   estimate, n=2
  tokens saved: cache read      13,064  tokens   estimate, n=2
  $ saved                       0.2642  usd      estimate, n=2
  $ saved / request             0.1321  usd      estimate, n=2
  removed: NotebookEdit              2  count
```

In observe mode `requests projected` replaces `requests pruned`, so a
projection is never mistaken for a realised saving.

### Per request, in the events timeline

The events pane's `TOKENS / SAVED` column splits the two halves across the rows
they belong to — the saving on the request that was rewritten, the billed total on
the response:

```text
#   PHASE  ACTION   PLUGIN            TOKENS / SAVED     CODE
12  req    modify   tool-prune        −24.7k  $0.117
12  resp   observe  inference-parser  34,702             200
```

Under `on_error: observe` the saving is **projected**: the plugin measured what it
would remove but sent the request unchanged, so nothing was actually saved. Those
figures render with a leading `~` and no `−`, and the aggregate counts them
separately — an observe-mode run must not be added up as money not spent.


The saving is not shown on the response row: nothing about the response was
reduced, and putting it there reads as though it had been. The two rows share a
`#` so they are read together anyway.

Two turns that removed the same bytes can still differ ~12x in value — a cache
miss writes the manifest to cache (~1.25x the input rate), a hit reads it (~0.1x).
An aggregate averages the two into a number that describes neither, which is why
this is per row.

The plugin publishes the byte saving and the applicable rates on the request
event; the paired response supplies the prompt token total behind the
bytes-to-tokens ratio and the tier that picks the rate, so `abctl` finishes the
arithmetic. Pairing is exact, on the proxy-stamped request id. A model with no
rate shows the token saving with no dollar figure rather than one priced at
another model's rate.

### Why tokens are reported per tier and never summed

Byte counts are exact. Tokens are an estimate, and — more importantly — they
are **not fungible**. Providers price prompt tiers very differently: Anthropic
charges 1.25x the input rate for a cache write and 0.1x for a cache read, so the
same pruned bytes are worth more than **12x** more on a cache miss than on a
cache hit.

A single "tokens saved" figure would invite multiplying by one rate, which is
wrong by that factor. So the saving is attributed to the tier it actually came
out of and reported separately. The tool manifest sits inside the cached prefix
(Claude Code puts `cache_control` on the tool block), so a cache-miss request
saves cache-*write* tokens and a hit saves cache-*read* tokens. Traffic that
alternates shows both rows, and the honest headline is a range rather than a
point.

The bytes-to-tokens ratio is calibrated on your own traffic — prompt tokens over
request bytes for the same request, both post-pruning so the two sides agree —
rather than bundling a tokenizer or assuming a constant.

### The figure is gross, not net

Changing the `remove` list changes the cached prompt prefix, so the first request
after a change re-writes the whole prefix at the cache-**write** rate (~1.25x
input) while the recurring saving accrues at the cache-**read** rate (~0.1x) on a
small delta. Order of magnitude: re-warming a ~30k-token prefix costs on the order
of tens of thousands of input-equivalents against a few hundred saved per
subsequent cache-read request — **tens of requests to break even after each list
change.**

Two consequences worth being blunt about:

- **`$ saved` is gross.** It counts what the removed definitions would have cost
  and subtracts nothing for the re-warm. The row says so.
- **The re-warm is invisible exactly when it is paid.** Applying a list change
  hot-reloads the config, which rebuilds the plugin and resets its counters — so
  the run that incurs the cost starts from zero.

Practical reading: change the list rarely, and treat a figure gathered over a few
requests immediately after a change as optimistic. Over a long steady session the
gross figure converges on the net one, because the re-warm is paid once.

### Costing it

**Dollars work out of the box.** The plugin ships a rate table measured from the
rossoctl LiteLLM gateway, so `$ saved` appears with no configuration:

| pattern | input | cache write (1.25x) | cache read (0.10x) |
|---|---|---|---|
| `*claude-opus-*` | $3.80/Mtok | $4.75/Mtok | $0.38/Mtok |
| `*claude-sonnet-*` | $1.52/Mtok | $1.90/Mtok | $0.152/Mtok |
| `*claude-haiku-*` | $0.76/Mtok | $0.95/Mtok | $0.076/Mtok |

Rates are keyed **per model** because they differ far more than the tiers do —
5x across this family — so a single flat rate would misprice the saving by that
factor depending on which model served the request. Each request is priced at its
own model's rate and the dollars accumulated, never a blended token total
multiplied by one number.

Keys are **globs, and the built-ins are keyed by family rather than by version**,
which is what stops a model rename from becoming a code change. Model names churn
— opus 4.6, 4.7, 4.8, 5 — and a table of exact versions would go stale on every
release and need a rebuild to fix, which is not something an operator can be
asked to do. One pattern per family absorbs the churn and also covers provider
prefixes (`aws/claude-opus-5`) and dated suffixes
(`claude-haiku-4-5-20251001`) without separate entries.

The tradeoff, stated plainly: this assumes a family bills at one rate. That has
held across the Claude versions measured. If a future version differs, pin it —
an exact key always beats a pattern, so `claude-opus-6:` overrides
`*claude-opus-*` for that one model and leaves the family default doing its job
for the rest.

Any figure derived from these carries `default rates — set pricing.<model> to use
yours` in its note, because they are a starting point rather than a fact about
your account: they are specific to that gateway (which bills below vendor list),
and nothing refreshes them when they change. A model in neither the table nor
your config is reported in a `requests unpriced` row rather than charged at
another model's rate.

To use your own, add a `pricing` entry — it overrides the built-in value for that
model outright:

```yaml
- name: tool-prune
  config:
    remove: [CronCreate, NotebookEdit]
    pricing:
      "*claude-opus-*":
        input_cost_per_million: 3.80
        cache_write_cost_per_million: 4.75
        cache_read_cost_per_million: 0.38
    # optional flat fallback for models absent from the table above
    input_cost_per_million: 3.80
```

**Rates are stated per million tokens**, because that is the unit every provider
publishes and the one you already have in hand — `3.80`, copied straight off a
price list, rather than `0.0000038` arrived at by dividing in your head. That
difference is not just ergonomics: `0.0000038` is six leading zeros, and
`0.000038` is a plausible-looking typo that misprices by 10x with nothing in the
readout to reveal it.

The per-token field names are still accepted (`input_cost_per_token`, …), for
parity with [`litellm-budget-track`](./litellm-budgettrack-plugin.md) and because
LiteLLM's own `model_prices_and_context_window.json` is per-token, so rates get
copied out of it verbatim. Different tiers may use different units.

**Setting both units for the same tier is a startup error**, not a precedence
question. The two differ by 10<sup>6</sup>: silently honouring one would either
overstate a saving a millionfold or bury it under rounding, and the readout gives
you no way to tell which happened. The error names the offending entry and tier.

Model keys match what the parser records (`Extensions.Inference.Model`) and are
matched case-insensitively, since gateways vary in how they echo the name and a
case mismatch would silently unprice the traffic.

Config keys may be globs too (`*`, `?`, `[...]` — `gobwas/glob` with no separator,
so `*` spans the `-` and `/` in a model name). Resolution is deliberately ordered
so the more specific statement wins:

1. exact key in your `pricing`
2. glob in your `pricing` — **longest pattern first**, so `*claude-opus-4-8*`
   beats `*claude-opus-*` deterministically rather than by map iteration luck
3. built-in family pattern
4. the flat `input_cost_per_million` fallback
5. unpriced

An invalid pattern fails startup with the offending key named, rather than
silently dropping to unpriced — a typo'd glob and a genuinely unknown model
should not look the same in the readout.

A model with no entry and no fallback is **counted, not guessed**: the readout
grows a `requests unpriced` row naming the models, so an incomplete table shows
as a visible gap rather than a quietly understated total. Tokens are still
reported for those requests — only the dollars are withheld.

Field names within each entry match
[`litellm-budget-track`](./plugin-catalog.md#litellm-budget-track). Cache rates
fall back to that model's input rate, though on Anthropic-family models that
fallback is poor — a real cache read is 0.1x input — so set them when known.
There is deliberately no output rate: pruning only shrinks the prompt.

**Deriving your own rates.** If your gateway reports cost on non-streaming
responses (LiteLLM's `x-litellm-response-cost`), send two non-streaming requests
of different prompt length and difference them: `rate = Δcost / Δinput_tokens`.
Repeat with a `cache_control` block sent twice to get the write and read rates.
This is exact and specific to your deployment. Do not assume list pricing: a
shared or enterprise gateway commonly bills at negotiated rates well below it,
and using list would overstate the saving by whatever that discount is.

Why rates rather than the gateway's own number: LiteLLM reports
`x-litellm-response-cost: 0` for **streaming** responses, because the total is
not known when the headers are sent — and Claude Code streams every
`/v1/messages`. So the authoritative per-request cost is unavailable for exactly
the traffic this plugin prunes. `litellm-budget-track` hits the same wall and
falls back to configured rates for streams.

A saving is also a counterfactual — the cost of a request that was never sent —
so even with a cost header it could only ever be priced from rates, not measured.

Counters are in-memory and per-process, and reset on a config hot-reload as well
as a restart — a reload rebuilds the plugin. That is the right trade for the
single-laptop case this targets and what keeps the plugin free of a storage
dependency; fleet aggregation belongs on the stats server later and would not
change the plugin.

## Where the list comes from

```sh
abctl tools scan [--days N | --all] [--keep Name,Name] [--dir PATH] [--write CONFIG]
```

It reads `~/.claude/projects/**/*.jsonl`, deduplicates tool calls by their
unique `tool_use` block id (a transcript is rewritten on every resume, so raw
occurrences would inflate heavily-resumed sessions), and windows to the last
`--days` (30 by default). Without `--write` it prints the YAML block; with
`--write` it patches the `remove:` list of the `tool-prune` entry in place,
idempotently and without reformatting the rest of the file.

`--all` drops the window entirely: every call in every transcript counts as use.
Widening is the cautious direction — a longer window can only find more tools in
use, so it proposes fewer for removal — which is why `--days 0` is an error
rather than a synonym for `--all`: a zero-width window finds nothing used and
would propose removing every tool in the table. The summary line states which
mode ran, so a figure is never ambiguous about the window behind it.

**The offered-set problem.** Transcripts record tools that were *called*, never
tools that were *offered*. This is structural, not a defect: a
configured-but-never-invoked tool leaves no trace. Two consequences:

- The removal candidates are tools abctl knows Claude Code ships that you never
  called — which is also where most of the wasted tokens sit.
- A tool name the scan has never heard of is **kept**. Removing a tool the model
  needs is the harmful direction of failure; carrying a few extra definitions is
  merely expensive. Drift in the known-tool table costs savings, never
  correctness.

A `--keep` flag and a small implies table cover tools whose use is indirect —
`Agent` implying `SendMessage`, say, which a transcript may never show being
called by name. At runtime the plugin also logs, once, any configured name
absent from the first manifest it sees, so a stale list surfaces as a warning
rather than a silent no-op.

## Failure behaviour

Every error path forwards the original bytes unmodified: the plugin fails open on
a malformed or truncated body, an unparseable manifest, a rewrite that does not
shrink the body, a rewrite that produces invalid JSON, an unexpected tool count
afterward, and any panic.

**What that does and does not promise.** It means the plugin's own failure modes
cannot break a request — a bug or a surprising input forwards the original bytes
rather than a damaged rewrite. It does **not** promise that a validly pruned
manifest is acceptable to every provider or gateway in front of one. Pruning
changes the request, so if a provider rejects a request for a reason the plugin
cannot see, `on_error: observe` is how you find out safely: it counts what it
would remove while sending the bytes untouched.

Three specifics worth knowing:

- **A forced `tool_choice` is never pruned.** `tool_choice: {"type":"tool",
  "name":"X"}` (or OpenAI's `{"type":"function","function":{"name":"X"}}`) makes
  `X` mandatory; a `tool_choice` naming a tool absent from the manifest is an
  invalid request. `X` is kept even when the remove list names it, and the rest
  of the list still applies.

- **Nothing else in the request changes.** Deletions are surgical: every byte
  outside the removed array elements is preserved, including key order and
  whitespace.
- **Removing every tool drops the keys.** An empty `tools: []` is not a safe
  output — OpenAI rejects it, and rejects `tool_choice` without `tools` — so an
  over-broad list removes both keys instead of emptying the array.

## What the saving does and does not change

`/cost` and anything derived from the API response `usage` block **do** move:
the server bills the request it received, so `input_tokens` and
`cache_read_input_tokens` genuinely drop.

Claude Code's `/context` breakdown **does not**. It is a client-side pre-flight
view of what the CLI assembled, and it computes `Free space` itself; the pruning
happens downstream. This is the first place anyone looks, so it is worth stating
plainly: proxy-side pruning saves money but does not return context window. The
client still believes it sent the full manifest, so auto-compact triggers at the
same point. Recovering headroom needs client-side configuration
(`--allowedTools`, disabling unused MCP servers). AuthBridge's advantage is the
complement — it applies to every agent behind it with no per-client change, and
it measures.

One further caveat on the list changing: a new `remove` list invalidates the
prompt-cache prefix once. That is inherent and bounded — the list is static, so
it happens on the change and then the prefix is stable again.

## Build tag

Compiled in by default; exclude with `-tags exclude_plugin_toolprune`. The
`authbridge-lite` image excludes it along with the other non-auth plugins.
