# Component PRD: Policy Model (`aiac.policy.model`)

## Problem Statement

`PolicyRule`, `AgentPolicyModel`, and `PolicyModel` were previously defined in `aiac.pdp.library.models`. Three independent consumers now need these types:

- `aiac.pdp.policy.library` — translates `PolicyModel` into HTTP calls to the PDP Policy Writer
- `aiac.policy.store.library` — reads/writes `AgentPolicyModel` from/to the Policy Store
- `aiac.policy.computation` — builds and merges `AgentPolicyModel` objects

Keeping the canonical model definitions inside a PDP-namespaced module (`aiac.pdp.library.models`) forces both the Policy Store library and the Policy Computation Engine to take a dependency on the PDP package — a wrong-layer coupling. Any of the three consumers importing from `aiac.pdp.library.models` would create a transitive dependency on an unrelated service namespace.

Additionally, the old `PolicyRule` used plain `str` for `role` and `scope`. The current SPM/APM PCE requires typed `Role` and `Scope` objects (from `aiac.idp.configuration.models`): it routes each rule to the owning service's SPM by `scope.serviceId`, classifies edges by `role.kind`, and reads `role.actorIds` when deriving each `AgentPolicyModel` — all fields that a plain `str` cannot carry. (These typed fields replaced the earlier reliance on `Configuration.get_services_by_role` / `get_services_by_scope`, which the SPM-based engine no longer calls.)

The original `source_roles`, `subject_roles`, and `scope_targets` maps used pydantic model objects (`Service`, `Subject`, `Scope`) as dict keys. Model-object keys do not round-trip through `model_dump(mode="json")` / JSON without custom key handling, and they couple `aiac.policy.model` to the id-only `__hash__`/`__eq__` of the IdP models. The outbound map was also keyed by scope (`scope → targets`), whereas consumers need the inverse (`target → scopes`) to emit per-target authorization directly.

### Order-dependence bug (the reason for the two-layer model)

The Policy Computation Engine (PCE) merges `list[PolicyRule]` from onboarding sub-agents into per-agent policy. Historically `AgentPolicyModel` (APM) was the **only persisted entity** and stored rules **denormalised onto the agent**. That made policy **order-dependent**.

Concretely, let `UR` be a user (realm) role mapped to agent `A`'s scope `AS` and tool `T`'s scope `TS`, and `AR` be agent `A`'s (client) role mapped to `TS`:

- **A onboarded, then T:** at T-onboarding `A`'s model already exists, so the `(UR, TS)` rule attaches to `A`'s outbound-subject gate. Correct.
- **T onboarded, then A:** at T-onboarding no agent targets `TS`, so `UR→TS` is **dropped**; A-onboarding never re-emits it, so **`UR→TS` is lost forever.**

The fix is a **two-layer model**: a per-service persistent source of truth (`ServicePolicyModel`) that stores every inbound edge durably on the service that owns the scope — so `UR→TS` lands on `SPM(T)` at tool-onboarding, no agent required, and can never be lost — with `AgentPolicyModel` demoted to a **pure derived projection** that is no longer persisted.

## Solution

A canonical, dependency-free model module at `aiac.policy.model` defines `ServicePolicyModel`, `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` with typed fields. No HTTP client, no service code — importable by any consumer without side effects. `PolicyRule.role` and `PolicyRule.scope` are typed `Role` and `Scope` objects from `aiac.idp.configuration.models`.

**Two-layer model.** `ServicePolicyModel` (SPM) is the **persistent source of truth**, one per **service** (agent *and* tool). It holds the service's **inbound** rules plus its own identity (owned roles and scopes). `AgentPolicyModel` (APM) becomes a **pure derived projection** built from the relevant SPMs by the PCE — it is **no longer persisted**. Its shape is unchanged so existing consumers (PDP Policy Library, Policy Store readers) keep working.

**Canonical form.** *Every rule is an inbound edge on the SPM of the service that owns the rule's scope.* An agent's outbound edge is the target's inbound edge — `AR→TS` is stored on `SPM(T)`, not on `A`. The routing key is `Scope.serviceId`: a rule `(role, scope)` routes to `SPM(scope.serviceId)`.

The relationship maps (`source_roles`, `subject_roles`, `target_scopes`) are keyed by the string `id` of the referenced entity rather than by a typed object, so they serialize to JSON natively and carry no hashability requirement into `aiac.policy.model`. Typed `Role` / `Scope` objects are retained as the map *values* (and in `PolicyRule`), preserving the typing the PCE needs for IdP queries. The outbound map is `target_scopes` (`target service id → scopes permitted`), the inverse of the former `scope_targets`.

