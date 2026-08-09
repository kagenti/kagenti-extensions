# Runbook — add lineage recording to a running Kagenti cluster (Windows/WSL2)

**Audience:** an AI coding agent executing on behalf of a human — written so a human can
also take over at any phase.
**Prerequisite:** the cluster built by [`CLUSTER-FROM-ZERO-windows.md`](CLUSTER-FROM-ZERO-windows.md)
is up and the weather agent answers in the Kagenti UI chat. Same environment assumed: WSL2
Ubuntu, **docker engine** (not podman, not Docker Desktop), Kind cluster `kagenti`, kagenti
installed with `--with-ui`, Ollama on the WSL host, weather agent + tool running in `team1`.

**What this runbook adds, in plain words:** a small recording proxy ("lineage sidecar") is
attached next to the weather agent and the weather tool. It watches every HTTP call they
make or receive — who called whom, with what request and what response — and sends those
facts to a new **Data-Governance (DG) service** we deploy, which assembles them into a tree
per conversation. At the end, you chat with the weather agent in your browser and then *see
that chat as a tree* (user → agent → LLM, user → agent → tool) at
`http://dg.localtest.me:8080/ui/traces`. No app code changes anywhere.

## The map

| Phase | Plain goal | ~Time |
|---|---|---|
| 0 | Get the two code repos | 2 min |
| 1 | Turn on the platform's telemetry collector (the "post office" the sidecar sends to) | 15 min |
| 2 | Build the 2 sidecar images | 10 min |
| 3 | Deploy the DG service (database + receiver + UI) and connect the collector to it | 20 min |
| 4 | Attach the recording sidecar to the weather agent + tool | 5 min |
| 5 | Make browser chats traceable (stamp the UI backend) | 10 min |
| 6 | Verify: see your chat as a tree; run the automated test | 10 min |

## Instructions to the AI agent (read first)

1. Execute phases in order. Each ends with a **CHECKPOINT** showing the expected result —
   do not continue until it matches. If a checkpoint fails twice, **stop and report** what
   you ran and what you saw.
2. Every command block is complete and copy-pasteable. When a block defines a shell
   variable, its use is in the **same block** — run the whole block as one unit.
3. Steps marked **[HUMAN]** need the human (browser). Ask, wait, then continue.
4. Work inside the WSL Ubuntu shell, under `~`, never `/mnt/c/`.
5. Do not improvise, and ignore these when you meet them in the repos:
   - anything mentioning **podman**, `host.containers.internal`, or
     `KIND_EXPERIMENTAL_PROVIDER` — that is the maintainer's macOS environment; the
     scripts auto-detect docker and do the right thing on this machine;
   - anything about auth, SPIRE, Keycloak token exchange, or `authproxy-routes` — the
     lineage sidecar runs a lineage-only pipeline, no auth involved;
   - `local-build-and-test.sh` at the extensions repo root — wrong image tags for this
     runbook;
   - docs mentioning model `qwen2.5:7b` — your weather agent keeps using the model it
     already has (`qwen2.5:3b`); no model change in this runbook.

---

## Phase 0 — Get the two repos

```bash
cd ~
git clone -b feat/two-span-lineage https://github.com/s-and-p-team/kagenti-extensions.git
git clone -b feat/interactions-sidecar-algorithm https://github.com/kagenti/lab-data-governance.git
```

**CHECKPOINT 0**
```bash
ls ~/kagenti-extensions/authbridge/demos/lineage-adapter/sidecar-patch.sh \
   ~/lab-data-governance/deploy/build-and-load.sh
```
You should see: both file paths printed, no "No such file" error.
If `sidecar-patch.sh` is missing, the branch on the server is stale — **stop and report**
(the maintainer must sync it; do not try other branches).

---

## Phase 1 — Enable the telemetry collector on the platform

