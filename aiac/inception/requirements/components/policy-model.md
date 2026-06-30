# Component PRD: Policy Model (`aiac.policy.model`)

## Problem Statement

`PolicyRule`, `AgentPolicyModel`, and `PolicyModel` were previously defined in `aiac.pdp.library.models`. Three independent consumers now need these types:

- `aiac.pdp.policy.library` — translates `PolicyModel` into HTTP calls to the PDP Policy Writer
- `aiac.policy.store.library` — reads/writes `AgentPolicyModel` from/to the Policy Store
- `aiac.policy.computation` — builds and merges `AgentPolicyModel` objects

Keeping the canonical model definitions inside a PDP-namespaced module (`aiac.pdp.library.models`) forces both the Policy Store library and the Policy Computation Engine to take a dependency on the PDP package — a wrong-layer coupling. Any of the three consumers importing from `aiac.pdp.library.models` would create a transitive dependency on an unrelated service namespace.

Additionally, the old `PolicyRule` used plain `str` for `role` and `scope`. The PCE algorithm requires typed `Role` and `Scope` objects (from `aiac.idp.configuration.models`) to invoke `Configuration.get_services_by_role` and `Configuration.get_services_by_scope`.

## Solution

A canonical, dependency-free model module at `aiac.policy.model` defines `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` with typed fields. No HTTP client, no service code — importable by any consumer without side effects. `PolicyRule.role` and `PolicyRule.scope` are typed `Role` and `Scope` objects from `aiac.idp.configuration.models`.

---

## User Stories

1. As the Policy Computation Engine, I want to import `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` from a shared, neutral namespace, so that I do not take an unwanted dependency on the PDP package.
2. As the PDP Policy Library, I want to import `PolicyModel` and `AgentPolicyModel` from `aiac.policy.model`, so that my HTTP serialization logic does not duplicate model definitions.
3. As the Policy Store Library, I want to import `AgentPolicyModel` and `PolicyModel` from `aiac.policy.model`, so that response deserialization uses the same canonical types as every other consumer.
4. As an AIAC Agent sub-UC agent, I want to construct a `PolicyRule` with typed `Role` and `Scope` objects, so that the PCE can use them for IdP queries without additional type conversion.
5. As a developer, I want `Service`, `Role`, and `Scope` to be usable as dict keys, so that the PCE can build `source_roles` and `scope_targets` maps without wrapping them.
6. As a developer, I want all models to silently ignore unknown fields from API responses, so that IdP API additions do not break deserialization.

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
    └── models.py      # PolicyRule, AgentPolicyModel, PolicyModel
