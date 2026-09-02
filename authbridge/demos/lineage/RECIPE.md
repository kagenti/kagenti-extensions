# Recipe — attach lineage to a running Deployment

A step-by-step for an operator or a coding agent. Every step is one command,
the one line of output that means it worked, and what to do when it did not.
Run from this directory. The explanations are in [README.md](README.md); the
reasons behind them in [DESIGN.md](DESIGN.md).

**Inputs.** `NS` — the namespace · `DEPLOY` — the Deployment · `CONTAINER` —
the name of its app container (`kubectl -n $NS get deploy/$DEPLOY -o jsonpath='{.spec.template.spec.containers[*].name}'`)
· `IMAGE` — that container's image, resolvable locally (`docker.io/library/<name>:latest` for a kind-loaded one).

## 0. Preconditions (read-only)

| check | command | pass |
|---|---|---|
| the Deployment exists and is not platform-enrolled | `kubectl -n $NS get deploy $DEPLOY -o jsonpath='{.spec.template.spec.containers[*].name}'` | prints the app container(s) only — no `envoy-proxy` |
| the platform rendered the sidecar's config here | `kubectl -n $NS get cm envoy-config` | found |
| the app's image is local (for the bake) | `podman image exists $IMAGE` (docker: `docker image inspect $IMAGE >/dev/null`) | exit 0 |
| an OTLP/gRPC collector is reachable in-cluster | `kubectl -n rossoctl-system get deploy otel-collector` | found (else set `OTEL_ENDPOINT` in step 3) |

Enrolled workloads (an `AgentRuntime` CR) already carry a sidecar: this recipe
refuses them by design; see README "Enrolled workloads".

## 1. A sidecar image that carries the plugin (once per cluster, until a release does)

```sh
( cd ../.. && podman build -f cmd/authbridge-envoy/Dockerfile -t docker.io/library/authbridge-envoy:latest . \
            && podman build -f proxy-init/Dockerfile.init -t docker.io/library/proxy-init:latest proxy-init/ )
for ref in authbridge-envoy proxy-init; do podman save docker.io/library/$ref:latest -o /tmp/$ref.tar \
  && KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/$ref.tar --name rossoctl; rm -f /tmp/$ref.tar; done
export SIDECAR_IMAGE=docker.io/library/authbridge-envoy:latest PROXY_INIT_IMAGE=docker.io/library/proxy-init:latest
```

Pass: `podman exec <kind-node> crictl images | grep -E 'library/(authbridge-envoy|proxy-init)'` lists both.
Docker hosts: `docker build` with the same `-f`/`-t`, then `kind load docker-image <ref> --name rossoctl`.
Skip this step once the published `ghcr.io/rossoctl/cortex/authbridge-envoy` carries `lineage-telemetry`;
the symptom of skipping it too early is the sidecar crash-looping with `unknown plugin "lineage-telemetry"`.

## 2. Bake the propagation shim onto the app image (once per image)

```sh
KIND_CLUSTER_NAME=rossoctl ./build-otel-shim.sh $IMAGE
```

Pass: last line `>> loaded docker.io/library/<name>-otel:latest into kind cluster rossoctl`
(exit 0; the attestation runs before the load and prints nothing when it passes).
Fail `REFUSING to bake … already instruments …` (exit 3): the app instruments itself — go to step 3 **without** `APP_CONTAINER`/`APP_IMAGE` (capture only).
Fail `REFUSING to bake … no runnable Python found` (exit 3): outside the shim's envelope (DESIGN "The envelope") — same, capture only; or pass the interpreter as arg 3 if you know it.
Fail `ATTESTATION FAILED` (exit 4): the bake itself is broken (the image was not loaded) — read the assertion it prints; not an app property.

## 3. Attach (once per Deployment)

```sh
NAMESPACE=$NS DEPLOY=$DEPLOY APP_CONTAINER=$CONTAINER APP_IMAGE=docker.io/library/<name>-otel:latest ./sidecar-patch.sh
```

Add `OUTBOUND_PORTS_EXCLUDE=5432,1025` (comma-separated) if the app speaks a plaintext **non-HTTP**
protocol to a store — Postgres, SMTP, Redis. Never exclude LLM, tool, peer or S3 ports.
Set `OTEL_ENDPOINT=host:port` for a collector other than the platform's.

Pass — the output ends with:
```
deployment "<deploy>" successfully rolled out
>> lineage sidecar attached to deploy/<deploy> (self_id=<deploy>, ns=<ns>)
```
(preceded by `configmap/authbridge-lineage-config-<deploy> created` and `deployment.apps/<deploy> patched`).
Fail `refusing … already declares containerPort` → enrolled or port-colliding workload (README "How to attach").
Fail `has no container named` → wrong `APP_CONTAINER`; nothing was applied.
Rollout stuck → `kubectl -n $NS rollout undo deploy/$DEPLOY`, then read the sidecar log (step 4).

## 4. Verify

```sh
kubectl -n $NS logs deploy/$DEPLOY -c envoy-proxy | grep 'lineage-telemetry: initialized'
```
Pass: `… endpoint=<otel_endpoint> self_id=<deploy>`.

Then one request **from inside the cluster** (a port-forward bypasses the sidecar) with a trace id you choose:
```sh
T=$(python3 -c 'import secrets;print(secrets.token_hex(16))')
kubectl -n $NS run drive --rm -i --restart=Never --image=curlimages/curl:8.11.1 -- \
  curl -s -H "traceparent: 00-$T-0000000000000001-01" http://<service>:<port>/<path>    # any request the app answers
kubectl -n rossoctl-system logs deploy/otel-collector | grep -c "$T"
```
Pass: a count ≥ 2 (one request span + one response span per exchange the app took part in).
Attribution check, when the app calls out: every hop after the entry must show
`lineage.parent.source: tracestate`; a `wire` on a non-entry hop is an un-propagated call
(DESIGN "Why the shim is needed").

## 5. Back out

```sh
kubectl -n $NS rollout undo deploy/$DEPLOY && kubectl -n $NS delete cm authbridge-lineage-config-$DEPLOY
```
The owner's spec is one revision back; nothing else was written.

## A fleet

Bake once per image, attach once per Deployment; two loops. Then check the **shape** of one trace
(one root, `wire` only at the entry), not just that spans arrived.

```sh
for img in agent-a agent-b tool-x; do KIND_CLUSTER_NAME=rossoctl ./build-otel-shim.sh docker.io/library/$img:latest; done
for d in agent-a agent-b tool-x; do
  NAMESPACE=$NS DEPLOY=$d APP_CONTAINER=app APP_IMAGE=docker.io/library/$d-otel:latest ./sidecar-patch.sh
done
```
