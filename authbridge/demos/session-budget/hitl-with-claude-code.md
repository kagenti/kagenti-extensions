# HITL pause mode with Claude Code — local demo

A variant of [`hitl-local.md`](hitl-local.md) that swaps the Ollama +
`curl` driver for **Claude Code**. Same Redis, same approver, same
pause-mode contract — the new pieces are a TLS bridge (so HTTPS from
`claude` is decrypted before the parsers see it) and Claude Code as
the client. If you don't need an Anthropic account in the loop, use
[`hitl-local.md`](hitl-local.md) instead — same demo, less setup.

The [README quickstart][qs] `install-demo.sh` binary isn't compiled
with `include_plugin_sessionbudget`, so this doc builds
`authbridge-proxy` from source with the tag on.

[qs]: ../../../README.md#quick-start-local-no-kubernetes

## Prerequisites

On top of [`hitl-local.md` § Prerequisites](hitl-local.md#prerequisites)
(Docker, Go), you also need:

- `claude` CLI (`npm i -g @anthropic-ai/claude-code`) and a working
  Anthropic credential — either `ANTHROPIC_API_KEY` for direct API
  access, or `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` for a
  gateway (LiteLLM, internal proxy)

Ollama is not needed — Claude Code replaces it.

## Setup

### 1. Redis and the proxy binary

Follow [`hitl-local.md` § Start Redis](hitl-local.md#start-redis) and
[§ Build the proxy binary](hitl-local.md#build-the-proxy-binary-once).

### 2. Config

Use [`local/config-https.yaml`](local/config-https.yaml). Compared to
`hitl-local.md`'s `local/config.yaml` it adds `tls_bridge` (so the
proxy can decrypt HTTPS) and `mcp-parser` + `a2a-parser` (so agentic
tool traffic is classified alongside inference).

### 3. Launch the approver and proxy

Approver, in its own terminal (see [`hitl-local.md` § Terminal 1 — the
approver](hitl-local.md#terminal-1--the-approver) for the expected
banner):

```bash
cd authbridge/demos/session-budget/local
go run ./approver.go
```

Proxy, from the repo root:

```bash
./authbridge/cmd/authbridge-proxy/authbridge-proxy \
  -config ./authbridge/demos/session-budget/local/config-https.yaml
```

`:8082` must be free — the proxy always opens a transparent listener
there even in `roles: [forward]`, and a stale `authbridge-proxy` from
a prior run will fail boot with `address already in use`. If that
happens: `pkill -f authbridge-proxy` and relaunch.

The proxy generates `cortex-ca/ca.crt` (relative to the directory it
runs from) on first launch — that's the trust anchor Claude Code
needs. From the repo root that resolves to
`<repo-root>/cortex-ca/ca.crt`; grab the absolute path with
`ls "$(pwd)/cortex-ca/ca.crt"` from the same terminal. Look for these
lines in the log:

```text
level=WARN msg="tls-bridge: generated self-signed CA ..." ca_dir=cortex-ca ...
level=INFO msg="tls-bridge enabled" ca_dir=cortex-ca
level=INFO msg="HTTP server listening" name=forward-proxy addr=127.0.0.1:47600
level=INFO msg="authbridge-proxy starting" mode=proxy-sidecar
```

### 4. Launch Claude Code through the proxy

In a project-scoped `.claude/settings.local.json` (in the directory
you'll launch `claude` from — Claude Code gitignores this file, so
the proxy env stays local to this demo and out of shared project
settings and out of `~/.claude/settings.json`):

```json
{
  "env": {
    "HTTPS_PROXY": "http://127.0.0.1:47600",
    "NODE_EXTRA_CA_CERTS": "/absolute/path/to/cortex-ca/ca.crt",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
```

`NODE_EXTRA_CA_CERTS` must be an absolute path — settings files do
no `$PWD` / `~` expansion.

Keep `ANTHROPIC_AUTH_TOKEN` (and any other credential) in the process
environment or a secret store — export it in the shell that launches
`claude`, don't paste it into `.claude/settings.local.json`. If you
use `ANTHROPIC_BASE_URL` for a gateway, that URL is fine in the same
`env` block alongside the proxy vars. The forward proxy MITMs whatever
host Claude Code speaks HTTPS to, and gateway-fronted deployments hit
`/v1/messages` the same as direct Anthropic — the parsers work on both.

Start an interactive Claude Code session from that directory:

```bash
claude
```

The REPL keeps one HTTPS connection open through the proxy so you can
send several turns in a row and drive the approver alongside.

## Drive it

At the Claude Code prompt, send a short message (e.g. `reply with
the word: ok`), wait for the response, then send another. One
interactive turn on this LiteLLM path measured ~42.7k prompt tokens
(system prompt + cached context), so `max_tokens: 40000` in
`config-https.yaml` trips on turn 2 — turn 1 is evaluated against an
empty counter and passes.

What to watch:

- **Turn 1 (under budget)** — responds normally. Proxy logs
  `inference-parser: response ... promptTokens=... completionTokens=...`;
  `HGETALL session-budget:default` shows `tokens` jump by that turn's
  prompt+completion.
- **Turn 2 (previous response pushed the counter over)** — approval
  requested. The plugin checks cached counters at request time, sees
  the limit exceeded from turn 1's recorded response, and pauses this
  turn. Proxy logs `budget exceeded, requesting approval` with
  `reason="token limit reached: <n>/40000"`; the approver prints the
  pause prompt (same format as [`hitl-local.md` § Terminal 3][t3]).

[t3]: hitl-local.md#terminal-3--drive-it-with-curl

  - Type `a` → the turn completes.
  - Type `d` → Claude Code surfaces the 403 as
    ```text
    Failed to authenticate. API Error: 403 token limit reached: <n>/40000 (approval denied)
    ```

Claude Code retries internally on 403, so a single denied turn fires
the pause webhook several times (~7 in our tests) before surfacing
the error. With `pause_grace_period: 1ms` every retry re-prompts —
approve one, deny the next, without restarting. To survive real
agentic work (file reads, tool calls), raise `max_tokens` in
`config-https.yaml`; hot-reload picks it up.

## Shadow mode: measure before you block

Swap in [`local/config-https-observe.yaml`](local/config-https-observe.yaml)
(`on_exceed: "observe"`) to count tokens without blocking or calling
the approver — useful for sizing `max_tokens` against real traffic
before switching to `pause`. Every over-budget turn logs:

```text
level=WARN msg="budget exceeded (shadow mode)" plugin=session-budget \
  reason="token limit reached: 42292/100" tokens=42292 calls=1
```

The request continues when the budget is exceeded, and Redis keeps
accumulating. Skip the approver terminal entirely.

## Reset, auto modes, and cleanup

See [`hitl-local.md`](hitl-local.md) —
[§ Reset between runs](hitl-local.md#reset-between-runs),
[§ Auto modes for CI](hitl-local.md#auto-modes-for-ci),
[§ Cleanup](hitl-local.md#cleanup). Add `rm -rf cortex-ca` in the
directory the proxy ran from if you want to regenerate the CA on the
next run — the new `ca.crt` has a fresh serial, so re-point
`NODE_EXTRA_CA_CERTS` in `.claude/settings.local.json` at it (same
absolute path if you relaunch from the same directory) or `claude`
will fail the TLS handshake against the proxy.

## Caveats

- **`default_session_fallback: true`** pools all egress into one Redis
  bucket. Fine for a single-workload laptop demo; in multi-tenant
  deployments one caller exhausting the budget denies all others.
  Leave it off in production and rely on the inbound A2A session ID.
- **`--demo` overwrites `cortex-ca/demo.yaml`.** If you also run
  `authbridge-proxy --demo` in the same directory, it will clobber the
  config from step 2. Use a separate working directory or a config
  path outside `cortex-ca/`.
