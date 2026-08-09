# Runbook — Kagenti from zero on a Windows machine, up to a working weather agent

**Audience:** an AI coding agent executing on behalf of a human who is new to Kagenti.
**Goal:** a fresh Windows machine ends up with a local Kagenti platform (Kind cluster) running
the sample **weather agent** + **weather tool** from `kagenti/agent-examples`, verified by an
actual chat answer. **No lineage sidecar, no AuthBridge, no SPIRE** — plain stock Kagenti only.

---

## Instructions to the AI agent (read first)

1. Execute the phases **in order**. Each phase ends with a **CHECKPOINT** — do not continue
   until it passes. If a checkpoint fails twice, stop and report to the human exactly what
   failed and what you observed.
2. **Do not improvise alternative install methods.** In particular, inside the kagenti repo you
   will find stale paths — ignore all of these:
   - `.claude/skills/kagenti:deploy/SKILL.md` and anything mentioning `uv run kagenti-installer`
     or a cluster named `agent-platform` — **dead code**, the Python installer no longer exists.
   - The README line `git checkout v0.6.0` — **stale**, stay on `main`.
   - Anything about **podman** — this runbook uses docker engine inside WSL.
   - Anything about AuthBridge / sidecars / SPIRE / lineage — out of scope for now.
3. Steps marked **[HUMAN]** need the human (admin PowerShell, browser, reboots). Ask, wait,
   then continue.
4. Everything else runs **inside the WSL Ubuntu shell**, in the WSL Linux filesystem
   (`~/...`) — **never under `/mnt/c/`**.

---

## Phase 0 — [HUMAN] Windows prep: WSL2 + resources

Kagenti's minimal footprint still wants real memory. The machine should have **≥16 GB RAM**
(32 GB is comfortable).

1. In **admin PowerShell**:
   ```powershell
   wsl --install -d Ubuntu-24.04
   ```
   Reboot if asked; create the Linux user when prompted.
2. Create/edit `%UserProfile%\.wslconfig` (Windows side) to give WSL enough resources:
   ```ini
   [wsl2]
   memory=16GB      # 12GB is the working floor for this runbook's minimal install
   processors=6     # or as many as the machine has minus 2
   swap=8GB
   ```
   Then in PowerShell: `wsl --shutdown`, and reopen the Ubuntu terminal.

**CHECKPOINT 0** — inside the Ubuntu shell:
```bash
free -h        # total memory should show roughly what you set above
systemctl is-system-running   # should print "running" or "degraded" (both fine)
```
If `systemctl` errors with "not been booted with systemd": add to `/etc/wsl.conf`:
```ini
[boot]
systemd=true
```
then `[HUMAN]` `wsl --shutdown` and reopen.

---

## Phase 1 — Tooling inside WSL (docker engine, kind, kubectl, helm, git, jq)

> Docker **engine** inside WSL — *not* Docker Desktop (licensing at IBM, and not needed).

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg jq software-properties-common

# recent git (kagenti docs ask for >= 2.48; Ubuntu's default is older)
sudo add-apt-repository -y ppa:git-core/ppa
sudo apt-get update && sudo apt-get install -y git

