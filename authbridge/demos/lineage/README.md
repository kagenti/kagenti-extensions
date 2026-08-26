# Lineage — per-request data lineage from the AuthBridge sidecar

Attach the AuthBridge envoy-sidecar to an agent or tool, switch on the
`lineage-telemetry` plugin, and every HTTP exchange the workload takes part in
becomes **two OTLP spans**: one when the request is seen, one when the response
stream ends. They are facts only — who called whom, over which protocol, with
what outcome — and they go to **any OTLP consumer**: the platform's own
collector, Jaeger, Phoenix, or your own endpoint.

> **Before anything else: the sidecar image must carry the plugin.** The
> `lineage-telemetry` plugin ships in its own pull request; until a release
> contains it, the published `ghcr.io/rossoctl/cortex/authbridge-envoy` image
> the scripts default to boots without it and logs
> `unknown plugin "lineage-telemetry"`. Build `authbridge-envoy` and
> `proxy-init` from this repo, load them into your cluster, and point
> `SIDECAR_IMAGE` / `PROXY_INIT_IMAGE` at your tags (see Prerequisites).

> **Where you will actually see them — on the rossoctl platform.** The
> platform's `rossoctl-deps` chart
> ([`charts/rossoctl-deps/values.yaml`](https://github.com/rossoctl/rossoctl/blob/main/charts/rossoctl-deps/values.yaml),
> `otel.collector`) runs an OTel collector (`deploy/otel-collector` in
> `rossoctl-system`) whose base `traces` pipeline exports to `debug` at
> `verbosity: detailed` — span attributes included — and does **not** install
> Phoenix (`components.phoenix.enabled` defaults to `false`). Spans arriving
> at the default endpoint are therefore printed to the collector's log and
> stored nowhere queryable:
>
> ```sh
> kubectl logs -n rossoctl-system deploy/otel-collector | grep lineage.exchange.id
> ```
>
> That is enough to prove the plugin works, and it is what the worked example
> below relies on. For a UI, either install the platform with Phoenix enabled
> (`--set components.phoenix.enabled=true`, which adds an `otlp/phoenix`
> exporter) or point `OTEL_ENDPOINT` straight at a sink of your own. Nothing in
> this demo depends on which you choose; on another platform, substitute its
> collector's OTLP/gRPC address.

The demo has two halves, and the second is the interesting one:

1. **The sidecar** captures. That part is just configuration — a plugin entry in
   the pipeline (`attach-lineage.sh` writes it for you).
2. **A propagate-only OTel shim** makes the capture *attributable*. Without it
   an agent's outbound calls carry no trace context and fragment out of their
   caller's trace. `Dockerfile.otel-shim` + `build-otel-shim.sh` layer it onto
   an app image without touching the app's source — or its command: the shim
   activates through ONE environment variable (`LINEAGE_PROPAGATE=1`), the
   same attach shape as the Java agent (`JAVA_TOOL_OPTIONS`) and Node
   (`NODE_OPTIONS`).

---

## What you get

Per HTTP exchange, two spans. The request span is named
`{self_id} {protocol} {operation}` and the response span appends ` response` —
so an inbound A2A call on a workload called `weather-lineage` produces
`weather-lineage a2a message/send` and `weather-lineage a2a message/send
response`. The pair is joined by `lineage.exchange.id` (the request span's own
id) and told apart by `lineage.role`:

| attribute | what it records |
|---|---|
| `lineage.exchange.id` | pairs the two spans of one exchange |
| `lineage.role` | `request` / `response` |
| `lineage.direction` | `inbound` / `outbound` |
| `lineage.self.id` | this workload's own stable id |
| `lineage.peer.host` | the other end of the hop |
| `lineage.protocol` | `a2a` / `mcp` / `inference` / `http` |
| `lineage.principal.sub`, `lineage.principal.client` | the caller's identity, when a validated token carried one — the generated pipeline runs no `jwt-validation`, so in this demo they never appear |
| `lineage.outcome`, `lineage.denied_by` | how the exchange ended |
| `lineage.parent.source` | how this hop was parented: `tracestate` or `wire` (below) |

**How a hop finds its parent.** Each sidecar stamps the id of the span it just
emitted into the request's `tracestate` header before forwarding; the next
sidecar parents on that stamp (`lineage.parent.source=tracestate`) and
re-stamps its own. When no stamp is present the hop is parented on whatever
`traceparent` the wire carried — which may be nothing — and records
`lineage.parent.source=wire`. The forwarded `traceparent` itself is never
modified, and nothing guesses: a hop without context is *visibly* unparented
rather than attached to a plausible caller.

With `capture_io: true` the request span also carries `input.value` and the
response span `output.value` — the parsed message content, so a trace viewer
shows the actual A2A message, MCP tool arguments or LLM prompt inline.

Nothing here interprets the traffic. The spans say what happened on the wire;
whatever consumes them decides what it means.

---

## Why the shim is needed (and what breaks without it)

The sidecar sits at the pod's network boundary. It sees every hop with parsed
bodies — but it lives **outside** the app's execution context and has no way to
know which internal coroutine issued which outbound call. To attribute an
outbound call to the inbound request that caused it, something must carry a
token **with the execution scope through the app**. That is precisely what the
W3C `traceparent` header is for, and only code running *inside* the request's
context can copy it from the inbound request onto the outbound ones.

When trace context is missing, the plugin does not guess: the exchange is
parented on the wire, records `lineage.parent.source=wire`, and renders as its
own trace entry instead of a child. The subtree beneath a non-propagating node
fragments out of its caller's trace — *visibly absent* lineage rather than
wrong lineage, but attribution is lost for everything downstream of that pod.
So:

> In-process `traceparent` propagation is mandatory, and the sidecar cannot do
> it from outside the app.

The shim supplies it with stock OpenTelemetry auto-instrumentation and
**exports nothing**:

- **server side** (`starlette` / `asgi` / `fastapi`) — extract the inbound
  `traceparent`, make it the active context.
- **client side** (`httpx` / `requests` / `aiohttp-client`) — inject
  `traceparent` on outbound calls.
- **`threading`** — carry the active context across `Thread.start` /
  `ThreadPoolExecutor.submit`. **Load-bearing**: frameworks that run the LLM
  call in a worker thread (anything using `loop.run_in_executor`) otherwise
  lose the context at the thread boundary and those legs silently fragment.
- **exporters pinned to `none`, and no `OTEL_EXPORTER_OTLP_ENDPOINT`** — the
  activation hook sets all three signal exporters to `none` as environment
  defaults the moment it wakes, so the shim's instrumentation generates spans
  that go nowhere. Your telemetry backend sees only what the sidecar emits.

Activation is a baked, env-gated site hook (`lineage-propagate-hook.py`,
installed into site-packages as a `.pth` + module pair): every Python process
of the image checks one variable at startup and either initializes stock
auto-instrumentation (`LINEAGE_PROPAGATE=1`, set by the generated Deployment)
or does nothing at all. The image's ENTRYPOINT/CMD is never rewritten, there
is no launcher process, and an unactivated `-otel` image behaves byte-for-byte
like its base — `build-otel-shim.sh` attests both halves of that claim on
every bake, before the image is ever loaded.

An app that configures its **own** exporter in code keeps exporting exactly as
before; the shim silences only its own auto-instrumentation (`setdefault`, so
an explicit `OTEL_*_EXPORTER` value in the image or the Deployment always
wins — the bake attestation accepts either). Nothing about the app's telemetry
changes.

---

## Which path — decide this before you run anything

**Two paths ship here, and choosing the wrong one destroys resources.** The
question is not what your app is; it is **who owns the Deployment**.

| | you own the Deployment | an operator, controller or UI owns it |
|---|---|---|
| **use** | `attach-lineage.sh` (`EMIT=manifest`) | `sidecar-patch.sh` |
| **what it does** | emits a complete ConfigMap + Service + Deployment and you apply it | strategic-merge patch that **adds** the sidecar containers to what is already there |
| **if you get it wrong** | **it replaces the existing Deployment.** Applying a generated manifest over an operator-managed workload overwrites the owner's spec — every field the owner set is gone | the patch is additive and safe, but its owner keeps owning the object (see below) |

`EMIT=manifest` is a *replacement*, by design: it is how you deploy an app under
lineage in one step. Point it at a name some other controller manages and you
have silently rewritten that controller's object.

Two honest limits on the patch path:

- **A patch is not durable.** The owner still owns the Deployment. Any
  platform-side rewrite — an operator reconcile, a chart upgrade, a UI redeploy —
  silently drops the sidecar. There is no error; lineage just stops. Re-run
  `sidecar-patch.sh` after any platform-side change.
- **Uninstrumented *and* operator-owned has exactly one lever: the image.**
  The owner's Deployment cannot carry the activation env — but if you can
  point the owner at a different image ref, `SELF_ACTIVATE=1
  build-otel-shim.sh` bakes the gate in and the image swap is the entire
  change. If you cannot even repoint the image, the corner stays unsolved: the
  sidecar still captures every hop, but nothing carries context through the
  app and its outbound exchanges fragment into their own traces.

One more route exists on the rossoctl platform, and it touches no Deployment
at all: when the platform injects its own AuthBridge sidecar (via an
`AgentRuntime` CR), lineage can be enabled for the whole namespace by adding the
three parsers + `lineage-telemetry` to both directions of the namespace's
`authbridge-runtime-config` ConfigMap (the operator-rendered one; this repo's
`aiac/k8s/opa-kind-*.sh` edits the same object). Leave `self_id` unset — each pod resolves
its own identity from the operator-mounted credential via `self_id_file`. The
propagation question is unchanged (the app still needs the shim or its own
instrumentation), and a platform upgrade re-renders the namespace ConfigMaps,
reverting the edit — re-apply it after any upgrade.

---

## The envelope

The shim is generic across the mainstream Python stack, not universal:

- **Python**, in any of the common layouts — a venv (declared via
  `$VIRTUAL_ENV` or at the usual paths) or a plain pip/system python;
  `build-otel-shim.sh` probes the image for the interpreter the app runs and
  refuses when it finds none (an explicit argument overrides the probe).
- Server is **ASGI / Starlette / FastAPI**; HTTP client is **httpx**,
  **requests** or **aiohttp**.
- The entry caller supplies the first `traceparent`. Nothing here seeds a root
  for an untraced entry point.
- **Not covered:** work handed to a `multiprocessing` child or a `subprocess`,
  and threads started at app boot rather than inside a request.
- The base image has a shell and coreutils (`Dockerfile.otel-shim` runs one
  `RUN` step to place the hook); a distroless base fails that step.

Outside the envelope, use the sidecar alone: every hop is still captured. Whether
those hops are *correctly attributed* depends entirely on the app propagating
`traceparent` itself — and that is a stronger condition than it sounds.

### Half-instrumented apps: the case that looks fine and is not

Propagation needs **two** halves: something that extracts the inbound
`traceparent` into an active context (`starlette` / `asgi` / `fastapi`), and
something that injects it on the way out (`httpx` / `requests` / `aiohttp`). An
app carrying only the client half can inject, but has nothing to inject *from*.

That app is in a corner this demo cannot get you out of:

- **The shim refuses it**, correctly — its client instrumentor is one this shim
  installs, so wrapping would stack a second one on the same library.
- **Sidecar-only does not save it** — with no server-side extraction, every
  outbound call starts a *fresh* trace.

Measured on exactly such an app: it served requests, called its LLM
successfully, and produced 26 outbound exchanges — **every one of them alone
in its own trace, none sharing a trace with the inbound that caused it.** No
correlation survives at all, and every per-trace consistency check stays green
while it happens.

**Check the shape, not just the count.** A concurrency check that only asks "is
each trace internally consistent?" will pass this app perfectly: a trace holding
one lonely inbound is clean by every structural measure. What exposes it is
expecting a *number of hops* per trace and finding one. If your app should make
three outbound calls per request, assert that three appear in the same trace —
otherwise a total attribution failure reads as a clean run.

If you own the image, the fix is to add the missing server-side instrumentor
(or activate stock auto-instrumentation, which brings both halves — that is
what the shim's hook does). If you do not, this is the same corner as the
operator-owned quadrant: solvable only if you can at least repoint the image.

**Baking is not purely additive.** The shim installs one pinned OpenTelemetry
contrib release (`OTEL_CONTRIB_VERSION` in `Dockerfile.otel-shim`), and the
resolver brings the SDK that release requires — on an image that already
carries an older SDK, the bake *upgrades* it (observed: `opentelemetry-sdk
1.42.1 → 1.44.0`, semconv `0.63b1 → 0.65b0`). Harmless for an app that does not
pin, but an app pinning an older SDK can be affected by being wrapped. Check
the build output if your app is sensitive to those versions.

`build-otel-shim.sh` refuses rather than guesses. It probes the image and stops
if it finds no runnable Python, or if any of the seven instrumentors the shim
installs is already present (wrapping would stack a second instrumentor on the
same library), or if the image already carries the shim's own hook. It does
**not** refuse on the mere presence of the `opentelemetry.instrumentation`
namespace or of a dormant SDK — see the worked example for why. `FORCE_BAKE=1`
overrides, which makes the human judgment explicit and greppable.

---

## Files

| file | what it is |
|---|---|
| `attach-lineage.sh` | **The one generator.** Emits every YAML byte: `EMIT=manifest` (ConfigMap + Service + Deployment), `EMIT=cm` (the plugin ConfigMap alone), `EMIT=patch` (sidecar pieces for an existing Deployment). Env-driven; writes to stdout; `NAME`/`NAMESPACE` are validated as Kubernetes names, ports as integers, and every free-form value is refused if it cannot be quoted safely into YAML. Generated containers carry resources (unless `APP_RESOURCES={}`), `seccompProfile: RuntimeDefault`, dropped capabilities (`proxy-init` adds back only `NET_ADMIN`/`NET_RAW`). |
| `Dockerfile.otel-shim` | The propagate-only shim layer. One recipe for every in-envelope app; only `BASE_IMAGE` changes. Instrumentors pinned to one contrib release. |
| `build-otel-shim.sh` | Builds the shim onto an app image, loads it into kind, and refuses images it cannot safely wrap. Detects the app's interpreter, uid and gid from the image itself (explicit arguments override; every probe runs the image with `--network=none`), then **attests the result before loading it**: gate off, nothing OTel-shaped may load; gate on, the SDK must come up and actually inject a `traceparent`. **The baked image is inert by default** — activation is the Deployment's `LINEAGE_PROPAGATE=1` env (`attach-lineage.sh` sets it), or `SELF_ACTIVATE=1` bakes it in for image-swap-only workloads. Deployed unactivated, it simply behaves like the base image: no propagation, honest fragmentation (`lineage.parent.source=wire`). |
| `lineage-propagate-hook.py` | The activation hook the Dockerfile bakes into site-packages (`.pth` + module). Checks one env var per interpreter start; when set, pins exporters off + `tracecontext,baggage` as env defaults and runs stock `initialize()`. Read its docstring for the full contract, including why a hook failure can never take the app down. |
| `sidecar-patch.sh` | The patch path — attaches the sidecar to a Deployment you do not own. Generates its YAML from `attach-lineage.sh`; refuses a target whose pod already binds a port the sidecar needs. |
| `container-runtime.sh` | Sourced helper: picks docker vs podman and loads images into kind either way. |

---

## Prerequisites

- A Kubernetes cluster with the platform installed, and the platform-rendered
  **`envoy-config` ConfigMap** present in your target namespace — the sidecar
  mounts it. `sidecar-patch.sh` checks for it; `attach-lineage.sh` only prints
  YAML, so on the manifest path a missing ConfigMap shows up as a pod stuck in
  `ContainerCreating` (see Troubleshooting).
- The sidecar images resolvable from the cluster. Defaults are the published
  ones:

  ```
  SIDECAR_IMAGE=ghcr.io/rossoctl/cortex/authbridge-envoy:latest
  PROXY_INIT_IMAGE=ghcr.io/rossoctl/cortex/proxy-init:latest
  ```

  > **Caveat.** A published image carries the `lineage-telemetry` plugin only
  > from the release that first contains it. Until then, build the two images
  > from this repo (`authbridge/cmd/authbridge-envoy/Dockerfile` and
  > `authbridge/proxy-init/Dockerfile.init`), load them into your cluster, and
  > point `SIDECAR_IMAGE` / `PROXY_INIT_IMAGE` at your local tags. With the
  > published image the sidecar logs
  > `initial pipeline build: inbound: unknown plugin "lineage-telemetry"` and
  > crash-loops — that is this caveat, not a broken manifest.

- An **OTLP/gRPC endpoint**. Default is
  `otel-collector.rossoctl-system.svc.cluster.local:4317`. Point `OTEL_ENDPOINT`
  anywhere else — Phoenix, Jaeger, your own collector. Nothing downstream of it
  is assumed.

---

## Worked example — the weather agent

Using the weather agent image this repo's weather demos deploy, measured as of
writing:

```console
$ podman inspect --format '{{json .Config.Cmd}}' ghcr.io/rossoctl/examples/weather_service:latest
["uv","run","--no-sync","server"]
```

It is a Python app with its venv at `/app/.venv`, running as UID 1001 — inside
the envelope. It also ships the **httpx** instrumentor already, which is one of
the seven this shim installs, so wrapping it would stack a second instrumentor
on the same library. The interlock says so and stops:

```console
$ ./build-otel-shim.sh ghcr.io/rossoctl/examples/weather_service:latest
REFUSING to bake ...: it already instruments httpx
```

Note what the check is asking: *would wrapping double-instrument?* Not "does this
image contain anything OpenTelemetry-shaped". The base
`opentelemetry-instrumentation` package arrives as a transitive dependency of
plenty of things and carries no library instrumentation at all — an app holding
only that still needs the shim, and refusing it would cost you propagation for
no reason.

**Read that refusal precisely: it means "do not wrap this", not "this one is
fine".** The probe detects that one of the shim's instrumentors is *installed*.
Whether the app actually *activates* it — and therefore whether it propagates
`traceparent` on its outbound calls — is not statically detectable, because
activation happens at runtime through `opentelemetry-instrument` or in the app's
own code. An image can carry the packages as dormant dependencies and propagate
nothing.

So the recipe for such an app is sidecar-only, with `NO_PROPAGATE=1` — the
image runs exactly as built (nothing is ever known or said about its command)
— and then **verify pairing before trusting it**. Drive several concurrent
requests, each with a distinct `traceparent`, and check that every outbound
hop lands in the trace of the inbound that caused it. Hops that don't are
provably un-propagated (`lineage.parent.source=wire`) and there is no way to
fix that from outside the process. That is the same corner as the
operator-owned quadrant, reached from a different direction.

```sh
cd authbridge/demos/lineage

NAME=weather-lineage \
IMAGE=ghcr.io/rossoctl/examples/weather_service:latest \
SELF_ID=weather-lineage \
APP_PORT=8000 SVC_PORT=8080 \
NO_PROPAGATE=1 \
NAMESPACE=team1 \
ENV_VARS="MCP_URL=http://weather-tool-advanced-mcp:8000/mcp LLM_API_BASE=${LLM_API_BASE} LLM_API_KEY=${LLM_API_KEY} LLM_MODEL=${LLM_MODEL}" \
OTEL_ENDPOINT=otel-collector.rossoctl-system.svc.cluster.local:4317 \
./attach-lineage.sh | kubectl apply -f -
```

`LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` are whatever OpenAI-compatible
endpoint the agent should talk to — the same values the
[weather-agent demo](../weather-agent/demo-ui-advanced.md) uses. (For an LLM
served on the kind host, Docker Desktop resolves `host.docker.internal` and
podman `host.containers.internal`.)

Read the manifest before you apply it — drop the `| kubectl apply -f -` and the
script just prints. Then drive one A2A request from **inside** the cluster (a
`kubectl port-forward` reaches the app on loopback and bypasses the sidecar's
inbound listener, so it captures nothing — and an agent-card fetch is bypassed
too, since `/.well-known/` is on the plugin's default `bypass_paths`):

```sh
kubectl run -n team1 --rm -i drive --image=curlimages/curl:8.11.1 --restart=Never -- \
  curl -s -X POST http://weather-lineage:8080/ \
    -H 'content-type: application/json' \
    -d '{"jsonrpc":"2.0","id":"1","method":"message/send","params":{"message":
         {"role":"user","messageId":"m1",
          "parts":[{"kind":"text","text":"what is the weather in Haifa?"}]}}}'
```

Two spans for that exchange now exist, in one trace. On the rossoctl platform
they land in the collector's `debug` exporter, so:

```sh
kubectl logs -n rossoctl-system deploy/otel-collector | grep -A2 lineage.exchange.id
```

This is the collector's output for the run above, cut to the lineage lines:

```
    Name           : weather-lineage a2a message/send
     -> lineage.role: Str(request)
     -> lineage.direction: Str(inbound)
     -> lineage.self.id: Str(weather-lineage)
     -> lineage.protocol: Str(a2a)
     -> lineage.parent.source: Str(wire)
     -> lineage.exchange.id: Str(e589944672fcf224)
    Name           : weather-lineage a2a message/send response
     -> lineage.role: Str(response)
     -> lineage.direction: Str(inbound)
     -> lineage.self.id: Str(weather-lineage)
     -> lineage.protocol: Str(a2a)
     -> lineage.exchange.id: Str(e589944672fcf224)
     -> lineage.outcome: Str(ok)
```

Two spans, one `lineage.exchange.id`. That pair is the whole claim: the
sidecar saw an exchange and recorded it as facts.

**The agent's own outbound hops are a separate matter.** The `MCP_URL` above
points at the weather tool from this repo's
[weather-agent advanced demo](../weather-agent/demo-ui-advanced.md) — if you have
not deployed it, the agent answers with a tool-connection error and you see only
the inbound pair, because a connection that never completes is not an exchange
to record. Deploy that demo's tool first and the same request adds a hop pair for
the MCP call and another for the LLM, linked to the inbound one through the
`traceparent` the app propagated.

For an app that does **not** instrument itself, the same flow gains one step —
bake the shim first, deploy the `-otel` image, and drop `NO_PROPAGATE=1`:

```sh
./build-otel-shim.sh <your-app>:latest        # -> <your-app>-otel:latest, attested, kind-loaded
NAME=... IMAGE=docker.io/library/<your-app>-otel:latest ... ./attach-lineage.sh | kubectl apply -f -
```

### The other path: adopt a Deployment you do not own

The target must be a Deployment the platform has **not** enrolled — no
`AgentRuntime` CR, no `<prefix>/inject: enabled` — because an enrolled workload
already carries an injected AuthBridge sidecar and the script refuses to add a
second one. It must also not listen on 9090, 15123 or 15124 itself. In this
repo, the [echo demo](../echo/README.md)'s `echo-upstream` is exactly such a
workload (plain HTTP on 8888, deliberately un-labelled):

```sh
DEPLOY=echo-upstream NAMESPACE=team1 ./sidecar-patch.sh
```

This adds the sidecar and leaves the app container exactly as its owner wrote
it; from then on every request the upstream serves is recorded as an inbound
pair under `lineage.self.id=echo-upstream`. `OUTBOUND_PORTS_EXCLUDE` keeps a
port out of the iptables redirect — use it for a port the app exports its *own*
telemetry on, so that keeps flowing untouched. Do **not** exclude LLM or tool
ports; those are the hops lineage exists to observe.

Re-read the two limits above before you rely on this path.

---

## Configuration

`attach-lineage.sh` writes the plugin entry into the generated ConfigMap. Three
keys matter:

```yaml
- name: lineage-telemetry
  config:
    otel_endpoint: "otel-collector.rossoctl-system.svc.cluster.local:4317"
    capture_io: true
    self_id: "weather-lineage"
```

| key | meaning |
|---|---|
| `otel_endpoint` | OTLP/gRPC endpoint as `host:port` (a URL scheme prefix is accepted). The default targets an in-cluster collector over plaintext. |
| `capture_io` | attach parsed request/response content as `input.value` / `output.value`. **Off by default** in the plugin; this demo turns it on so the worked example shows content — enable it only where the traffic carries no PII, or the backend enforces access control. |
| `self_id` | this workload's stable identity, emitted as `lineage.self.id`. Falls back to `self_id_file` (default `/shared/client-id.txt`, the operator-mounted credential). |

`bypass_paths` and `bypass_hosts` keep infrastructure noise out of the graph;
their defaults already cover agent-card discovery, health probes and the common
telemetry backends.

Script-level knobs (both paths): `NAME`, `IMAGE`, `SELF_ID`, `APP_PORT`,
`SVC_PORT`, `ENV_VARS`, `APP_RESOURCES`, `APP_COMMAND`, `NAMESPACE`,
`OTEL_ENDPOINT`, `OUTBOUND_PORTS_EXCLUDE`, `WORKLOAD_TYPE`,
`WORKLOAD_PROTOCOL`, `LABEL_PREFIX`, `SIDECAR_IMAGE`, `PROXY_INIT_IMAGE`,
`NO_PROPAGATE`, `NO_EMIT`, `EMIT`. Each script's header documents its own;
`NO_PROPAGATE=1 NO_EMIT=1` together is lineage fully off for one app, which
makes a clean A/B baseline.

`LABEL_PREFIX` (default `rossoctl.io`) sets the platform labels the generated
workload carries — `protocol.<prefix>/a2a`, and the `<prefix>/inject: disabled`
opt-out that keeps the platform from injecting a second sidecar next to the one
this manifest already brings. Retarget it for a differently-branded platform.

### Why the generated workload has no `<prefix>/type` label

Because the rossoctl platform will not let you set one, and it is right not to.

The platform installs a `ValidatingAdmissionPolicy` named
`agent-label-protection` (shipped by the platform operator, not by this repo —
`kubectl get validatingadmissionpolicy` shows it) that reserves
`<prefix>/type` for the operator, on `deployments` and `statefulsets`, with
`validationActions: [Deny]`. Apply a manifest carrying that label by hand and
admission rejects it, with a message saying the label can only be applied by
the operator via an `AgentRuntime` CR.

So this demo does not set it. Nothing is lost for lineage: the label is how the
platform *registers and classifies* a workload, not how this manifest attaches
its sidecar — the sidecar is in the manifest already, as an explicit
`proxy-init` initContainer and `envoy-proxy` container. Service and Deployment
selectors key on `app.kubernetes.io/name`, which is unique per workload, so
routing is unaffected.

What you give up is the workload appearing in the platform's agent inventory.
If you want that, the supported route is to create an **AgentRuntime CR**
targeting the workload and let the operator apply the label — which is exactly
what the policy's message tells you.

`WORKLOAD_TYPE` exists for platforms that do *not* guard the label: set it to
`agent` or `tool` and the label is emitted. It is empty by default, deliberately,
because the default has to work on a stock install.

---

## Limits

- **The sidecar sees plaintext HTTP only.** An HTTPS destination is TLS
  passthrough — no hop is recorded for it.
- **No producer-side payload size cap.** With `capture_io: true` a large message
  is attached whole.
- **Trace-context propagation is the app's job** — the shim does it for the
  in-envelope stack, and nothing else can do it from outside the process.
- **The patch path is not durable** and the uninstrumented + operator-owned
  quadrant is unsolved. Both are spelled out above; they are the two things most
  likely to surprise you in production.

---

## Troubleshooting

| symptom | cause |
|---|---|
| No spans at all | Wrong `OTEL_ENDPOINT`, or the sidecar image predates the plugin. Check the `envoy-proxy` container's logs. |
| Only inbound hops, never outbound | `proxy-init` did not install its iptables rules — check the init container's logs. |
| Outbound hops fragment into their own traces (`lineage.parent.source=wire`) | `traceparent` is not propagating. Check the app container carries `LINEAGE_PROPAGATE=1` (the generated Deployment sets it; an operator-owned Deployment needs the `SELF_ACTIVATE=1` image instead). If the app runs the LLM/tool call in a worker thread, the `threading` instrumentor is required — it is bundled in `Dockerfile.otel-shim`. If only the entry hop dangles, the caller is not sending a `traceparent` at all — expected at the trace edge. |
| Nothing captured when testing | You drove the app through `kubectl port-forward`. Loopback bypasses the inbound listener; drive it from inside the cluster. |
| `kind load` fails under podman | `container-runtime.sh` detects the runtime and saves + loads an image archive for podman, because `kind load docker-image` misbehaves under podman v5. Set `CONTAINER_TOOL` to force a runtime, `KIND_CLUSTER_NAME` for the cluster name. |
| Pod stuck `ImagePullBackOff` on the sidecar | `SIDECAR_IMAGE` / `PROXY_INIT_IMAGE` point at tags the cluster cannot resolve. Load them locally, or use the published refs. |
| Pod stuck `ContainerCreating` (`configmap "envoy-config" not found`) | The manifest path does not check for the platform-rendered `envoy-config` ConfigMap; the namespace is not one the platform set up. Use a platform namespace, or copy the ConfigMap in. |
| `envoy-proxy` restarts with `unknown plugin "lineage-telemetry"` | The sidecar image predates the plugin — the published image, until a release carries it. Build from this repo and set `SIDECAR_IMAGE`. Note the generated manifest uses `imagePullPolicy: IfNotPresent`, so once a release does carry the plugin a node that cached the older `:latest` keeps it until that image is removed or a versioned tag is used. |
| `sidecar-patch.sh` refuses: "already declares containerPort …" | The target is operator-enrolled (it has an injected sidecar) or the app itself listens on 9090/15123/15124. Use the namespace-ConfigMap route for enrolled workloads; a colliding app port cannot be adopted. |