**Goal in plain words:** the sidecar will mail its recordings to a central "post office"
(the OTel collector). Your minimal install doesn't have one — we re-run the installer with
one extra flag, plus a tiny override that enables the specific mail route ("phoenix
pipeline") that Phase 3 will tap. The installer is safe to re-run; it changes nothing else.

```bash
cat > ~/kagenti-deps-phoenix.yaml <<'EOF'
# Enables Phoenix so the otel-collector renders the traces/phoenix pipeline,
# which lab-data-governance's patch-kagenti-collector.sh taps for the DG receiver.
components:
  phoenix:
    enabled: true
EOF

cd ~/kagenti
scripts/kind/setup-kagenti.sh --with-ui --with-otel \
  --kagenti-deps-values ~/kagenti-deps-phoenix.yaml
```
Takes 10–20 minutes. If it fails with a transient error ("exceeded its progress
deadline"), re-run the same command — it is idempotent.

**CHECKPOINT 1**
```bash
kubectl get deploy -n kagenti-system otel-collector
kubectl get cm -n kagenti-system otel-collector-config -o yaml | grep -c "traces/phoenix"
curl -s -o /dev/null -w '%{http_code}\n' http://phoenix.localtest.me:8080
```
You should see: the deployment with `READY 1/1`; a number `1` or more (the pipeline
exists); and `200` (Phoenix UI answers).

---

## Phase 2 — Build the two sidecar images

**Goal in plain words:** the recorder is two containers — a tiny one that reroutes the
pod's network through the proxy (`proxy-init`), and the proxy itself with the recording
plugin (`authbridge-envoy`). Both are built from the extensions repo. The exact tags below
matter — the manifests expect them; don't use other build scripts from that repo.

```bash
cd ~/kagenti-extensions/authbridge
docker build -f cmd/authbridge-envoy/Dockerfile -t docker.io/library/authbridge-envoy:latest .
kind load docker-image docker.io/library/authbridge-envoy:latest --name kagenti

cd proxy-init
make docker-build-init KIND_CLUSTER_NAME=kagenti
make load-image KIND_CLUSTER_NAME=kagenti
```

**CHECKPOINT 2**
```bash
docker exec kagenti-control-plane crictl images | grep -E 'authbridge-envoy|proxy-init'
```
You should see: two lines, one per image.

---

## Phase 3 — Deploy the Data-Governance service

**Goal in plain words:** DG is where recordings become meaning — a Postgres database, a
receiver that accepts what the collector forwards, a processor that derives the
conversation trees, and the web UI you'll browse. Then we "patch the collector": add DG as
a second delivery address on the collector's existing route.

Host tools it needs first (one-time):
```bash
sudo apt-get install -y git-lfs python3-yaml
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

Build, deploy, patch:
```bash
cd ~/lab-data-governance
./deploy/build-and-load.sh
kubectl apply -f deploy/k8s/
kubectl wait -n data-governance --for=condition=available deploy --all --timeout=300s
./deploy/patch-kagenti-collector.sh
```
Notes:
- `build-and-load.sh` downloads ~500 MB of model files (git-lfs) on first run — be patient.
- First pod start runs database migrations — allow a minute.
- If the patch script says `pipeline 'traces/phoenix' not found` → Phase 1's override
  didn't take; redo Phase 1.

**CHECKPOINT 3**
```bash
kubectl get pods -n data-governance
kubectl get cm -n kagenti-system otel-collector-config -o yaml | grep -c data_governance
curl -s -o /dev/null -w '%{http_code}\n' http://dg.localtest.me:8080/ui/traces
```
You should see: all pods `Running` (postgres, receiver ×2, ui, interactions,
classification); a number `1` or more (the DG delivery address is in the collector); and
`200` (the DG UI answers).

---

## Phase 4 — Attach the recording sidecar to the weather pair

**Goal in plain words:** add the recorder to the two existing weather Deployments. The
script only *adds* containers next to the app — the app container is untouched, and the
interception is invisible to it. The weather apps are already OTel-instrumented, which is
why they need nothing else. (`OUTBOUND_PORTS_EXCLUDE=8335` = "don't record the app's own
telemetry mail — that's not conversation traffic".)

Run as ONE block (it discovers the tool's deployment name, prints it, then patches both):
```bash
cd ~/kagenti-extensions/authbridge/demos/lineage-adapter

kubectl get deploy -n team1    # for the human: the full list, for orientation

TOOL_DEPLOY=$(kubectl get deploy -n team1 -o name | grep -i weather | grep -v weather-service | cut -d/ -f2 | head -1)
echo "tool deployment discovered: '${TOOL_DEPLOY}'"
[ -n "$TOOL_DEPLOY" ] || { echo "STOP: no weather tool deployment found — report the deploy list above"; exit 1; }

DEPLOY="$TOOL_DEPLOY"        OUTBOUND_PORTS_EXCLUDE=8335 ./sidecar-patch.sh
DEPLOY=weather-service       OUTBOUND_PORTS_EXCLUDE=8335 ./sidecar-patch.sh
```
Each patch ends with `deployment "..." successfully rolled out` and a
`>> lineage sidecar attached ...` line.

**CHECKPOINT 4**
```bash
kubectl get pods -n team1
```
You should see: the weather pods now show `READY 2/2` (app + recorder) instead of `1/1`.
Then re-ask the weather question (UI chat or the curl from the previous runbook's
Phase 5a) — **the app must still answer normally**. If it doesn't:
`kubectl logs -n team1 deploy/weather-service -c envoy-proxy --tail=30` and report.

---

## Phase 5 — Make browser chats traceable (stamp the UI backend)

**Goal in plain words:** for one chat to appear as ONE tree, the very first caller (the
Kagenti UI's backend) must attach a "conversation ID" (a `traceparent` header) to its
calls. We rebuild its image with a wrapper that does exactly that — and sends nothing
anywhere itself — then point its Deployment at the new image.

Run as ONE block (it builds, discovers the backend's names, prints them, then swaps):
```bash
cd ~/kagenti-extensions/authbridge/demos/lineage-adapter
./stamp-ui-backend.sh

BACKEND_DEPLOY=$(kubectl -n kagenti-system get deploy -o name | grep -i backend | cut -d/ -f2 | head -1)
echo "backend deployment discovered: '${BACKEND_DEPLOY}'"
[ -n "$BACKEND_DEPLOY" ] || { echo "STOP: no backend deployment found in kagenti-system — report"; exit 1; }

echo "containers in it: $(kubectl -n kagenti-system get deploy "$BACKEND_DEPLOY" -o jsonpath='{.spec.template.spec.containers[*].name}')"
BACKEND_CONTAINER=$(kubectl -n kagenti-system get deploy "$BACKEND_DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].name}')

kubectl -n kagenti-system set image deploy/"$BACKEND_DEPLOY" \
  "$BACKEND_CONTAINER"=docker.io/library/backend-otel:latest
kubectl rollout status -n kagenti-system deploy/"$BACKEND_DEPLOY" --timeout=180s
```
(If the "containers in it" line lists MORE than one name, stop and report the list
instead of proceeding — the swap must target the app container.)

**CHECKPOINT 5**
```bash
kubectl -n kagenti-system get deploy -o wide | grep -i backend
curl -s -o /dev/null -w '%{http_code}\n' http://kagenti-ui.localtest.me:8080
```
You should see: the backend line showing image `docker.io/library/backend-otel:latest`,
and `200` (the UI still answers).

---

## Phase 6 — Verify end to end

### 6a. [HUMAN] One browser chat → one tree in DG

1. Open `http://kagenti-ui.localtest.me:8080`, chat with weather-service:
   *"What is the weather in Haifa now?"* — wait for the answer.
2. Open **`http://dg.localtest.me:8080/ui/traces`** — a new trace appears within ~30 s
   (refresh). Click it:
   - **Spans** tab: the recorded facts — request + response per HTTP exchange, paired by
     `lineage.exchange.id` (the apps' own telemetry spans appear too).
   - **Flow** tab: the tree — user → weather-service → LLM and → weather-tool, one
     connected tree with **one root**.

**Looks wrong but isn't** (do not debug these):
- Every trace in the list says **"missing parent"** — that's by design.
- Phoenix's "Root Spans" view shows nothing — use its **Traces** tab instead.
- The chat UI occasionally shows "No response from agent" while DG still recorded the
  full turn — known kagenti platform bug, not a lineage failure.

### 6b. The automated test (no browser; asserts against the DG database)

```bash
cd ~/kagenti-extensions/authbridge/demos/lineage-adapter
NAMESPACE=team1 SELF_ID=weather-service TARGET=weather-service.team1.svc.cluster.local:8080 \
  ./concurrency-test-interactions.sh
```
It fires 6 concurrent questions from inside the cluster, each with its own conversation
ID, and checks each derived tree in the DG database.
You should see, as the last line:
```
CLEAN FORESTS: 6/6   DISTINCT TRACES: 6/6
```
(each per-trace line above it ends `[OK]`). Anything less than 6/6 — report the full
output. Never drive this through `kubectl port-forward` — that path bypasses the sidecar.

**Finish line reached** when 6a shows the tree and 6b prints 6/6.

---

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `patch-kagenti-collector.sh`: `pipeline 'traces/phoenix' not found` | Phoenix override didn't take — redo Phase 1. |
| DG pods CrashLoopBackOff mentioning schema version | Rebuilt image vs old pod: `kubectl -n data-governance rollout restart deploy` (plain re-apply won't cycle `:latest` pods). |
| `build-and-load.sh` fails on `git lfs` or `uv` | Phase 3's host-tools block wasn't run (new shell lost PATH: `export PATH="$HOME/.local/bin:$PATH"`). |
| Weather pod stuck `Init:` / FailedMount on `csi.spiffe.io` | The operator's auto-injection fired on restart — **stop and report to the maintainer** (known platform behavior, has a label-based fix). |
| Weather answers but nothing appears in DG | Walk the chain: `kubectl logs -n team1 deploy/weather-service -c envoy-proxy --tail=30` (recorder emitting?) → `kubectl logs -n kagenti-system deploy/otel-collector --tail=30` (exporter errors?) → `kubectl logs -n data-governance deploy/data-governance-receiver --tail=30`. Most common: Phase 3's patch step skipped. |
| Weather behaves differently after the restart | Stock weather Deployments pull `:latest` on every restart — upstream may have changed the app. Not lineage-related. |
| App unreachable right after patching | `kubectl logs -n team1 <pod> -c proxy-init` — iptables errors; and confirm the envoy-proxy container runs as UID 1337 (it must). |
| Want it all off / on | `./lineage-switch.sh off` / `on` (DG stack + fleet apps; the weather pair keeps its sidecar either way — revert its Deployments to remove). |

---

## What happens after this runbook (context, not tasks)

Next stage is *your own app*. The recipe splits on one question — is your app already
OTel-instrumented?
- **No (typical):** it needs the propagate-only shim (so concurrent requests pair
  correctly) — the fleet path: a `fleet.conf` row + `deploy-fleet.sh`. See
  [`RUNBOOK.md`](RUNBOOK.md).
- **Yes (like weather):** `sidecar-patch.sh` on its Deployment is the whole job.

Either way the app must fit the envelope: Python ASGI server, httpx/requests/aiohttp for
all outbound calls, entry caller supplies a `traceparent` (the stamped backend does this
for browser chats), LLM over plain HTTP. Then the guard work begins: querying the
`interactions` tables DG derives from these recordings.
