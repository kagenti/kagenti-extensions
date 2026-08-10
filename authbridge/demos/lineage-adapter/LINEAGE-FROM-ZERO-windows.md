# Runbook — add lineage recording to a running Kagenti/Rossoctl cluster (Windows/WSL2)

**Audience:** an AI coding agent executing on behalf of a human — written so a human can
also take over at any phase.
**Prerequisite:** the cluster built by [`CLUSTER-FROM-ZERO-windows.md`](CLUSTER-FROM-ZERO-windows.md)
is up and the weather agent answers in the platform UI chat. Same environment assumed:
WSL2 Ubuntu, **docker engine** (not podman, not Docker Desktop), the platform's Kind
cluster, Ollama on the WSL host, weather agent + tool running in `team1`.

> **Naming note (important):** the upstream platform renamed itself from *kagenti* to
> *rossoctl*. Current installs (yours) use namespace **`rossoctl-system`**, installer
> **`setup-rossoctl.sh`**, and a Kind cluster named **`rossoctl`**. This runbook is written
> for that current naming. (On an older kagenti-branded cluster — namespace
> `kagenti-system` exists — substitute the old names; the scripts' defaults already
> match the old naming.)

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
| 0 | Get the two code repos (one needs access granted) | 5 min |
| 1 | Turn on the platform's telemetry collector (the "post office" the sidecar sends to) | 15 min |
| 2 | Build the 2 sidecar images | 10 min |
| 3 | Deploy the DG service (database + receiver + UI) and connect the collector to it | 20 min |
| 4 | Attach the recording sidecar to the weather agent + tool | 5 min |
| 5 | Make browser chats traceable (stamp the UI backend) | 10 min |
| 6 | Verify: see your chat as a tree; run the automated test | 10 min |

## Instructions to the AI agent (read first)

1. Execute phases in order. Each ends with a **CHECKPOINT** showing the expected result —
   do not continue until it matches. If a checkpoint fails twice, **stop and report** what
   you ran and what you saw. Do not adapt or rewrite commands on your own; report instead.
2. Every command block is complete and copy-pasteable. When a block defines a shell
   variable, its use is in the **same block** — run the whole block as one unit.
3. Steps marked **[HUMAN]** need the human (browser, access grants). Ask, wait, continue.
4. Work inside the WSL Ubuntu shell, under `~`, never `/mnt/c/`.
5. Ignore these when you meet them in the repos:
   - anything mentioning **podman**, `host.containers.internal`, or
     `KIND_EXPERIMENTAL_PROVIDER` — that is the maintainer's macOS environment; the
     scripts auto-detect docker and do the right thing on this machine;
   - anything about auth, SPIRE, Keycloak token exchange, or `authproxy-routes` — the
     lineage sidecar runs a lineage-only pipeline, no auth involved;
   - `local-build-and-test.sh` at the extensions repo root — wrong image tags for this
     runbook;
   - docs mentioning model `qwen2.5:7b` — the weather agent keeps the model it already
     has; no model change in this runbook.

---

## Phase 0 — Get the two repos

**[HUMAN] first:** the `lab-data-governance` repo is **private** — confirm with the
maintainer that access has been granted to your GitHub account, and authenticate git once
on this machine:
```bash
sudo apt-get install -y gh
gh auth login      # GitHub.com → HTTPS → login with a web browser
```

Then clone:
```bash
cd ~
git clone -b feat/two-span-lineage https://github.com/s-and-p-team/kagenti-extensions.git
git clone -b feat/interactions-sidecar-algorithm https://github.com/rossoctl/lab-data-governance.git
```

**CHECKPOINT 0**
```bash
ls ~/kagenti-extensions/authbridge/demos/lineage-adapter/sidecar-patch.sh \
   ~/lab-data-governance/deploy/build-and-load.sh
```
You should see: both file paths printed, no "No such file" error.
If the first is missing, the branch is stale; if the second clone failed with "repository
not found", access wasn't granted yet — **stop and report** either way.

