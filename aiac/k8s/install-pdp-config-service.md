# PDP Configuration Service — Standalone Dev Installation

> **Dev / isolated testing only.**
> This guide deploys the PDP Configuration Service as a standalone Pod.
> It does not reflect the production topology.
>
> In production both PDP services run as containers in a single PDP Interface Pod
> defined in `pdp-interface-deployment.yaml` (issue 4.2).

The PDP Configuration Service is a read-only FastAPI proxy over the Keycloak Admin REST API.
It exposes PDP-domain entity paths (`/subjects`, `/roles`, `/services`, `/scopes`, …) and is
consumed by the AIAC agent and library clients.

**Port:** 7071  
**Source:** `src/aiac/pdp/service/configuration/keycloak/`  
**Manifest:** `k8s/pdp-configuration-keycloak-pod.yaml`

## Prerequisites

- Kubernetes cluster with the `aiac-system` namespace (created by the manifest).
- Keycloak reachable from within the cluster (default: `http://keycloak-service.keycloak.svc:8080`).
- `kubectl` configured for the target cluster.
- For local Kind clusters: `kind` CLI and `docker`.

## 1 — Build the image

```bash
cd aiac/src/aiac/pdp/service/configuration/keycloak
docker build -t localhost/aiac-pdp-configuration-keycloak-service:local .
```

## 2 — Load the image into the cluster

**Kind (local development)**

```bash
kind load docker-image localhost/aiac-pdp-configuration-keycloak-service:local --name <cluster-name>
```

**Remote registry**

```bash
docker tag localhost/aiac-pdp-configuration-keycloak-service:local <registry>/aiac-pdp-configuration-keycloak-service:<tag>
docker push <registry>/aiac-pdp-configuration-keycloak-service:<tag>
```

Update the `image:` field in `k8s/pdp-configuration-keycloak-pod.yaml` to match.

## 3 — Create the admin secret

The manifest references a `keycloak-admin-secret` Secret that must exist before the pod starts.
Create it once per cluster:

```bash
kubectl create secret generic keycloak-admin-secret \
  -n aiac-system \
  --from-literal=KEYCLOAK_ADMIN_USERNAME=<admin-user> \
  --from-literal=KEYCLOAK_ADMIN_PASSWORD=<admin-password>
```

> The manifest contains placeholder credentials for reference only. For any non-local
> environment, create the secret manually as shown above and remove the `stringData` block
> from the manifest.

## 4 — Configure the environment

Edit the `aiac-pdp-configuration-keycloak-config` ConfigMap in `k8s/pdp-configuration-keycloak-pod.yaml` to match your
environment:

| Key | Default | Description |
|-----|---------|-------------|
| `KEYCLOAK_URL` | `http://keycloak-service.keycloak.svc:8080` | Keycloak base URL (in-cluster) |
| `KEYCLOAK_REALM` | `master` | Default realm used at startup |

## 5 — Deploy

```bash
kubectl apply -f aiac/k8s/pdp-configuration-keycloak-pod.yaml
```

Expected output:

```
namespace/aiac-system created (or unchanged)
configmap/aiac-pdp-configuration-keycloak-config created (or configured)
secret/keycloak-admin-secret configured
pod/pdp-configuration-keycloak-pod created
```

Wait for the pod to be ready:

```bash
kubectl wait pod/pdp-configuration-keycloak-pod -n aiac-system \
  --for=condition=Ready --timeout=60s
```

## 6 — Verify

Port-forward and hit the health endpoint:

```bash
kubectl port-forward pod/pdp-configuration-keycloak-pod 7071:7071 -n aiac-system &
curl http://localhost:7071/health
# {"status":"ok"}
```

Run the full data smoke test (requires the Python dev environment):

```bash
cd aiac
python test/pdp/library/show_keycloak_data.py
```

## Redeploying after a code change

```bash
# 1. Rebuild
cd aiac/src/aiac/pdp/service/configuration/keycloak
docker build -t localhost/aiac-pdp-configuration-keycloak-service:local .

# 2. Reload into Kind
kind load docker-image localhost/aiac-pdp-configuration-keycloak-service:local --name <cluster-name>

# 3. Bounce the pod
kubectl delete pod pdp-configuration-keycloak-pod -n aiac-system
kubectl apply -f aiac/k8s/pdp-configuration-keycloak-pod.yaml
```

## API reference

All endpoints accept an optional `?realm=<realm>` query parameter. When supplied, the request
uses a per-request `KeycloakAdmin` for that realm instead of the default startup realm.

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
