# Component PRD: IdP Configuration Library (`aiac.idp.configuration`)

## Location
`aiac/src/aiac/idp/configuration/`

## Package structure

```
aiac/src/aiac/idp/
└── configuration/
    ├── __init__.py     # empty
    ├── models.py       # Subject, Role, Service, Scope
    └── api.py          # Configuration class — reads + writes IdP entities
```

All `__init__.py` files are empty. Callers use explicit submodule paths:

```python
from aiac.idp.configuration.models import Subject, Role, Scope, Service
from aiac.idp.configuration.api import Configuration
```

---

## Submodule: `aiac.idp.configuration.models`

### Description
Dependency-free Pydantic `BaseModel` subclasses representing generic IdP configuration entities (subjects, roles, services, scopes). No HTTP client dependency — importable by any consumer without pulling in `requests` or `python-dotenv`. Model shapes are derived from Keycloak JSON but named generically.

### Dependencies
```
pydantic
```

### Pydantic models

All models use `model_config = ConfigDict(extra='ignore')` to silently discard unknown fields.

Model definition order: `Subject` → `Role` → `Service` → `Scope`. Because `Subject`, `Role`, and `Service` reference `Scope` (and `Subject` references `Role`) as forward references, the module calls `Subject.model_rebuild()`, `Role.model_rebuild()`, and `Service.model_rebuild()` after `Scope` is defined.

`Service`, `Role`, `Scope`, and `Subject` use pydantic's default equality (field-based) and are **not hashable** — they define no custom `__hash__`/`__eq__` and are never used as dict keys or set members. The relationship maps in `AgentPolicyModel` (`source_roles`, `subject_roles`, `target_scopes`) are keyed by the entity's string `id` instead, so no identity override is needed.

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
| `roles` | `list[Role]` | _(populated by `Configuration.get_subjects()` from `GET /subjects/{id}/assignments` → `realmMappings`; not present in the raw Keycloak user object)_ | `[]` |

#### `Role`

Represents a role (Keycloak: realm role).

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `id` | `str` | `id` | |
| `name` | `str` | `name` | |
| `description` | `str \| None` | `description` | |
| `composite` | `bool` | `composite` | |
| `childRoles` | `list[Role]` | `composites.realm` | `[]` |

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
from aiac.idp.configuration.models import Subject, Role, Scope, Service

raw = tool_result["content"]   # raw JSON list
subjects = [Subject.model_validate(s) for s in raw]
```

---

## Submodule: `aiac.idp.configuration.api`

### Description
HTTP client library that wraps the IdP Configuration Service REST API. Provides read and write access to IdP configuration entities (subjects, roles, services, scopes) and returns typed Pydantic model instances from `aiac.idp.configuration.models`.

All Keycloak interactions are consolidated here; the PDP Policy Writer (OPA) does not touch Keycloak directly.

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
    def get_service(self, service_id: str) -> Service: ...
    def get_scopes(self) -> list[Scope]: ...

    def get_services_by_role(self, role: Role) -> list[Service]: ...
    def get_services_by_scope(self, scope: Scope) -> list[Service]: ...
    def get_subjects_by_role(self, role: Role) -> list[Subject]: ...

    def create_scope(self, scope_name: str, scope_description: str) -> Scope: ...
    def map_scope_to_service(self, service: Service, scope: Scope) -> Service: ...

    def create_role(self, role_name: str, role_description: str) -> Role: ...
    def map_role_to_service(self, service: Service, role: Role) -> Service: ...
```

`get_scopes()` — simple read:
1. Issue `GET {AIAC_PDP_CONFIG_URL}/scopes`, always appending `?realm=<self.realm>`.
2. Raise `RuntimeError` on non-2xx HTTP status.
3. Parse the response into `list[Scope]` and return.

`get_subjects()` — enriched with per-subject realm role assignments:
1. `GET {AIAC_PDP_CONFIG_URL}/subjects?realm=<self.realm>` — fetch the base user list. Keycloak does not include role assignments in the user representation.
2. Call `_all_roles_map()` once to build a `{id: Role}` lookup (fully hydrated via `get_roles()`).
3. For each subject, delegate to `_build_subject(raw, all_roles)` which issues `GET /subjects/{id}/assignments?realm=<self.realm>`, extracts `realmMappings` role IDs, filters the roles map, and returns a validated `Subject` with `roles` populated.
4. Raise `RuntimeError` on any non-2xx HTTP status (primary or secondary calls).