```

### Dependencies

| Dependency | Purpose |
|------------|---------|
| `pydantic` | `BaseModel`, `ConfigDict` |
| `aiac.idp.configuration.models` | Typed `Role`, `Scope`, `Service`, `Subject` |

No HTTP client dependency. No `requests`, no `python-dotenv`.

### Pydantic Models

All models use `model_config = ConfigDict(extra='ignore')`.

#### `PolicyRule`

A single access rule pairing a typed role with a typed scope. Used in both inbound and outbound rule sets.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `Role` | Typed role from `aiac.idp.configuration.models` |
| `scope` | `Scope` | Typed scope from `aiac.idp.configuration.models` |

#### `AgentPolicyModel`

Complete policy definition for a single agent (service). Inbound and outbound rule sets are typed collections.

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Service ID from the AIAC trigger event (`aiac.apply.service.{id}`) |
| `agent_roles` | `list[Role]` | Realm roles assigned to this agent |
| `agent_scopes` | `list[Scope]` | Scopes this agent exposes |
| `source_roles` | `dict[Service, list[Role]]` | Inbound: source service → roles granted |
| `subject_roles` | `dict[Subject, list[Role]]` | Inbound: subject (user) → roles held on behalf of which this agent acts |
| `scope_targets` | `dict[Scope, list[Service]]` | Outbound: scope → target services permitted |
| `inbound_rules` | `list[PolicyRule]` | Who may call this agent: `(caller_role, requested_scope)` tuples |
| `outbound_rules` | `list[PolicyRule]` | What this agent may call: `(this_agent_role, requested_scope)` tuples |

**Inbound rule semantics:** a caller holding realm role `role` is permitted to invoke this agent requesting scope `scope`.

**Outbound rule semantics:** this agent acting as realm role `role` is permitted to request scope `scope` on a target service.

#### `PolicyModel`

A partial or full system policy model. When sent to `POST /policy` on the Policy Store, it may contain only the agents whose policies have changed.

| Field | Type |
|-------|------|
| `agents` | `list[AgentPolicyModel]` |

### Hashability of `Service`, `Role`, `Scope`

`Service`, `Role`, `Scope`, and `Subject` (defined in `aiac.idp.configuration.models`) are used as dict keys in `AgentPolicyModel.source_roles`, `AgentPolicyModel.subject_roles`, and `AgentPolicyModel.scope_targets`. They implement custom `__hash__` and `__eq__` based on their `id` field only:

```python
# On Service, Role, Scope, Subject in aiac.idp.configuration.models:
__hash__ = lambda self: hash(self.id)
__eq__ = lambda self, other: isinstance(other, type(self)) and self.id == other.id
```

`frozen=True` is **not** used — these models have list fields (`childRoles`, `roles`, `scopes`) that must remain mutable. The `id`-only hash is the correct approach.

### Usage

```python
from aiac.policy.model.models import PolicyRule, AgentPolicyModel, PolicyModel
from aiac.idp.configuration.models import Role, Scope, Service, Subject

role = Role(id="r1", name="weather-reader", composite=False)
scope = Scope(id="s1", name="read")
subject = Subject(id="u1", username="alice", enabled=True)

rule = PolicyRule(role=role, scope=scope)
agent_model = AgentPolicyModel(
    agent_id="weather-agent",
    agent_roles=[role],
    agent_scopes=[scope],
    source_roles={},
    subject_roles={subject: [role]},
    scope_targets={},
    inbound_rules=[rule],
    outbound_rules=[],
)
model = PolicyModel(agents=[agent_model])
```

### Replaces

`aiac.pdp.library.models` is deprecated. All consumers must migrate their imports to `aiac.policy.model.models`.

---

## Testing Decisions

**Seam:** model instantiation and serialization — no HTTP boundary, no mocking required.

Key behaviors to assert:
- `PolicyRule` accepts typed `Role` and `Scope` objects; rejects plain `str` where `Role`/`Scope` is expected.
- `AgentPolicyModel` with `Service`, `Subject`, and `Scope` keys in `source_roles`, `subject_roles`, and `scope_targets` is serializable and deserializable via `model_dump()` / `model_validate()`.
- Two `Role` / `Scope` / `Service` / `Subject` instances with the same `id` are equal and hash-equal (usable as dict keys without collision).
- Two instances with different `id` values are not equal.
- `ConfigDict(extra='ignore')` causes unknown fields to be silently discarded on `model_validate()`.

---

## Out of Scope

- HTTP serialization logic — handled by `aiac.policy.store.library`, `aiac.policy.store.service`, and `aiac.pdp.policy.library`.
- IdP API integration — `Service`, `Role`, `Scope` shapes are owned by `aiac.idp.configuration.models`.
- Rule revocation semantics — TBD; no model changes required until the design is finalised.

---

## Further Notes

- The `id`-only hash is intentional: two `Role` / `Subject` / `Service` / `Scope` objects representing the same Keycloak entity but fetched at different times (with potentially different enrichment fields) must be treated as equal for dict key lookup.
- `aiac/src/aiac/agent/policy/api.py` imports `PolicyRule` from `aiac.policy.model`. The `role_to_scopes` / `roles_to_scope` helpers in that file remain in place and are used by AIAC Agent sub-UC agents directly; they are not consumed by the PCE.
