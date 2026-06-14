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

Model definition order in the module: `Subject` → `Role` → `Service` → `Scope`. Because `Subject`, `Role`, and `Service` all reference `Scope` (and `Subject` references `Role`) as forward references, the module calls `Subject.model_rebuild()`, `Role.model_rebuild()`, and `Service.model_rebuild()` after `Scope` is defined.

#### `Subject`

Represents a user (Keycloak: `user`).

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `id` | `str` | `id` | |
| `username` | `str` | `username` | |
| `email` | `str \| None` | `email` | |
| `firstName` | `str \| None` | `firstName` | |
| `lastName` | `str \| None` | `lastName` | |
| `enabled` | `bool` | `enabled` | |
| `roles` | `list[Role]` | `realmRoles` | `[]` |

#### `Role`

Represents a realm-level role (Keycloak: `realm role`).

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `id` | `str` | `id` | |
| `name` | `str` | `name` | |
| `description` | `str \| None` | `description` | |
| `composite` | `bool` | `composite` | |
| `childRoles` | `list[Role]` | `composites.realm` | `[]` |
| `mappedScopes` | `list[Scope]` | _(client scopes mapped to role)_ | `[]` |

#### `Service`

Represents a service (Keycloak: `client`).

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `id` | `str` | `id` | |
| `name` | `str \| None` | `name` | |
| `description` | `str \| None` | `description` | `None` |
| `enabled` | `bool` | `enabled` | |
| `type` | `Literal["Agent", "Tool"] \| None` | `attributes.type` | `None` |
| `roles` | `list[Role]` | _(realm roles for this client)_ | `[]` |
| `scopes` | `list[Scope]` | _(default client scopes)_ | `[]` |

#### `Scope`

Represents a service scope (Keycloak: `client scope`).

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |

### Usage

```python
from aiac.pdp.library.models import Subject, Role, Scope, Service

raw = tool_result["content"]   # raw JSON list
subjects = [Subject.model_validate(s) for s in raw]
```

---

## Submodule: `aiac.pdp.library.configuration`

### Description
HTTP client library that wraps the PDP Configuration Service REST API. Provides both read and write access to PDP configuration entities (subjects, roles, services, scopes) and returns typed Pydantic model instances from `aiac.pdp.library.models`.

### Dependencies
```
requests
pydantic
python-dotenv
```

### Class: `Configuration`

Stateful client bound to a single realm. Construct via the factory method or directly.

```python
class Configuration:
    def __init__(self, realm: str) -> None: ...

    @classmethod
    def for_realm(cls, realm: str) -> "Configuration": ...

    def get_subjects(self) -> list[Subject]: ...
    def get_roles(self) -> list[Role]: ...
    def get_services(self) -> list[Service]: ...
    def get_scopes(self) -> list[Scope]: ...

    def create_scope(self, scope_name: str, scope_description: str) -> Scope: ...
    def map_scope_to_service(self, service: Service, scope: Scope) -> Service: ...

    def create_role(self, role_name: str, role_description: str) -> Role: ...
    def map_role_to_service(self, service: Service, role: Role) -> Service: ...
```

Read methods (`get_*`):
1. Issue `GET {AIAC_PDP_CONFIG_URL}/<endpoint>`, always appending `?realm=<self.realm>`.
2. Raise `RuntimeError` on non-2xx HTTP status.
3. Parse the response into the appropriate Pydantic model(s).

`create_scope`:
1. Issues `POST {AIAC_PDP_CONFIG_URL}/scopes` with body `{"name": scope_name, "description": scope_description}`, appending `?realm=<self.realm>`.
2. Raises `RuntimeError` on non-2xx HTTP status (including 409 if a scope with that name already exists).
3. Returns the created `Scope` instance parsed from the response.

`map_scope_to_service`:
1. Issues `POST {AIAC_PDP_CONFIG_URL}/services/{service.id}/scopes/{scope.id}`, appending `?realm=<self.realm>`.
2. Raises `RuntimeError` on non-2xx HTTP status (including 409 if the scope is already mapped to the service).
3. Re-fetches the service via `GET {AIAC_PDP_CONFIG_URL}/services/{service.id}`, appending `?realm=<self.realm>`.
4. Returns the updated `Service` instance parsed from the response.

`create_role`:
1. Issues `POST {AIAC_PDP_CONFIG_URL}/roles` with body `{"name": role_name, "description": role_description}`, appending `?realm=<self.realm>`.
2. Raises `RuntimeError` on non-2xx HTTP status (including 409 if a role with that name already exists).
3. Returns the created `Role` instance parsed from the response.

`map_role_to_service`:
1. Issues `POST {AIAC_PDP_CONFIG_URL}/services/{service.id}/roles/{role.id}`, appending `?realm=<self.realm>`.
2. Raises `RuntimeError` on non-2xx HTTP status (including 409 if the role is already mapped to the service).
3. Re-fetches the service via `GET {AIAC_PDP_CONFIG_URL}/services/{service.id}`, appending `?realm=<self.realm>`.
4. Returns the updated `Service` instance parsed from the response.

### Configuration

Read from a `.env` file co-located with `configuration.py` (`aiac/src/aiac/pdp/library/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_CONFIG_URL` | `http://127.0.0.1:7071` |

### Usage

```python
from aiac.pdp.library.configuration import Configuration

cfg = Configuration.for_realm("kagenti")
subjects = cfg.get_subjects()
for s in subjects:
    print(s.username, s.email)

scope = cfg.create_scope(scope_name="read", scope_description="Read access")
services = cfg.get_services()
service = next(s for s in services if s.id == "abc123")
updated_service = cfg.map_scope_to_service(service, scope)

role = cfg.create_role(role_name="reader", role_description="Read-only access")
updated_service = cfg.map_role_to_service(updated_service, role)
```

---

## Submodule: `aiac.pdp.library.policy`

### Description
HTTP client library that wraps the PDP Policy Service REST API. Abstracts the Phase 1 (Keycloak) and Phase 2 (OPA) policy backends behind a stable function interface — callers never interact with the backend directly. The active backend is determined by `AIAC_PDP_POLICY_URL`, which points to whichever policy service pod is deployed.

Handles policy operations: composite role mappings (Phase 1) and Rego rules (Phase 2). Configuration entity operations (e.g. scope creation) belong to `aiac.pdp.library.configuration`.

### Dependencies
```
requests
pydantic
python-dotenv
```

### Class: `Policy`

Stateful client bound to a single realm. Construct via the factory method or directly.

```python
class Policy:
    def __init__(self, realm: str) -> None: ...

    @classmethod
    def for_realm(cls, realm: str) -> "Policy": ...
```

Policy write methods (composite role management and permissions) are defined in the PDP Policy Service component PRD.

### Configuration

| Variable | Default |
|----------|---------|
| `AIAC_PDP_POLICY_URL` | `http://127.0.0.1:7072` |

### Usage

```python
from aiac.pdp.library.policy import Policy

policy = Policy.for_realm("kagenti")
```
