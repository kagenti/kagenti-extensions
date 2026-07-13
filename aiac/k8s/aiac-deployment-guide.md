# AIAC — Kubernetes Installation Guide

This guide covers the full AIAC deployment in the `aiac-system` namespace.

## Components deployed

| Manifest | Contents | Port(s) |
|---|---|---|
| `pdp-interface-deployment.yaml` | Kagenti Interface Pod (IdP Configuration Service + PDP Policy Writer **Phase 1 mock** `aiac-pdp-policy-keycloak`) + 2 ClusterIP Services | 7071, 7072 |
| `policy-store-statefulset.yaml` | Policy Store StatefulSet + 1 Gi PVC + headless Service + ClusterIP Service | 7074 |
| `agent-deployment.yaml` | Agent Pod Deployment (init container + AIAC Agent) + ClusterIP Service | 7070 |

## Prerequisites

- Kubernetes cluster with `kubectl` configured for the target cluster.
- Keycloak reachable from within the cluster (default: `http://keycloak-service.keycloak.svc:8080`).
- For local Kind clusters: `kind` CLI and `docker`.

## 1 — Build the images

Run from the repo root (`kagenti-extensions/`):

```bash
# IdP Configuration Service (Interface Pod container 1)
docker build -f aiac/src/aiac/idp/service/configuration/keycloak/Dockerfile \
  -t localhost/aiac-pdp-config:local aiac/src/

# PDP Policy Writer — Phase 1 mock (Interface Pod container 2, writes Rego to filesystem)
docker build -f aiac/src/aiac/pdp/service/policy/keycloak/Dockerfile \
  -t localhost/aiac-pdp-policy-keycloak:local aiac/src/

# Policy Store
docker build -f aiac/src/aiac/policy/store/service/Dockerfile \
  -t localhost/aiac-policy-store:local aiac/src/

# AIAC Agent (also used as the init container)
docker build -f aiac/src/aiac/agent/controller/Dockerfile \
  -t localhost/aiac-agent:local aiac/src/
```

## 2 — Load images into the cluster

**Kind (local development)**

```bash
kind load docker-image localhost/aiac-pdp-config:local       --name <cluster-name>
kind load docker-image localhost/aiac-pdp-policy-keycloak:local --name <cluster-name>
kind load docker-image localhost/aiac-policy-store:local     --name <cluster-name>
kind load docker-image localhost/aiac-agent:local            --name <cluster-name>
```

**Remote registry** — tag, push, then update the `image:` fields in the manifests to match.

## 3 — Create the admin secret

The Interface Pod requires a `keycloak-admin-secret` Secret. Create it once per cluster before applying the manifests:

```bash
kubectl create secret generic keycloak-admin-secret \
  -n aiac-system \
  --from-literal=KEYCLOAK_ADMIN_USERNAME=<admin-user> \
  --from-literal=KEYCLOAK_ADMIN_PASSWORD=<admin-password>
```

> `pdp-interface-deployment.yaml` contains placeholder credentials for reference only.
> For any non-local environment, create the secret manually and remove the `stringData` block.

## 4 — Configure the environment

Edit the `aiac-pdp-config` ConfigMap in `pdp-interface-deployment.yaml` to match your environment:

| Key | Default | Used by |
|-----|---------|---------|
| `KEYCLOAK_URL` | `http://keycloak-service.keycloak.svc:8080` | IdP Configuration Service, PDP Policy Writer |
| `KEYCLOAK_REALM` | `kagenti` | PDP Policy Writer |
| `KEYCLOAK_ADMIN_REALM` | `master` | IdP Configuration Service |
| `AIAC_PDP_CONFIG_URL` | `http://aiac-pdp-config-service:7071` | Agent |
| `AIAC_PDP_POLICY_URL` | `http://aiac-pdp-policy-service:7072` | Agent |
| `AIAC_POLICY_STORE_URL` | `http://aiac-policy-store-service:7074` | Agent |
| `AGENTPOLICY_DB_PATH` | `/data/state.db` | Policy Store |
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | Agent |
| `AIAC_RAG_INGEST_URL` | `http://aiac-rag-service:7073` | Agent |
| `AIAC_CHROMADB_URL` | `http://aiac-rag-service:8000` | Agent |

## 5 — Deploy

Apply in dependency order:

```bash
# 1. Interface Pod — creates the namespace, ConfigMap, Secret, and ClusterIP Services
kubectl apply -f aiac/k8s/pdp-interface-deployment.yaml

# 2. Policy Store — needs the aiac-system namespace
kubectl apply -f aiac/k8s/policy-store-statefulset.yaml

# 3. Agent — init container waits for Interface Pod + Policy Store to be healthy
kubectl apply -f aiac/k8s/agent-deployment.yaml
```

Wait for all pods to be ready:

```bash
kubectl wait deployment/aiac-interface     -n aiac-system --for=condition=Available --timeout=120s
kubectl wait statefulset/aiac-policy-store -n aiac-system --for=jsonpath='{.status.readyReplicas}'=1 --timeout=120s
kubectl wait deployment/aiac-agent         -n aiac-system --for=condition=Available --timeout=120s
```

## 6 — Verify

Port-forward each service and check its health endpoint:

```bash
# IdP Configuration Service
kubectl port-forward svc/aiac-pdp-config-service 7071:7071 -n aiac-system &
curl http://localhost:7071/health
# {"status":"ok"}

# PDP Policy Writer
kubectl port-forward svc/aiac-pdp-policy-service 7072:7072 -n aiac-system &
curl http://localhost:7072/health
# {"status":"ok"}

# Policy Store
kubectl port-forward svc/aiac-policy-store-service 7074:7074 -n aiac-system &
curl http://localhost:7074/health
# {"status":"ok"}

# AIAC Agent
kubectl port-forward svc/aiac-agent-service 7070:7070 -n aiac-system &
curl http://localhost:7070/health
# {"status":"ok"}

pkill -f "port-forward"
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
# Rebuild the changed image, e.g. IdP Configuration Service:
docker build -f aiac/src/aiac/idp/service/configuration/keycloak/Dockerfile \
  -t localhost/aiac-pdp-config:local aiac/src/
kind load docker-image localhost/aiac-pdp-config:local --name <cluster-name>

# Restart the affected deployment:
kubectl rollout restart deployment/aiac-interface -n aiac-system
```

---

## Phase 2: Upgrading to the OPA PDP Policy Writer

Phase 2 replaces the mock PDP Policy Writer with the OPA implementation (`aiac-pdp-policy-opa`), which writes Rego packages to an `AuthorizationPolicy` Kubernetes CR. The ClusterIP Service name and port are unchanged — no Agent reconfiguration required.

See issue [4.18 — K8s: OPA image swap + AuthorizationPolicy CR + RBAC](../inception/issues/deployment/4.18-k8s-opa-authorizationpolicy-rbac.md) for the full procedure (image swap, ServiceAccount, ClusterRole, ClusterRoleBinding, CR instance).

```bash
# Build Phase 2 PDP Policy Writer image
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
