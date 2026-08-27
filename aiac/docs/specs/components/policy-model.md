# Component PRD: Policy Model (`aiac.policy.model`)

## Problem Statement

`PolicyRule`, `AgentPolicyModel`, and `PolicyModel` were previously defined in `aiac.pdp.library.models`. Three independent consumers now need these types:

- `aiac.pdp.policy.library` — translates `PolicyModel` into HTTP calls to the PDP Policy Writer
- `aiac.policy.model_store.library` — reads/writes `AgentPolicyModel` from/to the Policy Model Store
- `aiac.policy.computation` — builds and merges `AgentPolicyModel` objects

Keeping the canonical model definitions inside a PDP-namespaced module (`aiac.pdp.library.models`) forces both the Policy Model Store library and the Policy Computation Engine to take a dependency on the PDP package — a wrong-layer coupling. Any of the three consumers importing from `aiac.pdp.library.models` would create a transitive dependency on an unrelated service namespace.

Additionally, the old `PolicyRule` used plain `str` for `role` and `scope`. The current SPM/APM PCE requires typed `Role` and `Scope` objects (from `aiac.idp.configuration.models`): it routes each rule to the owning service's SPM by `scope.serviceId`, classifies edges by `role.kind`, and reads `role.actorIds` when deriving each `AgentPolicyModel` — all fields that a plain `str` cannot carry. (These typed fields replaced the earlier reliance on `Configuration.get_services_by_role` / `get_services_by_scope`, which the SPM-based engine no longer calls.)

The original `source_roles`, `subject_roles`, and `scope_targets` maps used pydantic model objects (`Service`, `Subject`, `Scope`) as dict keys. Model-object keys do not round-trip through `model_dump(mode="json")` / JSON without custom key handling, and they couple `aiac.policy.model` to the id-only `__hash__`/`__eq__` of the IdP models. The outbound map was also keyed by scope (`scope → targets`), whereas consumers need the inverse (`target → scopes`) to emit per-target authorization directly.

### Order-dependence bug (the reason for the two-layer model)

The Policy Computation Engine (PCE) merges `list[PolicyRule]` from onboarding sub-agents into per-agent policy. Historically `AgentPolicyModel` (APM) was the **only persisted entity** and stored rules **denormalised onto the agent**. That made policy **order-dependent**.

Concretely, let `UR` be a user (realm) role mapped to agent `A`'s scope `AS` and tool `T`'s scope `TS`, and `AR` be agent `A`'s (client) role mapped to `TS`:

- **A onboarded, then T:** at T-onboarding `A`'s model already exists, so the `(UR, TS)` rule attaches to `A`'s outbound-subject gate. Correct.
- **T onboarded, then A:** at T-onboarding no agent targets `TS`, so `UR→TS` is **dropped**; A-onboarding never re-emits it, so **`UR→TS` is lost forever.**

The fix is a **two-layer model**: a per-service persistent source of truth (`ServicePolicyModel`) that stores every inbound edge durably on the service that owns the scope — so `UR→TS` lands on `SPM(T)` at tool-onboarding, no agent required, and can never be lost — with `AgentPolicyModel` demoted to a **pure derived projection** that is no longer persisted.

### Allowlist-only (no negative rules)

The model can express only **grants**. A `PolicyRule(role, scope)` is always positive — "this role *may* reach this scope" — and everything not granted is implicitly unreachable (`default allow := false`). There is no way to record that a role **must not** reach a scope. Policy authors describe access in mixed terms ("developers can read source files **but must not** touch issues"), but an allowlist-only model forces the negative to be expressed as the *absence* of a grant. That is fragile: any later, broader grant (a composite role, a role update, another onboarding) silently re-opens the path the author meant to keep closed, because no durable fact records the prohibition.

## Solution

A canonical, dependency-free model module at `aiac.policy.model` defines `ServicePolicyModel`, `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` with typed fields. No HTTP client, no service code — importable by any consumer without side effects. `PolicyRule.role` and `PolicyRule.scope` are typed `Role` and `Scope` objects from `aiac.idp.configuration.models`.

