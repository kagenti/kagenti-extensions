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
        │   ├── configuration/
        │   │   ├── __init__.py     # empty
        │   │   ├── models.py       # Pydantic model definitions (Subject, Role, Service, Scope)
        │   │   └── api.py          # HTTP client → PDP Configuration Service
        │   └── policy/
        │       ├── __init__.py     # empty
        │       ├── models.py       # PolicyModel, PolicyStatement
        │       └── api.py          # HTTP client → PDP Policy Service + apply_policy()
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
└── test_models.py              # unit tests for aiac.pdp.library.configuration.models
aiac/pyproject.toml   # pytest config: testpaths=["test"], pythonpath=["src"]
```

`aiac` is a regular package with an empty `__init__.py`. `aiac.pdp`, `aiac.pdp.library`, `aiac.pdp.library.configuration`, `aiac.pdp.library.policy`, `aiac.pdp.service`, `aiac.pdp.service.configuration`, `aiac.pdp.service.configuration.keycloak`, `aiac.pdp.service.policy`, and `aiac.pdp.service.policy.keycloak` are regular packages with empty `__init__.py` files. Callers must use explicit submodule paths.

---

## Submodule: `aiac.pdp.library.configuration.models`

### Description
Data structures and schema library. Contains only Pydantic `BaseModel` subclasses representing generic PDP configuration entities (subjects, roles, services, scopes). No HTTP client dependency — importable by any consumer without pulling in `requests` or `python-dotenv`. Backed by Keycloak in both phases; model shapes are derived from Keycloak JSON but named generically.

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

Represents a role (Keycloak: realm role).

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
| `serviceId` | `str \| None` | `clientId` | `None` |
| `name` | `str \| None` | `name` | |
| `description` | `str \| None` | `description` | `None` |
| `enabled` | `bool` | `enabled` | |
| `type` | `Literal["Agent", "Tool"] \| None` | `attributes.type` | `None` |
| `roles` | `list[Role]` | _(roles for this client)_ | `[]` |
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
from aiac.pdp.library.configuration.models import Subject, Role, Scope, Service

raw = tool_result["content"]   # raw JSON list
subjects = [Subject.model_validate(s) for s in raw]
```

---

## Submodule: `aiac.pdp.library.configuration.api`

### Description
HTTP client library that wraps the PDP Configuration Service REST API. Provides both read and write access to PDP configuration entities (subjects, roles, services, scopes) and returns typed Pydantic model instances from `aiac.pdp.library.configuration.models`.

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

Read methods (`get_subjects`, `get_scopes`):
1. Issue `GET {AIAC_PDP_CONFIG_URL}/<endpoint>`, always appending `?realm=<self.realm>`.
2. Raise `RuntimeError` on non-2xx HTTP status.
3. Parse the response into the appropriate Pydantic model(s).

`get_services()` — enriched read (N+1 per service):
1. `GET {AIAC_PDP_CONFIG_URL}/services?realm=<self.realm>` — fetch all services.
2. For each service, issue two additional requests to populate `Service.roles` and `Service.scopes`:
   - `GET /services/{id}/roles?realm=<self.realm>` → `Service.roles`
   - `GET /services/{id}/scopes?realm=<self.realm>` → `Service.scopes`
3. Raise `RuntimeError` on any non-2xx response.
4. Return `list[Service]` with `roles` and `scopes` populated.

> **Performance note:** `get_services()` issues 2N+1 HTTP requests where N is the number of services. If this becomes a bottleneck, the service's `GET /services` endpoint should be enriched server-side instead. `Service.roles` elements are not further hydrated (their `mappedScopes` are empty); call `get_roles()` for fully hydrated role objects.

`get_roles()` — enriched read (2 extra calls per role):
1. `GET {AIAC_PDP_CONFIG_URL}/roles?realm=<self.realm>` — fetch all realm roles.
2. For each role, issue additional requests:
   - If `role.composite` is `True`: `GET /roles/{name}/composites?realm=<self.realm>` → `Role.childRoles`
   - For every role: `GET /roles/{name}/scopes?realm=<self.realm>` → `Role.mappedScopes`
3. Raise `RuntimeError` on any non-2xx response.
4. Return `list[Role]` with `childRoles` and `mappedScopes` populated.

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

Read from a `.env` file co-located with `api.py` (`aiac/src/aiac/pdp/library/configuration/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_CONFIG_URL` | `http://127.0.0.1:7071` |

### Usage

```python
from aiac.pdp.library.configuration.api import Configuration

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

## Submodule: `aiac.pdp.library.policy.models`

### Description
Data structures for PDP policy representation. Contains PDP-agnostic Pydantic `BaseModel` subclasses that decouple agent graph nodes from any specific policy backend. The PDP Policy Service translates these models internally (Keycloak composite mappings for Phase 1, Rego rules for Phase 2) — no translation logic lives in the agent.

### Dependencies
```
pydantic
```

### Pydantic models

#### `PolicyStatement`

Represents a single policy assertion. **Shape is TBD.** Constraint: must carry sufficient information for the Policy Apply sub-agent to verify entity existence via `aiac.pdp.library.configuration.api` (roles, service IDs, and scopes in Keycloak) before committing.

#### `PolicyModel`

A collection of `PolicyStatement` instances representing a complete proposed policy for a service or role. Produced by the Service Policy sub-agent and consumed by the shared Policy Apply sub-agent.

| Field | Type | Notes |
|-------|------|-------|
| `statements` | `list[PolicyStatement]` | Ordered list of policy statements |

### Usage

```python
from aiac.pdp.library.policy.models import PolicyModel, PolicyStatement
```

---

## Submodule: `aiac.pdp.library.policy.api`

### Description
HTTP client library that wraps the PDP Policy Service REST API. Abstracts the Phase 1 (Keycloak) and Phase 2 (OPA) policy backends behind a stable function interface — callers never interact with the backend directly. The active backend is determined by `AIAC_PDP_POLICY_URL`, which points to whichever policy service pod is deployed.

Handles policy operations: committing `PolicyModel` instances (both phases), composite role mappings (Phase 1), and Rego rules (Phase 2). Configuration entity operations (e.g. scope creation) belong to `aiac.pdp.library.configuration.api`.

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

    def apply_policy(self, policy: PolicyModel) -> None: ...
```

`apply_policy`:
1. Translates the PDP-agnostic `PolicyModel` into the backend-specific representation (Keycloak composite mappings or Rego rules — handled internally by the PDP Policy Service).
2. Issues the appropriate request(s) to `{AIAC_PDP_POLICY_URL}`, appending `?realm=<self.realm>`.
3. Raises `RuntimeError` on non-2xx HTTP status.

Additional policy write methods (composite role management and permissions) are defined in the PDP Policy Service component PRD.

### Configuration

Read from a `.env` file co-located with `api.py` (`aiac/src/aiac/pdp/library/policy/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_POLICY_URL` | `http://127.0.0.1:7072` |

### Usage

```python
from aiac.pdp.library.policy.api import Policy
from aiac.pdp.library.policy.models import PolicyModel

policy = Policy.for_realm("kagenti")
policy.apply_policy(policy_model)
```
