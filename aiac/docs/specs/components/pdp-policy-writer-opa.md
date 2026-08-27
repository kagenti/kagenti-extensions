# Component PRD: PDP Policy Writer (OPA)

## Location
`aiac/src/aiac/pdp/service/policy/opa/`

## Description
A FastAPI web service that translates a **Policy Model** into OPA Rego packages and, for each agent, **server-side-applies** the two generated packages into a per-agent `AuthorizationPolicy` Kubernetes Custom Resource (`agent.rossoctl.dev/v1alpha1`, `scope: client` — one CR per agent). The `bundle-service` (operator repo) composes those per-agent CRs into per-pod OPA bundles; the OPA plugin embedded in each AuthBridge instance polls the bundle relevant to its pod and evaluates it.
A FastAPI web service that translates a **Policy Model** into OPA Rego packages and, for each agent, **server-side-applies** the two generated packages into a per-agent `AuthorizationPolicy` Kubernetes Custom Resource (`agent.rossoctl.dev/v1alpha1`, `scope: client` — one CR per agent). The `bundle-service` (operator repo) composes those per-agent CRs into per-pod OPA bundles; the OPA plugin embedded in each AuthBridge instance polls the bundle relevant to its pod and evaluates it.

The service is deployed as a container in the **Rossoctl Interface Pod** alongside the IdP Configuration Service, behind the `aiac-pdp-policy-service:7072` ClusterIP.
The service is deployed as a container in the **Rossoctl Interface Pod** alongside the IdP Configuration Service, behind the `aiac-pdp-policy-service:7072` ClusterIP.

The service has no dependency on Keycloak. All Keycloak operations (entity reads) are handled by the **IdP Configuration Service** and its library (`aiac.idp.configuration`). The legacy Keycloak composite / authorization-services policy writer has been **removed** (handoff 04); this OPA CR writer is the sole policy-writer surface.
The service has no dependency on Keycloak. All Keycloak operations (entity reads) are handled by the **IdP Configuration Service** and its library (`aiac.idp.configuration`). The legacy Keycloak composite / authorization-services policy writer has been **removed** (handoff 04); this OPA CR writer is the sole policy-writer surface.

---

## Pydantic models (`aiac.policy.model.models`)

The Policy Writer deserializes the **canonical** `PolicyModel` / `AgentPolicyModel` / `PolicyRule` defined in [policy-model.md](policy-model.md) and imported from `aiac.policy.model.models`. This service does **not** define its own copies; the tables below summarize the fields the Rego generator consumes. (The former `aiac.pdp.library.models` module is deprecated — see policy-model.md "Replaces".)

All models use `model_config = ConfigDict(extra='ignore')`.

### `PolicyRule`

A single access rule pairing a typed role with a typed scope. Used in both inbound and outbound rule sets.

| Field | Type |
|-------|------|
| `role` | `Role` |
| `scope` | `Scope` |
| `effect` | `RuleEffect` (`Allow` default / `Deny`) |

`Role` and `Scope` are the typed models from `aiac.idp.configuration.models`. The Rego generator emits their `.name` as the string literal OPA matches against. `effect` selects whether the rule contributes to an `*_allow_scopes` or `*_deny_scopes` map (see below).

### `AgentPolicyModel`

