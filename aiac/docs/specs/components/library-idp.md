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

`Service`, `Role`, `Scope`, and `Subject` use pydantic's default equality (field-based) and are **not hashable** — they define no custom `__hash__`/`__eq__` and are never used as dict keys or set members. The relationship maps in `AgentPolicyModel` (`source_roles`, `subject_roles`, and the split target maps `target_allow_scopes` / `target_deny_scopes`) are keyed by the entity's string `id` instead, so no identity override is needed.

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

Represents a role. Per Assumption 3 (policy-model spec), **user** roles are Keycloak realm roles and **agent** roles are Keycloak client roles on the agent's client; `kind` (a `RoleKind`) carries that distinction and is populated from Keycloak's `clientRole` flag at the IdP boundary.

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `id` | `str` | `id` | |
| `name` | `str` | `name` | |
| `description` | `str \| None` | `description` | |
| `composite` | `bool` | `composite` | |
| `childRoles` | `list[Role]` | `composites.realm` | `[]` |
| `attributes` | `dict[str, Any]` | `attributes` | `{}` |
| `kind` | `RoleKind` | `clientRole` flag (user = realm role, agent = client role; resolved at the IdP boundary) | |
| `actorIds` | `list[str]` | _(member **usernames** of a user-kind (realm) role, or the owning agent's `serviceId`(s) for an agent-kind (client) role; populated service-side, user-kind values aligned with `get_subjects_by_role`, handoff 02)_ | `[]` |

> **Field pass-through (handoff 03 audit).** `kind`, `actorIds`, and `Scope.serviceId` are declared
> on the models by **handoff 01** and populated by the **IdP service** (handoff 02). The
> `Configuration` library deserializes them **automatically** — `model_validate` picks up any declared
> field present in the service JSON, and there is no hand-rolled field mapping that would omit them.
> `kind` is **authoritative**: the library never re-derives a role's user/agent kind client-side
> (e.g. by calling `get_services_by_role`). See the api-submodule audit note below.

Roles also expose an `aiac_managed` property (`bool`): `True` when `attributes` carries the AIAC provisioning marker `aiac.managed` (realm-role attribute values are lists, so the marker appears as `["true"]`). See the naming convention in the idp-configuration-service spec.

> **SPM/APM (service-side).** `Role.actorIds` is populated per kind at the IdP boundary: for an **agent** (client) role, the owning client's `serviceId`(s) (resolved from the role's `containerId`); for a **user** (realm) role, the role's member usernames (aligned with `get_subjects_by_role`). The IdP service also fails loud on cross-kind roles (Assumption 1) and multi-owner AIAC-managed scopes (Assumption 2). See "Agent roles are client roles, field population, and assumption enforcement" in idp-configuration-service.md.

#### `Service`

Represents a service (Keycloak: `client`).

| Field | Type | Keycloak field | Default |
|-------|------|----------------|---------|
| `id` | `str` | `id` | |
| `serviceId` | `str \| None` | `clientId` | `None` |
| `name` | `str \| None` | `name` | |
| `description` | `str \| None` | `description` | `None` |
| `enabled` | `bool` | `enabled` | |
| `type` | `ServiceType \| None` | `attributes.client.type` | `None` |
| `roles` | `list[Role]` | _(roles for this client)_ | `[]` |
| `scopes` | `list[Scope]` | _(default client scopes)_ | `[]` |

**Service type resolution** (`Service._resolve_keycloak_fields`, a `model_validator(mode="before")`). AIAC calls the concept "service type" everywhere; the backing Keycloak client attribute is named **`client.type`**. Resolution precedence:

1. An explicit `type` already present on the input wins (never overridden).
2. Otherwise the Keycloak client attribute **`client.type`** ∈ {`Agent`, `Tool`} — a **plain string**. Client attribute values are plain strings; a **list** value (e.g. `["Agent"]`, the shape realm-role attributes use) fails the check and resolves to `None`. Capitalization matches the `ServiceType` values.
3. Otherwise `None`.

