# AIAC — Kubernetes Installation Guide

This guide covers the full AIAC deployment in the `aiac-system` namespace.

## Components deployed

| Manifest | Contents | Port(s) |
|---|---|---|
| `pdp-interface-deployment.yaml` | Rossoctl Interface Pod (IdP Configuration Service + PDP Policy Writer **Phase 1 rego-file mock** `aiac-pdp-policy-opa`) + 2 ClusterIP Services | 7071, 7072 |
| `policy-model-store-statefulset.yaml` | Policy Model Store StatefulSet + 1 Gi PVC + headless Service + ClusterIP Service | 7074 |
| `event-broker-deployment.yaml` | NATS JetStream Event Broker Deployment + ClusterIP Service | 4222 |
| `agent-deployment.yaml` | Agent Pod Deployment (`aiac-init` init container + AIAC Agent) + ClusterIP Service | 7070 |

## Prerequisites

- Kubernetes cluster with `kubectl` configured for the target cluster.
- Keycloak reachable from within the cluster (default: `http://keycloak-service.keycloak.svc:8080`).
- For local Kind clusters: `kind` CLI and `docker`.

## 1 — Build the images

Run from the repo root (`cortex/`):

```bash
# IdP Configuration Service (Interface Pod container 1)
# Build context is the component directory (Dockerfile copies requirements.txt + main.py from there)
docker build -f aiac/src/aiac/idp/service/configuration/keycloak/Dockerfile \
  -t localhost/aiac-pdp-config:local \
  aiac/src/aiac/idp/service/configuration/keycloak/

# PDP Policy Writer — Phase 1 OPA rego-file mock (Interface Pod container 2, writes .rego to filesystem)
# Build context is aiac/src/ (the OPA Dockerfile COPYs the whole tree and sets PYTHONPATH)
docker build -f aiac/src/aiac/pdp/service/policy/opa/Dockerfile \
  -t localhost/aiac-pdp-policy-opa:local \
  aiac/src/

# Policy Model Store
docker build -f aiac/src/aiac/policy/model_store/service/Dockerfile \
  -t localhost/aiac-policy-model-store:local aiac/src/

# AIAC Agent (also used as the aiac-init init container, via a command override) — context: aiac/src/
docker build -f aiac/src/aiac/agent/controller/Dockerfile \
  -t localhost/aiac-agent:local aiac/src/
```

The Event Broker uses the stock `nats:2.14-alpine` image (pinned in
`event-broker-deployment.yaml`) — no build step.

## 2 — Load images into the cluster

**Kind (local development)**

```bash
kind load docker-image localhost/aiac-pdp-config:local       --name <cluster-name>
kind load docker-image localhost/aiac-pdp-policy-opa:local    --name <cluster-name>
kind load docker-image localhost/aiac-policy-model-store:local     --name <cluster-name>
kind load docker-image localhost/aiac-agent:local            --name <cluster-name>
```

For a fully air-gapped Kind cluster (no outbound network access), also pull and load the
NATS image; `event-broker-deployment.yaml` uses `imagePullPolicy: IfNotPresent`, so a
networked cluster can skip this and pull it directly:

```bash
docker pull nats:2.14-alpine
kind load docker-image nats:2.14-alpine --name <cluster-name>
```

**Remote registry** — tag, push, then update the `image:` fields in the manifests to match.

## 3 — Create the secrets

Two Secrets must exist in `aiac-system` before applying the manifests. Create the namespace first, then both secrets.

```bash
kubectl create namespace aiac-system
```

**`keycloak-admin-secret`** — required by the Interface Pod:

```bash
kubectl create secret generic keycloak-admin-secret \
  -n aiac-system \
  --from-literal=KEYCLOAK_ADMIN_USERNAME=<admin-user> \
  --from-literal=KEYCLOAK_ADMIN_PASSWORD=<admin-password>
```

> `pdp-interface-deployment.yaml` contains placeholder credentials for reference only.
> For any non-local environment, create the secret manually and remove the `stringData` block.

## 3b — Configure the Agent LLM (ConfigMap + Secret)

The AIAC Agent's Policy Rules Builder calls an **OpenAI-compatible** LLM endpoint
(`ChatOpenAI(base_url=LLM_BASE_URL, model=LLM_MODEL, api_key=LLM_API_KEY)`). This configuration is
split across two objects the Agent consumes via `envFrom`, and `agent-deployment.yaml` ships only
**placeholders** — you must supply the real values per environment:

| Key | Object | Notes |
|-----|--------|-------|
| `LLM_BASE_URL` | `aiac-agent-config` ConfigMap | OpenAI-compatible base URL (e.g. a litellm proxy). Placeholder in the manifest. |
| `LLM_MODEL` | `aiac-agent-config` ConfigMap | Model the endpoint serves. Placeholder in the manifest. |
| `LLM_API_KEY` | `aiac-agent-secret` Secret | **Not** defined in any manifest — the Deployment only references it. |

