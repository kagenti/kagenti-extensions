# Component PRD: IdP Configuration Service

## Location
`aiac/src/aiac/idp/service/configuration/keycloak/`

## Description
A FastAPI web service that proxies Keycloak Admin REST API endpoints. Returns IdP (Keycloak) entity state in generic form for consumption by the AIAC Agent and library clients. Consolidates all Keycloak interactions into a single container. Stateless — no caching. Backed exclusively by Keycloak.

## Endpoints

| Method | Path | Keycloak Admin API call | Description |
|--------|------|------------------------|-------------|
| GET | `/subjects` | `GET /admin/realms/{realm}/users` | All subjects (users) in realm |
| GET | `/roles` | `GET /admin/realms/{realm}/roles` | All realm-level roles |
| GET | `/subjects/{subject_id}/assignments` | `GET /admin/realms/{realm}/users/{subject_id}/role-mappings` | Realm and service permission assignments for a subject |
| GET | `/services` | `GET /admin/realms/{realm}/clients` | All services (clients) |
| GET | `/services/{service_id}` | `GET /admin/realms/{realm}/clients/{service_id}` | Single service by ID |
| GET | `/scopes` | `GET /admin/realms/{realm}/client-scopes` | All scopes |
| GET | `/services/{service_id}/roles` | `admin.get_client_service_account_user(service_id)` → `admin.get_realm_roles_of_user(user_id)` | Realm roles assigned to a service's account |
| GET | `/services/{service_id}/scopes` | `admin.get_client_default_client_scopes(service_id)` | Default client scopes assigned to a service |
| GET | `/roles/{role_name}/composites` | `GET /admin/realms/{realm}/roles/{role-name}/composites` | Current composite permissions assigned to a role |
| GET | `/roles/{role_name}/scopes` | _(iterates all realm client scopes; filters to those with role mapped)_ | Scopes that have this realm role mapped |
| POST | `/scopes` | `POST /admin/realms/{realm}/client-scopes` | Create realm-level scope |
| POST | `/services/{service_id}/scopes/{scope_id}` | `PUT /admin/realms/{realm}/default-default-client-scopes/{scope_id}` | Assign existing scope as default scope to service |
| POST | `/roles` | `POST /admin/realms/{realm}/roles` | Create realm-level role |
| POST | `/services/{service_id}/roles/{role_id}` | `admin.get_client_service_account_user(service_id)` → `admin.assign_realm_roles(user_id, ...)` | Assign existing realm role to service account |
| GET | `/health` | `admin.get_server_info()` — uses `KEYCLOAK_ADMIN_REALM`; no `?realm=` param | Readiness probe |

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

`GET /services/{service_id}/roles`:
1. Calls `admin.get_client_service_account_user(service_id)` to get the service account user.
2. Extracts `user["id"]` from the result.
3. Calls `admin.get_realm_roles_of_user(user_id)` to return the realm roles assigned to the service account.
4. Returns `200 OK` with a JSON array of realm role objects.
5. Returns `[]` (empty array) if `KeycloakError` has `response_code == 400` (service has no service account — not an error).
6. Returns `502 Bad Gateway` with `{"error": ...}` on other `KeycloakError`.

`GET /services/{service_id}/scopes`:
1. Calls `admin.get_client_default_client_scopes(service_id)` to return the realm-level client scopes assigned as defaults to the service.
2. Returns `200 OK` with a JSON array of client scope objects.
3. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

`GET /roles/{role_name}/scopes`:
1. Calls `admin.get_realm_role(role_name)` to resolve the role's ID.
2. Iterates all realm client scopes via `admin.get_client_scopes()`.
3. For each scope, calls `admin.get_all_roles_of_client_scope(scope["id"])` and includes the scope if the role's ID appears in the `realmMappings` list of the result.
4. Returns `200 OK` with a JSON array of client scope objects that have this realm role mapped.
5. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

> **Performance note:** This is an O(scopes) endpoint — one Keycloak call per realm client scope. Suitable for infrequent enrichment calls; not intended for high-throughput polling.

`POST /services/{service_id}/roles/{role_id}`:
1. Calls `admin.get_client_service_account_user(service_id)` to get the service account user.
2. Extracts `user["id"]` from the result.
3. Calls `admin.assign_realm_roles(user_id, [{"id": role_id}])` to assign the realm role to the service account.
4. Returns `201 Created` on success.
5. Returns `409 Conflict` if the role is already assigned.
6. Returns `502 Bad Gateway` with `{"error": ...}` on `KeycloakError`.