Complete policy definition for a single agent (service). Contains two sets of `PolicyRule` entries plus supporting data maps used by the Rego packages.

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Service ID from the AIAC trigger event (`aiac.apply.service.{id}`) |
| `default_effect` | `RuleEffect` | How the generated Rego treats a `(role, scope)` pair **no rule mentions**: `Allow` / `Deny`. Default `Deny`. Selects which **decision block** the generators emit (see [Per-policy default effect](#per-policy-default-effect-default_effect)); every declaration map and `*_allow_ok` / `*_deny_ok` gate is emitted identically in both modes. |
| `agent_roles` | `list[Role]` | Realm roles assigned to this agent. Effect-agnostic identity. |
| `agent_scopes` | `list[Scope]` | Scopes this agent exposes. Effect-agnostic identity. |
| `source_roles` | `dict[str, list[Role]]` | Inbound: source (calling service) **id** → roles held. Keyed by the inbound `input.identity.client_id`. **Optional** gate input — an absent `client_id`, or a platform bypass client, passes. Effect-agnostic; **includes deny-edge roles**. |
| `subject_roles` | `dict[str, list[Role]]` | Inbound + outbound: subject (end-user) **id** → roles held. Keyed by `input.identity.subject`. Inbound gate: **mandatory**. Effect-agnostic; **includes deny-edge roles**. |
| `target_allow_scopes` | `dict[str, list[Scope]]` | Outbound: target service **id** → scopes this agent **may** request on it. Keys stay the **full** target service id (matching `input.identity.service_id`, a full SPIFFE ID); the scope **values** are de-prefixed to the bare MCP tool names carried in `input.mcp.params.name` (Q9). |
| `target_deny_scopes` | `dict[str, list[Scope]]` | Outbound: target service **id** → scopes this agent **must not** request on it. Same key/value shape as `target_allow_scopes` (full target service id keys, de-prefixed scope values). |
| `inbound_subject_allow_rules` / `inbound_subject_deny_rules` | `list[PolicyRule]` | Who may / must-not call this agent: `(subject_role, agent_scope)` tuples |
| `inbound_source_allow_rules` / `inbound_source_deny_rules` | `list[PolicyRule]` | Which calling services may / must-not call this agent: `(source_role, agent_scope)` tuples |
| `outbound_target_allow_rules` / `outbound_target_deny_rules` | `list[PolicyRule]` | What this agent may / must-not call: `(this_agent_role, target_scope)` tuples |
| `outbound_subject_allow_rules` / `outbound_subject_deny_rules` | `list[PolicyRule]` | Which users may / must-not reach the agent's targets: `(user_role, tool_scope)` tuples. Default `[]`. |

**`agent_roles` / `agent_scopes` provenance:** these carry the agent's **own** identity — the service-account realm roles it holds and the scopes it exposes. The Policy Computation Engine resolves them from the agent's IdP `Service` record (P2) and embeds them on every agent model it writes; a realm-level agent with no owning service keeps `[]`. Together with `subject_roles` / `source_roles` they are **effect-agnostic**: a role appearing only in a DENY rule is still listed here, so the Rego deny lookup can resolve it.

**Inbound rule semantics (deny-overrides):** a subject holding realm role `role` may invoke this agent for agent scope `scope` iff an allow edge grants it and no deny edge prohibits it. Grouped by role, the allow/deny lists become `subject_role_allow_scopes` / `subject_role_deny_scopes` (and `source_role_allow_scopes` / `source_role_deny_scopes`) that the inbound package evaluates.

**Outbound target rule semantics (deny-overrides):** this agent acting as realm role `role` may request target scope `scope` iff an allow edge grants it and no deny edge prohibits it. Grouped by role, the allow list becomes the single informational `agent_role_scopes` map, and the effective capability gate materializes into `target_allow_scopes` / `target_deny_scopes`.

**Outbound subject rule semantics (deny-overrides):** a subject holding realm role `role` (a **user** role) may reach a **tool** exposing scope `scope` iff an allow edge grants it and no deny edge prohibits it. Grouped by role, the lists become `subject_role_allow_scopes` / `subject_role_deny_scopes` (user role → tool scopes) that the **outbound** package's subject gate evaluates as `input.mcp.params.name in subject_role_allow_scopes[role]` (mirrored against `subject_role_deny_scopes`); their scope **values** are **de-prefixed** to the bare MCP tool name (Q9). This is distinct from the inbound subject rules (user → *agent* scope): the outbound subject gate answers "may this user reach the tool?", not "may this user call the agent?".

**Note on target-map direction:** `target_allow_scopes` / `target_deny_scopes` are keyed by **target service id → scopes** (the inverse of the former `scope_targets`, which was `scope → targets`). The outbound Rego generator emits the **full** target service id as the map key and evaluates `target_allow_scopes[input.identity.service_id]` / `target_deny_scopes[input.identity.service_id]` directly — there is no inversion (see below). Only the scope **values** are de-prefixed to bare MCP tool names; the **keys** stay the full target service id (Q9).

### `PolicyModel`

A partial or full system policy model. When sent to the PDP Policy Writer, contains only the agents whose policies have changed.

| Field | Type |
|-------|------|
| `agents` | `list[AgentPolicyModel]` |

### Usage

```python
from aiac.policy.model.models import PolicyModel, AgentPolicyModel, PolicyRule
```

---

## Endpoints

No `?realm=` parameter — the service operates on a Kubernetes CR, not a Keycloak realm.

| Method | Path | Body | Operation |
|--------|------|------|-----------|
| `POST` | `/policy` | `PolicyModel` | Upsert Rego packages for all agents in the partial model |
| `POST` | `/policy/agents/{agent_id}` | `AgentPolicyModel` | Upsert Rego packages for a single agent |
| `DELETE` | `/policy/agents/{agent_id}` | — | Remove all Rego packages for a specific agent (off-boarding) |
| `DELETE` | `/policy` | — | Clear all Rego packages from the CR (rebuild pre-step) |
| `GET` | `/health` | — | Readiness probe |

### Status codes

| Endpoint | Success | Error |
|----------|---------|-------|
| `POST /policy` | `204 No Content` | **400** `{"error": …}` for a malformed / namespace-less `agent_id` (batch aborts, naming the bad agent; agents already applied stay written — no rollback); **502** `{"error": …}` for a Kubernetes API failure (or the additive dump's `OSError`) |
| `POST /policy/agents/{agent_id}` | `204 No Content` | **400** for a malformed `agent_id`; **502** for a Kubernetes API / dump failure |
| `DELETE /policy/agents/{agent_id}` | `204 No Content` | **400** for a malformed `agent_id`; **502** for a Kubernetes API failure. Deleting a **missing** agent is a no-op **204** (k8s 404 treated as success — idempotent) |
| `DELETE /policy` | `204 No Content` | **502** for a Kubernetes API failure |
| `GET /health` | `200 OK` `{"status": "ok"}` | `503 Service Unavailable` `{"status": "unavailable", "error": …}` if the bounded CR list fails |

`GET /health` performs a bounded (`limit=1`) cluster-wide list of the CRD: a successful list — **including an empty one** — is `200`; any failure (unreachable API, RBAC-forbidden, CRD not served) is `503`.

**400 vs 502 (Q11).** `400` is reserved strictly for a malformed / namespace-less `agent_id` — the `identity_ref` `ValueError`, whose message names the bad id. `502` is strictly for Kubernetes API failures and the additive rego dump's `OSError`. The two are never conflated.
| `POST /policy` | `204 No Content` | **400** `{"error": …}` for a malformed / namespace-less `agent_id` (batch aborts, naming the bad agent; agents already applied stay written — no rollback); **502** `{"error": …}` for a Kubernetes API failure (or the additive dump's `OSError`) |
| `POST /policy/agents/{agent_id}` | `204 No Content` | **400** for a malformed `agent_id`; **502** for a Kubernetes API / dump failure |
| `DELETE /policy/agents/{agent_id}` | `204 No Content` | **400** for a malformed `agent_id`; **502** for a Kubernetes API failure. Deleting a **missing** agent is a no-op **204** (k8s 404 treated as success — idempotent) |
| `DELETE /policy` | `204 No Content` | **502** for a Kubernetes API failure |
| `GET /health` | `200 OK` `{"status": "ok"}` | `503 Service Unavailable` `{"status": "unavailable", "error": …}` if the bounded CR list fails |

`GET /health` performs a bounded (`limit=1`) cluster-wide list of the CRD: a successful list — **including an empty one** — is `200`; any failure (unreachable API, RBAC-forbidden, CRD not served) is `503`.

**400 vs 502 (Q11).** `400` is reserved strictly for a malformed / namespace-less `agent_id` — the `identity_ref` `ValueError`, whose message names the bad id. `502` is strictly for Kubernetes API failures and the additive rego dump's `OSError`. The two are never conflated.

---

## Rego package structure

For each `AgentPolicyModel`, the service generates **two Rego packages** — one for the inbound pipeline and one for the outbound pipeline — and server-side-applies them as the two `policies[]` entries of the agent's `AuthorizationPolicy` CR.

**Fixed package names — no slug (Q2).** Both packages use **fixed** names, regardless of agent:

| Tier | Package | CR `policies[].path` |
|------|---------|----------------------|
| inbound | `authbridge.client.inbound.request` | `inbound/request.rego` |
| outbound | `authbridge.client.outbound.request` | `outbound/request.rego` |

Each package begins with `import rego.v1`. The names never contain a slug: the `bundle-service` combiner requires the **exact** path `data.authbridge.client.<tier>`, so a per-agent package name would break the composition. Per-agent isolation is achieved at the **CR / bundle level** — bundle-service looks a CR up by namespace + name — not in the package name.

**`identity_ref` drives the CR metadata, not a package name (Q3).** `identity_ref(agent_id) -> (namespace, name)` accepts a SPIFFE URI (`spiffe://<trust-domain>/ns/<ns>/sa/<name>`) or a plain `<ns>/<name>` clientId, validates both segments as DNS-1123 labels (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, ≤63 chars), and returns the `(namespace, name)` used for the CR's `metadata`. There is **no** fallback — a bare `github-agent` (no derivable namespace) or an invalid label raises `ValueError` (→ 400). This function replaces the former per-package slug: it feeds `metadata`, never a package name.
For each `AgentPolicyModel`, the service generates **two Rego packages** — one for the inbound pipeline and one for the outbound pipeline — and server-side-applies them as the two `policies[]` entries of the agent's `AuthorizationPolicy` CR.

**Fixed package names — no slug (Q2).** Both packages use **fixed** names, regardless of agent:

| Tier | Package | CR `policies[].path` |
|------|---------|----------------------|
| inbound | `authbridge.client.inbound.request` | `inbound/request.rego` |
| outbound | `authbridge.client.outbound.request` | `outbound/request.rego` |

Each package begins with `import rego.v1`. The names never contain a slug: the `bundle-service` combiner requires the **exact** path `data.authbridge.client.<tier>`, so a per-agent package name would break the composition. Per-agent isolation is achieved at the **CR / bundle level** — bundle-service looks a CR up by namespace + name — not in the package name.

**`identity_ref` drives the CR metadata, not a package name (Q3).** `identity_ref(agent_id) -> (namespace, name)` accepts a SPIFFE URI (`spiffe://<trust-domain>/ns/<ns>/sa/<name>`) or a plain `<ns>/<name>` clientId, validates both segments as DNS-1123 labels (`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`, ≤63 chars), and returns the `(namespace, name)` used for the CR's `metadata`. There is **no** fallback — a bare `github-agent` (no derivable namespace) or an invalid label raises `ValueError` (→ 400). This function replaces the former per-package slug: it feeds `metadata`, never a package name.

> **Two identifiers, two layers (no contradiction).** UC-1 onboarding and the Trigger use the internal Keycloak **client UUID** (`service.id` / `Trigger.entity_id`) purely to *look up* a service in the IdP — that UUID **never reaches this writer**. What flows down the policy pipeline into `PolicyRule.scope.serviceId` / `Role.actorIds` and lands as `AgentPolicyModel.agent_id` is the **clientId** (the `<ns>/<name>` / SPIFFE form), which `identity_ref` maps to the CR's `(namespace, name)`. The UUID→clientId resolution happens once, in the IdP Configuration Service, before the AgentPolicyModel is ever built.
> **Two identifiers, two layers (no contradiction).** UC-1 onboarding and the Trigger use the internal Keycloak **client UUID** (`service.id` / `Trigger.entity_id`) purely to *look up* a service in the IdP — that UUID **never reaches this writer**. What flows down the policy pipeline into `PolicyRule.scope.serviceId` / `Role.actorIds` and lands as `AgentPolicyModel.agent_id` is the **clientId** (the `<ns>/<name>` / SPIFFE form), which `identity_ref` maps to the CR's `(namespace, name)`. The UUID→clientId resolution happens once, in the IdP Configuration Service, before the AgentPolicyModel is ever built.

### Live plugin input shape (Q4)

The Rego packages evaluate the `input` document the live AuthBridge OPA plugin populates — never IDs-plus-roles supplied per request. The fields the packages read:

| Input field | Meaning | Tier |
|-------------|---------|------|
| `input.identity.subject` | The delegated end-user id (JWT `sub`) | inbound + outbound |
| `input.identity.client_id` | The calling client — the inbound source | inbound |
| `input.identity.service_id` | The downstream target audience the exchanged token was minted for — a **full SPIFFE id** | outbound |
| `input.mcp.params.name` | The **bare** invoked MCP tool name (e.g. `source-read`) | outbound |

On the outbound leg there is no validated JWT; the plugin synthesizes `input.identity` from the token-exchange delegation hop. A **missing** `input.mcp.params.name` (e.g. a `tools/list` discovery request, which carries no tool name) or an **absent** `input.identity.service_id` matches nothing in the maps and is therefore **denied**.
### Live plugin input shape (Q4)

The Rego packages evaluate the `input` document the live AuthBridge OPA plugin populates — never IDs-plus-roles supplied per request. The fields the packages read:

| Input field | Meaning | Tier |
|-------------|---------|------|
| `input.identity.subject` | The delegated end-user id (JWT `sub`) | inbound + outbound |
| `input.identity.client_id` | The calling client — the inbound source | inbound |
| `input.identity.service_id` | The downstream target audience the exchanged token was minted for — a **full SPIFFE id** | outbound |
| `input.mcp.params.name` | The **bare** invoked MCP tool name (e.g. `source-read`) | outbound |

On the outbound leg there is no validated JWT; the plugin synthesizes `input.identity` from the token-exchange delegation hop. A **missing** `input.mcp.params.name` (e.g. a `tools/list` discovery request, which carries no tool name) or an **absent** `input.identity.service_id` matches nothing in the maps and is therefore **denied**.

The generator embeds these symbols, derived from the `AgentPolicyModel`:

**Symmetric rename — no alias, no back-compat.** The single inbound `role_scopes` map splits into `subject_role_allow_scopes` / `subject_role_deny_scopes` / `source_role_allow_scopes` / `source_role_deny_scopes`; the outbound `subject_role_scopes` splits into `subject_role_allow_scopes` / `subject_role_deny_scopes`; `target_scopes` splits into `target_allow_scopes` / `target_deny_scopes`. Identity maps `subject_roles` / `source_roles` / `agent_roles` keep their names.

| Rego symbol | Source | Shape | De-prefixed? |
|-------------|--------|-------|--------------|
| `agent_scopes` | `model.agent_scopes` | `[scope.name, …]` — **inbound only** (the audience gate) | no — full scope names |
| `subject_roles` | `model.subject_roles` | subject id → `[role.name, …]` (effect-agnostic; includes deny-edge roles) | n/a (roles) |
| `source_roles` | `model.source_roles` | source client id → `[role.name, …]` — **inbound only** (effect-agnostic; includes deny-edge roles) | n/a (roles) |
| `subject_role_allow_scopes` / `subject_role_deny_scopes` | grouped `inbound_subject_{allow,deny}_rules` (inbound) / `outbound_subject_{allow,deny}_rules` (outbound) | role → `[scope name, …]` — inbound: agent scopes; outbound: tool names | inbound no; outbound **yes** |
| `source_role_allow_scopes` / `source_role_deny_scopes` | grouped `inbound_source_{allow,deny}_rules` | role → `[agent scope name, …]` — **inbound only** | no — full scope names |
| `agent_roles` | `model.agent_roles` | `[role.name, …]` — **outbound only** (informational) | n/a (roles) |
| `agent_role_scopes` | grouped `outbound_target_allow_rules` | agent role → `[tool name, …]` — **outbound only** (informational; single map, no deny variant emitted) | **yes** — bare tool names |
| `target_allow_scopes` / `target_deny_scopes` | `model.target_allow_scopes` / `model.target_deny_scopes` | full target service id → `[tool name, …]` — **outbound only** | **values yes, keys no** |

De-prefixing (Q9) is **outbound-only**: provisioned scope names are prefixed with their owning workload (`github-tool.source-read`), but the value that arrives in `input.mcp.params.name` at runtime is the bare tool name (`source-read`), so the outbound map **values** are stripped of a leading `"<owner>."` (where `owner = identity_ref(scope.serviceId).name`). The **keys** of `target_allow_scopes` / `target_deny_scopes` stay the full target service id (they match `input.identity.service_id`). Inbound `agent_scopes` and the `*_role_allow_scopes` / `*_role_deny_scopes` maps keep their **full** names — the inbound gate compares scopes internally, never against `input.mcp.params.name`.

### Per-policy default effect (`default_effect`)

`AgentPolicyModel.default_effect` (`Allow` / `Deny`, default `Deny`) decides how each package treats a `(role, scope)` pair that **no rule mentions**. Three states exist per pair: **explicitly allowed** (an allow rule/edge names it), **explicitly denied** (a deny rule/edge names it), and **unspecified** (no rule names it → resolves to `default_effect`).

- `Deny` (default) reproduces today's least-privilege output **byte-for-byte**: `default allow := false` plus one incremental `allow if { … }` rule per package (the blocks shown below).
- `Allow` opens the default while explicit denies still override.

**Only the trailing decision block branches on `default_effect`.** Every declaration map (`subject_role_allow_scopes`, `target_allow_scopes`, the informational `agent_role_scopes`, …) and every `*_allow_ok` / `*_deny_ok` gate is emitted **identically** in both modes. Under `Allow` the allow-side machinery (the allow scope maps, `subject_allow_ok` / `source_allow_ok` / `target_allow_ok`, and the inbound platform-bypass rules) is still generated but **inert** — an allowed pair and an unmentioned pair both resolve to `allow` — mirroring how `agent_role_scopes` is already emitted-but-unreferenced.

**Why a literal flip of the `default allow :=` constant is insufficient.** In Rego, `default allow := <v>` supplies a value only when every other `allow` rule is undefined, and an incremental `allow if { <body> }` rule can only push `allow` *toward* `true`. Keeping the existing `allow if { …; not …_deny_ok }` body and merely flipping the constant to `true` would leave `allow` `true` whenever that body is undefined, so the `not …_deny_ok` guard subtracts nothing and **every prohibition silently evaporates**. Overriding a permissive default therefore requires **separate** complete rules — `allow := false if { <deny_gate> }`, one per deny gate; an assigned `false` wins over `default true` when its body holds, which is exactly deny-overrides.

**The generator assumes disjoint allow/deny per `(role, scope)` and never reconciles an overlap.** A genuine grant/deny overlap on the same pair is a real policy conflict surfaced **upstream** as HTTP 422 (the PRB raises `PolicyContradictionError`); the PCE assumes a conflict-free model. The generator therefore adds **no** logic that silently reconciles an allow-vs-deny overlap — doing so would mask a conflict that is *supposed* to surface as a 422. The `allow := false if { <deny> }` rules are **not** conflict reconciliation: (1) they give a deny precedence over the permissive default (for a pair unmentioned-by-allow, hence not an overlap), and (2) they let a deny on **one** of a subject's several roles — or on **one** of the two outbound gates — beat an allow arriving from a *different* role / the *other* gate. Each individual `(role, scope)` stays allow-XOR-deny; the denies merely co-occur within a single request.

### Inbound package: `authbridge.client.inbound.request`

Evaluated by the AuthBridge OPA plugin in the **inbound pipeline** — "who may call this agent". `allow` requires `subject_allow_ok` **and** `source_allow_ok` and **neither** `subject_deny_ok` **nor** `source_deny_ok` (deny-overrides). `subject_allow_ok` passes when the subject (`input.identity.subject`) holds a role granting at least one of the agent's own `agent_scopes` via `subject_role_allow_scopes`; `subject_deny_ok` mirrors it against `subject_role_deny_scopes`. `source_allow_ok` passes when (a) there is no calling `input.identity.client_id` (pure end-user traffic), (b) the `client_id` is one of the **platform bypass clients** — `rossoctl` by default, from `PLATFORM_SOURCE_CLIENTS` (Q5); this bypass is **mandatory**, since end-user traffic carries the platform client and would otherwise be denied — or (c) that client holds a role granting an agent scope via `source_role_allow_scopes`; `source_deny_ok` mirrors it against `source_role_deny_scopes`.

The block below mirrors the current `generate_inbound_rego` output (`inbound/request.rego`) under the default `default_effect == Deny`, reproduced with light blank-line spacing for readability — every declaration map, gate, and the trailing decision block are identical to what the generator emits. (The `docs/examples/opa-team1-policy.yaml` golden fixture has been regenerated to these split gates and carries a `default_effect` annotation.)

```rego
package authbridge.client.inbound.request
import rego.v1

agent_scopes := ["github-agent.issue_operations", "github-agent.source_operations"]

subject_roles := {
    "dev-user": ["developer"],
    "test-user": ["tester"],
}

source_roles := {}

subject_role_allow_scopes := {
    "developer": ["github-agent.issue_operations", "github-agent.source_operations"],
    "tester": ["github-agent.issue_operations"],
}
subject_role_deny_scopes := {}
source_role_allow_scopes := {}
source_role_deny_scopes := {}

subject_allow_ok if {
    some role in subject_roles[input.identity.subject]
    some scope in subject_role_allow_scopes[role]
    scope in agent_scopes
}
subject_deny_ok if {
    some role in subject_roles[input.identity.subject]
    some scope in subject_role_deny_scopes[role]
    scope in agent_scopes
}

source_allow_ok if { not input.identity.client_id }
source_allow_ok if { input.identity.client_id == "rossoctl" }
source_allow_ok if {
    some role in source_roles[input.identity.client_id]
    some scope in source_role_allow_scopes[role]
    scope in agent_scopes
}
source_deny_ok if {
    some role in source_roles[input.identity.client_id]
    some scope in source_role_deny_scopes[role]
    scope in agent_scopes
}

default allow := false
allow if { subject_allow_ok; source_allow_ok; not subject_deny_ok; not source_deny_ok }
```

Under `default_effect == Allow`, **only** the trailing decision block changes — every declaration map and gate above is emitted identically; the allow gates and the platform-bypass rules become inert, the package opens by default, and explicit denies still override:

```rego
default allow := true
allow := false if { subject_deny_ok }
allow := false if { source_deny_ok }
```

An unmentioned subject/source (matched by no deny gate) falls through to `default allow := true`; a subject or source named by a deny edge forces `allow := false` (see [Per-policy default effect](#per-policy-default-effect-default_effect) for why this can't be a bare constant flip).

**Deny-overrides:** `allow` fires only when both allow gates pass **and** neither deny gate matches. A subject or source barred by a deny edge is rejected even when an allow edge would otherwise admit it. (An absent `input.identity.client_id` makes `source_allow_ok` true and — because `source_roles[input.identity.client_id]` is undefined — leaves `source_deny_ok` false, so an absent source still passes.)

> **Security property — source-side deny reach.** The `source_allow_ok`
> bypass sets only the *allow* gate; `allow` still requires `not
> source_deny_ok` **and** `not subject_deny_ok`, so a bypassed source is
> **not** immune to a deny — a subject-side deny still applies, and a
> source-side deny applies too *when it can fire*. The limit is on the
> source deny gate specifically:
> - **Pure end-user traffic (no `client_id`)** is structurally
>   un-revokable on the **source** side: `source_roles[input.identity.client_id]`
>   is undefined, so `source_deny_ok` can never fire against it. Such
>   traffic can still be denied by a **subject**-side deny (it always
>   carries `input.identity.subject`).
> - A **platform bypass client** (`rossoctl` et al.) keeps
>   `source_allow_ok` unconditionally, but `source_deny_ok` *does* fire
>   if an explicit deny edge names a role that client holds. In normal
>   operation platform clients carry no authored rules, so their source
>   trust is effectively un-revokable — but it is not structurally
>   un-revokable, and no separate exemption shields them from a deny that
>   is actually authored against their role.
>
> Net: **DENY cannot revoke *source-side* trust for a caller that presents
> no `client_id`.** This is deliberate — dropping the bypass would deny
> the platform-fronted end-user traffic the mesh depends on (see
> `PLATFORM_SOURCE_CLIENTS`, Q5) — and is a property of the source gate,
> not a global "platform clients are always allowed" carve-out.

### Outbound package: `authbridge.client.outbound.request`

Evaluated by the AuthBridge OPA plugin in the **outbound pipeline** — "what this agent may call", **per invoked tool**. `allow` is an AND on the **same** `input.mcp.params.name`, requiring **both** allow gates to pass and **neither** deny gate to match (deny-overrides): `subject_allow_ok` (the delegated user's role admits the tool — `input.mcp.params.name in subject_role_allow_scopes[role]`, de-prefixed values) AND `target_allow_ok` (the target service, keyed by the full `input.identity.service_id`, admits the tool — `input.mcp.params.name in target_allow_scopes[input.identity.service_id]`), with `subject_deny_ok` / `target_deny_ok` mirroring them against `subject_role_deny_scopes` / `target_deny_scopes`. `agent_roles` / `agent_role_scopes` are emitted for debugging but are **not** referenced by `allow` — `target_allow_scopes[input.identity.service_id]` already *is* the per-scope capability gate. This package emits neither `agent_scopes` nor the inbound subject gate: outbound decisions never consider the agent's own audience scopes.

The block below mirrors the current `generate_outbound_rego` output (`outbound/request.rego`) under the default `default_effect == Deny`, annotated with explanatory `#` comments and spacing for readability — the maps, gates, and trailing decision block are identical to what the generator emits (which itself emits only the single `# informational/debugging only` comment). (As above, the `docs/examples/opa-team1-policy.yaml` golden fixture has been regenerated to these split gates with a `default_effect` annotation.)

```rego
package authbridge.client.outbound.request
import rego.v1

agent_roles := ["github-agent.issue_operations", "github-agent.source_operations"]
subject_roles := {
    "dev-user": ["developer"],
    "test-user": ["tester"]
}
# The deployed github-tool (aiac/demo/assets/tools/github_tool) exposes
# exactly four MCP tools — source-read, source-write, issues-read,
# issues-write — one per skill. These names ARE the values that arrive in
# input.mcp.params.name when a specific tool is invoked, so the maps
# below key on them.
subject_role_allow_scopes := {
    "developer": ["issues-read", "source-write", "source-read"],
    "tester": ["issues-read", "issues-write"],
}
subject_role_deny_scopes := {}
# informational/debugging only — not referenced by allow
agent_role_scopes := {
    "github-agent.issue_operations": ["issues-read", "issues-write"],
    "github-agent.source_operations": ["source-write", "source-read"],
}
target_allow_scopes := {
    "spiffe://localtest.me/ns/team1/sa/github-tool": ["source-read", "source-write", "issues-read", "issues-write"],
}
target_deny_scopes := {}
# user may reach the tool: holds a role granted the invoked tool (input.mcp.params.name)
subject_allow_ok if {
    some role in subject_roles[input.identity.subject]
    input.mcp.params.name in subject_role_allow_scopes[role]
}
subject_deny_ok if {
    some role in subject_roles[input.identity.subject]
    input.mcp.params.name in subject_role_deny_scopes[role]
}
# agent may reach the tool: the invoked tool is one the target accepts (direct, per-scope)
target_allow_ok if {
    input.mcp.params.name in target_allow_scopes[input.identity.service_id]
}
target_deny_ok if {
    input.mcp.params.name in target_deny_scopes[input.identity.service_id]
}
default allow := false
allow if { subject_allow_ok; target_allow_ok; not subject_deny_ok; not target_deny_ok }
```

Under `default_effect == Allow`, **only** the trailing decision block changes — the two-gate AND is **dropped** and replaced by **deny-if-either-side**:

```rego
default allow := true
allow := false if { subject_deny_ok }
allow := false if { target_deny_ok }
```

**Why not a negated allow-gate AND.** Today's `allow` is `subject_allow_ok AND target_allow_ok AND not (either deny)` — a conjunction correct only under `Deny`, where a pair must be affirmatively granted by *both* gates. Under `Allow` you must **not** carry that AND forward as `allow := false if { not subject_allow_ok }` / `{ not target_allow_ok }`: every unmentioned `(role, tool)` pair matches neither allow gate and would be wrongly **denied**, defeating the permissive default. With deny-if-either-side an unmentioned pair (no deny on either side) falls through to `default allow := true`, and an explicit deny on **either** the subject side or the target/capability side overrides it.

A worked example (agent `github-agent`, users `developer`/`tester`, tool `github-tool`) is maintained alongside the tests. The `docs/examples/opa-team1-policy.yaml` mirror has been regenerated to the split ALLOW/DENY gates and annotated with the `default_effect` semantics.

### `AuthorizationPolicy` Custom Resource (Q6)

The two packages become the `policies[]` of a per-agent CR. The writer builds the body in `_build_cr`, keyed on `identity_ref(agent_id)`:

```yaml
apiVersion: agent.rossoctl.dev/v1alpha1
kind: AuthorizationPolicy
metadata:
  name: github-agent            # identity_ref(agent_id).name
  namespace: team1              # identity_ref(agent_id).namespace
  labels:
    app.kubernetes.io/managed-by: aiac-pdp-policy-writer
spec:
  scope: client
  clientID: "github-agent"      # display / print-column only
  policies:
    - path: "inbound/request.rego"
      content: |
        # ... generate_inbound_rego(model) ...
    - path: "outbound/request.rego"
      content: |
        # ... generate_outbound_rego(model) ...
```

- **Written via server-side apply.** `_upsert_agent` calls `patch_namespaced_custom_object` with `_content_type="application/apply-patch+yaml"`, `field_manager="aiac-pdp-policy-writer"`, `force=True` — create-or-update in one idempotent call.
- **`metadata.labels["app.kubernetes.io/managed-by"] = "aiac-pdp-policy-writer"`** marks every CR the writer owns; `_delete_all` selects on it.
- **`spec.clientID` is display-only** — bundle-service looks the CR up by `metadata.name` + `metadata.namespace` (matched against the SPIFFE ServiceAccount segment), **never** by `clientID`. It must nonetheless be a valid DNS label (no `spiffe://`, no `/`); the writer sets it to the `name`.
- **Delete-by-label vs single-agent delete.** `_delete_all` (`DELETE /policy`) lists every CR carrying the managed-by label **cluster-wide** and deletes each; `_delete_agent` (`DELETE /policy/agents/{agent_id}`) deletes the single `(name, namespace)` from `identity_ref` (a k8s 404 is treated as success — idempotent).

### Both tiers always emitted; delete = off-boarding (Q7)

Every upsert writes **both** tiers, each with `default allow := false`. The shipped global combiner allows a tier only when `ns_ok AND client_ok`, where `client_ok` comes from this package's `allow` — **except** that a tier with **no** CR falls back to allow via the combiner's `client_ok if not data.authbridge.client.<tier>` rule.

Consequently **deleting a CR is off-boarding, not lockdown**: removing an agent's CR returns that agent to the combiner's **allow-fallback**, it does *not* deny the agent. To actually block an agent while keeping it in the system, **upsert** a CR with empty maps (so `allow` stays `false`) rather than deleting it.

---

## Library: `aiac.pdp.policy.library.api`

HTTP client module wrapping the PDP Policy Writer REST API. Exposes four module-level functions. Service URL is read from the `AIAC_PDP_POLICY_URL` environment variable (default: `http://127.0.0.1:7072`). All functions raise `RuntimeError` on non-2xx response.

```python
def apply_policy(model: PolicyModel) -> None
    # POST /policy

def apply_agent_policy(agent_id: str, model: AgentPolicyModel) -> None
    # POST /policy/agents/{agent_id}

def delete_agent_policy(agent_id: str) -> None
    # DELETE /policy/agents/{agent_id}

def delete_policy() -> None
    # DELETE /policy
```

### Dependencies

```
requests
pydantic
python-dotenv
```

### Usage

```python
from aiac.pdp.policy.library.api import apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
from aiac.policy.model.models import PolicyModel, AgentPolicyModel, PolicyRule

apply_agent_policy("weather-agent", agent_model)
delete_policy()
apply_policy(full_model)
```

---

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PLATFORM_SOURCE_CLIENTS` | No | `rossoctl` | Comma-separated platform bypass clients, sourced from the `aiac-pdp-config` ConfigMap. Drives the inbound package's `source_allow_ok if { input.identity.client_id == "<c>" }` bypass rules (Q5). Blanks are dropped; an unset or all-blank value falls back to `rossoctl` (dropping the bypass would deny end-user traffic, which carries the platform client). |
| `POLICY_WRITER_DUMP_REGO` | No | off | When truthy (`1`/`true`/`yes`/`on`) enables the **additive** local rego dump (see below). Never gates the CR write. |
| `REGO_OUTPUT_DIR` | No | `/rego` | Destination for the additive dump — only consulted when `POLICY_WRITER_DUMP_REGO` is on. |

There are **no** CR-name or CR-namespace env vars — CR coordinates are derived per-agent from `identity_ref(agent_id)`, and the group/version/plural/field-manager/label are code constants (Q8).

**Auth model.** The writer authenticates to the Kubernetes API as an **in-cluster ServiceAccount** (`aiac-pdp-policy-writer`), bound cluster-wide to a `ClusterRole` granting `get`, `list`, `create`, `update`, `patch`, `delete` on `authorizationpolicies.agent.rossoctl.dev` — **no `watch`** (the writer only creates/patches; bundle-service polls). The `ServiceAccount`, `ClusterRole`, and `ClusterRoleBinding` are declared in `k8s/pdp-interface-deployment.yaml`. The binding is cluster-scoped (not a namespaced `RoleBinding`) because the writer creates CRs in arbitrary workload namespaces (`team1`, …), derived from each agent's `identity_ref` namespace. For local development, the `kubernetes` client falls back to `~/.kube/config` automatically.

## Always-on CR write + additive debug dump

The **CR server-side-apply is always active** — it is never gated by an env var. The former filesystem-stub behaviour survives **only** as an additive debug/test aid, toggled by `POLICY_WRITER_DUMP_REGO` (default off). When on, `_upsert_agent` **also** writes the same rego to `<REGO_OUTPUT_DIR>/<ns>/<name>/inbound/request.rego` and `<REGO_OUTPUT_DIR>/<ns>/<name>/outbound/request.rego`, mirroring the CR `policies[].path` so the on-disk output equals the CR content; `_delete_agent` / `_delete_all` clear the corresponding dumped tree. The dump is **never** a substitute for, or a switch away from, the CR write — production runs with it off (`k8s/pdp-interface-deployment.yaml` sets no `POLICY_WRITER_DUMP_REGO`). A dump `OSError` maps to 502, so a broken debug mount surfaces rather than silently dropping files.
## Always-on CR write + additive debug dump

The **CR server-side-apply is always active** — it is never gated by an env var. The former filesystem-stub behaviour survives **only** as an additive debug/test aid, toggled by `POLICY_WRITER_DUMP_REGO` (default off). When on, `_upsert_agent` **also** writes the same rego to `<REGO_OUTPUT_DIR>/<ns>/<name>/inbound/request.rego` and `<REGO_OUTPUT_DIR>/<ns>/<name>/outbound/request.rego`, mirroring the CR `policies[].path` so the on-disk output equals the CR content; `_delete_agent` / `_delete_all` clear the corresponding dumped tree. The dump is **never** a substitute for, or a switch away from, the CR write — production runs with it off (`k8s/pdp-interface-deployment.yaml` sets no `POLICY_WRITER_DUMP_REGO`). A dump `OSError` maps to 502, so a broken debug mount surfaces rather than silently dropping files.

---

## Runtime

- Framework: FastAPI
- Server: uvicorn
- Bind: `0.0.0.0:7072`
- Base image: `python:3.12-slim`
- Kubernetes ClusterIP Service: `aiac-pdp-policy-service:7072`
- Deployment: co-located with IdP Configuration Service as a container in the **Rossoctl Interface Pod** (`pdp-interface-deployment.yaml`)

---

## Dependencies (`requirements.txt`)

```
fastapi
uvicorn[standard]
kubernetes
pydantic
```

---

## File structure

```
aiac/src/aiac/pdp/service/
├── __init__.py
└── policy/
    ├── __init__.py
    └── opa/
        ├── __init__.py
        ├── Dockerfile
        ├── requirements.txt
        ├── rego.py         # identity_ref + generate_inbound_rego + generate_outbound_rego
        └── main.py         # the always-on CR writer (with optional additive dump)

aiac/src/aiac/pdp/policy/
├── __init__.py
└── library/
    ├── __init__.py
    └── api.py          # apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
                        # (models now imported from aiac.policy.model.models)
```

There is **no** `stub.py` and no separate filesystem-writer module: `main.py` is the single, always-on CR writer (rego rendering lives in `rego.py`; the optional dump is a branch inside `main.py`, not a distinct mode).

Build command:
```bash
docker build -f aiac/src/aiac/pdp/service/policy/opa/Dockerfile \
  -t aiac-pdp-policy-opa:latest aiac/src/
```

---

## `main.py` behaviour notes

- **Kube config at import:** `_load_kube_config()` tries `config.load_incluster_config()`, falling back to `config.load_kube_config()` (local dev). Both failing is non-fatal — the module stays importable and API calls surface as 502/503 until real config exists. A module-level `client.CustomObjectsApi` handles all CR operations.
- **Code constants (never env vars):** `_GROUP = "agent.rossoctl.dev"`, `_VERSION = "v1alpha1"`, `_PLURAL = "authorizationpolicies"`, `_MANAGED_BY_LABEL = {"app.kubernetes.io/managed-by": "aiac-pdp-policy-writer"}`, `_FIELD_MANAGER = "aiac-pdp-policy-writer"` (Q8).
- **`identity_ref(agent_id) -> (namespace, name)`** (in `rego.py`): SPIFFE or `<ns>/<name>` → DNS-1123-validated `(namespace, name)`; raises `ValueError` (→ 400) when no namespace is derivable or a segment is an invalid label — no fallback.
- **`generate_inbound_rego(model, platform_clients)` / `generate_outbound_rego(model)`** (in `rego.py`): render the two fixed-package strings under the ALLOW/DENY model. The inbound generator emits `subject_roles` / `source_roles` (effect-agnostic) plus the grouped `subject_role_allow_scopes` / `subject_role_deny_scopes` (from `inbound_subject_{allow,deny}_rules`) and `source_role_allow_scopes` / `source_role_deny_scopes` (from `inbound_source_{allow,deny}_rules`), one `source_allow_ok` bypass rule per `platform_clients` entry (plus the no-`client_id` and role-based rules), and the mirrored `subject_deny_ok` / `source_deny_ok` gates; `allow` applies deny-overrides. The outbound generator emits `subject_role_allow_scopes` / `subject_role_deny_scopes` (from `outbound_subject_{allow,deny}_rules`), the single informational `agent_role_scopes` (from `outbound_target_allow_rules`), and `target_allow_scopes` / `target_deny_scopes`, de-prefixing its map values; `allow` is a per-scope AND with deny-overrides. Both generators branch on `model.default_effect`: `Deny` (default) emits today's `default allow := false` + single `allow if { … }` block **byte-for-byte**; `Allow` emits `default allow := true` + one `allow := false if { <deny_gate> }` rule per deny gate (`subject_deny_ok` / `source_deny_ok` inbound; `subject_deny_ok` / `target_deny_ok` outbound). Only the decision block differs — all declaration maps and `*_allow_ok` / `*_deny_ok` gates are emitted identically in both modes, and the generator never reconciles an allow-vs-deny overlap (a genuine overlap is an upstream 422; see [Per-policy default effect](#per-policy-default-effect-default_effect)).

> **Rollout impact.** These identifier renames are symmetric with **no alias / no back-compat**. All generated `.rego` **golden fixtures must be regenerated** to match the split gates. The demo helper `demo/use-cases/uc1-onboarding/lib/_lib.py` (which reads the `target_scopes` Rego map) must **retarget to `target_allow_scopes`**.
- **`_build_cr(model)`:** assemble the CR body — `metadata.name`/`.namespace` from `identity_ref`, the managed-by label, `spec.scope: client`, `spec.clientID` = the display name, and `policies[]` = the two rendered packages. Raises `ValueError` (via `identity_ref`) on a malformed `agent_id`.
- **`_upsert_agent(model)`:** server-side apply via `patch_namespaced_custom_object` (`_content_type="application/apply-patch+yaml"`, `field_manager=_FIELD_MANAGER`, `force=True`); then, if the dump is enabled, `_dump_cr`.
- **`_delete_agent(agent_id)`:** `delete_namespaced_custom_object` for the single `(name, namespace)`; a k8s **404 is swallowed** (idempotent → 204); then dump-clear the agent's tree if enabled.
- **`_delete_all()`:** `list_cluster_custom_object` filtered by the managed-by label selector, then delete each item (per-item 404 tolerated for concurrent-delete races); then clear the dumped tree if enabled.
- **`_run_write(op)`** maps outcomes to responses: success → **204**; `ValueError` → **400** `{"error": …}`; `ApiException` → **502** `{"error": …}`; `OSError` (the additive dump) → **502**.
- **`POST /policy`:** iterate `policy.agents`, `_upsert_agent` each; a malformed `agent_id` aborts the batch with a 400 naming it (agents already applied before that point stay written — no rollback).
- **`POST /policy/agents/{agent_id}`:** `_upsert_agent(model)`.
- **`DELETE /policy/agents/{agent_id}`:** `_delete_agent(agent_id)`.
- **`DELETE /policy`:** `_delete_all()`.
- **`GET /health`:** a bounded `list_cluster_custom_object(..., limit=1)` — a successful list (even empty) → `200 {"status": "ok"}`; any failure (unreachable API, RBAC-forbidden, CRD not served) → `503 {"status": "unavailable", "error": …}`. The dump dir is not part of this signal.
