# Installing the demo assets (`github-tool` + `github-agent`)

Single install guide for both reusable demo workloads under `demo/assets/`, deployed to a
rossoctl/Kind cluster in namespace `team1`. Consolidates what used to be split across
[`docs/specs/demo/github-tool.md`](../../docs/specs/demo/github-tool.md) §7–8 and the agent
README's former "Deploying to Rossoctl" section, so the two installation paths can't drift apart
again. Prefer [`install.sh`](install.sh) over doing this by hand; the steps below are what it
automates.

## Prerequisites

- A running rossoctl/Kind cluster with the rossoctl-operator installed.
- Namespace **`team1`** already exists — created by the Rossoctl installer, not by
  anything in this repo. Nothing here creates cluster-owned resources.
- `kubectl`, `kind`, and a container runtime (`docker` or `podman`) on `PATH`.

## What gets deployed

| Workload | Image | Manifests, in order | Objects created (ns `team1`) |
|---|---|---|---|
| tool | `localhost/github-tool:latest` | `tools/github_tool/k8s/github-tool-deployment.yaml` | ServiceAccount + Deployment + Service `github-tool` (`:9090`) + `AgentRuntime` `github-tool` (`type: tool`) |
| agent | `localhost/github-agent:latest` | `agents/github_agent/k8s/configmaps.yaml`, **then** `agents/github_agent/k8s/github-agent-deployment.yaml` | ServiceAccount + Deployment + Service `github-agent` (`:8001`, `:8080`) + `AgentRuntime` `github-agent` (`type: agent`); the ConfigMaps are `authbridge-config` + `authproxy-routes` |

Per workload, in order: build the image → `kind load docker-image <image> --name <cluster>` →
apply the manifests → `kubectl rollout status`.

## Non-obvious invariants

These each fail far from their cause — read before editing the manifests or install path.

- **The tool `Service` must carry `protocol.rossoctl.io/mcp: "true"`.** This is a *deploy-time*
  label that the rossoctl operator does **not** add. Without it, UC-1's `analyze_tool` returns
  **502** — during onboarding, long after deployment looked fine. It is already present in the
  committed manifest; don't drop it when editing.
- **`rossoctl.io/type` is applied by the operator** from the `AgentRuntime` CR. Don't hand-set it
  on the pod.
- **The tool's declared `PORT` must not be `9090`.** The AuthBridge sidecar reuses the declared
  `PORT` as its own reverse-proxy listener and shifts the app's real listen port to `PORT+1`.
  AuthBridge also has a *fixed* health-check listener hardcoded to `9091`. `PORT: 9090` shifts the
  app to `9091`, colliding with that fixed listener — the container crash-loops fighting the
  sidecar for the port. The manifest uses `PORT: 9095` (shifted: `9096`), which clears every
  AuthBridge-fixed port (`8080`, `8081`, `9091`, `9093`, `9094`); the Service's external port stays
  `9090`.
- **Keycloak client registration is asynchronous.** The operator registers each workload as a
  client named `team1/<workload>` *after* the pods come up, so "rollout complete" is **not**
  "ready to onboard". Anything that needs the client must poll for it (the UC-1 onboarding demo
  does).
- **`github-tool-mcp` is not required for this install.** The agent README names the production
  44-tool `github-tool-mcp` server (deployed from `authbridge/demos/github-issue/`) as its
  `MCP_URL` target, and that is a different thing from `tools/github_tool/` (a 4-tool stub used
  only for UC-1 onboarding discovery). The MCP connection happens **per request inside
  `GithubExecutor.execute()`** (`a2a_agent.py:188-211`), not at startup — `run()` only builds the
  AgentCard and serves it. So the agent pod becomes ready and serves
  `/.well-known/agent-card.json` with no MCP server present, which is all UC-1 discovery needs.
  **Do not add `github-tool-mcp` to this install path.**
- **Namespace `team1` is a precondition, not an output.** No manifest here creates it; the
  Rossoctl installer owns it (and any labels it carries). `install.sh` fails fast with a
  pointer to the installer if the namespace is missing.

## Manual steps (what `install.sh` automates)

**Tool:**
```bash
cd tools/github_tool
podman build -t localhost/github-tool:latest .   # or docker; the localhost/ prefix must match the manifest's image ref
kind load docker-image localhost/github-tool:latest --name rossoctl
kubectl apply -f k8s/github-tool-deployment.yaml
kubectl rollout status deployment/github-tool -n team1
```

**Agent:**
```bash
cd agents/github_agent
podman build -t localhost/github-agent:latest .   # or docker; the localhost/ prefix must match the manifest's image ref
kind load docker-image localhost/github-agent:latest --name rossoctl
kubectl apply -f k8s/configmaps.yaml
kubectl apply -f k8s/github-agent-deployment.yaml
kubectl rollout status deployment/github-agent -n team1
```

## Verifying

```bash
# Tool got its MCP label and the operator stamped rossoctl.io/type
kubectl get svc github-tool -n team1 -o jsonpath='{.metadata.labels.protocol\.rossoctl\.io/mcp}'
kubectl get pod -l app=github-tool -n team1 -o jsonpath='{.items[0].metadata.labels.rossoctl\.io/type}'

# Agent serves its card with no MCP server present
kubectl port-forward svc/github-agent 8080:8080 -n team1 &
curl -s http://localhost:8080/.well-known/agent-card.json | python3 -m json.tool
```

## Out of scope

- Deploying `github-tool-mcp`, Keycloak, SPIRE, the rossoctl operator, or the cluster itself.
- Deploying the AIAC stack (`k8s/`) — see `k8s/aiac-deployment-guide.md`.
- Waiting for Keycloak client registration — that needs Keycloak credentials this install path
  has no business holding; it belongs to whatever use-case demo consumes the client (e.g. UC-1's
  `00-prereqs.py`).