**Two-layer model.** `ServicePolicyModel` (SPM) is the **persistent source of truth**, one per **service** (agent *and* tool). It holds the service's **inbound** rules plus its own identity (owned roles and scopes). `AgentPolicyModel` (APM) becomes a **pure derived projection** built from the relevant SPMs by the PCE — it is **no longer persisted**. Its shape is unchanged so existing consumers (PDP Policy Library, Policy Model Store readers) keep working.

**Canonical form.** *Every rule is an inbound edge on the SPM of the service that owns the rule's scope.* An agent's outbound edge is the target's inbound edge — `AR→TS` is stored on `SPM(T)`, not on `A`. The routing key is `Scope.serviceId`: a rule `(role, scope)` routes to `SPM(scope.serviceId)`.

The relationship maps (`source_roles`, `subject_roles`, `target_allow_scopes` / `target_deny_scopes`) are keyed by the string `id` of the referenced entity rather than by a typed object, so they serialize to JSON natively and carry no hashability requirement into `aiac.policy.model`. Typed `Role` / `Scope` objects are retained as the map *values* (and in `PolicyRule`), preserving the typing the PCE needs for IdP queries. The outbound maps are `target_allow_scopes` / `target_deny_scopes` (`target service id → scopes permitted / prohibited`), the inverse of the former `scope_targets`.

