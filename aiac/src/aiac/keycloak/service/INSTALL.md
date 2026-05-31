# Keycloak Service — Installation Guide

FastAPI proxy over the Keycloak Admin REST API. Stateless, no caching.
Binds to `0.0.0.0:7070`.

## Prerequisites

- Python 3.13+ (local) or Docker/Podman (container)
- A running Keycloak instance with admin credentials
- `kubectl` + a Kind cluster (Kubernetes deploy only)

---

## 1. Local (uvicorn)

### Configure

Copy or edit `.env` in the service directory:

```
src/aiac/keycloak/service/.env
```

```dotenv
KEYCLOAK_URL=http://keycloak.localtest.me:8080/
KEYCLOAK_REALM=kagenti
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=admin
```

OS environment variables override `.env` values when both are present.

### Install dependencies

```bash
pip install -r src/aiac/keycloak/service/requirements.txt
```

### Run

```bash
uvicorn aiac.keycloak.service.main:app --host 0.0.0.0 --port 7070
```

Run from the `aiac/` directory (the `src/` directory must be on `PYTHONPATH`):

```bash
cd aiac
PYTHONPATH=src uvicorn aiac.keycloak.service.main:app --host 0.0.0.0 --port 7070
```

### Smoke test

```bash
curl http://localhost:7070/users
```

---

## 2. Docker / Podman

### Build

```bash
docker build -f src/aiac/keycloak/service/Dockerfile \
  -t aiac-keycloak-service:local src/
```

### Run

```bash
docker run --rm -p 7070:7070 \
  -e KEYCLOAK_URL=http://keycloak.localtest.me:8080/ \
  -e KEYCLOAK_REALM=kagenti \
  -e KEYCLOAK_ADMIN_USERNAME=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=admin \
  aiac-keycloak-service:local
```

---

## 3. Kubernetes (Kind)

### Build and load into Kind

```bash
docker build -f src/aiac/keycloak/service/Dockerfile \
  -t aiac-keycloak-service:local src/

kind load docker-image aiac-keycloak-service:local --name kagenti
```

> **Podman note:** `kind load` tags images with a `localhost/` prefix.
> The manifest at `k8s/keycloak-service-pod.yaml` already uses
> `localhost/aiac-keycloak-service:local` to match this.

### Deploy

```bash
kubectl apply -f aiac/k8s/keycloak-service-pod.yaml
```

This creates, in namespace `aiac-system`:

| Resource | Name | Purpose |
|----------|------|---------|
| Namespace | `aiac-system` | Isolation namespace |
| ConfigMap | `aiac-keycloak-config` | `KEYCLOAK_URL`, `KEYCLOAK_REALM` |
| Secret | `keycloak-admin-secret` | `KEYCLOAK_ADMIN_USERNAME`, `KEYCLOAK_ADMIN_PASSWORD` |
| Pod | `aiac-keycloak-service` | The service, port 7070 |

The ConfigMap uses the in-cluster Keycloak DNS name:

```
KEYCLOAK_URL=http://keycloak-service.keycloak.svc:8080
```

### Verify

```bash
kubectl get pod aiac-keycloak-service -n aiac-system
```

Expected: `STATUS = Running`, `READY = 1/1`.

### Smoke test (port-forward)

```bash
kubectl port-forward -n aiac-system pod/aiac-keycloak-service 7070:7070
curl http://localhost:7070/users
```

---

## Endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET | `/users` | JSON array of users |
| GET | `/realm-roles` | JSON array of realm roles |
| GET | `/clients` | JSON array of clients |
| GET | `/client-scopes` | JSON array of client scopes |
| GET | `/users/{user_id}/role-mappings` | JSON object with `realmMappings` and `clientMappings` |
| GET | `/clients/{client_id}/roles` | JSON array of client roles |
| POST | `/users/{user_id}/role-mappings/clients/{client_id}` | 204 No Content |
| DELETE | `/users/{user_id}/role-mappings/clients/{client_id}` | 204 No Content |

All endpoints return `502 Bad Gateway` with `{"error": "..."}` on Keycloak errors.

---

## Running tests

From `aiac/`:

```bash
.venv/bin/pytest test/test_service.py -v
```

No live Keycloak required — `KeycloakAdmin` is mocked.
