# Weather Agent — Advanced AuthBridge Demo (UI)

UI-driven companion to the beginner [Weather Agent demo](demo-ui.md). Same agent
and tool images, but with the full **token-exchange + ingress validation** story
turned on.

## How this differs from the standard demo

| | Standard ([demo-ui.md](demo-ui.md)) | **Advanced (this guide)** |
|---|---|---|
| AuthBridge on tool | Off (passthrough) | **On** — JWT validated at the tool's ingress |
| Outbound from agent | Passthrough | **RFC 8693 token exchange** to the tool's audience |
| Keycloak setup | None | One Python script (audience scopes + token-exchange enabled) |
| `authproxy-routes` | Not used | One outbound route, set in the UI |
| Resource names | `weather-service`, `weather-tool` | `weather-service-advanced`, `weather-tool-advanced` |

The `-advanced` names live alongside the beginner names, so both demos can run
in the same namespace. **Use them exactly** — the Keycloak script registers
SPIFFE / audience scopes for the `-advanced` ServiceAccounts, so a wrong name
breaks `MCP_URL`, the outbound route, and token-exchange audiences.

## Prerequisites

- Beginner [demo-ui.md prerequisites](demo-ui.md#prerequisites) (Rossoctl UI
  reachable, an LLM provider).
- In **`team1`**: installer-provided `authbridge-config`,
  `authbridge-runtime-config`, `spiffe-helper-config`, `envoy-config`. No extra
  Secrets or ConfigMaps up front — in particular **no `keycloak-admin-secret`**
  in `team1` *or* `rossoctl-system` (v0.7.0 registers clients via the operator's
  own SPIFFE identity, so a `NotFound` in either namespace is expected).
- Python 3.10+ for the Keycloak setup script (Step 1).
- For OpenAI: a `team1` Secret named `openai-secret` (created in Step 3).

SPIFFE IDs (trust domain `localtest.me`):

- Agent: `spiffe://localtest.me/ns/team1/sa/weather-service-advanced`
- Tool: `spiffe://localtest.me/ns/team1/sa/weather-tool-advanced`

---

## Step 1: Configure Keycloak (one-time)

Adds the audience scopes and enables `standard.token.exchange.enabled` on the
agent and tool clients.

```bash
cd authbridge
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python demos/weather-agent/setup_keycloak_weather_advanced.py \
  -n team1 --wait-tool-client
```

> **Do not `kubectl port-forward` Keycloak on `:8080`.** `keycloak.localtest.me:8080`
> is already reachable directly; a forward on local `:8080` hijacks the Rossoctl UI
> (`rossoctl-ui.localtest.me:8080`) into the Keycloak admin console. If you must
> forward, use another local port and set `KEYCLOAK_URL` to match.

> **Timing:** `--wait-tool-client` blocks ~5 min for the tool's SPIFFE client, then
> times out. Simplest: **deploy the tool (Step 2) first**, then run this — it finds
> the client immediately. On timeout, just re-run after the tool is up (idempotent).

**Re-run the same command (without `--wait-tool-client`) after Step 3** so the
agent client picks up the optional exchange scope.

The script (idempotent) adds the default scope `agent-team1-weather-service-advanced-aud`
(agent SPIFFE in `aud` for UI / `alice` tokens) and the optional scope
`weather-tool-exchange-aud` (tool SPIFFE in `aud` during exchange), enables token
exchange on both clients, and creates demo user `alice` (for the Step 5 CLI verify).

---

## Step 2: Import the Weather Tool via Rossoctl UI