---

## User Stories

1. As the Policy Computation Engine, I want to import `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` from a shared, neutral namespace, so that I do not take an unwanted dependency on the PDP package.
2. As the PDP Policy Library, I want to import `PolicyModel` and `AgentPolicyModel` from `aiac.policy.model`, so that my HTTP serialization logic does not duplicate model definitions.
3. As the Policy Store Library, I want to import `AgentPolicyModel` and `PolicyModel` from `aiac.policy.model`, so that response deserialization uses the same canonical types as every other consumer.
4. As an AIAC Agent sub-UC agent, I want to construct a `PolicyRule` with typed `Role` and `Scope` objects, so that the PCE can use them for IdP queries without additional type conversion.
5. As the Policy Computation Engine, I want `source_roles`, `subject_roles`, and `target_scopes` keyed by string entity IDs, so that I build them with `entity.id` and they serialize to JSON without custom key handling.
6. As a developer, I want all models to silently ignore unknown fields from API responses, so that IdP API additions do not break deserialization.
7. As the PDP Policy Library, I want outbound permissions expressed as `target service id → allowed scopes`, so that I can emit per-target authorization directly without inverting a `scope → targets` map.
8. As a consumer serializing an `AgentPolicyModel` to JSON, I want every relationship map to have string keys, so that `model_dump(mode="json")` round-trips without a custom key serializer.

---

## Implementation Decisions

### Module Identity

**Namespace:** `aiac.policy.model`

**Location:** `aiac/src/aiac/policy/model/`

**Package structure:**

```
aiac/src/aiac/policy/
└── model/
    ├── __init__.py    # empty
    └── models.py      # ServicePolicyModel, PolicyRule, AgentPolicyModel, PolicyModel
```

### Dependencies

| Dependency | Purpose |
|------------|---------|
| `pydantic` | `BaseModel`, `ConfigDict` |
| `aiac.idp.configuration.models` | Typed `Role`, `Scope`, `ServiceType` (as map values, in `PolicyRule`, and in `ServicePolicyModel`) |

No HTTP client dependency. No `requests`, no `python-dotenv`.

### Pydantic Models

All models use `model_config = ConfigDict(extra='ignore')`.

#### New `Role` / `Scope` fields (defined in `aiac.idp.configuration.models`)