`get_services()` — fully-enriched read:
1. `GET {AIAC_PDP_CONFIG_URL}/services?realm=<self.realm>` — fetch the base service list.
2. Call `get_roles()` and `get_scopes()` once upfront to build `{id: Role}` and `{id: Scope}` lookup maps.
3. For each service, delegate to `_build_service(raw, all_roles, all_scopes)` which issues:
   - `GET /services/{id}/roles?realm=<self.realm>` → filter `all_roles` map → `Service.roles`
   - `GET /services/{id}/scopes?realm=<self.realm>` → filter `all_scopes` map → `Service.scopes`
4. Raise `RuntimeError` on any non-2xx response.
5. Return `list[Service]` with fully-enriched `roles` (including `childRoles`) and `scopes` (including `description`).

> **Performance note:** `get_services()` issues 2N + 1 + (roles overhead) HTTP requests where N is the number of services. `get_roles()` is called once and its fully-enriched objects are shared across all services. If this becomes a bottleneck, enrichment should be moved server-side.

`get_service(service_id)` — fetch a single service with the same full enrichment:
1. `GET {AIAC_PDP_CONFIG_URL}/services/{service_id}?realm=<self.realm>` — fetch the single service.
2. Call `get_roles()` and `get_scopes()` to build lookup maps (same as `get_services()`).
3. Delegate to `_build_service(raw, all_roles, all_scopes)`.
4. Raise `RuntimeError` on any non-2xx response.
5. Return a single enriched `Service`.

> **Note:** Callers that previously called `get_services()` and filtered by ID should be switched to `get_service(service_id)` to avoid fetching the full list.

`get_roles()` — enriched read:
1. `GET {AIAC_PDP_CONFIG_URL}/roles?realm=<self.realm>` — fetch all realm roles.
2. For each role, if `role.composite` is `True`: `GET /roles/{name}/composites?realm=<self.realm>` → `Role.childRoles`
3. Raise `RuntimeError` on any non-2xx response.
4. Return `list[Role]` with `childRoles` populated.

`get_services_by_role(role: Role) -> list[Service]`:
1. `GET {AIAC_PDP_CONFIG_URL}/services?role_id={role.id}&realm=<self.realm>`
2. Returns all services that have this role mapped to them.
3. Raises `RuntimeError` on non-2xx. Returns an empty list when no service owns the role (realm-level role).

`get_services_by_scope(scope: Scope) -> list[Service]`:
1. `GET {AIAC_PDP_CONFIG_URL}/services?scope_id={scope.id}&realm=<self.realm>`
2. Returns all services that expose this scope.
3. Raises `RuntimeError` on non-2xx. Returns an empty list when no service exposes the scope.

`get_subjects_by_role(role: Role) -> list[Subject]`:
1. `GET {AIAC_PDP_CONFIG_URL}/subjects?role_id={role.id}&realm=<self.realm>`
2. Returns all subjects (users) that have this role directly assigned, enriched with their full realm role assignments (same enrichment as `get_subjects()`).
3. Raises `RuntimeError` on non-2xx. Returns an empty list when no subject holds the role.

> **Note:** This method returns only subjects with a **direct** assignment of the given role. Composite role traversal (resolving `childRoles` and querying each) is the caller's responsibility — see PCE algorithm in `aiac.policy.computation`.

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

Read from a `.env` file co-located with `api.py` (`aiac/src/aiac/idp/configuration/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_CONFIG_URL` | `http://127.0.0.1:7071` |

> **TBD:** whether `AIAC_PDP_CONFIG_URL` should be renamed to `AIAC_IDP_CONFIG_URL`. Not yet decided — keep `AIAC_PDP_CONFIG_URL` until this is resolved.

### Usage

```python
from aiac.idp.configuration.api import Configuration

cfg = Configuration.for_realm("kagenti")
subjects = cfg.get_subjects()
for s in subjects:
    print(s.username, s.email)

scope = cfg.create_scope(scope_name="read", scope_description="Read access")
service = cfg.get_service("abc123")  # preferred over get_services() + filter
updated_service = cfg.map_scope_to_service(service, scope)

role = cfg.create_role(role_name="reader", role_description="Read-only access")
updated_service = cfg.map_role_to_service(updated_service, role)

# PCE usage — resolve services owning a given role or scope
services_with_role = cfg.get_services_by_role(role)
services_with_scope = cfg.get_services_by_scope(scope)
```
