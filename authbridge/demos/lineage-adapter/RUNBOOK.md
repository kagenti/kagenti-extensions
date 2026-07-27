# Runbook — adapt an arbitrary Kagenti app for correct lineage in N steps

*How to take any uninstrumented (or already-instrumented) Kagenti Python app and,
with minimum friction, get arielf's lineage sidecar to reconstruct a **correct
per-request execution forest** for it under concurrency — target 6/6 pairing.*

Proven across 7 apps (trivia, currency_converter, contact_extractor, git_issue,
slack_researcher→slack_tool, reservation_service→reservation_tool,
wiki_memory_tool). Full reasoning + per-scenario results: `DESIGN.md §10.5,
§10.10`. This runbook is the operational recipe; STUDY is the why.

---

## The idea in one paragraph

The lineage sidecar (AuthBridge, envoy-sidecar mode) sees every HTTP hop with
bodies, but it **cannot** correlate an outbound call to the inbound request that
caused it without a `traceparent` that travels *through the app* in-process
(under concurrency it otherwise collapses all outbound onto one inbound — 1/N).
The fix is a **deploy-time, propagate-only OpenTelemetry shim**: auto-instrument
the app's HTTP libraries to extract the inbound `traceparent` and inject it on
outbound calls, **exporting nothing** (so DG stays sidecar-only). Zero app-source
changes. The caller must supply the initial `traceparent` (DESIGN §10.9).

## Precondition (the envelope)

