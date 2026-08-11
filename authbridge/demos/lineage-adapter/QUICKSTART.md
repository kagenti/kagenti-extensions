# Quickstart — stock app to lineage-included pod

This is the whole path. You do not need to read any script source; when a step
can refuse, it refuses loudly and tells you what to do instead.

What you get: your app runs unchanged, with two additions made at deploy time —
a propagate-only OpenTelemetry layer baked onto its image (exports nothing;
only lets the `traceparent` header flow *through* the app), and a lineage
sidecar that captures every HTTP hop with bodies. Data Governance then shows
each request as its own connected execution forest, correct under concurrency.

## 0 · One-time cluster prerequisites

A kind cluster `kagenti` running the Kagenti platform, plus:

```bash
# the two sidecar images, under exactly these names (from this repo)
cd authbridge
podman build -f cmd/authbridge-envoy/Dockerfile -t docker.io/library/authbridge-envoy:latest .
podman save -o /tmp/abe.tar docker.io/library/authbridge-envoy:latest
KIND_EXPERIMENTAL_PROVIDER=podman kind load image-archive /tmp/abe.tar --name kagenti
cd proxy-init && make docker-build-init KIND_CLUSTER_NAME=kagenti && make load-image KIND_CLUSTER_NAME=kagenti
```

- the platform-rendered `envoy-config` ConfigMap present in the target
  namespace (`team1` — the kagenti chart puts it there),
- the collector patched to also feed the DG receiver
  (`lab-data-governance/deploy/patch-kagenti-collector.sh`),
- host Ollama with `qwen2.5:7b` at `host.containers.internal:11434`,
- `python3` with PyYAML (`python3 -m pip install pyyaml`).

## 1 · Add your app to the catalog

One stanza in `fleet.yaml` (all keys documented in its header):

```yaml
  my-agent:
    role: agent            # agent | tool | raw
    image: ghcr:my_agent   # ghcr:<name> | local:<path> | kit:<dir> | pull:<ref>
    entrypoint: server
    port: 8000
    env:
      LLM_API_BASE: http://host.containers.internal:11434/v1
      LLM_MODEL: qwen2.5:7b
      LLM_API_KEY: ollama
```

`entrypoint` is the app's own command with any leading `uv run --no-sync`
dropped: a console script (`server`), `python -m <pkg> ...`, `python <file>.py`,
or `uvicorn <mod>:app --host 0.0.0.0 --port 8000`. Point the LLM env at
plaintext Ollama (var names differ per framework — copy a similar stanza).

A malformed stanza refuses to deploy with a named error; an image the shim
cannot safely wrap (non-Python, or already auto-instrumented) refuses to bake
and names the sidecar-only alternative.

## 2 · Deploy

```bash
./lineage deploy my-agent      # pull/build -> bake shim -> deploy app+sidecar
```

## 3 · See it

```bash
./lineage verify               # catalog entry points -> harmony table, 6/6 each
```

For a brand-new app, drive it directly (in-cluster — port-forward bypasses the
sidecar):

```bash
SELF_ID=my-agent TARGET=my-agent.team1.svc.cluster.local:8080 \
  PROMPT='Say hi. Reference code {TOKEN}.' ./test/concurrency-test-interactions.sh
```

Then open `http://dg.localtest.me:8080/ui/traces` — a trace's `/flow` shows the
interaction tree (`?showInfra=1` reveals protocol plumbing), `/spans` the raw
spans. Correct-by-design surprises: every trace shows one "missing parent" (the
entry caller's span never reaches DG), and HTTPS legs derive no interaction
(TLS passthrough).

## Existing / special apps

- **Already instrumented, or non-Python** (e.g. the stock weather pair):
  `./lineage adopt <deployment>` attaches the sidecar to the live Deployment —
  no shim, no image change. If the app exports its own OTLP spans, add
  `OUTBOUND_PORTS_EXCLUDE=<its export port>`. The platform still owns that
  object: if it rewrites the Deployment, re-run adopt.
- **The UI backend** (browser chats root the trace): `./lineage stamp-backend`,
  then point its Deployment at the stamped image.

## Switches

`./lineage off` / `on` / `status` — the whole pipeline (fleet bare + DG stack
scaled to 0, data kept). Per-app: `NO_PROPAGATE=1` (context stops flowing
through the app) and/or `NO_EMIT=1` (sidecar emits nothing), passed to
`./lineage deploy`.

Deeper: `docs/RUNBOOK.md` (per-app recipe + troubleshooting), `docs/DESIGN.md`
(why the shim exists), `README.md` (map of everything).