# docker engine
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io
sudo usermod -aG docker "$USER"
```
**Open a new shell** (so the docker group applies), then:

```bash
# kind (latest release)
KIND_VER=$(curl -s https://api.github.com/repos/kubernetes-sigs/kind/releases/latest | jq -r .tag_name)
curl -Lo /tmp/kind "https://kind.sigs.k8s.io/dl/${KIND_VER}/kind-linux-amd64"
sudo install /tmp/kind /usr/local/bin/kind

# kubectl (latest stable)
curl -Lo /tmp/kubectl "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install /tmp/kubectl /usr/local/bin/kubectl

# helm 3
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
```

**CHECKPOINT 1**
```bash
docker run --rm hello-world     # prints "Hello from Docker!"
kind version && kubectl version --client && helm version && git --version && jq --version
```
Requirements: kubectl ≥ 1.32, helm ≥ 3.17 and < 4, git ≥ 2.48.

---

## Phase 2 — Ollama (the local LLM the weather agent talks to)

Install Ollama **inside WSL** and make it listen on all interfaces (pods inside the Kind
cluster reach it via the docker bridge, so `localhost`-only is not enough):

```bash
curl -fsSL https://ollama.com/install.sh | sh

sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0"\n' \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama

ollama pull qwen2.5:3b        # ~2 GB; the model the weather-agent manifest is configured for
```

**CHECKPOINT 2**
```bash
curl -s http://localhost:11434/api/tags | jq '.models[].name'   # must list "qwen2.5:3b"
ss -tlnp | grep 11434                                           # must show 0.0.0.0:11434 (not 127.0.0.1)
```

---

## Phase 3 — Clone and install Kagenti

Clone into the **WSL home directory**. Cloning under `/mnt/c/` fails with
`error: invalid path '.claude/skills/auth:keycloak-...'` — Windows filesystems can't hold the
`:` in some filenames.

```bash
cd ~
git clone https://github.com/kagenti/kagenti.git
cd kagenti
```

Run the installer — one non-interactive bash script. Minimal component set for this goal
(core platform + UI; deliberately **no** `--with-spire`, so the UI has no login screen, and
**no** `--with-all`, which would pull in the heavy mesh/observability stack):

```bash
scripts/kind/setup-kagenti.sh --with-ui
```

Notes for the agent:
- Takes 10–25 minutes (image pulls). It is **idempotent** — if it fails with a transient error
  (e.g. "exceeded its progress deadline"), simply re-run the same command.
- No secrets are needed: everything used here is public. If it asks nothing and copies
  `.secrets_template.yaml` by itself, that is expected.
- It creates a Kind cluster named `kagenti`, installs cert-manager, Gateway API, Istio gateway,
  Keycloak, the kagenti operator + webhook, backend and UI, and creates namespaces
  `team1`/`team2`. Host port **8080** is mapped into the cluster's gateway.

**CHECKPOINT 3**
```bash
kubectl get nodes                          # kagenti-control-plane Ready
kubectl get pods -A | grep -Ev 'Running|Completed'   # should output nothing (give it a few minutes)
curl -s -o /dev/null -w '%{http_code}\n' http://kagenti-ui.localtest.me:8080   # 200
```

---

## Phase 4 — Wire the cluster to Ollama, then deploy the weather example

### 4a. Create the `dockerhost` endpoint (required — the installer does NOT do this)

The example manifests point the agent's LLM at `http://dockerhost:11434/v1`. `dockerhost` is
**not a real DNS name**; a helper script creates a headless Service + EndpointSlice in `team1`
that resolves it to the docker-network gateway (= the WSL host, where Ollama listens):

```bash
cd ~/kagenti
.github/scripts/common/70-configure-dockerhost.sh
```

**CHECKPOINT 4a**
```bash
kubectl get svc,endpointslice -n team1 | grep dockerhost   # both exist
kubectl run curltest --rm -i --restart=Never -n team1 --image=curlimages/curl -- \
  curl -s http://dockerhost:11434/api/tags
# must return JSON listing qwen2.5:3b
```

### 4b. Deploy the weather tool, then the weather agent

Preferred route — the repo's own deploy scripts (they wait for readiness and apply a known
Service-port fix):

```bash
.github/scripts/kagenti-operator/72-deploy-weather-tool.sh
.github/scripts/kagenti-operator/74-deploy-weather-agent.sh
```

**Fallback route** (only if a script above fails on some CI assumption) — apply the manifests
directly and reproduce the scripts' known fix:

```bash
kubectl apply -f kagenti/examples/mcpservers/weather_tool.yaml
kubectl apply -f kagenti/examples/agents/weather_service_deployment.yaml
kubectl apply -f kagenti/examples/agents/weather_service_service.yaml

# Known upstream quirk: the Service is created with targetPort 8080 but the agent listens on 8000
kubectl patch svc weather-service -n team1 --type=json \
  -p '[{"op":"replace","path":"/spec/ports/0/targetPort","value":8000}]'
```

Notes:
- The agent pod may sit in `ContainerCreating` up to ~2 minutes while the operator creates a
  Keycloak client-credentials secret that the webhook mounts. This is normal — wait.
- Images are public prebuilt ones: `ghcr.io/kagenti/agent-examples/weather_service:latest` and
  `.../weather_tool:latest`. No GitHub token needed.

**CHECKPOINT 4b**
```bash
kubectl get pods -n team1        # weather-service and weather-tool pods Running, READY
kubectl get svc -n team1         # weather-service 8080/TCP, weather-tool-mcp 8000/TCP
kubectl exec -n team1 deploy/weather-service -- env | grep -E 'LLM_|MCP_URL'
# LLM_API_BASE must be http://dockerhost:11434/v1
# LLM_MODEL must be a model you pulled in Phase 2 (qwen2.5:3b) — if it names a different
#   model, run `ollama pull <that model>` on the host
# MCP_URL must point at weather-tool-mcp on port 8000 (if it says 9090, that is a known
#   upstream bug — patch the env to port 8000)
```

