# Component PRD: Library

## Location
`aiac/src/`

## Package structure

```
aiac/src/
└── aiac/
    ├── __init__.py         # empty
    └── keycloak/
        ├── __init__.py     # empty
        ├── library/
        │   ├── __init__.py     # empty
        │   ├── models.py       # Pydantic model definitions only
        │   └── api.py          # HTTP client functions
        └── service/
            ├── __init__.py     # empty
            ├── main.py         # FastAPI app
            ├── Dockerfile
            └── requirements.txt
aiac/test/
└── test_models.py              # unit tests for aiac.keycloak.library.models
aiac/pyproject.toml   # pytest config: testpaths=["test"], pythonpath=["src"]
```

`aiac` is a regular package with an empty `__init__.py`. `aiac.keycloak`, `aiac.keycloak.library`, and `aiac.keycloak.service` are regular packages with empty `__init__.py` files. Callers must use explicit submodule paths.

---

## Submodule: `aiac.keycloak.library.models`

### Description
Data structures and schema library. Contains only Pydantic `BaseModel` subclasses representing Keycloak entities. No HTTP client dependency — importable by any consumer without pulling in `requests` or `python-dotenv`.

### Dependencies
```
pydantic
```

### Pydantic models

All models use `model_config = ConfigDict(extra='ignore')` to silently discard unknown Keycloak fields, ensuring stability across Keycloak version upgrades.

#### `User`

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `username` | `str` | `username` |
| `email` | `str \| None` | `email` |
| `firstName` | `str \| None` | `firstName` |
| `lastName` | `str \| None` | `lastName` |
| `enabled` | `bool` | `enabled` |

#### `RealmRole`

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `composite` | `bool` | `composite` |
| `clientRole` | `bool` | `clientRole` |

#### `RoleMappings`

Represents the Keycloak Admin REST API response from `GET /users/{id}/role-mappings`.

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `realmMappings` | `list[RealmRole]` | `realmMappings` | `[]` |
| `clientMappings` | `dict[str, Any]` | `clientMappings` | `{}` |

`realmMappings` is a typed list of `RealmRole` instances. `clientMappings` is kept as a raw dict (structure varies by Keycloak version). `RoleMappings` is defined after `RealmRole` in the module.

#### `Client`

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `clientId` | `str` | `clientId` |
| `name` | `str \| None` | `name` |
| `enabled` | `bool` | `enabled` |
| `protocol` | `str \| None` | `protocol` |
| `publicClient` | `bool` | `publicClient` |

#### `ClientScope`

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `protocol` | `str \| None` | `protocol` |

#### `ClientRole`

Represents a Keycloak client role. Used as both the return type of `get_client_roles` and the payload element for assign/revoke operations.

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `composite` | `bool` | `composite` |
| `clientRole` | `bool` | `clientRole` |

### Usage

```python
from aiac.keycloak.library.models import User

raw = tool_result["content"]   # raw JSON list
users = [User.model_validate(u) for u in raw]
```

---

## Submodule: `aiac.keycloak.library.api`

### Description
HTTP client library that wraps the Keycloak Configuration Service REST API and returns typed Pydantic model instances from `aiac.keycloak.library.models`.

### Dependencies
```
requests
pydantic
python-dotenv
```

### Functions

All eleven functions require a mandatory `realm: str` parameter, forwarded to the Service as `?realm=<name>`.

```python
def get_users(realm: str) -> list[User]: ...
def get_realm_roles(realm: str) -> list[RealmRole]: ...
def get_user_role_mappings(user_id: str, realm: str) -> RoleMappings: ...
def get_clients(realm: str) -> list[Client]: ...
def get_client_scopes(realm: str) -> list[ClientScope]: ...
def get_client_roles(client_id: str, realm: str) -> list[ClientRole]: ...
def assign_client_roles(user_id: str, client_id: str, roles: list[ClientRole], realm: str) -> None: ...
def revoke_client_roles(user_id: str, client_id: str, roles: list[ClientRole], realm: str) -> None: ...
def create_client_role(client_id: str, role_name: str, description: str, realm: str) -> ClientRole: ...
def create_client_scope(client_id: str, scope_name: str, description: str, realm: str) -> ClientScope: ...
def revoke_all_role_assignments(realm: str) -> None: ...
```

Each read function:
1. Issues `GET {AC_SERVICE_URL}/<endpoint>` (with path parameters substituted as needed), always appending `?realm=<name>`.
2. Raises `RuntimeError` on non-2xx HTTP status.
3. Parses the response into the appropriate Pydantic model(s).

`assign_client_roles` and `revoke_client_roles`:
1. Issue `POST` / `DELETE {AC_SERVICE_URL}/users/{user_id}/role-mappings/clients/{client_id}` with the serialised role list as JSON body, always appending `?realm=<name>`.
2. Raise `RuntimeError` on non-2xx HTTP status.
3. Return `None` on success.

`create_client_role`:
1. Issues `POST {AC_SERVICE_URL}/clients/{client_id}/roles` with body `{"name": role_name, "description": description}`, appending `?realm=<name>`.
2. Raises `RuntimeError` on non-2xx HTTP status.
3. Returns the created `ClientRole` instance parsed from the response.

`create_client_scope`:
1. Issues `POST {AC_SERVICE_URL}/clients/{client_id}/scopes` with body `{"name": scope_name, "description": description}`, appending `?realm=<name>`.
2. The service creates the scope at realm level and assigns it to the client as a default scope in a single atomic operation.
3. Raises `RuntimeError` on non-2xx HTTP status.
4. Returns the created `ClientScope` instance parsed from the response.

`revoke_all_role_assignments`:
1. Issues `DELETE {AC_SERVICE_URL}/role-mappings`, appending `?realm=<name>`.
2. The service iterates all users in the realm, fetches each user's role mappings, and revokes all client role assignments.
3. Raises `RuntimeError` on non-2xx HTTP status.
4. Returns `None` on success.

### Configuration

Read from a `.env` file co-located with `api.py` (`aiac/src/aiac/keycloak/library/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AC_SERVICE_URL` | `http://127.0.0.1:7070` |

### Usage

```python
from aiac.keycloak.library.api import get_users, get_realm_roles

users = get_users(realm="kagenti")
for u in users:
    print(u.username, u.email)
```