```bash
# API key — create the Secret BEFORE applying agent-deployment.yaml (step 5); the manifest only
# references it. (To update an existing one, append: --dry-run=client -o yaml | kubectl apply -f -)
kubectl create secret generic aiac-agent-secret -n aiac-system \
  --from-literal=LLM_API_KEY=<your-api-key>

# Endpoint + model — patch the LIVE ConfigMap AFTER step 5 (agent-deployment.yaml creates it with
# placeholders). Do not commit real endpoints/keys to the manifest.
kubectl patch configmap aiac-agent-config -n aiac-system --type merge \
  -p '{"data":{"LLM_BASE_URL":"https://<your-openai-compatible-endpoint>/v1","LLM_MODEL":"<model>"}}'
```

Both are read by the Agent at startup, so a change to either takes effect on the next (re)start:
`kubectl rollout restart deployment/aiac-agent -n aiac-system`.

## 4 — Configure the environment

Edit the `aiac-pdp-config` ConfigMap in `pdp-interface-deployment.yaml` to match your environment:

| Key | Default | Used by |
|-----|---------|---------|
| `KEYCLOAK_URL` | `http://keycloak-service.keycloak.svc:8080` | IdP Configuration Service |
| `KEYCLOAK_REALM` | `rossoctl` | IdP Configuration Service |
| `KEYCLOAK_ADMIN_REALM` | `master` | IdP Configuration Service |
| `AIAC_PDP_CONFIG_URL` | `http://aiac-pdp-config-service:7071` | Agent |
| `AIAC_PDP_POLICY_URL` | `http://aiac-pdp-policy-service:7072` | Agent |
| `AIAC_POLICY_MODEL_STORE_URL` | `http://aiac-policy-model-store-service:7074` | Agent |
| `SERVICEPOLICY_DB_PATH` | `/data/policy_model.db` | Policy Model Store |
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | Agent, `aiac-init` — Event Broker ClusterIP address |
| `AIAC_RAG_INGEST_URL` | `http://aiac-rag-service:7073` | Init container — **added in Phase 3** (RAG Pod, issue 4.20) |
| `AIAC_CHROMADB_URL` | `http://aiac-rag-service:8000` | Agent — **added in Phase 3** (RAG Pod, issue 4.20) |

`aiac-init` treats `AIAC_RAG_INGEST_URL` as optional and skips the RAG Ingest health check
when it is unset (the current phase has no RAG pod deployed yet).

## 5 — Deploy

Apply in dependency order:

```bash
# 1. Interface Pod — creates the namespace, ConfigMap, Secret, and ClusterIP Services
kubectl apply -f aiac/k8s/pdp-interface-deployment.yaml

# 2. Event Broker — NATS JetStream, no dependencies
kubectl apply -f aiac/k8s/event-broker-deployment.yaml

# 3. Policy Model Store — needs the aiac-system namespace
kubectl apply -f aiac/k8s/policy-model-store-statefulset.yaml

# 4. Agent — aiac-init waits for NATS + Interface Pod to be healthy (it does not
#    currently gate on Policy Model Store readiness)
kubectl apply -f aiac/k8s/agent-deployment.yaml
```

Wait for all pods to be ready:

```bash
kubectl wait deployment/aiac-interface     -n aiac-system --for=condition=Available --timeout=120s
kubectl wait deployment/aiac-event-broker  -n aiac-system --for=condition=Available --timeout=120s
kubectl wait statefulset/aiac-policy-model-store -n aiac-system --for=jsonpath='{.status.readyReplicas}'=1 --timeout=120s
kubectl wait deployment/aiac-agent         -n aiac-system --for=condition=Available --timeout=120s
```

## 6 — Verify

Port-forward each service and check its health endpoint:

```bash
# IdP Configuration Service
kubectl port-forward svc/aiac-pdp-config-service 7071:7071 -n aiac-system &
pf_pids=$!
curl http://localhost:7071/health
# {"status":"ok"}

# PDP Policy Writer
kubectl port-forward svc/aiac-pdp-policy-service 7072:7072 -n aiac-system &
pf_pids="$pf_pids $!"
curl http://localhost:7072/health
# {"status":"ok"}

# Policy Model Store
kubectl port-forward svc/aiac-policy-model-store-service 7074:7074 -n aiac-system &
pf_pids="$pf_pids $!"
curl http://localhost:7074/health
# {"status":"ok"}

# AIAC Agent
kubectl port-forward svc/aiac-agent-service 7070:7070 -n aiac-system &
pf_pids="$pf_pids $!"
curl http://localhost:7070/health
# {"status":"ok"}

# cleanup only the tunnels started above (not unrelated port-forward sessions)
kill $pf_pids
```

