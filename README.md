# Cortex

Cortex delivers easy-to-use platform services to agentic workloads. It runs in a workload's request path — a sidecar in Kubernetes, or a standalone binary anywhere else — and provides:

- **Identity & access** — a verifiable identity for each workload, authentication and authorization of its calls, and the right credentials for each downstream service.
- **Guardrails** — block agent actions that stray from the user's intent or aren't grounded in the conversation.
- **Observability** — decrypt and parse a workload's model, tool, and agent-to-agent traffic into a live view.
- **Egress control** — govern which external services a workload can reach.
- **Optimizations** — trim the model context a workload sends and cap its spend, to cut latency and cost.

It ships as a single binary; the identity and access layer is **AuthBridge**, and the code lives under [`authbridge/`](./authbridge/).

## Quick start (local, no Kubernetes)

Watch an AI agent's traffic — its model, tool, and agent-to-agent calls — decrypted and parsed live on your laptop.

1. **Install and start the demo** (macOS/Linux). Downloads two small binaries and starts the proxy in the background:

   ```sh
   curl -fsSL https://raw.githubusercontent.com/rossoctl/cortex/main/authbridge/install-demo.sh | sh
   ```

2. **Open the live viewer** in another terminal:

   ```sh
   abctl --endpoint http://localhost:47601
   ```

3. **Send an agent's traffic through it** — e.g. Claude Code, from the directory where you started the demo:

   ```sh
   HTTPS_PROXY=http://localhost:47600 \
     NODE_EXTRA_CA_CERTS="$PWD/cortex-ca/ca.crt" \
     CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
     claude
   ```

   Its calls stream into `abctl`, decrypted and parsed.

## Running on Kubernetes

In a cluster, Cortex sidecars are injected automatically by the [operator](https://github.com/rossoctl/operator), with Keycloak + SPIFFE/SPIRE for identity and token exchange. Start with the end-to-end **[Weather Agent walkthrough](./authbridge/demos/weather-agent/demo-ui.md)** (or the [`abctl` version](./authbridge/demos/weather-agent/demo-with-abctl.md)); see the [demos index](./authbridge/demos/README.md) and the [architecture reference](./authbridge/README.md) for all modes and details.

## Related repositories

- [rossoctl](https://github.com/rossoctl/rossoctl) — core platform
- [operator](https://github.com/rossoctl/operator) — sidecar injection + admission webhook

## License

[Apache 2.0](./LICENSE)
