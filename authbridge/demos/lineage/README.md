# Lineage — per-request data lineage from the AuthBridge sidecar

Attach the AuthBridge envoy-sidecar to a workload that is **already deployed**,
switch on the `lineage-telemetry` plugin, and every HTTP exchange the workload
takes part in becomes **two OTLP spans**: one when the request is seen, one
when the response stream ends. They are facts only — who called whom, over
which protocol, with what outcome — and they go to **any OTLP consumer**: the
platform's own collector, Jaeger, Phoenix, or your own endpoint.

This demo deliberately does **not** deploy applications. Your app is deployed
however its owner deploys apps — the platform UI, your own manifests, someone
else's pipeline. Everything here is additive: a strategic-merge patch and a
ConfigMap that attach lineage to what already runs, plus an image shim that
makes the capture attributable. Nothing that is not lineage is touched.

> **Before anything else: the sidecar image must carry the plugin.** The
> `lineage-telemetry` plugin ships in its own pull request; until a release
> contains it, the published `ghcr.io/rossoctl/cortex/authbridge-envoy` image
> the scripts default to boots without it and logs
> `unknown plugin "lineage-telemetry"`. Build `authbridge-envoy` and
> `proxy-init` from this repo, load them into your cluster, and point
> `SIDECAR_IMAGE` / `PROXY_INIT_IMAGE` at your tags (see Prerequisites).

> **Where you will actually see the spans — on the rossoctl platform.** The
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
   the pipeline (`attach-lineage.sh` writes it for you) plus the sidecar patch.
2. **A propagate-only OTel shim** makes the capture *attributable*. Without it
   an agent's outbound calls carry no trace context and fragment out of their
   caller's trace. `Dockerfile.otel-shim` + `build-otel-shim.sh` layer it onto
   an app image without touching the app's source — or its command: the shim
   activates through ONE environment variable (`LINEAGE_PROPAGATE=1`), the
   same attach shape as the Java agent (`JAVA_TOOL_OPTIONS`) and Node
   (`NODE_OPTIONS`). The patch can flip that variable on the app's container
   for you (`APP_CONTAINER`, below).

---

## What you get

Per HTTP exchange, two spans. The request span is named
`{self_id} {protocol} {operation}` and the response span appends ` response` —
so an inbound A2A call on a workload called `echo-upstream` produces
`echo-upstream a2a message/send` and `echo-upstream a2a message/send
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
- **client side** (`httpx` / `requests` / `aiohttp-client` / `urllib3`) — inject
  `traceparent` on outbound calls. `urllib3` is what covers `boto3`/`botocore`:
  an S3 read from a tool otherwise escapes into a trace of its own.
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
auto-instrumentation (`LINEAGE_PROPAGATE=1`) or does nothing at all. The
image's ENTRYPOINT/CMD is never rewritten, there is no launcher process, and
an unactivated `-otel` image behaves byte-for-byte like its base —
`build-otel-shim.sh` attests both halves of that claim on every bake, before
the image is ever loaded.

An app that already configures OpenTelemetry **itself** is outside the shim's
envelope, and the bake interlock cannot see that (it probes for instrumentors,
not for SDK use). The hook's `initialize()` installs the global
`TracerProvider` first, so a provider the app sets in code is rejected
(`Overriding of current TracerProvider is not allowed`) and the app's own
exporter goes silent. An `OTEL_*_EXPORTER` the app sets in its environment
wins over the hook's `none` (`setdefault`), so the shim's own server and client
spans then flow to the app's backend — and `otlp` needs an exporter package
the shim does not install, in which case the hook fails at startup and
propagation stays off. For such an app attach capture only (no
`APP_CONTAINER`) and let its own instrumentation carry `traceparent`.

---

## How to attach

The target is a Deployment that already exists. `attach-lineage.sh` generates
exactly two things — the per-app plugin ConfigMap (`EMIT=cm`) and a
strategic-merge patch with the sidecar pieces (`EMIT=patch`, the default) —
and there are two ways to consume them:

### Adopt a live Deployment

```sh
DEPLOY=<deployment> [APP_CONTAINER=<container>] ./sidecar-patch.sh
```

`sidecar-patch.sh` checks the preconditions (the Deployment exists, the
platform-rendered `envoy-config` ConfigMap is in the namespace, no port
collision, `APP_CONTAINER` — if given — names a real container), applies the
ConfigMap, patches the Deployment, and waits for the rollout. The patch only
*adds*: lists merge by name, so everything the owner wrote stays as written.

Two honest limits:

- **The target must not already carry an AuthBridge sidecar.** A workload the
  platform enrolled (an `AgentRuntime` CR / `<prefix>/inject: enabled`) has an
  injected one, also named `envoy-proxy` — a strategic merge would silently
  merge into it and re-point it at this ConfigMap, so the script refuses (it
  detects the sidecar's ports). For enrolled workloads, see the
  namespace-ConfigMap route below.
- **A patch is not durable.** The owner still owns the Deployment. Any
  platform-side rewrite — an operator reconcile, a chart upgrade, a UI
  redeploy — silently drops the sidecar. There is no error; lineage just
  stops. Re-run `sidecar-patch.sh` after any platform-side change, or use the
  manifests route so the attachment lives with the source.

To back out — after a failed rollout, or at will — `kubectl rollout undo
deploy/<name>` returns the owner's spec (the patch is one revision), then
`kubectl delete cm authbridge-lineage-config-<name>`.

### Bring your own manifests

If you own the app's manifests, make lineage part of them instead of patching
live — the attachment then survives every re-deploy:

```sh
NAME=my-agent EMIT=cm    ./attach-lineage.sh > lineage-cm.yaml
NAME=my-agent EMIT=patch APP_CONTAINER=agent \
  APP_IMAGE=docker.io/library/my-agent-otel:latest \
  ./attach-lineage.sh > lineage-patch.yaml