The attribute is set via `Configuration.set_service_type` (below); its authoritative origin is UC1 Service Onboarding — `classify_service` **discovers** the type from the operator's `rossoctl.io/type` label and `provision_service` **persists** it onto the client via `set_service_type` (see the aiac-agent UC1 spec). There is **no** `spiffe://` clientId fallback and **no** description-keyword inference — typing is `client.type`-attribute-only. (The former `spiffe:// ⇒ Agent` fallback was **removed**: a `spiffe://` clientId indicates a SPIRE-enabled workload, **not** necessarily an agent — it could mis-type a SPIRE-enabled tool — so clients without a `client.type` attribute now resolve to `None`.)

> **`ServiceType`** (`aiac.idp.configuration.models`) is a `str` enum — `AGENT = "Agent"`, `TOOL = "Tool"` — shared by `Service.type`, `set_service_type`, and the aiac-agent sub-agents (one vocabulary, no duplication). Values are capitalized to match the `client.type` attribute; because it subclasses `str`, `ServiceType.AGENT == "Agent"`, so it is a drop-in for the former `Literal["Agent", "Tool"]`. The operator's lowercase `rossoctl.io/type` pod label is normalized to a member via `ServiceType(label.capitalize())` in UC1 `classify_service`.

#### `Scope`

Represents a service scope (Keycloak: `client scope`).

| Field | Type | Keycloak field |
|-------|------|----------------|
| `id` | `str` | `id` |
| `name` | `str` | `name` |
| `description` | `str \| None` | `description` |
| `attributes` | `dict[str, Any]` | `attributes` |
| `serviceId` | `str \| None` | _(the `serviceId`/`clientId` of the service that exposes this scope; declared by handoff 01, populated service-side by handoff 02)_ |

> **Field pass-through (handoff 03 audit).** `Scope.serviceId` round-trips through the library's
> deserialization automatically (same `model_validate` pass-through as `Role.kind`/`actorIds`). It makes
> a scope's exposing service an explicit field read rather than something the client must infer.

Scopes also expose an `aiac_managed` property (`bool`): `True` when `attributes` carries the AIAC provisioning marker `aiac.managed` (client-scope attribute values are plain strings, so the marker appears as `"true"`). See the naming convention in the idp-configuration-service spec.

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

> **Handoff 03 audit outcome (amended).** Most read paths surface `Role.kind` / `Role.actorIds` /
> `Scope.serviceId` faithfully as a thin `model_validate` pass-through with no code change, and the P1
> client-side filter in `get_services_by_role` / `get_services_by_scope` was already an `id`-membership
> read (not an inference) needing no simplification. **Exception — `_build_service` (nested
> service roles/scopes) is _not_ a plain pass-through and required a fix:** it looked service roles up
> in the `all_roles` map (built from `GET /roles`, where every role is `kind = User`), discarding the
> per-service endpoint's authoritative `kind = Agent` / `actorIds`, and it never stamped nested
> `Scope.serviceId`. It now merges the endpoint's `kind`/`actorIds` and stamps `serviceId` (see
> `get_services()` below). No client-side *re-derivation* of role kind is introduced — the merged
> `kind` still comes from the service, which remains authoritative. `get_subjects_by_role` stays
> consistent with a role's `actorIds` (both from the same service-side source). See the per-method
> audit notes below.

**Transport retries.** Every HTTP call is issued through a private `_request(method, path, **kwargs)` helper that wraps the request in the project-level `run_upstream` retry primitive (`aiac.shared.upstream`): transient failures are retried up to `UPSTREAM_MAX_RETRIES` times (default `3`) with exponential backoff before a non-2xx status is raised as `RuntimeError`. Retry lives inside the library (not in callers), and applies at the leaf request, so composite methods (`create_service_role` / `create_service_scope`) retry each sub-request without compounding.