### NATS Event Broker — end-to-end check

Requires the [`nats` CLI](https://github.com/nats-io/natscli).

```bash
kubectl port-forward svc/aiac-event-broker-service 4222:4222 -n aiac-system &
pf_pid=$!

# Wait for the tunnel to accept connections before publishing through it — port-forward
# starts in the background and needs a moment; fail fast if it exits instead of connecting.
for i in $(seq 1 30); do
  if ! kill -0 "$pf_pid" 2>/dev/null; then
    echo "port-forward exited before becoming ready" >&2
    exit 1
  fi
  (exec 3<>/dev/tcp/localhost/4222) 2>/dev/null && exec 3>&- && break
  sleep 0.5
done

nats context save aiac --server nats://localhost:4222
nats context select aiac

# Publish a test service-onboarding event (use a real IdP client UUID to see it
# processed end to end; any string will demonstrate delivery either way):
nats pub aiac.apply.service.<test-uuid> '{"id":"<test-uuid>"}'

# Confirm the Agent processed and acked it (no redelivery):
kubectl logs deployment/aiac-agent -n aiac-system -c aiac-agent --tail=50

kill "$pf_pid"
```

Run the IdP data smoke test:

```bash
kubectl port-forward svc/aiac-pdp-config-service 7071:7071 -n aiac-system &
cd aiac
.venv/bin/python test/idp/configuration/show_keycloak_data.py
pkill -f "port-forward.*7071"
```

## Redeploying after a code change

```bash
# Rebuild the changed image, e.g. IdP Configuration Service (context: the service dir):
docker build -f aiac/src/aiac/idp/service/configuration/keycloak/Dockerfile \
  -t localhost/aiac-pdp-config:local \
  aiac/src/aiac/idp/service/configuration/keycloak/
kind load docker-image localhost/aiac-pdp-config:local --name <cluster-name>

# Restart the affected deployment:
kubectl rollout restart deployment/aiac-interface -n aiac-system
```

---

## Phase 2: Upgrading the OPA PDP Policy Writer to the CR-backed implementation

Phase 1 already deploys the OPA PDP Policy Writer (`aiac-pdp-policy-opa`) as a filesystem
stub that writes `.rego` files to `REGO_OUTPUT_DIR`. Phase 2 upgrades that same container
in place to the CR-backed implementation, which writes Rego packages to an
`AuthorizationPolicy` Kubernetes CR instead. The image name, ClusterIP Service name, and
port are unchanged — no image swap and no Agent reconfiguration required.

See issue [4.18 — K8s: OPA PDP Policy Writer AuthorizationPolicy CR + RBAC upgrade](../docs/issues/deployment/4.18-k8s-opa-authorizationpolicy-rbac.md) for the full procedure (ServiceAccount, ClusterRole, ClusterRoleBinding, CR instance).

```bash
# Rebuild the OPA PDP Policy Writer image with the Phase 2 (CR-backed) implementation
docker build -f aiac/src/aiac/pdp/service/policy/opa/Dockerfile \
  -t localhost/aiac-pdp-policy-opa:local aiac/src/
kind load docker-image localhost/aiac-pdp-policy-opa:local --name <cluster-name>
```

---

## Isolated dev: IdP Configuration Service only

To test the IdP Configuration Service in isolation without deploying the full stack, use the standalone dev pod manifest:

```bash
kubectl apply -f aiac/k8s/idp-configuration-keycloak-pod.yaml
kubectl wait pod/idp-configuration-keycloak-pod -n aiac-system \
  --for=condition=Ready --timeout=60s
```

See [idp-configuration-keycloak-pod.yaml](idp-configuration-keycloak-pod.yaml) for the minimal ConfigMap and pod spec.

---

## IdP Configuration Service API reference

All endpoints accept a `?realm=<realm>` query parameter. `/health` uses `KEYCLOAK_ADMIN_REALM` directly.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/subjects` | List users |
| GET | `/roles` | List realm roles |
| GET | `/services` | List clients |
| GET | `/scopes` | List client scopes |
| GET | `/subjects/{subject_id}/assignments` | Realm and service role mappings for a user |
| GET | `/services/{service_id}/permissions` | Client roles for a service |
| GET | `/roles/{role_name}/composites` | Composite roles for a realm role |
| GET | `/health` | Readiness probe — `200 ok` or `503 unavailable` |

All list endpoints return a JSON array. `/subjects/{id}/assignments` returns:

```json
{
  "realmMappings": [...],
  "serviceMappings": { "<clientId>": { "mappings": [...] } }
}
```

Errors from Keycloak are returned as `502` with `{"error": "<message>"}`.