```

and in your `kustomization.yaml`:

```yaml
resources:
  - deployment.yaml        # yours, untouched
  - service.yaml           # yours, untouched
  - lineage-cm.yaml        # generated
patches:
  - path: lineage-patch.yaml
    target: { kind: Deployment, name: my-agent }
```

`kubectl apply -k .` deploys your app exactly as you wrote it, with lineage
attached as a version-controlled, reviewable layer.

### The propagation half

Both routes above attach **capture**. For *attribution* (outbound hops landing
in the trace of the inbound that caused them) the app must propagate
`traceparent` — its own instrumentation, or the shim:

```sh
./build-otel-shim.sh <your-app>:latest    # -> <your-app>-otel:latest, attested, kind-loaded
```

then hand the patch the container to switch on:
`APP_CONTAINER=<container-name>` adds `LINEAGE_PROPAGATE=1` to that
container's env (merged by name — nothing else in the container changes) and
`APP_IMAGE=docker.io/library/<your-app>-otel:latest` points it at the baked
image. Without `APP_CONTAINER` the app container is not touched at all.

The `-otel` image must be resolvable exactly the way the base image is: the
patch swaps `image` and leaves the owner's `imagePullPolicy` alone. A
kind-loaded image satisfies `IfNotPresent` (what the platform's own Deployments
carry); a Deployment that pulls its base from a registry — or a `:latest` one
whose policy defaulted to `Always` — needs the `-otel` image pushed to that
registry and `APP_IMAGE` pointed at it.

**`APP_CONTAINER` must name a container that exists.** A strategic merge
*adds* a stub container for an unknown name rather than failing;
`sidecar-patch.sh` verifies the name against the live Deployment before
applying, but on the manifests route the check is yours.

One corner case: a Deployment whose env you cannot control at all
(operator-owned, and the operator wipes your patch) has exactly one lever —
the image reference. `SELF_ACTIVATE=1 build-otel-shim.sh` bakes
`LINEAGE_PROPAGATE=1` *into* the image, and pointing the owner at the `-otel`
tag is then the entire change.

### Enrolled workloads: the namespace-ConfigMap route

One more route exists on the rossoctl platform, and it touches no Deployment
at all: when the platform injects its own AuthBridge sidecar (via an
`AgentRuntime` CR), lineage can be enabled for the whole namespace by adding the
three parsers + `lineage-telemetry` to both directions of the namespace's
`authbridge-runtime-config` ConfigMap (the operator-rendered one). Leave
`self_id` unset — each pod resolves its own identity from the operator-mounted
credential via `self_id_file`. The propagation question is unchanged (the app
still needs the shim or its own instrumentation), and a platform upgrade
re-renders the namespace ConfigMaps, reverting the edit — re-apply it after
any upgrade.

---

## What it costs, per workload

Deploying an app is your work and stays your work: an image, a Deployment, a
Service, whatever wiring the app needs. Attaching lineage adds **two commands
per workload and no YAML**:

| step | command | per | what it detects or generates for you |
|---|---|---|---|
| bake the shim | `./build-otel-shim.sh <image>` | image | interpreter, uid:gid, the interlock, the attestation, the kind load |
| attach | `DEPLOY=<name> APP_CONTAINER=<container> APP_IMAGE=docker.io/library/<image>-otel:latest ./sidecar-patch.sh` | Deployment | the ConfigMap, the patch, the four preconditions, the rollout wait |

Capture only (an app that already propagates, or one the bake refuses) is one
command: the attach without `APP_CONTAINER`. Two Deployments that run the same
image bake once and attach twice — the image is the shim's unit, the
Deployment is the sidecar's.

For a fleet of ten, that is two loops:

```sh
for a in agent-a agent-b tool-x tool-y …; do ./build-otel-shim.sh "$a:latest"; done
for a in agent-a agent-b tool-x tool-y …; do
  DEPLOY=$a APP_CONTAINER=app APP_IMAGE=docker.io/library/$a-otel:latest ./sidecar-patch.sh