> ⚠️ **Tool Name** must be `weather-tool-advanced` exactly (see [naming](#how-this-differs-from-the-standard-demo)).
> Import as `weather-tool` and the Service becomes `weather-tool-mcp`, so the
> Step 3 `MCP_URL` + outbound route won't resolve.

1. Open [Import Tool](http://rossoctl-ui.localtest.me:8080/tools/import).
2. **Namespace**: `team1` · **Tool Name**: `weather-tool-advanced` (exact).
3. **Deploy from Image** · **Container Image**:
   `ghcr.io/rossoctl/examples/weather_tool` · **Image Tag**: `latest`.
4. **MCP Transport Protocol**: `streamable HTTP`.
5. **Enable AuthBridge sidecar injection**: ✅ **check** (advanced demo
   validates JWTs at the tool's ingress — this is the difference vs. the
   standard demo).
6. **Enable SPIRE identity (JWT-SVID via spiffe-helper)**: ✅ **check**.
7. **Service Port** `8000` · **Target Port** `8000`.
8. Click **Build & Deploy Tool**.

Wait for the tool pod to be **Ready**. Once it registers in Keycloak, the
`setup_keycloak_weather_advanced.py` from Step 1 unblocks.

```bash
kubectl get pods -n team1 -l app.kubernetes.io/name=weather-tool-advanced
# Expect 2/2 (mcp + authbridge-proxy). No separate spiffe-helper container —
# authbridge-proxy sources its SPIRE credentials in-process — so it's 2/2, not 3/3.
```

---

## Step 3: Import the Weather Agent via Rossoctl UI

If you're using OpenAI, create the secret first (replace `<YOUR_OPENAI_API_KEY>`
with your real key — an empty secret makes the agent fail with `Error: No LLM API
key configured.`, see [Troubleshooting](#troubleshooting)):

```bash
kubectl create secret generic openai-secret -n team1 \
  --from-literal=apikey="<YOUR_OPENAI_API_KEY>"
```

> ⚠️ **Agent Name** must be `weather-service-advanced` exactly (see
> [naming](#how-this-differs-from-the-standard-demo)); a wrong name gives
> mismatched audiences and a 401/503 from token exchange.

Now the UI flow (order matches the actual import form top-to-bottom):

1. Open [Import Agent](http://rossoctl-ui.localtest.me:8080/agents/import).
2. **Namespace**: `team1` · **Agent Name**: `weather-service-advanced` (exact).
3. **Build from Source**:
   - Git Repository URL: `https://github.com/rossoctl/examples`
   - Git Branch or Tag: `main`
   - Select Agent: `Weather Service Agent`
   - Source Subfolder: `a2a/weather_service`
4. **Protocol**: `A2A` · **Workload Type**: leave the default `Sandbox`.

   > ⚠️ **Do not select `Deployment` for the agent.** Choosing `Deployment`
   > currently fails: the source build succeeds but the workload create is
   > rejected by the `agent-label-protection` admission policy (the
   > `rossoctl.io/type` label may only be set by the operator via an
   > AgentRuntime CR), so **no agent pod and no AgentRuntime are created**.
   > The `Sandbox` default goes through the operator and works. The agent
   > then runs as a bare pod owned by a `Sandbox` CR — verify/exec commands
   > below use label selectors rather than `deploy/...`.
5. **Secure with AuthBridge**: ✅ (default).
6. **Enable SPIRE identity (JWT-SVID via spiffe-helper)**: ✅ (default).
7. Expand **Outbound OIDC token exchange rules** and add one route — this is
   what triggers the RFC 8693 exchange when the agent calls the tool. The
   table has three columns; after you add the route the header should read
   **(1 route)**:

   - **Host Pattern**: `weather-tool-advanced-mcp`
   - **Target OIDC Audience**: `spiffe://localtest.me/ns/team1/sa/weather-tool-advanced`
   - **OIDC Token Scopes**: `openid weather-tool-exchange-aud`

   > If **Outbound Routing Rules** is missing or unresponsive, your Rossoctl
   > backend may pre-date [rossoctl#1194](https://github.com/rossoctl/rossoctl/pull/1194).
   > Apply the equivalent ConfigMap with kubectl (
   > `kubectl apply -f authbridge/demos/weather-agent/k8s/configmaps-advanced.yaml`)
   > and skip this expander. Same content, list-shaped `routes.yaml`.

8. **(Ollama only)** Expand **AuthBridge Advanced Configuration** and set
   **Bypass AuthBridge on these outbound ports** to `11434`. OpenAI uses HTTPS
   and needs no exclusion.
9. **Service Port** `8080` · **Target Port** `8000`.
10. Under **Environment Variables**, click **Import from File/URL** → **From
    URL**, paste one of the beginner agent's env files, and click
    **Fetch & Parse**:
    - OpenAI: `https://raw.githubusercontent.com/rossoctl/examples/refs/heads/main/a2a/weather_service/.env.openai`
    - Ollama: `https://raw.githubusercontent.com/rossoctl/examples/refs/heads/main/a2a/weather_service/.env.ollama`

    The OpenAI variant adds `LLM_API_KEY` and `OPENAI_API_KEY` as **Secret**
    entries pointing at `openai-secret`.

    After import, **edit `MCP_URL`** in the variable list to point at the
    advanced tool service:
    ```text
    MCP_URL=http://weather-tool-advanced-mcp:8000/mcp
    ```
11. Click **Build & Deploy Agent**.

After the agent pod is **Ready**, re-run the Keycloak script so the agent's
dynamic client gets the optional exchange scope:

```bash
python demos/weather-agent/setup_keycloak_weather_advanced.py -n team1
```

---

## Step 4: Chat via Rossoctl UI

> **Expected catalog quirk.** The **Agent Catalog** shows **two** entries
> (`weather-service-advanced` *and* `weather-tool-advanced`) and the **Tool
> Catalog** is empty — by design, because the tool's AgentRuntime uses
> `type: agent` (see [Operator gotchas](#operator-gotchas)). Pick
> `weather-service-advanced` for chat.

1. **Agent Catalog** → namespace `team1` → `weather-service-advanced` →
   **View Details**. The agent card should render (proves the agent is up and
   `/.well-known/*` is bypassed).
2. In the **Chat** panel, ask: *"What is the weather in New York?"*
3. The response should be live weather. Behind the scenes: the UI's JWT hits the
   agent's AuthBridge ingress → the agent matches the outbound route and exchanges
   for a token with `aud = weather-tool-advanced` SPIFFE → the tool's AuthBridge
   validates that JWT before MCP sees it.

`Connection error` or `No LLM API key configured` are LLM-side failures, not
AuthBridge — see [Troubleshooting](#troubleshooting).

---

## Step 5 (Optional): Verify via CLI

`deploy_and_verify_advanced.sh` exercises the AuthBridge / MCP path
end-to-end **without an LLM**. It's the right tool to confirm token exchange
and ingress validation when you want to isolate AuthBridge from agent-side
issues.

Pick the invocation based on the current cluster state:

```bash
# You got here via Steps 2–3 (tool + agent already deployed): verify only.
SKIP_DEPLOY=1 ./authbridge/demos/weather-agent/deploy_and_verify_advanced.sh

# Starting from scratch / CLI-only (nothing deployed yet): deploy, then verify.
./authbridge/demos/weather-agent/deploy_and_verify_advanced.sh
```

Most readers reach this step **after** deploying via the UI, so `SKIP_DEPLOY=1`
is the usual choice — the bare command re-applies the manifests, which the
[kubectl-only appendix](#appendix-kubectl-only-path) path uses. Both are
idempotent; if the run aborts with `verify pod produced no MCP_HTTP_CODE line`,
just re-run it (see [Troubleshooting](#troubleshooting)).

What it does: password-grants `alice`, token-exchanges to the tool SPIFFE audience
(scope `openid weather-tool-exchange-aud`), then `POST /mcp` **with** the exchanged
token → expects **2xx** (JWT accepted, `initialize` completes) and **without** an
`Authorization` header → expects **401** (AuthBridge rejects before MCP). It sends
`Accept: application/json, text/event-stream` so streamable HTTP doesn't return 406.

> **It does NOT call the LLM** — so it can pass while UI chat still returns
> `Connection error` (see Troubleshooting).

Env knobs: `SKIP_DEPLOY=1` (verify only — the default after Steps 2–3) and
`NAMESPACE` (default `team1`) are the common ones. Advanced overrides —
`KC_INTERNAL`, `KC_USER_CLIENT_ID`, `KEYCLOAK_ADMIN_USERNAME` /
`KEYCLOAK_ADMIN_PASSWORD` — are documented in the script header.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| UI returns `Error: LLM execution failed: Connection error.` | Agent can't reach its LLM. Ollama not running, or **Bypass AuthBridge on these outbound ports** not set to `11434`. `deploy_and_verify_advanced.sh` doesn't catch this — it never calls the LLM. | Start Ollama (`ollama serve` + `ollama pull llama3.2:3b-instruct-fp16`), or re-import with the OpenAI `.env` URL. |
| UI returns `Error: No LLM API key configured. Set the LLM_API_KEY environment variable.` | `openai-secret` is empty (often because `$OPENAI_API_KEY` wasn't exported when you ran `kubectl create secret`), or the agent wasn't restarted after fixing it. | Recreate with the literal value, then verify `kubectl get secret openai-secret -n team1 -o jsonpath='{.data.apikey}' \| base64 -d \| wc -c` is non-zero. The agent runs as a **Sandbox** (bare pod, no Deployment), so `kubectl rollout restart deploy/...` fails; restart by deleting the pod — the `Sandbox` CR recreates it: `kubectl delete pod -n team1 -l app.kubernetes.io/name=weather-service-advanced`. |
| UI: **Outbound Routing Rules** expander missing | Rossoctl backend pre-dates [rossoctl#1194](https://github.com/rossoctl/rossoctl/pull/1194) | `kubectl apply -f authbridge/demos/weather-agent/k8s/configmaps-advanced.yaml` and skip the UI step. |
| UI: agent card not available | AuthBridge failed to load `authproxy-routes` (invalid YAML shape) | See the same section in the [GitHub Issue UI demo](../github-issue/demo-ui.md#agent-card-not-available-in-the-ui). |
| `401` on tool MCP from CLI verify | Wrong `target_audience` or scope mapper | `target_audience` must equal the tool SPIFFE; scope `weather-tool-exchange-aud` must map that audience. Re-run `setup_keycloak_weather_advanced.py`. |
| CLI verify aborts with `ERROR: verify pod produced no MCP_HTTP_CODE line` | Transient: the ephemeral `netshoot` verify pod was torn down (`--rm`) before it printed a result — a slow first-run pod start, not an auth failure. Its stderr is lost, so the message looks alarming. | Just re-run the script — it's idempotent. To see the pod's own diagnostics on a genuine failure, capture stderr too: `SKIP_DEPLOY=1 ./...deploy_and_verify_advanced.sh 2>&1 \| tee /tmp/v.log`. |
| `invalid_scope` / `503` from agent | Optional exchange scope not on agent client | Re-run `setup_keycloak_weather_advanced.py -n team1` **after** the agent is running. |
| Token exchange denied | Tool client missing `standard.token.exchange.enabled` | Re-run setup with `--wait-tool-client` after the tool pod registers. |
| Tool pod CrashLoopBackOff (`mcp` container) | The `weather_tool` image runs as UID 1001; a `securityContext` overriding the user breaks `uv run` | Use the manifests in `k8s/` as-is (they set `runAsUser/Group/fsGroup: 1001`). On OpenShift, see the [upstream Dockerfile](https://github.com/rossoctl/examples/blob/main/mcp/weather_tool/Dockerfile). |
| Tool ingress logs missing `[Inbound]` | The `authbridge-proxy` sidecar uses different log text | Grep for `Token validated` instead, or widen the log window. |
| Deleted the agent or tool, but the Deployment + Service reappear within seconds | The Rossoctl backend's reconciliation service finalizes "orphaned" Shipwright builds by re-creating workloads | Also delete the Shipwright `Build` and `BuildRun` (see the [Cleanup](#cleanup) snippet). |
| Chat returns `Cannot connect to MCP weather service at http://weather-tool-advanced-mcp:8000/mcp` | UI import used the standard names (`weather-tool` / `weather-service`) instead of `-advanced`, so the actual Service is `weather-tool-mcp` and `MCP_URL` doesn't resolve | Re-import using the exact `-advanced` names. The Keycloak script from Step 1 also expects those names. |

AuthBridge logs (sidecar is `authbridge-proxy` in the default mode, `envoy-proxy`
in envoy-sidecar). The tool is a **Deployment** (`deploy/...` works); the agent is
a **Sandbox**, so resolve its pod name first:

```bash
# Tool ingress:
kubectl logs deploy/weather-tool-advanced -n team1 -c authbridge-proxy 2>&1 | grep -E "Inbound|Token validated"

# Agent outbound (Sandbox):
AGENT_POD=$(kubectl get pod -n team1 -l app.kubernetes.io/name=weather-service-advanced \
  -o jsonpath='{.items[0].metadata.name}')
kubectl logs "$AGENT_POD" -n team1 -c authbridge-proxy 2>&1 | grep -E "Resolver|exchange|Injecting token"
```

---

## Cleanup

Delete via the Rossoctl UI (Tool Catalog / Agent Catalog), or via CLI.

**Delete the `AgentRuntime` CRs — they are the parent resource.** The agent runs
as a `Sandbox` and the tool as a `Deployment`, both owned by an AgentRuntime. If
you delete the child workload (Sandbox/Deployment) but leave the AgentRuntime, the
operator enters a reconcile error loop (`Failed to resolve targetRef ... not
found`) every ~30s. Deleting the AgentRuntime cascades to its children:

```bash
kubectl delete agentruntime -n team1 \
  weather-service-advanced weather-tool-advanced --ignore-not-found

# Also delete the Shipwright Build/BuildRun, otherwise the Rossoctl
# backend's reconciliation service treats them as "orphaned" and
# recreates the workload within seconds:
kubectl delete build.shipwright.io,buildrun.shipwright.io -n team1 \
  -l app.kubernetes.io/name=weather-service-advanced --ignore-not-found
kubectl delete build.shipwright.io,buildrun.shipwright.io -n team1 \
  -l app.kubernetes.io/name=weather-tool-advanced --ignore-not-found
```

Keycloak clients for the SPIFFE IDs can be removed from the admin console.

---

## Appendix: kubectl-only path

If you'd rather skip the UI entirely, the same demo runs via raw manifests.

```bash
# 1. Keycloak setup (same as Step 1 above, but with --wait-tool-client first)
python authbridge/demos/weather-agent/setup_keycloak_weather_advanced.py \
  -n team1 --wait-tool-client &

# 2. Apply manifests
kubectl apply -f authbridge/demos/weather-agent/k8s/configmaps-advanced.yaml
kubectl apply -f authbridge/demos/weather-agent/k8s/weather-tool-advanced.yaml
kubectl apply -f authbridge/demos/weather-agent/k8s/agentruntime-weather-tool-advanced.yaml
kubectl rollout status deploy/weather-tool-advanced -n team1 --timeout=300s

kubectl apply -f authbridge/demos/weather-agent/k8s/weather-service-advanced.yaml
kubectl apply -f authbridge/demos/weather-agent/k8s/agentruntime-weather-service-advanced.yaml
kubectl rollout status deploy/weather-service-advanced -n team1 --timeout=420s

# 3. Re-run Keycloak setup so the agent client gets the optional exchange scope
python authbridge/demos/weather-agent/setup_keycloak_weather_advanced.py -n team1
```

The shipped agent manifest defaults to Ollama. To switch to OpenAI without the
UI:

```bash
kubectl create secret generic openai-secret -n team1 \
  --from-literal=apikey="<YOUR_OPENAI_API_KEY>"

kubectl set env deploy/weather-service-advanced -n team1 -c agent \
  LLM_API_BASE="https://api.openai.com/v1" \
  LLM_MODEL="gpt-4o-mini-2024-07-18" \
  LLM_API_KEY-

kubectl patch deploy weather-service-advanced -n team1 --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{
    "name":"LLM_API_KEY",
    "valueFrom":{"secretKeyRef":{"name":"openai-secret","key":"apikey"}}
  }},
  {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{
    "name":"OPENAI_API_KEY",
    "valueFrom":{"secretKeyRef":{"name":"openai-secret","key":"apikey"}}
  }}
]'
```

(The `LLM_API_KEY-` clears the manifest's literal `"ollama"` value before the
`patch` re-adds it as a secret reference — otherwise both exist, and while the
secret wins it's noisy in `kubectl describe`.)

### Operator gotchas

If the demo silently produces unprotected pods, check these:

- **`AgentRuntime` is required.** The mutating webhook **skips** AuthBridge
  injection unless an `agent.rossoctl.dev/v1alpha1` `AgentRuntime` matches the
  Deployment. The k8s manifests here include them; if you build by hand,
  add them and **restart the Deployment** so new pods are admitted.
- **`spec.type: agent`, not `tool`.** With `spec.type: tool` the operator
  relabels the pod and the `injectTools` feature gate (off by default)
  controls injection — so the tool ends up with no AuthBridge. This demo
  uses `spec.type: agent` for the tool's runtime CR.
- **Don't set `rossoctl.io/client-registration-inject: "true"`** — that label
  references a removed in-pod sidecar and disables operator-managed
  registration entirely (#411).

---

## Related Files

| File | Role |
|------|------|
| [k8s/configmaps-advanced.yaml](k8s/configmaps-advanced.yaml) | `authproxy-routes` for token exchange |
| [k8s/weather-tool-advanced.yaml](k8s/weather-tool-advanced.yaml) | Tool Deployment + Service + SA |
| [k8s/weather-service-advanced.yaml](k8s/weather-service-advanced.yaml) | Agent Deployment + Service + SA |
| [setup_keycloak_weather_advanced.py](setup_keycloak_weather_advanced.py) | Keycloak realm tuning |
| [deploy_and_verify_advanced.sh](deploy_and_verify_advanced.sh) | One-shot deploy + CI-style verification (no LLM) |