---

## Phase 1 — Enable the telemetry collector on the platform

**Goal in plain words:** the sidecar will mail its recordings to a central "post office"
(the OTel collector). Your install doesn't have one — we re-run the installer with one
extra flag, plus a tiny override that enables the specific mail route ("phoenix
pipeline") that Phase 3 will tap. The installer is safe to re-run.

```bash
cat > ~/rossoctl-deps-phoenix.yaml <<'EOF'
# Enables Phoenix so the otel-collector renders the traces/phoenix pipeline,
# which lab-data-governance's patch-kagenti-collector.sh taps for the DG receiver.
components:
  phoenix:
    enabled: true
EOF

cd ~/kagenti
scripts/kind/setup-rossoctl.sh --with-ui --with-otel \
  --rossoctl-deps-values ~/rossoctl-deps-phoenix.yaml
```
Takes 10–20 minutes. If it fails with a transient error ("exceeded its progress
deadline"), re-run the same command — it is idempotent.

**CHECKPOINT 1**
```bash
kubectl get deploy -n rossoctl-system otel-collector
kubectl get cm -n rossoctl-system otel-collector-config -o yaml | grep -c "traces/phoenix"
curl -s -o /dev/null -w '%{http_code}\n' http://phoenix.localtest.me:8080
```
You should see: the deployment with `READY 1/1`; a number `1` or more (the pipeline
exists); and `200` (Phoenix UI answers).

---

## Phase 2 — Build the two sidecar images

**Goal in plain words:** the recorder is two containers — a tiny one that reroutes the
pod's network through the proxy (`proxy-init`), and the proxy itself with the recording
plugin (`authbridge-envoy`). The exact tags below matter — the manifests expect them.

Run as ONE block (it discovers the Kind cluster's name first):
```bash
KC=$(kind get clusters | head -1)
echo "kind cluster: '${KC}'"     # expect: rossoctl
[ -n "$KC" ] || { echo "STOP: no kind cluster found"; exit 1; }

cd ~/kagenti-extensions/authbridge
docker build -f cmd/authbridge-envoy/Dockerfile -t docker.io/library/authbridge-envoy:latest .
kind load docker-image docker.io/library/authbridge-envoy:latest --name "$KC"

cd proxy-init
make docker-build-init KIND_CLUSTER_NAME="$KC"
make load-image KIND_CLUSTER_NAME="$KC"
```

**CHECKPOINT 2**
```bash
docker exec "$(kind get clusters | head -1)-control-plane" crictl images | grep -E 'authbridge-envoy|proxy-init'
```
You should see: two lines, one per image.

---

## Phase 3 — Deploy the Data-Governance service

**Goal in plain words:** DG is where recordings become meaning — a Postgres database, a
receiver that accepts what the collector forwards, a processor that derives the
conversation trees, and the web UI you'll browse. Its manifests were written for the old
`kagenti-system` naming, so we rename on the fly while applying. Then we "patch the
collector": add DG as a second delivery address on the collector's existing route.

Host tools it needs first (one-time):
```bash
sudo apt-get install -y git-lfs python3-yaml
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

Build, deploy (with the namespace rename), patch — run as ONE block:
```bash
export PATH="$HOME/.local/bin:$PATH"
KC=$(kind get clusters | head -1)
cd ~/lab-data-governance

KIND_CLUSTER="$KC" ./deploy/build-and-load.sh

# Apply every manifest, renaming the old platform namespace to the current one
# (affects the UI route and the network policies; files without a match pass
# through unchanged):
for f in deploy/k8s/*.yaml; do
  sed 's/kagenti-system/rossoctl-system/g; s/^\( *- \)kagenti$/\1rossoctl/' "$f" | kubectl apply -f -
done

kubectl wait -n data-governance --for=condition=available deploy --all --timeout=300s

COLLECTOR_NAMESPACE=rossoctl-system ./deploy/patch-kagenti-collector.sh
```
Notes:
- `build-and-load.sh` downloads ~500 MB of model files (git-lfs) on first run — be patient.
- First pod start runs database migrations — allow a minute.
- If the patch script says `pipeline 'traces/phoenix' not found` → Phase 1's override
  didn't take; redo Phase 1.

**CHECKPOINT 3**
```bash
kubectl get pods -n data-governance
kubectl get cm -n rossoctl-system otel-collector-config -o yaml | grep -c data_governance
curl -s -o /dev/null -w '%{http_code}\n' http://dg.localtest.me:8080/ui/traces
```
You should see: all pods `Running` (postgres, receiver ×2, ui, interactions,
classification); a number `1` or more (the DG delivery address is in the collector); and
`200` (the DG UI answers).

---

## Phase 4 — Attach the recording sidecar to the weather pair

**Goal in plain words:** add the recorder to the two existing weather Deployments. The
script only *adds* containers next to the app — the app container is untouched. The
weather apps are already OTel-instrumented, which is why they need nothing else.
(`OUTBOUND_PORTS_EXCLUDE=8335` = "don't record the app's own telemetry mail";
`OTEL_ENDPOINT=...rossoctl-system...` = "mail recordings to the current platform's
collector" — the script's default points at the old naming.)

Run as ONE block:
```bash
cd ~/kagenti-extensions/authbridge/demos/lineage-adapter
OTEL_EP="otel-collector.rossoctl-system.svc.cluster.local:4317"

kubectl get deploy -n team1    # for the human: the full list, for orientation

TOOL_DEPLOY=$(kubectl get deploy -n team1 -o name | grep -i weather | grep -v weather-service | cut -d/ -f2 | head -1)
echo "tool deployment discovered: '${TOOL_DEPLOY}'"
[ -n "$TOOL_DEPLOY" ] || { echo "STOP: no weather tool deployment found — report the deploy list above"; exit 1; }

DEPLOY="$TOOL_DEPLOY"  OTEL_ENDPOINT="$OTEL_EP" OUTBOUND_PORTS_EXCLUDE=8335 ./sidecar-patch.sh
DEPLOY=weather-service OTEL_ENDPOINT="$OTEL_EP" OUTBOUND_PORTS_EXCLUDE=8335 ./sidecar-patch.sh
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
platform UI's backend) must attach a "conversation ID" (a `traceparent` header) to its
calls. We rebuild its image with a wrapper that does exactly that — and sends nothing
anywhere itself — then point its Deployment at the new image.

Run as ONE block (it discovers the backend's names and current image, then builds + swaps):
```bash
cd ~/kagenti-extensions/authbridge/demos/lineage-adapter
KC=$(kind get clusters | head -1)

BACKEND_DEPLOY=$(kubectl -n rossoctl-system get deploy -o name | grep -i backend | cut -d/ -f2 | head -1)
echo "backend deployment discovered: '${BACKEND_DEPLOY}'"    # expect: rossoctl-backend
[ -n "$BACKEND_DEPLOY" ] || { echo "STOP: no backend deployment found in rossoctl-system — report"; exit 1; }

echo "containers in it: $(kubectl -n rossoctl-system get deploy "$BACKEND_DEPLOY" -o jsonpath='{.spec.template.spec.containers[*].name}')"
BACKEND_CONTAINER=$(kubectl -n rossoctl-system get deploy "$BACKEND_DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].name}')
BASE_IMG=$(kubectl -n rossoctl-system get deploy "$BACKEND_DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "current backend image: '${BASE_IMG}'"

KIND_CLUSTER="$KC" ./stamp-ui-backend.sh "$BASE_IMG"

kubectl -n rossoctl-system set image deploy/"$BACKEND_DEPLOY" \
  "$BACKEND_CONTAINER"=docker.io/library/backend-otel:latest
kubectl rollout status -n rossoctl-system deploy/"$BACKEND_DEPLOY" --timeout=180s
```
(If the "containers in it" line lists MORE than one name, stop and report the list
instead of proceeding — the swap must target the app container. If the stamped image tag
printed by the script differs from `backend-otel:latest`, use the printed one in the
`set image` line.)

**CHECKPOINT 5**
```bash
kubectl -n rossoctl-system get deploy -o wide | grep -i backend
```
You should see: the backend line showing image `docker.io/library/backend-otel:latest`
and the pod cycling to Running. Then **[HUMAN]**: open the platform UI in the browser
(the same URL you've been using) — it must still load and chat must still work.

---

## Phase 6 — Verify end to end

### 6a. [HUMAN] One browser chat → one tree in DG

1. In the platform UI, chat with weather-service: *"What is the weather in Haifa now?"* —
   wait for the answer.
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
  full turn — known platform bug, not a lineage failure.

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
| Installer: `unknown option --kagenti-deps-values` | Old flag name — the platform renamed; use `--rossoctl-deps-values` (Phase 1 as written). |
| `patch-kagenti-collector.sh`: `pipeline 'traces/phoenix' not found` | Phoenix override didn't take — redo Phase 1. Also confirm `COLLECTOR_NAMESPACE=rossoctl-system` was set (without it the script looks in the old namespace). |
| `kind load` says cluster not found | Wrong cluster name — always use `$(kind get clusters | head -1)` as in the blocks. |
| DG pods CrashLoopBackOff mentioning schema version | Rebuilt image vs old pod: `kubectl -n data-governance rollout restart deploy`. |
| `build-and-load.sh` fails on `git lfs` or `uv` | Phase 3's host-tools block wasn't run in this shell (`export PATH="$HOME/.local/bin:$PATH"`). |
| `dg.localtest.me` gives 404 while pods are Running | The UI route landed in the wrong namespace — re-run Phase 3's sed-apply loop (it renames the route's namespace). |
| Weather pod stuck `Init:` / FailedMount on `csi.spiffe.io` | Operator auto-injection fired on restart — **stop and report to the maintainer**. |
| Weather answers but nothing appears in DG | Walk the chain: `kubectl logs -n team1 deploy/weather-service -c envoy-proxy --tail=30` (recorder emitting? look for export errors — a wrong `OTEL_ENDPOINT` shows here) → `kubectl logs -n rossoctl-system deploy/otel-collector --tail=30` → `kubectl logs -n data-governance deploy/data-governance-receiver --tail=30`. |
| Weather behaves differently after the restart | Stock weather Deployments pull `:latest` on every restart — upstream may have changed the app. Not lineage-related. |
| App unreachable right after patching | `kubectl logs -n team1 <pod> -c proxy-init` — iptables errors; the envoy-proxy container must run as UID 1337. |
| Want it all off / on | `./lineage-switch.sh off` / `on` (DG stack + fleet apps; the weather pair keeps its sidecar either way). |

---

## What happens after this runbook (context, not tasks)

Next stage is *your own app*. The recipe splits on one question — is your app already
OTel-instrumented?
- **No (typical):** it needs the propagate-only shim (so concurrent requests pair
  correctly) — the fleet path: a `fleet.conf` row + `deploy-fleet.sh`. See
  [`RUNBOOK.md`](RUNBOOK.md).
- **Yes (like weather):** `sidecar-patch.sh` on its Deployment is the whole job
  (remember `OTEL_ENDPOINT` as in Phase 4).

Either way the app must fit the envelope: Python ASGI server, httpx/requests/aiohttp for
all outbound calls, entry caller supplies a `traceparent` (the stamped backend does this
for browser chats), LLM over plain HTTP. Then the guard work begins: querying the
`interactions` tables DG derives from these recordings.