done
```

(naming every app container the same — `app` — is what makes the second loop
a loop). Then check the *shape* of one end-to-end trace, not just that spans
arrived: one root per user turn, and `lineage.parent.source=wire` only at the
entry edge — a workload whose shim did not activate shows up as extra roots,
not as missing spans (see "The envelope").

## The envelope

The shim is generic across the mainstream Python stack, not universal:

- **Python**, in any of the common layouts — a venv (declared via
  `$VIRTUAL_ENV` or at the usual paths) or a plain pip/system python;
  `build-otel-shim.sh` probes the image for the interpreter the app runs and
  refuses when it finds none (an explicit argument overrides the probe).
- Server is **ASGI / Starlette / FastAPI**; HTTP client is **httpx**,
  **requests**, **aiohttp** or **urllib3** (which is how `boto3` talks).
- The entry caller supplies the first `traceparent`. Nothing here seeds a root
  for an untraced entry point.
- **Not covered:** work handed to a `multiprocessing` child or a `subprocess`,
  threads started at app boot rather than inside a request, and an app that
  configures the OpenTelemetry SDK itself — in code or through `OTEL_*`
  environment variables (see "Why the shim is needed").
- The base image has a shell and coreutils (`Dockerfile.otel-shim` runs one
  `RUN` step to place the hook); a distroless base fails that step.

Outside the envelope, attach capture only (no `APP_CONTAINER`): every hop is
still recorded. Whether those hops are *correctly attributed* depends entirely
on the app propagating `traceparent` itself — and that is a stronger condition
than it sounds.

### Half-instrumented apps: the case that looks fine and is not

Propagation needs **two** halves: something that extracts the inbound
`traceparent` into an active context (`starlette` / `asgi` / `fastapi`), and
something that injects it on the way out (`httpx` / `requests` / `aiohttp` /
`urllib3`). An
app carrying only the client half can inject, but has nothing to inject *from*.

That app is in a corner this demo cannot get you out of:

- **The shim refuses it**, correctly — its client instrumentor is one this shim
  installs, so wrapping would stack a second one on the same library.
- **Capture-only does not save it** — with no server-side extraction, every
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
what the shim's hook does). If you do not, the image reference is the only
lever left (`SELF_ACTIVATE=1`, above) — and only if the shim's interlock lets
the image through.

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
namespace or of a dormant SDK — an image can carry those as transitive
dependencies and still need the shim. `FORCE_BAKE=1` overrides, which makes
the human judgment explicit and greppable.

---

## Files

Two moments, seven files. The bake happens once per image, on a laptop; the
attach happens once per Deployment, against the cluster. The only thing that
crosses from one to the other is an image reference.

```
BAKE — once per app image (laptop)
  build-otel-shim.sh <app>:latest
    ├─ sources container-runtime.sh        podman-or-docker, kind load
    ├─ builds  Dockerfile.otel-shim        which installs lineage-propagate-hook.py
    └─ output  <app>-otel:latest           inert until LINEAGE_PROPAGATE=1