The two-layer model requires ownership and a user/agent distinction on the IdP types. These fields are **defined in `aiac.idp.configuration.models`** (the deep population from Keycloak is handoff 02's concern), but the policy model depends on them for SPM routing and APM derivation:

- **`Scope.serviceId: str`** — the single owning service's `serviceId`. This is the SPM routing key: a rule `(role, scope)` routes to `SPM(scope.serviceId)`. See Assumption 2 (a scope has exactly one owner).
- **`RoleKind(str, Enum)`** — `USER = "User"`, `AGENT = "Agent"` (mirrors `ServiceType`'s style).
- **`Role.kind: RoleKind`** — whether the role is held by users or by agent service accounts.
- **`Role.actorIds: list[str]`** — context-dependent on `kind`:
  - `kind == AGENT` ⇔ a Keycloak **client role** on the agent's client; `actorIds` = the owning **agent `serviceId`(s)** (usually one).
  - `kind == USER` ⇔ a Keycloak **realm role**; `actorIds` = the **holder usernames**.

A `model_validator` on `Role` enforces what it can locally (`kind` present/valid; `actorIds` is a `list[str]`). The **cross-kind** invariant (Assumption 1) and the **client/realm ⇔ agent/user** invariant (Assumption 3) are enforced **upstream at construction** (the Keycloak IdP boundary), because the raw Keycloak facts are only visible there — see handoff 02 for that enforcement and field population.

#### `ServicePolicyModel`

The persistent source of truth — one per service (agent *and* tool), keyed by `service_id`. Holds the service's inbound rules plus its own identity.

| Field | Type | Description |
|-------|------|-------------|
| `service_id` | `str` | The owning service's id. |
| `service_type` | `ServiceType` | `Agent` or `Tool`. Drives derivation: only `Agent` services get an APM. |
| `owned_roles` | `list[Role]` | This service's own client roles (`aiac.managed` marker only). |
| `owned_scopes` | `list[Scope]` | This service's exposed scopes (`aiac.managed` marker only). |
| `inbound_rules` | `list[PolicyRule]` | Canonical: every edge granting access to `owned_scopes`. |

`owned_roles` / `owned_scopes` are the service's own identity, filtered to the `aiac.managed` marker (this is where the PCE's P2 identity now lives). They are seeded from the catalog by the PCE; this module only defines the shape. `ServicePolicyModel` round-trips through `model_dump(mode="json")` / `model_validate()` with string keys only.

#### `PolicyRule`

A single access rule pairing a typed role with a typed scope. Used in both inbound and outbound rule sets.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `Role` | Typed role from `aiac.idp.configuration.models` |
| `scope` | `Scope` | Typed scope from `aiac.idp.configuration.models` |

#### `AgentPolicyModel`

Complete policy definition for a single agent (service). Inbound and outbound rule sets are typed collections.

> **Derived, not persisted.** `AgentPolicyModel` is now a **pure derived projection** built by the PCE from the relevant `ServicePolicyModel`s. It is **no longer a persisted entity** — the durable source of truth is `ServicePolicyModel`. Its shape is **unchanged** so existing consumers (PDP Policy Library, Policy Store readers) keep working; the docstring on the model states this explicitly.

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Service ID from the AIAC trigger event (`aiac.apply.service.{id}`) |
| `agent_roles` | `list[Role]` | Realm roles assigned to this agent |
| `agent_scopes` | `list[Scope]` | Scopes this agent exposes |
| `source_roles` | `dict[str, list[Role]]` | Inbound: source (calling service) **id** → roles held. **Optional** gate input — an absent source passes. |
| `subject_roles` | `dict[str, list[Role]]` | Inbound: subject (end-user) **id** → roles held. **Mandatory** gate input. |
| `target_scopes` | `dict[str, list[Scope]]` | Outbound: target service **id** → scopes this agent may request on it |
| `inbound_rules` | `list[PolicyRule]` | Who may call this agent: `(subject_role, agent_scope)` tuples |
| `outbound_rules` | `list[PolicyRule]` | What this agent may call: `(this_agent_role, target_scope)` tuples |
| `outbound_subject_rules` | `list[PolicyRule]` | Which users may reach the agent's targets: `(user_role, tool_scope)` tuples. Defaults to `[]`. |

**Inbound rule semantics:** a subject holding realm role `role` is permitted to invoke this agent for the agent scope `scope`. The PDP Policy Writer consumes `inbound_rules` as a role → agent-scope map; its inbound gate is keyed on the subject id (mandatory), with the calling source id optional.

**Outbound rule semantics:** this agent acting as realm role `role` is permitted to request the target scope `scope`. The PDP Policy Writer consumes `outbound_rules` as an agent-role → target-scope map; its outbound gate requires both the subject and the agent to be authorized.

**Outbound subject rule semantics:** `outbound_subject_rules` holds `(user role, tool scope)` pairs — the outbound subject gate; a user holding `role` may reach a tool exposing `scope`. It is the outbound counterpart of `inbound_rules` (which pairs a user role with an *agent* scope): where `inbound_rules` answers "may this user call the agent?", `outbound_subject_rules` answers "may this user reach the tool the agent targets?". The PDP Policy Writer groups it into `subject_role_scopes` (user role → tool-scope names) and matches against `target_scopes[input.target]`, not against `agent_scopes`.

#### `PolicyModel`

A partial or full system policy model. When sent to `POST /policy` on the Policy Store, it may contain only the agents whose policies have changed.

| Field | Type |
|-------|------|
| `agents` | `list[AgentPolicyModel]` |

### Map keys are string IDs

`source_roles`, `subject_roles`, and `target_scopes` are keyed by the string `id` of the referenced Keycloak entity (source service id, subject id, target service id) rather than by the typed `Service` / `Subject` / `Scope` object. Rationale:

- JSON object keys must be strings. A dict keyed by a pydantic model does not round-trip through `model_dump(mode="json")` / JSON without a custom key serializer; a `str` key serializes natively.
- The IdP models are plain pydantic models (default field-based equality, not hashable). Consumers build these maps with `entity.id` as the key.

As a result, no field in `aiac.policy.model` uses a typed object as a dict key, and this module imports only `Role` and `Scope` from `aiac.idp.configuration.models` (as map *values* and in `PolicyRule`). `Service` and `Subject` are no longer referenced here.

### Usage

```python
from aiac.policy.model.models import PolicyRule, AgentPolicyModel, PolicyModel
from aiac.idp.configuration.models import Role, Scope

role = Role(id="r1", name="weather-reader", composite=False)
scope = Scope(id="s1", name="read")

rule = PolicyRule(role=role, scope=scope)
agent_model = AgentPolicyModel(
    agent_id="weather-agent",
    agent_roles=[role],
    agent_scopes=[scope],
    source_roles={},
    subject_roles={"u1": [role]},        # keyed by subject id
    target_scopes={"github-tool": [scope]},  # target service id → scopes
    inbound_rules=[rule],
    outbound_rules=[],
    outbound_subject_rules=[],               # (user_role, tool_scope) pairs; defaults to []
)
model = PolicyModel(agents=[agent_model])
```

### Replaces

`aiac.pdp.library.models` is deprecated. All consumers must migrate their imports to `aiac.policy.model.models`.

---

## Assumptions

The two-layer model rests on three invariants. All three are **AIAC invariants**, not Keycloak guarantees, and the ones that require raw Keycloak facts are enforced upstream at the IdP boundary (handoff 02):

1. **No role spans both kinds.** A role is held by users *or* by agent service accounts, never both. This is what lets `Role.actorIds` be a single list. Enforced upstream at construction (cross-kind invariant not visible to the local `Role` validator).
2. **No scope shared across services.** A scope has exactly one owner → a single `Scope.serviceId`. This reconciles with the existing `get_services_by_scope(scope) -> list[Service]` (plural, because Keycloak client scopes are realm-level and assignable to many clients): for AIAC-managed scopes that list is always length 1.
3. **Agent role ⇔ Keycloak client role; user role ⇔ Keycloak realm role.** The IdP config service sources agent roles from the agent's **client** roles (`Service.roles`), and `Role.kind` is populated from Keycloak's `clientRole` flag. Enforced upstream at construction.

---

## Testing Decisions

**Seam:** model instantiation and serialization — no HTTP boundary, no mocking required.

Key behaviors to assert:
- `Scope.serviceId` is present; `Role.kind` (a `RoleKind`) and `Role.actorIds` (a `list[str]`) are present; the `Role` `model_validator` accepts a valid `kind` + `list[str]` `actorIds` and rejects a malformed one.
- `ServicePolicyModel` constructs with `service_id`, `service_type`, `owned_roles`, `owned_scopes`, `inbound_rules`, and round-trips via `model_dump(mode="json")` / `model_validate()` (string keys only) with typed `Role` / `Scope` / `PolicyRule` values preserved.
- `PolicyRule` accepts typed `Role` and `Scope` objects; rejects plain `str` where `Role`/`Scope` is expected.
- `AgentPolicyModel` with string-ID keys in `source_roles`, `subject_roles`, and `target_scopes` round-trips through `model_dump(mode="json")` / `model_validate()` with the typed `Role` / `Scope` list values preserved.
- `target_scopes` maps a target service id to the list of `Scope` objects permitted on it (outbound direction is `target → scopes`, not `scope → targets`).
- `outbound_subject_rules` defaults to `[]` (constructors that omit it still validate) and round-trips through `model_dump(mode="json")` / `model_validate()` with its `(user_role, tool_scope)` `PolicyRule` values preserved.
- A relationship map keyed by a plain string serializes to a JSON object without a custom key serializer.
- `ConfigDict(extra='ignore')` causes unknown fields to be silently discarded on `model_validate()`.

---

## Out of Scope

- HTTP serialization logic — handled by `aiac.policy.store.library`, `aiac.policy.store.service`, and `aiac.pdp.policy.library`.
- IdP API integration — `Service`, `Role`, `Scope` shapes are owned by `aiac.idp.configuration.models`.
- Rule revocation semantics — TBD; no model changes required until the design is finalised.

---

## Further Notes

- Keying maps by string `id` sidesteps the previous reliance on id-only hashing of the IdP models: two records for the same Keycloak entity fetched at different times (with potentially different enrichment fields) collapse to the same string key regardless of those differences.
- `aiac/src/aiac/agent/policy/api.py` imports `PolicyRule` from `aiac.policy.model`. The `role_to_scopes` / `roles_to_scope` helpers in that file remain in place and are used by AIAC Agent sub-UC agents directly; they are not consumed by the PCE.