The app uses **a mainstream Python HTTP server** (ASGI / Starlette / FastAPI) and
**a mainstream client** (httpx / requests / aiohttp), and **the entry caller
supplies a `traceparent`**. Exceptions (out of envelope, documented): outbound via
a **CLI subprocess** (`claude_agent`) or a **local subprocess** such as git
(wiki's git hop) — not a Python HTTP client; and **non-Python** services
(`github_tool`, Go). Those legs carry no traceparent and are not covered.

## The four reusable artifacts (in this directory)

| Artifact | What it is |
|---|---|
| `Dockerfile.otel-shim` | ONE shim image, `--build-arg BASE_IMAGE=<app>`. Bundles `opentelemetry-distro` + instrumentors `{starlette,asgi,fastapi,httpx,requests,aiohttp-client,threading}`, propagate-only. Same recipe for every app. |
| `build-otel-shim.sh` | Builds the shim FROM an app image and kind-loads it. |
| `attach-lineage.sh` | Emits (stdout) a full Deployment+Service+lineage-ConfigMap: OTEL-wrapped app container + `proxy-init` initContainer + `envoy-proxy` sidecar. Per-app: `NAME`, `IMAGE`, `SELF_ID`, `APP_ENTRYPOINT`, `ENV_VARS`. |
| `concurrency-test-interactions.sh` (A2A) / `concurrency-test-mcp-interactions.sh` (MCP tool) | In-cluster driver: N concurrent turns, distinct caller-minted traceparent each; asserts per-trace **interaction forests** in the DG Postgres (+ optional `EXPECT_KINDS`). Target **N/N**. |

Prereqs already in the cluster (DESIGN §8.5): the sidecar images
`docker.io/library/authbridge-envoy:latest` and `proxy-init:latest`, the
platform `envoy-config` ConfigMap in `team1`, the DG pod fed by the patched
collector, and host Ollama (`qwen2.5:7b`) at `host.containers.internal:11434`.

---

## The recipe (per app)

### 1. Get the image into kind
Prebuilt (most agent-examples) — pull, retag under `docker.io/library/`, kind-load:
```bash
img="ghcr.io/kagenti/agent-examples/<name>:latest"
podman pull "$img"
podman tag "$img" "docker.io/library/<name>:latest"           # BRACE ${x}:latest in zsh!
podman save -o /tmp/<name>.tar "docker.io/library/<name>:latest"
KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/<name>.tar --name kagenti
```
No prebuilt image (e.g. wiki_memory_tool) — `podman build` it locally first, then
tag/save/load as above. If a Dockerfile does `uv sync --locked` with a missing
gitignored `uv.lock`, run `uv lock` first.

### 2. Build the propagate-only shim on top of it
```bash
./build-otel-shim.sh <name>:latest
# -> builds + kind-loads docker.io/library/<name>-otel:latest
```
Same command every app; only the base image differs.

### 3. Determine the app's own entrypoint (`APP_ENTRYPOINT`)
This is the app's command tokens that run *after* `opentelemetry-instrument`.
**Read the base image's Dockerfile `CMD`** and drop any leading `uv run --no-sync`:
- If the CMD ran a **console script** (defined in `[project.scripts]`, e.g. trivia's
  `server`): use it as-is → `APP_ENTRYPOINT='server'`.
- If the CMD ran `uv run . ` or `uv run <pkg>` with **no console script**
  (uv's package-run magic — currency_converter): `opentelemetry-instrument`
  cannot replicate it, so use the module form → **`APP_ENTRYPOINT='python -m <pkg> <args>'`**.
- If the CMD ran a **bare script** (`uv run x.py`): `APP_ENTRYPOINT='python x.py'`.
- If it ran **uvicorn** directly (FastAPI, wiki): `APP_ENTRYPOINT='uvicorn <mod>:app --host 0.0.0.0 --port 8000'`.

Verify quickly: `podman run --rm --entrypoint sh <name>:latest -c 'cd /app && opentelemetry-instrument --traces_exporter none <APP_ENTRYPOINT> --help'`.

### 4. Deploy (app + sidecar) with the attachment script
```bash
NAME=<k8s-name> \
IMAGE=docker.io/library/<name>-otel:latest \
APP_PORT=8000 SVC_PORT=8080 \
APP_ENTRYPOINT='<from step 3>' \
ENV_VARS='LLM_API_BASE=http://host.containers.internal:11434/v1 LLM_MODEL=qwen2.5:7b LLM_API_KEY=ollama' \
./attach-lineage.sh | kubectl apply -f -
kubectl rollout status deploy/<k8s-name> -n team1
```
- `SELF_ID` defaults to `NAME`; only this varies in the lineage config.
- `KAGENTI_TYPE=tool` for MCP tools; default `agent`.
- **LLM env** — set the app's own vars to point at plaintext Ollama (so the
  sidecar can read the LLM hop). Var names vary: usually `LLM_API_BASE/LLM_MODEL/
  LLM_API_KEY`; crewai uses `TASK_MODEL_ID` (+`openai/` prefix); ag2 uses bare
  model name; Marvin/pydantic-ai uses `OPENAI_BASE_URL`/`OPENAI_API_KEY`/
  `MARVIN_AGENT_MODEL`. Use any **non-placeholder** API key (`dummy` is rejected
  by some apps for non-localhost hosts).
- `OUTBOUND_PORTS_EXCLUDE=<port>` only if the app runs its OWN OTLP exporter you
  want to keep (rare — the shim already exports nothing; see "What happens to
  the app's own telemetry" below).
- For a **cross-service** chain, deploy the downstream tool FIRST (same recipe,
  `KAGENTI_TYPE=tool`), then point the agent's `MCP_URL` at its in-cluster
  Service (`http://<tool>.team1.svc.cluster.local:<port>/mcp`).

### 5. Verify with the interaction-level concurrency test
A2A-entry agent:
```bash
SELF_ID=<self_id> TARGET=<name>.team1.svc.cluster.local:8080 \
  PROMPT='<a prompt that embeds {TOKEN}>' \
  ./concurrency-test-interactions.sh
```
MCP-entry tool (streamable-http tools/call):
```bash
SELF_ID=<self_id> \
  MCP_URL=http://<name>.team1.svc.cluster.local:8000/mcp TOOL=<tool_name> \
  DRIVER_IMAGE=docker.io/library/<name>-otel:latest \
  ./concurrency-test-mcp-interactions.sh
```
Expect **6/6 clean**. (Drive from IN-cluster only — `kubectl port-forward` uses
pod-loopback, which the iptables rules exclude, so it bypasses the sidecar inbound
listener — DESIGN §10.7.)

For a per-app **validation run**, fill an expectation card BEFORE firing:
copy `validation/TEMPLATE-expectation-card.md`, state the expected entities,
per-turn interaction kinds/counts (including the lifecycle/discovery
interactions the DG UI hides by default) and the TLS legs expected to produce
NO interaction, then derive its `EXPECT_KINDS='kind=count,…'` line and pass it
to either harness to also pin per-trace payload content_kind counts; record
the outcome in the card's Results section. (The one-span-era
`concurrency-test.sh` / `concurrency-test-mcp.sh` are retired — their
assertions grep span attributes the two-span plugin no longer emits.)

---

## What happens to the app's own telemetry

Nothing — by design (do no harm). `--traces_exporter none` silences only the
shim's own auto-instrumentation: the shim never sets
`OTEL_EXPORTER_OTLP_ENDPOINT`, so the SDK it bootstraps has nowhere to send —
while an app that configures its own exporter in code keeps exporting. Kagenti
apps gate that exporter on `OTEL_EXPORTER_OTLP_ENDPOINT` (the platform injects
it into every deployed agent via the backend's `DEFAULT_ENV_VARS`) —
weather-service under the sidecar still ships its 78 app spans per trace, live
proof.

To **keep** an app's own export when deploying with `attach-lineage.sh`, add
both of:
```bash
ENV_VARS='... OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector>:<port>' \
OUTBOUND_PORTS_EXCLUDE=<export port> \
```
— the endpoint re-supplies what the platform would have injected, and the port
exclusion routes export traffic past the proxy so it flows untouched (and isn't
observed as a lineage hop). Live pattern: weather-service excludes its export
port `8335` (`lineage-sidecar/weather-service-sidecar-patch.yaml` in the
umbrella workspace). The shim itself stays propagation-only — no exporter
packages, no export modes.

## Troubleshooting (all seen in practice — DESIGN §10.10)

| Symptom | Cause → fix |
|---|---|
| Pairing collapses to **1/N** (all outbound under one inbound) | Traceparent not propagating. If the framework runs the LLM/tool call in a **worker thread** (ag2/autogen `run_in_executor`, any ThreadPoolExecutor), the `-threading` instrumentor is required — it's already bundled in `Dockerfile.otel-shim`. If still 1/N, the caller isn't supplying a `traceparent` (the test always does). |
| Pod `CrashLoopBackOff`: `Failed to initialize cache … Permission denied` | Base image ships a non-writable uv cache. Already handled by the attach's `UV_NO_CACHE=1`+`HOME=/tmp`. |
| Pod error: `opentelemetry-instrument: <cmd>: not found` | `APP_ENTRYPOINT` used a name that isn't a real executable. Use `python -m <pkg>` (step 3). |
| Inbound captured but **zero outbound LLM hops** | The app's LLM call is failing *before* the HTTP request (e.g. an upstream framework/version bug). Check `kubectl logs … -c agent`; fix the app's deps (a per-app image overlay), not the shim. (Contact_extractor: pin `pydantic-ai==1.20.0`.) |
| Framework rejects the Ollama model name (strict allowlist) | Alias the model in Ollama to an accepted name: `ollama cp qwen2.5:7b <allowed-name>`, then point the app at it. (Marvin.) |
| Tool pod won't start | Some tools require env to boot (slack_tool: `SLACK_BOT_TOKEN`; wiki: `JWT_SECRET_KEY`). Pass a (possibly fake) value. |
| DG shows a duplicate `agent:(,authbridge)` entity / app spans polluting | Not an issue with this method: `--traces_exporter none` means the app exports nothing, so DG is sidecar-only (verified on the already-instrumented #5). Don't set `OTEL_EXPORTER_OTLP_ENDPOINT` — unless deliberately keeping app-owned export (see above). |

## Environment gotchas (macOS/podman/zsh)

- **zsh `${x}:latest`** — always brace; bare `$x:latest` silently applies the `:l`
  (lowercase) modifier and corrupts the tag.
- Build/load via `podman build` + `kind load image-archive` (docker daemon is off;
  `kind load docker-image` misbehaves under podman) — `build-otel-shim.sh` does this.
- Kind + podman: `KIND_EXPERIMENTAL_PROVIDER=podman` on every `kind` call.
- kubectl-run driver pods need `--image-pull-policy=IfNotPresent` (kind-loaded
  images aren't in a registry) — the test scripts set this.