ATTACH — once per Deployment (cluster)
  sidecar-patch.sh  DEPLOY=<name> [APP_CONTAINER=<container> APP_IMAGE=docker.io/library/<app>-otel:latest]
    ├─ checks   Deployment exists · envoy-config ConfigMap present ·
    │           no 15123/15124/9090 declared · APP_CONTAINER is a real container
    ├─ runs     attach-lineage.sh EMIT=cm     → kubectl apply        (the sidecar's config)
    ├─ runs     attach-lineage.sh EMIT=patch  → kubectl patch deploy (proxy-init, envoy-proxy,
    │                                            volumes, + the switch on APP_CONTAINER)
    └─ waits    kubectl rollout status

  or, without sidecar-patch.sh — bring your own manifests:
    run attach-lineage.sh twice yourself, commit both files,
    list the cm under resources: and the patch under patches:, kubectl apply -k
```

| file | what it is |
|---|---|
| `attach-lineage.sh` | **The one generator.** Emits every YAML byte of the attachment: `EMIT=patch` (default — the sidecar pieces as a strategic-merge patch, plus the optional `APP_CONTAINER` propagation switch), `EMIT=cm` (the plugin ConfigMap). Env-driven; writes to stdout; never touches the cluster. Every input is validated or refused — including the knobs of the removed app-deployment mode, which fail loudly rather than being ignored. |
| `sidecar-patch.sh` | The live applier: precondition checks (Deployment exists, `envoy-config` present, no port collision, `APP_CONTAINER` names a real container), then ConfigMap + patch + rollout wait. Owns no YAML — it calls the generator twice. |
| `Dockerfile.otel-shim` | The propagate-only shim layer. One recipe for every in-envelope app; only `BASE_IMAGE` changes. Instrumentors pinned to one contrib release. |
| `build-otel-shim.sh` | Builds the shim onto an app image, loads it into kind, and refuses images it cannot safely wrap. Detects the app's interpreter, uid and gid from the image itself (explicit arguments override; every probe runs the image with `--network=none`), then **attests the result before loading it**: gate off, nothing OTel-shaped may load; gate on, the SDK must come up and actually inject a `traceparent`. **The baked image is inert by default** — activation is the `LINEAGE_PROPAGATE=1` env (the patch's `APP_CONTAINER` sets it), or `SELF_ACTIVATE=1` bakes it in for image-swap-only workloads. Deployed unactivated, it simply behaves like the base image: no propagation, honest fragmentation (`lineage.parent.source=wire`). |
| `lineage-propagate-hook.py` | The activation hook the Dockerfile bakes into site-packages (`.pth` + module). Checks one env var per interpreter start; when set, pins exporters off + `tracecontext,baggage` as env defaults and runs stock `initialize()`. Read its docstring for the full contract, including why a hook failure can never take the app down. |
| `container-runtime.sh` | Sourced helper: picks docker vs podman and loads images into kind either way. |

---

## Prerequisites

- A Kubernetes cluster with the platform installed, and the platform-rendered
  **`envoy-config` ConfigMap** present in your target namespace — the sidecar
  mounts it. `sidecar-patch.sh` checks for it; on the manifests route a missing
  ConfigMap shows up as a pod stuck in `ContainerCreating` (see
  Troubleshooting).
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

## Worked example — adopt the echo upstream

The target must be a Deployment the platform has **not** enrolled — no
`AgentRuntime` CR, no `<prefix>/inject: enabled` — because an enrolled workload
already carries an injected AuthBridge sidecar and the script refuses to add a
second one. It must also not listen on 9090, 15123 or 15124 itself. In this
repo, the [echo demo](../echo/README.md)'s `echo-upstream` is exactly such a
workload (plain HTTP on its own port, deliberately un-labelled):

```sh
cd authbridge/demos/lineage
DEPLOY=echo-upstream NAMESPACE=team1 ./sidecar-patch.sh
```

This adds the sidecar and leaves the app container exactly as its owner wrote
it; from then on every request the upstream serves is recorded as an inbound
span pair under `lineage.self.id=echo-upstream`. Drive one request from
**inside** the cluster (a `kubectl port-forward` reaches the app on loopback
and bypasses the sidecar's inbound listener, so it captures nothing):

```sh
kubectl run -n team1 --rm -i drive --image=curlimages/curl:8.11.1 --restart=Never -- \
  curl -s http://echo-upstream:8888/echo -d 'hello lineage'
```

and read the pair off the collector:

```sh
kubectl logs -n rossoctl-system deploy/otel-collector | grep -A2 lineage.exchange.id
```

Two spans with one shared `lineage.exchange.id` — a request span
(`lineage.role=request`, `lineage.direction=inbound`) and its response twin
carrying `lineage.outcome`. That pair is the whole claim: the sidecar saw an
exchange and recorded it as facts.

`OUTBOUND_PORTS_EXCLUDE` keeps a port out of the iptables redirect — use it
for a port the app exports its *own* telemetry on, so that keeps flowing
untouched, and for a **plaintext non-HTTP store** the app talks to (Postgres
5432, SMTP 1025, Redis 6379, …): the sidecar's outbound listener hands every
non-TLS connection to its HTTP codec, which closes a foreign wire protocol —
seen live as `psycopg` "server terminated abnormally". Those hops are
invisible to lineage either way (no HTTP, no facts); excluding them just
lets the app work. Do **not** exclude LLM or tool ports, nor S3 or any other
HTTP-spoken store; those are the hops lineage exists to observe.

### With propagation: bake, then flip the switch

For an app that makes outbound calls and does not instrument itself, add the
shim so those calls land in their caller's trace:

```sh
./build-otel-shim.sh <your-app>:latest      # -> <your-app>-otel:latest, attested, kind-loaded
DEPLOY=<deployment> APP_CONTAINER=<container> \
  APP_IMAGE=docker.io/library/<your-app>-otel:latest \
  ./sidecar-patch.sh
```

The patch swaps the container's image to the baked one and adds
`LINEAGE_PROPAGATE=1` to its env — nothing else in the Deployment changes.
Then verify **pairing under concurrency** before relying on it: drive several
concurrent requests, each with a distinct `traceparent`, and check that every
outbound hop lands in the trace of the inbound that caused it
(`lineage.parent.source=tracestate` on hops after the first). Hops that
fragment instead are provably un-propagated (`lineage.parent.source=wire`).

---

## Configuration

`attach-lineage.sh` writes the plugin entry into the generated ConfigMap. Three
keys matter:

```yaml
- name: lineage-telemetry
  config:
    otel_endpoint: "otel-collector.rossoctl-system.svc.cluster.local:4317"
    capture_io: true
    self_id: "echo-upstream"
```

| key | meaning |
|---|---|
| `otel_endpoint` | OTLP/gRPC endpoint as `host:port` (a URL scheme prefix is accepted). The default targets an in-cluster collector over plaintext — which carries `lineage.principal.*` and, with `capture_io`, full payloads in the clear; the platform collector's 4317 speaks no TLS, so in-cluster that is the accepted trade. For an endpoint outside the cluster use an `https://` prefix, which turns on the plugin's `otel_tls`. |
| `capture_io` | attach parsed request/response content as `input.value` / `output.value`. **Off by default** in the plugin; this demo turns it on so the worked example shows content — enable it only where the traffic carries no PII, or the backend enforces access control. |
| `self_id` | this workload's stable identity, emitted as `lineage.self.id`. Falls back to `self_id_file` (default `/shared/client-id.txt`, the operator-mounted credential). |

`bypass_paths` and `bypass_hosts` keep infrastructure noise out of the graph;
their defaults already cover agent-card discovery, health probes and the common
telemetry backends.

Script-level knobs: `NAME` (the generator) / `DEPLOY` (the applier),
`NAMESPACE`, `SELF_ID`, `OTEL_ENDPOINT`, `APP_CONTAINER`, `APP_IMAGE`,
`OUTBOUND_PORTS_EXCLUDE`, `SIDECAR_IMAGE`, `PROXY_INIT_IMAGE`, `NO_EMIT`,
`EMIT`. Each script's header documents its own. `NO_EMIT=1` keeps the sidecar
as a pure proxy that emits nothing — a clean A/B baseline.

---

## Limits

- **The sidecar sees plaintext HTTP only.** An HTTPS destination is TLS
  passthrough — no hop is recorded for it.
- **Trace-context propagation is the app's job** — the shim does it for the
  in-envelope stack, and nothing else can do it from outside the process.
- **The patch path is not durable** and a workload whose image *and* env you
  cannot influence stays capture-only. Both are spelled out above; they are
  the two things most likely to surprise you in production.

---

## Troubleshooting

| symptom | cause |
|---|---|
| No spans at all | Wrong `OTEL_ENDPOINT`, or the sidecar image predates the plugin. Check the `envoy-proxy` container's logs. |
| Only inbound hops, never outbound | `proxy-init` did not install its iptables rules — check the init container's logs. |
| Outbound hops fragment into their own traces (`lineage.parent.source=wire`) | `traceparent` is not propagating. Check the app container carries `LINEAGE_PROPAGATE=1` (the patch sets it when `APP_CONTAINER` is given; an operator-owned Deployment needs the `SELF_ACTIVATE=1` image instead). If the app runs the LLM/tool call in a worker thread, the `threading` instrumentor is required — it is bundled in `Dockerfile.otel-shim`. If only the entry hop dangles, the caller is not sending a `traceparent` at all — expected at the trace edge. |
| Nothing captured when testing | You drove the app through `kubectl port-forward`. Loopback bypasses the inbound listener; drive it from inside the cluster. |
| `kind load` fails under podman | `container-runtime.sh` detects the runtime and saves + loads an image archive for podman, because `kind load docker-image` misbehaves under podman v5. Set `CONTAINER_TOOL` to force a runtime, `KIND_CLUSTER_NAME` for the cluster name. |
| Pod stuck `ImagePullBackOff` on the sidecar | `SIDECAR_IMAGE` / `PROXY_INIT_IMAGE` point at tags the cluster cannot resolve. Load them locally, or use the published refs. |
| App container `ErrImagePull` after the patch | `APP_IMAGE` is not resolvable under the container's own `imagePullPolicy` — a kind-loaded image needs `IfNotPresent`; a registry-pulled base needs the `-otel` image pushed beside it ("The propagation half"). `kubectl rollout undo deploy/<name>` restores the owner's spec. |
| Pod stuck `ContainerCreating` (`configmap "envoy-config" not found`) | The namespace is not one the platform set up (`sidecar-patch.sh` checks; the manifests route cannot). Use a platform namespace, or copy the ConfigMap in. |
| `envoy-proxy` restarts with `unknown plugin "lineage-telemetry"` | The sidecar image predates the plugin — the published image, until a release carries it. Build from this repo and set `SIDECAR_IMAGE`; `kubectl rollout undo deploy/<name>` backs the failed rollout out meanwhile. Note the patch uses `imagePullPolicy: IfNotPresent`, so once a release does carry the plugin a node that cached the older `:latest` keeps it until that image is removed or a versioned tag is used. |
| `sidecar-patch.sh` refuses: "already declares containerPort …" | The target is operator-enrolled (it has an injected sidecar) or the app itself listens on 9090/15123/15124. Use the namespace-ConfigMap route for enrolled workloads; a colliding app port cannot be adopted. |
| `sidecar-patch.sh` refuses: "has no container named …" | `APP_CONTAINER` does not match any container in the target Deployment. The check exists because a strategic merge would otherwise *add* a stub container by that name instead of failing. |