All endpoints except `/health` require a `?realm=<realm>` query parameter specifying the Keycloak realm to operate in. Returns `422 Unprocessable Entity` if the parameter is absent. `/health` accepts no realm parameter — it calls `_get_or_create_admin(os.environ["KEYCLOAK_ADMIN_REALM"])` directly.

All GET endpoints return `200 OK` with a JSON array on success, except `/subjects/{subject_id}/assignments` which returns a JSON object with `realmMappings` and `serviceMappings` fields. All endpoints return `502 Bad Gateway` with a JSON error body if the Keycloak Admin API call fails.

## Configuration

Environment variables (injected via Kubernetes Deployment manifest):

| Variable | Required | Description |
|----------|----------|-------------|
| `KEYCLOAK_URL` | Yes | Keycloak base URL, e.g. `http://keycloak-service.keycloak.svc:8080` |
| `KEYCLOAK_ADMIN_REALM` | Yes | Realm where the admin credentials live, e.g. `master` |
| `KEYCLOAK_ADMIN_USERNAME` | Yes | Admin username (from `keycloak-admin-secret`) |
| `KEYCLOAK_ADMIN_PASSWORD` | Yes | Admin password (from `keycloak-admin-secret`) |

## Runtime

- Framework: FastAPI
- Server: uvicorn
- Bind: `0.0.0.0:7071`
- Base image: `python:3.12-slim`
- Kubernetes ClusterIP Service: `aiac-pdp-config-service:7071`
- Deployment: co-located with PDP Policy Writer as a container in the **Kagenti Interface Pod** (`pdp-interface-deployment.yaml`)
- Python library: `aiac.idp.library.configuration`

## Dependencies (`requirements.txt`)

```
fastapi
uvicorn[standard]
python-keycloak
```

## File structure

```
aiac/src/aiac/idp/service/
├── __init__.py
└── configuration/
    ├── __init__.py
    └── keycloak/
        ├── __init__.py
        ├── Dockerfile
        ├── requirements.txt
        └── main.py
```

Build command:
```bash
docker build -f aiac/src/aiac/idp/service/configuration/keycloak/Dockerfile \
  -t aiac-pdp-config:latest aiac/src/
```

## `main.py` behaviour notes

- Maintain a `dict[str, KeycloakAdmin]` cache keyed by realm name, protected by a `threading.Lock`.
- `get_admin(realm: str = Query(...))` is a FastAPI dependency. On each call it checks the cache; on a miss it acquires the lock, double-checks, and constructs a new `KeycloakAdmin(realm_name=realm, user_realm_name=KEYCLOAK_ADMIN_REALM, ...)`. FastAPI returns `422` automatically if `realm` is absent.
- All endpoints except `/health` declare `admin: KeycloakAdmin = Depends(get_admin)`. `/health` calls `_get_or_create_admin` directly with `os.environ["KEYCLOAK_ADMIN_REALM"]` — no FastAPI dependency, no realm query param.
- Each GET endpoint calls the corresponding `python-keycloak` method and returns the result directly via `JSONResponse`.
- `GET /services/{service_id}/roles`: call `admin.get_client_service_account_user(service_id)` → extract `user["id"]` → call `admin.get_realm_roles_of_user(user_id)`. Returns `[]` if `KeycloakError.response_code == 400` (service has no service account); `502` on other `KeycloakError`.
- `GET /services/{service_id}/scopes`: call `admin.get_client_default_client_scopes(service_id)`.
- `GET /roles/{role_name}/composites`: call `admin.get_composite_realm_roles_of_role(role_name=role_name)`.
- `GET /roles/{role_name}/scopes`: resolve role ID via `admin.get_realm_role(role_name)`, then iterate `admin.get_client_scopes()` and for each scope call `admin.get_all_roles_of_client_scope(scope["id"])`; extract `realmMappings` from the result and include the scope if the role ID appears in that list.
- `POST /services/{service_id}/roles/{role_id}`: call `admin.get_client_service_account_user(service_id)` → extract `user["id"]` → call `admin.assign_realm_roles(user_id, [{"id": role_id}])`.
- On `KeycloakError`, return HTTP 502 with `{"error": str(e)}`.
