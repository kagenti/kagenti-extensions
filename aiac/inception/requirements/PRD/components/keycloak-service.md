# Component PRD: Keycloak Configuration Service

## Location
`aiac/service/`

## Description
A FastAPI web service that proxies Keycloak Admin REST API endpoints. Returns raw Keycloak JSON unchanged for read operations; forwards write operations directly. Stateless — no caching.

## Endpoints

| Method | Path | Keycloak Admin API call | Description |
|--------|------|------------------------|-------------|
| GET | `/users` | `GET /admin/realms/{realm}/users` | All users in realm |
| GET | `/realm-roles` | `GET /admin/realms/{realm}/roles` | All realm-level roles |
| GET | `/users/{user_id}/role-mappings` | `GET /admin/realms/{realm}/users/{user_id}/role-mappings` | Realm and client role mappings for a user |
| GET | `/clients` | `GET /admin/realms/{realm}/clients` | All clients |
| GET | `/client-scopes` | `GET /admin/realms/{realm}/client-scopes` | All client scopes |
| GET | `/clients/{client_id}/roles` | `GET /admin/realms/{realm}/clients/{client_id}/roles` | Roles defined for a specific client |
| POST | `/users/{user_id}/role-mappings/clients/{client_id}` | `POST /admin/realms/{realm}/users/{user_id}/role-mappings/clients/{client_id}` | Assign client roles to a user |
| DELETE | `/users/{user_id}/role-mappings/clients/{client_id}` | `DELETE /admin/realms/{realm}/users/{user_id}/role-mappings/clients/{client_id}` | Revoke client roles from a user |

Every endpoint accepts an optional `realm` query parameter. When supplied, the request targets the named Keycloak realm instead of the service default (`KEYCLOAK_REALM`); a new `KeycloakAdmin` bound to that realm is instantiated per request. When omitted, the singleton admin initialised at startup is used.

The GET endpoints return `200 OK` with a JSON array on success, except `/users/{user_id}/role-mappings` which returns a JSON object with `realmMappings` and `clientMappings` fields. The POST and DELETE endpoints accept a JSON array of role representation objects in the request body and return `204 No Content` on success. All endpoints return `502 Bad Gateway` with a JSON error body if the Keycloak Admin API call fails.

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
- Base image: `python:3.14-slim`

## Dependencies (`requirements.txt`)

```
fastapi
uvicorn[standard]
python-keycloak
```

## File structure

```
aiac/src/aiac/keycloak/service/
├── Dockerfile
├── requirements.txt
└── main.py
```

## `main.py` behaviour notes

- Instantiate the default `KeycloakAdmin` once at startup using env vars.
- `get_admin` is a FastAPI dependency accepting `realm: str | None = Query(None)`. When `realm` is `None` it returns the startup singleton; when `realm` is set it returns a new `KeycloakAdmin` for that realm.
- Each endpoint declares `admin: KeycloakAdmin = Depends(get_admin)` — no per-route changes needed for realm routing.
- Each GET endpoint calls the corresponding `python-keycloak` method and returns the result directly via `JSONResponse`.
- POST `/users/{user_id}/role-mappings/clients/{client_id}`: assign the provided roles and return `Response(status_code=204)`.
- DELETE `/users/{user_id}/role-mappings/clients/{client_id}`: revoke the provided roles and return `Response(status_code=204)`.
- On `KeycloakError`, return HTTP 502 with `{"error": str(e)}`.