### Dependencies
```
requests
pydantic
python-dotenv
tenacity
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

    # Idempotent create-or-get (by name) + map to the service. Accept any object exposing
    # .name / .description (e.g. the aiac-agent RoleDefinition / ScopeDefinition), so the
    # library never imports the agent layer.
    def create_service_role(self, service_id: str, role) -> Role: ...
    def create_service_scope(self, service_id: str, scope) -> Scope: ...

    def set_service_type(self, service: Service, service_type: ServiceType) -> Service: ...

    # Mint a bearer token, minted as the service's own client, whose `aud` contains that
    # client's client-id — for authenticating UC-1 tool discovery against the tool's
    # AuthBridge sidecar. Returns the raw access_token string.
    def mint_discovery_token(self, service_id: str) -> str: ...
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
   - `GET /services/{id}/roles?realm=<self.realm>` → for each returned role, **copy the matching
     `all_roles` object and merge its authoritative `kind`/`actorIds` onto the copy** — the merge is a
     fresh dict (`{**base.model_dump(), **authoritative_fields}`), so the shared `all_roles` entry is
     never mutated and cannot leak one service's agent-context values into another's view → `Service.roles`.
     The copy carries the full representation (`composite`/`childRoles`/`attributes`). Merging (not a
     plain `all_roles` lookup) is required: the per-service endpoint is the only source of the
     agent-context `kind = Agent` / `actorIds = [serviceId]`; taking the role from `all_roles` alone
     would leave it at the realm-level `kind = User` / `actorIds = [members]`.
   - `GET /services/{id}/scopes?realm=<self.realm>` → filter `all_scopes` map → `Service.scopes`, and
     **stamp each scope's `serviceId` = this service's `clientId`** (the owning client — the PCE's SPM
     routing key; `all_scopes` from `GET /scopes` carries no owner).
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

`mint_discovery_token(service_id)` — thin wrapper over the config service's minting endpoint:
1. `GET {AIAC_PDP_CONFIG_URL}/services/{service_id}/discovery-token?realm=<self.realm>`.
2. Raise `RuntimeError` on non-2xx HTTP status.
3. Return `response.json()["access_token"]` — the raw bearer token string. The config service (which
   holds the Keycloak admin) does the minting and the in-endpoint `iss`/`aud` verification; this method
   does no decoding itself.

`get_roles()` — enriched read:
1. `GET {AIAC_PDP_CONFIG_URL}/roles?realm=<self.realm>` — fetch all realm roles.
2. For each role, if `role.composite` is `True`: `GET /roles/{name}/composites?realm=<self.realm>` → `Role.childRoles`
3. Raise `RuntimeError` on any non-2xx response.
4. Return `list[Role]` with `childRoles` populated.

`get_services_by_role(role: Role) -> list[Service]`:
1. Fetches the fully-enriched service list via `get_services()` and filters it **client-side**: returns those services whose `.roles` contains a role with `role.id`. The server `GET /services` endpoint has no `role_id` filter, so filtering happens in the library.
2. Returns an empty list when no service owns the role (e.g. a realm-level role).
3. Raises `RuntimeError` on any underlying non-2xx (propagated from `get_services()` / `_build_service()`).

`get_services_by_scope(scope: Scope) -> list[Service]`:
1. Fetches the fully-enriched service list via `get_services()` and filters it **client-side**: returns those services whose `.scopes` contains a scope with `scope.id`. The server `GET /services` endpoint has no `scope_id` filter, so filtering happens in the library.
2. Returns an empty list when no service exposes the scope.
3. Raises `RuntimeError` on any underlying non-2xx (propagated from `get_services()` / `_build_service()`).

> **Performance note:** because both methods delegate to `get_services()`, each call inherits its full fan-out cost (see the `get_services()` performance note above — `2N + 1 + roles` HTTP requests for N services). Acceptable for the low-frequency PCE resolution path. If it becomes a bottleneck, the right fix is a real server-side `role_id` / `scope_id` filter on `GET /services`.

> **P1 client-side filter — handoff 03 audit (no change required).** With ownership now explicit on
> the entities (`Role.kind`/`actorIds`, `Scope.serviceId`), the P1 client-side filter was audited for
> possible simplification. **Outcome: the filter is retained as-is.** `get_services_by_role` /
> `get_services_by_scope` already select owners/exposers by **`id`-membership** in each service's
> enriched `.roles` / `.scopes` lists — that is a direct field read, **not an inference**, so there is
> nothing to simplify away. Membership in the service's `.scopes` remains the authoritative
> owner/exposer signal (consistent with the new `Scope.serviceId`); the two agree because both derive
> from the service-side truth. The filter still returns **only** genuine owners/exposers and returns
> `[]` for realm-level roles that no service owns. No client-side re-derivation of role kind is
> introduced by these methods; `get_services_by_role` is **retained as a method** for API completeness
> and ad-hoc callers, but the current SPM-based PCE no longer calls it: its **only** runtime IdP read is
> `Configuration.get_services()` (see `aiac.policy.computation`), and owner/role lookups against stored
> policy go through the Policy Model Store's `get_service_policies_by_role`, not this IdP filter.

`get_subjects_by_role(role: Role) -> list[Subject]`:
1. `GET {AIAC_PDP_CONFIG_URL}/subjects?role_id={role.id}&realm=<self.realm>`
2. Returns all subjects (users) that have this role directly assigned, enriched with their full realm role assignments (same enrichment as `get_subjects()`).
3. Raises `RuntimeError` on non-2xx. Returns an empty list when no subject holds the role.

> **Note:** This method returns only subjects with a **direct** assignment of the given role. Composite role traversal (resolving `childRoles` and querying each) is the caller's responsibility — see PCE algorithm in `aiac.policy.computation`.

> **`actorIds` consistency — handoff 03 audit.** The subject/username set this method reports is the
> same set the **IdP service** uses to populate a user-kind role's `Role.actorIds` (handoff 02). The
> library surfaces both from the same service-side source, so there is **no divergence** between the
> `actorIds` carried on a returned `Role` and the subjects `get_subjects_by_role(role)` reports. The
> library does not recompute `actorIds` client-side.

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

`create_service_role(service_id: str, role) -> Role`: idempotent create-or-get + map.
1. `get_roles()` and reuse an existing realm role whose `name == role.name`; otherwise `create_role(role.name, role.description)`.
2. `map_role_to_service(get_service(service_id), resolved_role)` (itself idempotent).
3. Raises `RuntimeError` on any underlying non-2xx HTTP status. Returns the resolved `Role`.
4. `role` is any object exposing `.name` / `.description` (e.g. the aiac-agent `RoleDefinition`); the library does not import the agent layer.

`create_service_scope(service_id: str, scope) -> Scope`: idempotent create-or-get + map.
1. `get_scopes()` and reuse an existing client scope whose `name == scope.name`; otherwise `create_scope(scope.name, scope.description)`.
2. `map_scope_to_service(get_service(service_id), resolved_scope)` (itself idempotent).
3. Raises `RuntimeError` on any underlying non-2xx HTTP status. Returns the resolved `Scope`.
4. `scope` is any object exposing `.name` / `.description` (e.g. the aiac-agent `ScopeDefinition`).

`set_service_type(service: Service, service_type: ServiceType) -> Service`:
1. Issues `POST {AIAC_PDP_CONFIG_URL}/services/{service.id}/type` with body `{"type": <value>}` (the `ServiceType`'s `Agent`/`Tool` value; a bare `"Agent"`/`"Tool"` string is accepted too since `ServiceType` is a `str` enum), appending `?realm=<self.realm>`.
2. The service persists the value onto the Keycloak client as the **`client.type`** attribute (a plain string, capitalized). The Keycloak attribute name is an IdP-Service/mapping-layer detail — callers pass the generic `service_type` and never see it.
3. Raises `RuntimeError` on non-2xx HTTP status.
4. Returns the updated `Service` instance parsed from the response (`type` now resolved from the new attribute).

### Configuration

Read from a `.env` file co-located with `api.py` (`aiac/src/aiac/idp/configuration/.env`) via `python-dotenv`. Falls back to the default if the file is absent or the key is not set.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_CONFIG_URL` | `http://127.0.0.1:7071` |

> **TBD:** whether `AIAC_PDP_CONFIG_URL` should be renamed to `AIAC_IDP_CONFIG_URL`. Not yet decided — keep `AIAC_PDP_CONFIG_URL` until this is resolved.

### Usage

```python
from aiac.idp.configuration.api import Configuration

cfg = Configuration.for_realm("rossoctl")
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
