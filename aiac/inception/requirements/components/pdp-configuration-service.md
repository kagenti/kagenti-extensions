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
| GET | `/services/{service_id}` | `GET /admin/realms/{realm}/clients/{service_id}` | Single service by ID |
| GET | `/scopes` | `GET /admin/realms/{realm}/client-scopes` | All scopes |
| GET | `/services/{service_id}/permissions` | `GET /admin/realms/{realm}/clients/{service_id}/roles` | Permissions (roles) defined for a specific service |
| GET | `/roles/{role_name}/composites` | `GET /admin/realms/{realm}/roles/{role-name}/composites` | Current composite permissions assigned to a realm role |
| POST | `/scopes` | `POST /admin/realms/{realm}/client-scopes` | Create realm-level scope |
| POST | `/services/{service_id}/scopes/{scope_id}` | `PUT /admin/realms/{realm}/default-default-client-scopes/{scope_id}` | Assign existing scope as default scope to service |
| POST | `/roles` | `POST /admin/realms/{realm}/roles` | Create realm-level role |
| POST | `/services/{service_id}/roles/{role_id}` | `POST /admin/realms/{realm}/clients/{service_id}/scope-mappings/realm` | Assign existing realm role to service |

`GET /services/{service_id}`:
1. Calls `admin.get_client(service_id)`.
2. Returns `200 OK` with the client JSON on success.
3. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

`POST /scopes`:
Accepts JSON body `{"name": ..., "description": ...}`. It:
1. Calls `admin.create_client_scope({"name": ..., "description": ..., "protocol": "openid-connect"})` to create the scope at realm level.
2. Returns `201 Created` with the created scope JSON (`{"id": ..., "name": ..., "description": ...}`).
3. Returns `409 Conflict` if a scope with that name already exists.
4. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

`POST /services/{service_id}/scopes/{scope_id}`:
1. Calls `admin.add_default_default_client_scope(service_id, scope_id)` to assign the scope as a default scope to the service.
2. Returns `201 Created` on success.
3. Returns `409 Conflict` if the scope is already assigned to the service.
4. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

`POST /roles`:
Accepts JSON body `{"name": ..., "description": ...}`. It:
1. Calls `admin.create_realm_role({"name": ..., "description": ...})` to create the role at realm level.
2. Returns `201 Created` with the created role JSON (`{"id": ..., "name": ..., "description": ...}`).
3. Returns `409 Conflict` if a role with that name already exists.
4. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

`POST /services/{service_id}/roles/{role_id}`:
1. Calls `admin.assign_realm_roles_to_client_scope(service_id, [{"id": role_id}])` to assign the realm role to the service's scope mappings.
2. Returns `201 Created` on success.
3. Returns `409 Conflict` if the role is already assigned to the service.
4. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

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
- Bind: `0.0.0.0:7071`
- Base image: `python:3.12-slim`
- Kubernetes ClusterIP Service: `aiac-pdp-config-service:7071`
- Deployment: co-located with PDP Policy Service as a container in the **PDP Interface Pod** (`pdp-interface-deployment.yaml`)

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
