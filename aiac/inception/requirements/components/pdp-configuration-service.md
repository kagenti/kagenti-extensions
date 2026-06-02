# Component PRD: PDP Configuration Service

## Location
`aiac/src/aiac/pdp/service/configuration/keycloak/`

## Description
A FastAPI web service that proxies Keycloak Admin REST API read endpoints. Returns PDP entity state in generic form for consumption by the AIAC Agent and library clients. Stateless — no caching. Backed exclusively by Keycloak in both Phase 1 and Phase 2; the read interface is stable across phases.

## Endpoints

| Method | Path | Keycloak Admin API call | Description |
|--------|------|------------------------|-------------|
| GET | `/subjects` | `GET /admin/realms/{realm}/users` | All subjects (users) in realm |
| GET | `/roles` | `GET /admin/realms/{realm}/roles` | All realm-level roles |
| GET | `/subjects/{subject_id}/assignments` | `GET /admin/realms/{realm}/users/{subject_id}/role-mappings` | Realm and service permission assignments for a subject |
| GET | `/services` | `GET /admin/realms/{realm}/clients` | All services (clients) |
| GET | `/scopes` | `GET /admin/realms/{realm}/client-scopes` | All scopes |
| GET | `/services/{service_id}/permissions` | `GET /admin/realms/{realm}/clients/{service_id}/roles` | Permissions (roles) defined for a specific service |
| GET | `/roles/{role_name}/composites` | `GET /admin/realms/{realm}/roles/{role-name}/composites` | Current composite permissions assigned to a realm role |

Every endpoint accepts an optional `realm` query parameter. When supplied, the request targets the named Keycloak realm instead of the service default (`KEYCLOAK_REALM`); a new `KeycloakAdmin` bound to that realm is instantiated per request. When omitted, the singleton admin initialised at startup is used.

All GET endpoints return `200 OK` with a JSON array on success, except `/subjects/{subject_id}/assignments` which returns a JSON object with `realmMappings` and `serviceMappings` fields. All endpoints return `502 Bad Gateway` with a JSON error body if the Keycloak Admin API call fails.

## Configuration

Environment variables (injected via Kubernetes Deployment manifest):

| Variable | Required | Description |
|----------|----------|-------------|
| `KEYCLOAK_URL` | Yes | Keycloak base URL, e.g. `http://keycloak-service.keycloak.svc:8080` |
| `KEYCLOAK_REALM` | Yes | Realm name, e.g. `kagenti` |
| `KEYCLOAK_ADMIN_USERNAME` | Yes | Admin username (from `keycloak-admin-secret`) |
| `KEYCLOAK_ADMIN_PASSWORD` | Yes | Admin password (from `keycloak-admin-secret`) |

## Runtime

- Framework: FastAPI
- Server: uvicorn
- Bind: `0.0.0.0:7070`
- Base image: `python:3.12-slim`
- Kubernetes ClusterIP Service: `aiac-pdp-config-service`

## Dependencies (`requirements.txt`)

```
fastapi
uvicorn[standard]
python-keycloak
```

## File structure

```
aiac/src/aiac/pdp/service/
├── __init__.py
└── configuration/
    ├── __init__.py
    └── keycloak/
        ├── __init__.py
        ├── Dockerfile
        ├── requirements.txt
        └── main.py
```

## `main.py` behaviour notes

- Instantiate the default `KeycloakAdmin` once at startup using env vars.
- `get_admin` is a FastAPI dependency accepting `realm: str | None = Query(None)`. When `realm` is `None` it returns the startup singleton; when `realm` is set it returns a new `KeycloakAdmin` for that realm.
- Each endpoint declares `admin: KeycloakAdmin = Depends(get_admin)`.
- Each GET endpoint calls the corresponding `python-keycloak` method and returns the result directly via `JSONResponse`.
- `GET /roles/{role_name}/composites`: call `admin.get_composite_realm_roles_of_role(role_name=role_name)`; return the result as `JSONResponse`.
- On `KeycloakError`, return HTTP 502 with `{"error": str(e)}`.