---

## Phase 5 — Verify end to end

### 5a. From the shell (A2A protocol, no UI involved)

```bash
kubectl port-forward -n team1 svc/weather-service 8000:8000 >/tmp/pf.log 2>&1 &
sleep 3

# Agent discovery card
curl -s http://localhost:8000/.well-known/agent-card.json | jq .name

# Actual question → the agent calls the LLM (Ollama) and the weather tool (Open-Meteo API)
curl -s -X POST http://localhost:8000/ -H 'Content-Type: application/json' -d '{
  "jsonrpc":"2.0","id":1,"method":"message/send",
  "params":{"message":{"messageId":"test-123","role":"user",
    "parts":[{"kind":"text","text":"What is the weather in New York?"}]}}}' | jq .

kill %1   # stop the port-forward
```
**Success =** a JSON-RPC result whose text talks about actual New York weather (temperature
etc.). First call can take 30–60 s (model load). Note: the tool calls the public Open-Meteo
API, so the machine needs internet access.

### 5b. [HUMAN] From the Windows browser

Open `http://kagenti-ui.localtest.me:8080` (works from Windows: `*.localtest.me` resolves to
127.0.0.1 and WSL2 forwards localhost ports). No login screen is expected in this install.
Go to **Agent Catalog → team1 → weather-service → Chat**, ask:

> What is the weather in New York?

If the browser can't reach it (corporate DNS/VPN blocking `localtest.me`), fallback:
`kubectl port-forward -n kagenti-system svc/http-istio 8080:80` and browse with a hosts-file
entry, or just rely on 5a.

**This is the finish line.** A chat answer means: UI → backend → A2A agent → Ollama (LLM) and
MCP weather tool → real weather API, all on the local Kagenti platform.

---

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `git clone` fails: `invalid path '.claude/skills/auth:keycloak...'` | Cloned under `/mnt/c/`. Clone under `~` in WSL. |
| `docker: permission denied` | Group not applied — open a new shell (or `newgrp docker`). |
| Installer: `exceeded its progress deadline` / transient helm error | Re-run the exact same installer command — it is idempotent. |
| `Init:ErrImagePull` / 403 from ghcr.io | Stale cached creds: `docker logout ghcr.io`; no token is needed, images are public. |
| Agent pod stuck `ContainerCreating` > 3 min | Waiting on `kagenti-keycloak-client-credentials*` secret in `team1` — check `kubectl get events -n team1`; if the operator pod is unhealthy, restart it. |
| Chat/curl returns 503 or `peer closed connection without sending complete message body` | The agent can't reach Ollama. Re-run CHECKPOINT 4a's in-cluster curl; confirm `OLLAMA_HOST=0.0.0.0` (Phase 2) and the `dockerhost` Service exists. |
| Agent answers but with an LLM error about the model | `LLM_MODEL` in the pod ≠ a pulled model. `ollama pull` the exact name from the pod env. |
| UI/Keycloak stop responding after a while | `kubectl rollout restart -n kagenti-system deploy/http-istio` (and `deploy/kagenti-ui`). |
| After a Windows reboot everything is gone | The Kind node container stopped. `docker start kagenti-control-plane`, wait ~2 min, re-check pods. Ollama restarts itself via systemd. |
| Keycloak wedged (crash-looping with DB errors) | Known issue: `helm uninstall keycloak -n keycloak`, re-run the installer, then restart `http-istio` and `kagenti-ui`. |
| Everything is hopeless | `scripts/kind/cleanup-kagenti.sh --destroy-cluster`, then redo Phase 3 — total rebuild is ~25 min. |

---

## What happens after this runbook (context, not tasks)

This install is **stock Kagenti with zero lineage capability** — that is intentional. The next
stages (separate runbooks, later) will be:
1. Deploy **your own app** (converted to the agent-examples shape: A2A agents + MCP tools,
   Python ASGI server, httpx/requests clients) the same way the weather example was deployed.
2. Attach the **lineage sidecar** (OTel shim + AuthBridge) so every HTTP exchange is recorded
   as facts-spans and derived into the Data-Governance interactions tables.
3. Build the guard against those tables.

Nothing in this runbook needs to be undone for those stages — they layer on top.