**Two-sided rules (ALLOW / DENY).** Every rule carries a `RuleEffect` — `Allow` or `Deny` — and both kinds are stored side by side as first-class facts in **explicitly separated** parallel lists (never one intermixed list). A DENY rule is a durable prohibition that **subtracts** from what the ALLOW rules grant, honored uniformly at every gate (inbound subject, inbound source, outbound subject, outbound target). Generated policy applies **deny-overrides**: a request is allowed only if some ALLOW gate passes **and** no DENY gate matches, so a later broad grant can no longer silently re-open a denied path. For now the model assumes **no conflict** — no `(role, scope)` is ever both ALLOW and DENY for the same subject — so there is **no precedence/tie-break logic**; DENY simply subtracts. Cross-role conflict resolution is a deliberate later concern (see [Out of Scope](#out-of-scope)).

**Per-policy default effect.** `AgentPolicyModel` carries a `default_effect: RuleEffect` (default `Deny`) that decides how the deployed Rego treats a `(role, scope)` pair that **no rule mentions**. Three states exist per pair: **explicitly ALLOWed** (an allow rule names it), **explicitly DENYed** (a deny rule names it), and **unspecified** (no rule names it → resolves to `default_effect`). `default_effect = Deny` reproduces today's least-privilege behavior exactly (`default allow := false`, granting only what an allow gate matches); `default_effect = Allow` opens the default while explicit denies still override via deny-overrides. The allow/deny rule lists and the effect-agnostic identity maps are emitted **identically** in both modes — only the generated decision block differs (see [`pdp-policy-writer-opa.md`](pdp-policy-writer-opa.md)). `default_effect` is itself effect-agnostic: it is **not** an allow/deny rule split.

---

## User Stories

1. As the Policy Computation Engine, I want to import `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` from a shared, neutral namespace, so that I do not take an unwanted dependency on the PDP package.
2. As the PDP Policy Library, I want to import `PolicyModel` and `AgentPolicyModel` from `aiac.policy.model`, so that my HTTP serialization logic does not duplicate model definitions.
3. As the Policy Model Store Library, I want to import `AgentPolicyModel` and `PolicyModel` from `aiac.policy.model`, so that response deserialization uses the same canonical types as every other consumer.
4. As an AIAC Agent sub-UC agent, I want to construct a `PolicyRule` with typed `Role` and `Scope` objects, so that the PCE can use them for IdP queries without additional type conversion.
5. As the Policy Computation Engine, I want `source_roles`, `subject_roles`, `target_allow_scopes`, and `target_deny_scopes` keyed by string entity IDs, so that I build them with `entity.id` and they serialize to JSON without custom key handling.
6. As a developer, I want all models to silently ignore unknown fields from API responses, so that IdP API additions do not break deserialization.
7. As the PDP Policy Library, I want outbound permissions expressed as `target service id → allowed scopes`, so that I can emit per-target authorization directly without inverting a `scope → targets` map.
8. As a consumer serializing an `AgentPolicyModel` to JSON, I want every relationship map to have string keys, so that `model_dump(mode="json")` round-trips without a custom key serializer.
9. As a policy author, I want to tag a rule `Deny` so that it records a durable prohibition, independent of the grants around it.
10. As a consumer, I want `PolicyRule.effect` to default to `Allow`, so that existing allow-only producers keep working without change.
11. As a consumer, I want ALLOW and DENY rule sets held in separate lists, so that a gate can evaluate each side without filtering an intermixed list by effect.
12. As the PDP Policy Writer, I want a role that appears **only** in DENY edges still registered into the effect-agnostic identity maps (`subject_roles` / `source_roles`), so that the Rego deny lookup can resolve it at request time.
13. As a policy author, I want to choose a policy's **default effect** (allow or deny for pairs no rule mentions), so that I can deploy a permissive-by-default policy that still honors explicit denies, without touching the allow/deny rule lists.
14. As a consumer, I want `AgentPolicyModel.default_effect` to default to `Deny`, so that existing constructions and serialized models keep today's least-privilege behavior unchanged.

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

#### `RuleEffect`

```python
class RuleEffect(str, Enum):
    ALLOW = "Allow"
    DENY = "Deny"
```

A string enum (mirroring `ServiceType` / `RoleKind`) tagging a `PolicyRule` as a **grant** (`Allow`) or a **prohibition** (`Deny`). Serializes as the string `"Allow"` / `"Deny"`.

#### `ServicePolicyModel`

The persistent source of truth — one per service (agent *and* tool), keyed by `service_id`. Holds the service's inbound rules plus its own identity.

| Field | Type | Description |
|-------|------|-------------|
| `service_id` | `str` | The owning service's id. |
| `service_type` | `ServiceType` | `Agent` or `Tool`. Drives derivation: only `Agent` services get an APM. |
| `owned_roles` | `list[Role]` | This service's own client roles (`aiac.managed` marker only). |
| `owned_scopes` | `list[Scope]` | This service's exposed scopes (`aiac.managed` marker only). |
| `inbound_allow_rules` | `list[PolicyRule]` | Canonical positive edges: every `Allow` rule granting access to `owned_scopes`. |
| `inbound_deny_rules` | `list[PolicyRule]` | Canonical negative edges: every `Deny` rule prohibiting access to `owned_scopes`. |

`inbound_rules` splits into two **explicitly separated** parallel lists — `inbound_allow_rules` + `inbound_deny_rules` — not one intermixed list filtered by `effect`. `owned_roles` / `owned_scopes` are the service's own identity, filtered to the `aiac.managed` marker (this is where the PCE's P2 identity now lives). They are seeded from the catalog by the PCE; this module only defines the shape. `ServicePolicyModel` round-trips through `model_dump(mode="json")` / `model_validate()` with string keys only.

#### `PolicyRule`

A single access rule pairing a typed role with a typed scope, tagged with an effect. Used in both inbound and outbound rule sets.

| Field | Type | Description |
|-------|------|-------------|
| `role` | `Role` | Typed role from `aiac.idp.configuration.models` |
| `scope` | `Scope` | Typed scope from `aiac.idp.configuration.models` |
| `effect` | `RuleEffect` | `Allow` (default) or `Deny`. Defaulting to `Allow` keeps existing allow-only producers working unchanged. |

**Dedup identity is `(role.id, scope.id, effect)`** (was `(role.id, scope.id)`). Including `effect` lets the same `(role, scope)` exist once as `Allow` and once as `Deny` without one clobbering the other on append. (Under the no-conflict assumption that never happens for the *same subject*, but the identity is effect-aware regardless.)

#### `AgentPolicyModel`

Complete policy definition for a single agent (service). Inbound and outbound rule sets are typed collections.

> **Derived, not persisted.** `AgentPolicyModel` is now a **pure derived projection** built by the PCE from the relevant `ServicePolicyModel`s. It is **no longer a persisted entity** — the durable source of truth is `ServicePolicyModel`. Its shape is **unchanged** so existing consumers (PDP Policy Library, Policy Model Store readers) keep working; the docstring on the model states this explicitly.

The rule lists split into **8 entity×effect lists** — {inbound subject, inbound source, outbound subject, outbound target} × {allow, deny} — plus split target maps. The identity/aggregate maps (`subject_roles`, `source_roles`, `agent_roles`, `agent_scopes`) stay **effect-agnostic**.

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Service ID from the AIAC trigger event (`aiac.apply.service.{id}`) |
| `default_effect` | `RuleEffect` | How the deployed Rego treats a `(role, scope)` pair that **no rule mentions**: `Allow` / `Deny`. Defaults to `Deny` (least-privilege — reproduces today's `default allow := false` byte-for-byte). Effect-agnostic — not an allow/deny rule split. See **Per-policy default effect** above and [`pdp-policy-writer-opa.md`](pdp-policy-writer-opa.md). |
| `agent_roles` | `list[Role]` | Realm roles assigned to this agent. **Effect-agnostic identity.** |
| `agent_scopes` | `list[Scope]` | Scopes this agent exposes. **Effect-agnostic identity.** |
| `source_roles` | `dict[str, list[Role]]` | Inbound: source (calling service) **id** → roles held. **Optional** gate input — an absent source passes. **Effect-agnostic identity** (see deny-inclusion note). |
| `subject_roles` | `dict[str, list[Role]]` | Inbound: subject (end-user) **id** → roles held. **Mandatory** gate input. **Effect-agnostic identity** (see deny-inclusion note). |
| `target_allow_scopes` | `dict[str, list[Scope]]` | Outbound: target service **id** → scopes this agent **may** request on it |
| `target_deny_scopes` | `dict[str, list[Scope]]` | Outbound: target service **id** → scopes this agent **must not** request on it |
| `inbound_subject_allow_rules` | `list[PolicyRule]` | Who may call this agent: `(subject_role, agent_scope)` `Allow` tuples |
| `inbound_subject_deny_rules` | `list[PolicyRule]` | Which subjects are barred: `(subject_role, agent_scope)` `Deny` tuples |
| `inbound_source_allow_rules` | `list[PolicyRule]` | Which calling services may call this agent: `(source_role, agent_scope)` `Allow` tuples |
| `inbound_source_deny_rules` | `list[PolicyRule]` | Which calling services are barred: `(source_role, agent_scope)` `Deny` tuples |
| `outbound_target_allow_rules` | `list[PolicyRule]` | What this agent may call: `(this_agent_role, target_scope)` `Allow` tuples |
| `outbound_target_deny_rules` | `list[PolicyRule]` | What this agent must not call: `(this_agent_role, target_scope)` `Deny` tuples |
| `outbound_subject_allow_rules` | `list[PolicyRule]` | Which users may reach the agent's targets: `(user_role, tool_scope)` `Allow` tuples. Defaults to `[]`. |
| `outbound_subject_deny_rules` | `list[PolicyRule]` | Which users are barred from the agent's targets: `(user_role, tool_scope)` `Deny` tuples. Defaults to `[]`. |

> **Effect-agnostic identity maps must include deny-edge roles.** `subject_roles` / `source_roles` (and `agent_roles` / `agent_scopes`) carry **no** allow/deny split — the split lives only in the rule lists and target maps. A role or subject that appears **only** in DENY edges **must still be registered** into `subject_roles` / `source_roles`, or the Rego deny lookup (`subject_roles[input.subject]` → deny-scope map) cannot resolve the role and the prohibition silently fails to fire.

> **`default_effect` is PCE-produced, not stored.** Because `AgentPolicyModel` is a **pure derived projection** rebuilt on every relevant recompute, `default_effect` must be set by the PCE at derive time — it is never read back from a store. The minimal design threads an optional `default_effect: RuleEffect = RuleEffect.DENY` through `compute_and_apply` → `_run` → `_derive`, so every existing call site keeps the `Deny` default and only a caller that explicitly opts in (e.g. an onboarding request) asks for `Allow`. The Policy Rules Builder is **not** involved — it never sets `default_effect`. **Caveat:** this parameter path is **not durable** — an unrelated later recompute that re-derives this agent rebuilds the APM with the `Deny` default unless that call also passes `Allow`. If the value must survive independent re-derivation, persist it on `ServicePolicyModel` (add the field there, seed from onboarding input, and have `_derive` copy it onto the APM); that is the only origin that reproduces across recomputes.

**Inbound rule semantics (deny-overrides):** a subject holding realm role `role` is permitted to invoke this agent for the agent scope `scope` iff an `inbound_subject_allow` edge grants it **and no** `inbound_subject_deny` edge prohibits it; the same allow-and-not-deny logic applies to the source gate. The PDP Policy Writer consumes the allow/deny lists as separate role → agent-scope maps; its inbound gate is keyed on the subject id (mandatory), with the calling source id optional.

**Outbound target rule semantics (deny-overrides):** this agent acting as realm role `role` is permitted to request the target scope `scope` iff an `outbound_target_allow` edge grants it **and no** `outbound_target_deny` edge prohibits it. The PDP Policy Writer consumes the allow/deny lists as separate agent-role → target-scope maps; its outbound gate requires both the subject and the agent to be authorized and neither to be denied.

**Outbound subject rule semantics (deny-overrides):** the outbound subject gate pairs `(user role, tool scope)` — a user holding `role` may reach a tool exposing `scope` iff an `outbound_subject_allow` edge grants it and no `outbound_subject_deny` edge prohibits it. It is the outbound counterpart of the inbound subject rules (which pair a user role with an *agent* scope): where those answer "may this user call the agent?", these answer "may this user reach the tool the agent targets?". The PDP Policy Writer groups them into `subject_role_allow_scopes` / `subject_role_deny_scopes` (user role → tool-scope names) and matches against `target_allow_scopes[input.target]` / `target_deny_scopes[input.target]`, not against `agent_scopes`.

#### `PolicyModel`

A partial or full system policy model. When sent to `POST /policy` on the Policy Model Store, it may contain only the agents whose policies have changed.

| Field | Type |
|-------|------|
| `agents` | `list[AgentPolicyModel]` |

### Map keys are string IDs

`source_roles`, `subject_roles`, `target_allow_scopes`, and `target_deny_scopes` are keyed by the string `id` of the referenced Keycloak entity (source service id, subject id, target service id) rather than by the typed `Service` / `Subject` / `Scope` object. Rationale:

- JSON object keys must be strings. A dict keyed by a pydantic model does not round-trip through `model_dump(mode="json")` / JSON without a custom key serializer; a `str` key serializes natively.
- The IdP models are plain pydantic models (default field-based equality, not hashable). Consumers build these maps with `entity.id` as the key.

As a result, no field in `aiac.policy.model` uses a typed object as a dict key, and this module imports only `Role` and `Scope` from `aiac.idp.configuration.models` (as map *values* and in `PolicyRule`). `Service` and `Subject` are no longer referenced here.

### Usage

```python
from aiac.policy.model.models import PolicyRule, RuleEffect, AgentPolicyModel, PolicyModel
from aiac.idp.configuration.models import Role, Scope

reader = Role(id="r1", name="weather-reader", composite=False)
issues_role = Role(id="r2", name="developer", composite=False)
read = Scope(id="s1", name="read")
issues = Scope(id="s2", name="issues")

allow_rule = PolicyRule(role=reader, scope=read)                       # effect defaults to Allow
deny_rule = PolicyRule(role=issues_role, scope=issues, effect=RuleEffect.DENY)

agent_model = AgentPolicyModel(
    agent_id="weather-agent",
    agent_roles=[reader],
    agent_scopes=[read],
    source_roles={},
    subject_roles={"u1": [reader], "u2": [issues_role]},   # keyed by subject id; deny-only role u2 still listed
    target_allow_scopes={"github-tool": [read]},           # target service id → allowed scopes
    target_deny_scopes={"github-tool": [issues]},          # target service id → prohibited scopes
    inbound_subject_allow_rules=[allow_rule],
    inbound_subject_deny_rules=[],
    inbound_source_allow_rules=[],
    inbound_source_deny_rules=[],
    outbound_target_allow_rules=[],
    outbound_target_deny_rules=[deny_rule],
    outbound_subject_allow_rules=[],                       # (user_role, tool_scope) pairs; defaults to []
    outbound_subject_deny_rules=[],                        # defaults to []
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
- `RuleEffect` values serialize as `"Allow"` / `"Deny"`; `PolicyRule.effect` defaults to `RuleEffect.ALLOW` when omitted.
- `AgentPolicyModel.default_effect` defaults to `RuleEffect.DENY` when omitted (existing constructions and serialized models unchanged), accepts `RuleEffect.ALLOW`, and round-trips as `"Deny"` / `"Allow"`.
- The same `(role, scope)` coexists as one `Allow` and one `Deny` rule (dedup identity `(role.id, scope.id, effect)` keeps them distinct).
- `ServicePolicyModel` constructs with `service_id`, `service_type`, `owned_roles`, `owned_scopes`, `inbound_allow_rules`, `inbound_deny_rules`, and round-trips via `model_dump(mode="json")` / `model_validate()` (string keys only) with typed `Role` / `Scope` / `PolicyRule` values preserved.
- `PolicyRule` accepts typed `Role` and `Scope` objects; rejects plain `str` where `Role`/`Scope` is expected.
- `AgentPolicyModel` with string-ID keys in `source_roles`, `subject_roles`, `target_allow_scopes`, and `target_deny_scopes`, and the 8 entity×effect rule lists populated, round-trips through `model_dump(mode="json")` / `model_validate()` with the typed values preserved.
- `target_allow_scopes` / `target_deny_scopes` each map a target service id to the list of `Scope` objects permitted / prohibited on it (outbound direction is `target → scopes`, not `scope → targets`).
- **Deny-edge role registered in identity maps:** a role appearing **only** in a DENY rule is still present in the `subject_roles` / `source_roles` value list for its subject/source — assert the identity map is effect-agnostic and complete.
- The 8 rule lists and both target maps default to empty (constructors that omit them still validate) and round-trip with their `PolicyRule` / `Scope` values preserved.
- A relationship map keyed by a plain string serializes to a JSON object without a custom key serializer.
- `ConfigDict(extra='ignore')` causes unknown fields to be silently discarded on `model_validate()` (this is exactly why the rename requires a store reset — see Migration).

---

## Out of Scope

- HTTP serialization logic — handled by `aiac.policy.model_store.library`, `aiac.policy.model_store.service`, and `aiac.pdp.policy.library`.
- IdP API integration — `Service`, `Role`, `Scope` shapes are owned by `aiac.idp.configuration.models`.
- Rule revocation semantics — TBD; no model changes required until the design is finalised.
- **PRB deny-extraction** — pulling `Deny` rules out of natural-language policy text is the Policy Rules Builder's concern, not this model module's; this module only defines the `effect` field the PRB populates. (The PRB now extracts `Deny` rules — explicit prohibitions, description-driven denies, and the derived exclusivity complement — see [`aiac-agent/policy-rules-builder.md`](aiac-agent/policy-rules-builder.md).)
- **Conflict / precedence resolution** — the model assumes no `(role, scope)` is both `Allow` and `Deny` for the same subject, so there is no tie-break. Cross-role ALLOW-vs-DENY conflict resolution is an explicit later concern.

---

## Migration (state reset, no back-compat)

**Symmetric rename, no alias, no dual-read shim, no record migration.** `inbound_rules` → `inbound_allow_rules` + `inbound_deny_rules` (SPM) and the APM rule-list/target-map renames are hard renames. Because every model uses `ConfigDict(extra='ignore')`, loading an old single-list record would **silently drop** the now-unknown `inbound_rules` field and yield a stale, half-migrated read. So the Policy Model Store state is **nuked out-of-band and re-seeded by re-onboarding** — there is no alias, no dual-read compatibility path, and no migration of old records. All generated `.rego` golden fixtures are regenerated as part of the rollout.

## Further Notes

- Keying maps by string `id` sidesteps the previous reliance on id-only hashing of the IdP models: two records for the same Keycloak entity fetched at different times (with potentially different enrichment fields) collapse to the same string key regardless of those differences.
- The `effect` split lives **only** in the rule lists (`*_allow_rules` / `*_deny_rules`) and the target maps (`target_allow_scopes` / `target_deny_scopes`). The identity/aggregate maps (`subject_roles`, `source_roles`, `agent_roles`, `agent_scopes`) remain effect-agnostic and must include deny-edge roles so the Rego deny lookup can resolve them.
