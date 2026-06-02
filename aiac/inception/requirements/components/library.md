# Component PRD: Library

## Location
`aiac/src/`

## Package structure

```
aiac/src/
└── aiac/
    ├── __init__.py         # empty
    └── pdp/
        ├── __init__.py     # empty
        ├── library/
        │   ├── __init__.py     # empty
        │   ├── models.py       # Pydantic model definitions only
        │   ├── configuration.py  # HTTP client → PDP Configuration Service
        │   └── policy.py         # HTTP client → PDP Policy Service
        └── service/
            ├── __init__.py     # empty
            ├── configuration/
            │   ├── __init__.py     # empty
            │   └── keycloak/
            │       ├── __init__.py     # empty
            │       ├── main.py         # FastAPI app (Keycloak read service)
            │       ├── Dockerfile
            │       └── requirements.txt
            └── policy/
                ├── __init__.py     # empty
                └── keycloak/
                    ├── __init__.py     # empty
                    ├── main.py         # FastAPI app (Keycloak write service)
                    ├── Dockerfile
                    └── requirements.txt
aiac/test/
└── test_models.py              # unit tests for aiac.pdp.library.models
aiac/pyproject.toml   # pytest config: testpaths=["test"], pythonpath=["src"]
```

`aiac` is a regular package with an empty `__init__.py`. `aiac.pdp`, `aiac.pdp.library`, `aiac.pdp.service`, `aiac.pdp.service.configuration`, `aiac.pdp.service.configuration.keycloak`, `aiac.pdp.service.policy`, and `aiac.pdp.service.policy.keycloak` are regular packages with empty `__init__.py` files. Callers must use explicit submodule paths.

---

## Submodule: `aiac.pdp.library.models`

### Description
Data structures and schema library. Contains only Pydantic `BaseModel` subclasses representing generic PDP entities. No HTTP client dependency — importable by any consumer without pulling in `requests` or `python-dotenv`. Backed by Keycloak in both phases; model shapes are derived from Keycloak JSON but named generically.

### Dependencies
```
pydantic
```

### Pydantic models

All models use `model_config = ConfigDict(extra='ignore')` to silently discard unknown fields, ensuring stability across backend version upgrades.

#### `Subject`

Represents a user (Keycloak: `user`).

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `username` | `str` | `username` |
| `email` | `str \| None` | `email` |
| `firstName` | `str \| None` | `firstName` |
| `lastName` | `str \| None` | `lastName` |
| `enabled` | `bool` | `enabled` |

#### `Role`

Represents a realm-level role (Keycloak: `realm role`).

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `composite` | `bool` | `composite` |
| `clientRole` | `bool` | `clientRole` |

#### `Assignments`

Represents the current role and permission assignments for a subject (Keycloak: `GET /users/{id}/role-mappings` response).

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `realmMappings` | `list[Role]` | `realmMappings` | `[]` |
| `serviceMappings` | `dict[str, Any]` | `clientMappings` | `{}` |

`realmMappings` is a typed list of `Role` instances. `serviceMappings` is kept as a raw dict (structure varies by Keycloak version). `Assignments` is defined after `Role` in the module.

#### `Service`

Represents a service (Keycloak: `client`).

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `clientId` | `str` | `clientId` |
| `name` | `str \| None` | `name` |
| `enabled` | `bool` | `enabled` |
| `protocol` | `str \| None` | `protocol` |
| `publicClient` | `bool` | `publicClient` |

#### `Scope`

Represents a service scope (Keycloak: `client scope`).

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `protocol` | `str \| None` | `protocol` |

#### `Permission`

Represents a service permission (Keycloak: `client role`). Used as both the return type of `get_service_permissions` / `get_role_composites` and the payload element for composite add/remove operations.

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `composite` | `bool` | `composite` |
| `clientRole` | `bool` | `clientRole` |

### Usage

```python
from aiac.pdp.library.models import Subject

raw = tool_result["content"]   # raw JSON list
subjects = [Subject.model_validate(s) for s in raw]
```

---

## Submodule: `aiac.pdp.library.configuration`

### Description
HTTP client library that wraps the PDP Configuration Service REST API and returns typed Pydantic model instances from `aiac.pdp.library.models`.

