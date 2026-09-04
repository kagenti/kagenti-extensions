# Cortex

Cortex delivers easy-to-use platform services to agentic workloads. It runs in a workload's request path — a sidecar in Kubernetes, or a standalone binary anywhere else — and provides:

- **Identity & access** — a verifiable identity for each workload, authentication and authorization of its calls, and the right credentials for each downstream service.
- **Guardrails** — block agent actions that stray from the user's intent or aren't grounded in the conversation.
- **Observability** — decrypt and parse a workload's model, tool, and agent-to-agent traffic into a live view.
- **Egress control** — govern which external services a workload can reach.
- **Optimizations** — trim the model context a workload sends and cap its spend, to cut latency and cost.

It ships as a single binary; the identity and access layer is **AuthBridge**, and the code lives under [`authbridge/`](./authbridge/).

## Quick start — Claude Code on your laptop

See what Claude Code sends: model calls, tool calls, and agent-to-agent traffic,
decrypted and parsed live. No Kubernetes. macOS or Linux, amd64 or arm64.

```sh
curl -fsSL https://raw.githubusercontent.com/rossoctl/cortex/main/authbridge/install.sh \
  | sh -s -- --claude-code
```

That is the install. It asks before changing your Claude Code settings, then runs
Cortex as a background service that comes back at login and restarts itself if it
crashes.

> **macOS note:** launchd will not restart a user agent added mid-session, so on
> macOS the proxy runs under a small supervisor process that does. Verify with
> `kill -9 $(pgrep -f 'authbridge-proxy --config')` — it comes back within ~2s.

Now open two terminals:

```sh
abctl      # the viewer
claude     # Claude Code, as usual — no environment variables needed
```

Claude Code's calls stream into `abctl`. Cortex only reads them; nothing is rewritten.

**Cut token cost too** — strip the tool definitions your agent never calls, worth
4–20% of the prompt per turn, median 6%:
**[one more command](./authbridge/docs/laptop-token-savings.md)**.

**Starting, stopping, removing** — `abctl service status | start | stop`, and
`abctl claude-code disable` to unwire Claude Code:
**[the full lifecycle](./authbridge/docs/laptop-service.md)**.

Any agent works, not just Claude Code — point it at `localhost:47600` and trust
`~/.cortex/ca/ca.crt`. The URL is on `main`, but the script re-runs the copy from the
newest **release**, so `curl | sh` does not run unreleased code — `--ref` overrides.

## Running on Kubernetes

In a cluster, Cortex sidecars are injected automatically by the [operator](https://github.com/rossoctl/operator), with Keycloak + SPIFFE/SPIRE for identity and token exchange. Start with the end-to-end **[Weather Agent walkthrough](./authbridge/demos/weather-agent/demo-ui.md)** (or the [`abctl` version](./authbridge/demos/weather-agent/demo-with-abctl.md)); see the [demos index](./authbridge/demos/README.md) and the [architecture reference](./authbridge/README.md) for all modes and details.

## Related repositories

- [rossoctl](https://github.com/rossoctl/rossoctl) — core platform
- [operator](https://github.com/rossoctl/operator) — sidecar injection + admission webhook

## License

[Apache 2.0](./LICENSE)