### Dependencies
```
requests
pydantic
python-dotenv
```

### Functions

All seven functions require a mandatory `realm: str` parameter, forwarded to the Service as `?realm=<name>`.

```python
def get_subjects(realm: str) -> list[Subject]: ...
def get_roles(realm: str) -> list[Role]: ...
def get_subject_assignments(subject_id: str, realm: str) -> Assignments: ...
def get_services(realm: str) -> list[Service]: ...
def get_scopes(realm: str) -> list[Scope]: ...
def get_service_permissions(service_id: str, realm: str) -> list[Permission]: ...
def get_role_composites(role_name: str, realm: str) -> list[Permission]: ...
```

Each function:
1. Issues `GET {AIAC_PDP_CONFIG_URL}/<endpoint>` (with path parameters substituted as needed), always appending `?realm=<name>`.
2. Raises `RuntimeError` on non-2xx HTTP status.
3. Parses the response into the appropriate Pydantic model(s).

### Configuration

Read from a `.env` file co-located with `configuration.py` (`aiac/src/aiac/pdp/library/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_CONFIG_URL` | `http://127.0.0.1:7070` |

### Usage

```python
from aiac.pdp.library.configuration import get_subjects, get_roles

subjects = get_subjects(realm="kagenti")
for s in subjects:
    print(s.username, s.email)
```

---

## Submodule: `aiac.pdp.library.policy`

### Description
HTTP client library that wraps the PDP Policy Service REST API. Abstracts the Phase 1 (Keycloak) and Phase 2 (OPA) policy write backends behind a stable function interface — callers never interact with the backend directly. The active backend is determined by `AIAC_PDP_POLICY_URL`, which points to whichever policy service pod is deployed.

### Dependencies
```
requests
pydantic
python-dotenv
```

### Functions

All functions require a mandatory `realm: str` parameter.

```python
def add_role_composites(role_name: str, permissions: list[Permission], realm: str) -> None: ...
def remove_role_composites(role_name: str, permissions: list[Permission], realm: str) -> None: ...
def clear_all_composites(realm: str) -> None: ...
def create_service_permission(service_id: str, permission_name: str, description: str, realm: str) -> Permission: ...
def create_service_scope(service_id: str, scope_name: str, description: str, realm: str) -> Scope: ...
```

`add_role_composites` and `remove_role_composites`:
1. Issue `POST` / `DELETE {AIAC_PDP_POLICY_URL}/roles/{role_name}/composites` with the serialised permission list as JSON body, appending `?realm=<name>`.
2. Raise `RuntimeError` on non-2xx HTTP status.
3. Return `None` on success.

`clear_all_composites`:
1. Issues `DELETE {AIAC_PDP_POLICY_URL}/composites`, appending `?realm=<name>`.
2. The service iterates all realm roles and removes all composite mappings.
3. Raises `RuntimeError` on non-2xx HTTP status.
4. Returns `None` on success.

`create_service_permission`:
1. Issues `POST {AIAC_PDP_POLICY_URL}/services/{service_id}/permissions` with body `{"name": permission_name, "description": description}`, appending `?realm=<name>`.
2. Raises `RuntimeError` on non-2xx HTTP status.
3. Returns the created `Permission` instance parsed from the response.

`create_service_scope`:
1. Issues `POST {AIAC_PDP_POLICY_URL}/services/{service_id}/scopes` with body `{"name": scope_name, "description": description}`, appending `?realm=<name>`.
2. The service creates the scope at realm level and assigns it to the service as a default scope in a single atomic operation.
3. Raises `RuntimeError` on non-2xx HTTP status.
4. Returns the created `Scope` instance parsed from the response.

### Configuration

| Variable | Default |
|----------|---------|
| `AIAC_PDP_POLICY_URL` | `http://127.0.0.1:7073` |

### Usage

```python
from aiac.pdp.library.policy import add_role_composites
from aiac.pdp.library.models import Permission

permissions = [Permission(id="abc", name="editor", description=None, composite=False, clientRole=True)]
add_role_composites(role_name="developer", permissions=permissions, realm="kagenti")
```
