# Keycloak Access Control in Rossoctl: U → Agent A → Tool T

> Analysis of the minimum Keycloak configuration required for the
> scenario where a user U calls agent A, which in turn calls tool T —
> and why users Y and agent B are denied.
> Includes a full RBAC section (§10–§13) covering the role-mapped model
> where user U holds realm role UR and user Y holds realm role YR.
>
> **§18–§23 cover the OPA-as-PDP architecture** — the production model where
> OPA plugins replace Keycloak gate 3, reducing the required Keycloak
> configuration to gates 1 and 2 only. §§1–17 remain as reference material
> for the Keycloak-native RBAC model.

---

## 1. Architecture Overview

The Rossoctl platform uses a **three-layer trust chain** enforced by:

- **Keycloak** — OAuth2/OIDC token issuer and policy decision point (PDP)
- **AuthBridge** — transparent sidecar proxy acting as pure policy enforcement point (PEP)
- **SPIFFE/SPIRE** — workload identity via SPIFFE IDs used as Keycloak `clientId`

Every workload (agent or tool) has an AuthBridge sidecar injected by the rossoctl-operator. The sidecar:
- **Inbound**: validates incoming JWTs (signature, issuer, audience) — returns `401` on failure
- **Outbound**: intercepts HTTP calls to other services and performs RFC 8693 token exchange transparently

The application code never sees a raw Keycloak call. All token lifecycle management is handled by the sidecar.

```
User U
  │  (1) POST /token  scope=agent-A-aud  → token₁  {sub:U, aud:[A-SPIFFE]}
  │
  ▼
Agent A pod
  ├── AuthBridge inbound: verify sig + issuer + aud == A-SPIFFE
  ├── App processes request, calls Tool T
  └── AuthBridge outbound: RFC 8693 exchange
        subject_token = token₁
        audience      = T-SPIFFE
        scope         = tool-T-aud
        acting client = A's client credentials
        → token₂  {sub:U, aud:[T-SPIFFE]}

Tool T pod
  └── AuthBridge inbound: verify sig + issuer + aud == T-SPIFFE  ✓
```

---

## 2. Minimum Keycloak Configuration

### 2.1 Required Objects

| # | Object | Type | Key Properties | Purpose |
|---|--------|------|----------------|---------|
| 1 | `rossoctl` | Realm | `enabled: true` | Hosts all principals |
| 2 | Agent A client | Keycloak Client | `clientId=A-SPIFFE`, `standard.token.exchange.enabled=true` | A's service identity; acting party in exchange |
| 3 | Tool T client | Keycloak Client | `clientId=T-SPIFFE`, `standard.token.exchange.enabled=true` | Exchange target; T's audience anchor |
| 4 | `agent-A-aud` scope | Client Scope + `oidc-audience-mapper` | `aud += A-SPIFFE`; assigned as **default** on U's client | Lets U obtain a token that A's inbound will accept |
| 5 | `tool-T-aud` scope | Client Scope + `oidc-audience-mapper` | `aud += T-SPIFFE`; assigned as **optional** on A's client | Lets AuthBridge exchange for a token T's inbound will accept |
| 6 | User U | Realm User | `enabled: true` | Authenticating principal |
| — | `authproxy-routes` | Kubernetes ConfigMap | `host → target_audience + token_scopes` | Tells AuthBridge when and how to exchange outbound tokens |

### 2.2 `authproxy-routes` ConfigMap (non-Keycloak, but required)

```yaml
# ConfigMap: authproxy-routes, namespace: <agent-ns>
- host: "tool-T-service"
  target_audience: "spiffe://…/sa/tool-T"
  token_scopes: "openid tool-T-aud"
```

### 2.3 Token Exchange: Three Keycloak Gates

When AuthBridge performs the RFC 8693 exchange on A's outbound call to T, Keycloak enforces three
sequential checks:

| Gate | What Keycloak checks | What must exist |
|------|----------------------|-----------------|
| **Client allowed?** | `standard.token.exchange.enabled=true` on A's client | Set by the setup script on A's client |
| **Scope registered?** | Is `tool-T-aud` assigned (default or optional) to A's client? | `add_client_optional_client_scope(A_client, tool-T-aud)` |
| **Subject holds it?** | Does the subject's token carry the scope or the required role? | Scope-to-role gate (RBAC mode) or realm default assignment |

---

## 3. Q1 — What ensures User U can access Agent A?

### Keycloak side (token issuance)

The `agent-A-aud` client scope carries an `oidc-audience-mapper` that injects A's SPIFFE ID into
the `aud` claim. It is assigned as a **default scope** on the client U authenticates through, so
every token U obtains automatically includes `aud: ["spiffe://…/sa/agent-A"]`.

```python
# From setup_keycloak_weather_advanced.py
keycloak_admin.add_client_default_client_scope(u_client_internal_id, agent_aud_scope_id, {})
```

### AuthBridge side (token validation)

The `jwt-validation` plugin in A's sidecar reads the expected audience from `/shared/client-id.txt`
(A's SPIFFE ID, written by the operator at pod start). On every inbound request it calls
`auth.HandleInbound()` (`authlib/auth/auth.go:278`), which performs three checks in sequence:

| Check | Failure |
|-------|---------|
| **Signature** — verified via JWKS endpoint | `401` — token forged or tampered |
| **Issuer** (`iss` claim) — compared to configured `issuer` | `401` — wrong realm / wrong IdP |
| **Audience** (`aud` claim) — must contain A's SPIFFE ID | `401` — token not issued for A |

All three must pass before the request reaches agent A's application process.

---

## 4. Q2 — What ensures Agent A can access Tool T on behalf of User U?

Three components work in sequence:

**Step 1 — AuthBridge outbound routing (`authproxy-routes`):**
When A calls `http://tool-T-service/…`, the `token-exchange` plugin intercepts the request,
matches the `Host` header against `authproxy-routes`, and resolves `target_audience=T-SPIFFE` and
`token_scopes=openid tool-T-aud`. The current `Authorization: Bearer token₁` is extracted as
`subject_token`.

**Step 2 — Keycloak RFC 8693 exchange:**
AuthBridge POSTs to Keycloak using A's own client credentials as the acting party:
```
grant_type         = urn:ietf:params:oauth:grant-type:token-exchange
subject_token      = <U's token₁>
subject_token_type = urn:ietf:params:oauth:token-type:access_token
audience           = spiffe://…/sa/tool-T
scope              = openid tool-T-aud
client_id          = spiffe://…/sa/agent-A       ← A is the acting client
client_secret      = <A's credential>
```
Keycloak applies the three gates and — on success — issues token₂ with the same `sub` (U's
identity is preserved) but `aud: ["spiffe://…/sa/tool-T"]`.

**Step 3 — Tool T inbound validation:**
T's AuthBridge sidecar runs the same `jwt-validation` plugin. It verifies signature, issuer, and
`aud == T-SPIFFE`. Token₂ passes. T's process receives the request.

---

## 5. Q3 — Why can't User Y access Agent A? How to allow U but deny Y?

### Why Y is denied

Y is a legitimate Keycloak user but their token does not contain `aud = A-SPIFFE`. This is because
the `agent-A-aud` scope is **not assigned** to the client Y authenticates through. Keycloak only
includes a scope's audience mapper in a token if that scope is assigned to the requesting client.
Y's token therefore has no matching `aud` entry, and A's `jwt-validation` plugin returns `401`.

### Mechanism 1: per-client scope assignment (different client apps)

Assign `agent-A-aud` as a default scope only on U's client. Y's client never receives this
assignment:

```python
keycloak_admin.add_client_default_client_scope(u_client_id, agent_aud_scope_id, {})
# Y's client is never passed to this function
```

### Mechanism 2: scope-to-role gating (same client app, different users)

When U and Y authenticate through the **same** client, use Keycloak's scope-to-role mapping:

**① Disable `fullScopeAllowed` on the client** — `fullScopeAllowed` is a boolean field on the
**ClientRepresentation** (not on a client scope). While it is `true`, every role the client can
access is added to its tokens regardless of scope-mappings, which defeats role gating. Set it
`false` on the client so only roles bound via scope-mappings are included:
```python
client_representation["fullScopeAllowed"] = False
admin.update_client(client_id, client_representation)
```

**② Bind the scope to a client role via scope-mappings** — the scope is only included in tokens
issued to users who hold the mapped role:
```python
# POST /admin/realms/{realm}/client-scopes/{scope_id}/scope-mappings/clients/{client_id}
admin.connection.raw_post(url, data=json.dumps([role_representation]))
```

**③ Assign realm roles to users** — U gets a role that maps to the gating client role; Y does not:
```python
admin.assign_realm_roles(u_id, [admin.get_realm_role("developer")])
admin.assign_realm_roles(y_id, [admin.get_realm_role("tech-support")])
```

Result: even through the same client, U's token carries `aud=A-SPIFFE`; Y's does not.

---

## 6. Q4 — Why can't Agent A access Tool T on behalf of User Y? How to allow U but deny Y?

### Why the exchange fails for Y

When A's AuthBridge exchanges Y's token it sends Y's token as `subject_token` and requests
`scope=tool-T-aud`. Keycloak evaluates gate 3:

> *"Does the subject (Y) have access to the `tool-T-aud` scope?"*

The `tool-T-aud` scope has `fullScopeAllowed=false` and a scope-to-role mapping that gates it to
users holding the `tool-T:tool-T-aud` client role. Y's realm role does not map to that client role.
Keycloak refuses to include the scope and returns:

```
HTTP 400  {"error": "invalid_scope"}
```

AuthBridge receives this and the outbound call to T fails. A returns `503` to Y. T is never reached.

The policy decision lives entirely in Keycloak's scope-to-role evaluation. Changing which users
can reach T requires only a Keycloak configuration change — no agent redeployment.

---

## 7. Q5 — Why can't Agent B access Tool T? How to allow A but deny B?

### Why B is denied

B is a fully registered Keycloak client with `standard.token.exchange.enabled=true`. When B's
AuthBridge attempts the exchange, Keycloak applies gate 2:

> *"Is `tool-T-aud` assigned (default or optional) to B's client?"*

The answer is **no**. `tool-T-aud` was added as an optional scope only to A's client. B's client
has no such assignment → `400 invalid_scope`.

### What if B omits the scope and sends only `audience=T-SPIFFE`?

Keycloak falls back to T's default scopes. `tool-T-aud` is registered as **optional** (not
default) at realm level, so it is not included. T's inbound `jwt-validation` rejects the token
with `401` because `aud` does not contain T-SPIFFE.

### The two complementary locks

| Lock | Blocks | Configuration |
|------|--------|---------------|
| `tool-T-aud` not assigned as optional to B's client | `400 invalid_scope` | `client_audience_targets` omits B; `add_client_optional_client_scope` called only for A |
| `tool-T-aud` is optional (not default) on T's client | `401` at T inbound even if B omits scope | `add_default_optional_client_scope` (optional), never `add_default_default_client_scope` |

---

## 8. Summary (flat model)

| Boundary | Enforcement point | Keycloak mechanism | Failure |
|----------|------------------|--------------------|---------|
| Y cannot reach A | AuthBridge `jwt-validation` at A | `agent-A-aud` scope not on Y's client / scope-role gate excludes Y | `401 Unauthorized` |
| A cannot reach T on Y's behalf | Keycloak RFC 8693 gate 3 | `tool-T-aud` scope-role mapping excludes Y's realm role | `400 invalid_scope` → `503` to Y |
| B cannot reach T on anyone's behalf | Keycloak RFC 8693 gate 2 | `tool-T-aud` not assigned as optional to B's client | `400 invalid_scope` → `503` to B's caller |

In all three cases the policy **decision** is made in **Keycloak at token-issuance time** — Keycloak
acts as the **PDP** (Policy Decision Point), deciding which audiences/scopes a token may carry.
AuthBridge is a pure **PEP** (Policy Enforcement Point): it enforces those decisions by validating the
issued token and carries no policy knowledge of its own.

---

---

# RBAC Extension: Role-Mapped Model (UR and YR) — Keycloak-Native Reference

> **Note:** The RBAC sections below (§9–§17) describe the Keycloak-native
> access control model where Keycloak itself evaluates gate 3 (scope-to-role
> gating). This model is retained as reference material.
>
> The **production model** is OPA-as-PDP (§18–§23): OPA plugins on the AuthBridge
> inbound and outbound pipelines own all access control rule evaluation.
> Keycloak is reduced to a token issuer and exchange facilitator (gates 1 and 2
> only). Scope-to-role mappings, client roles, and composite role mappings on
> realm roles are **not required** in the OPA model.

> The following sections (§9–§13) re-answer all questions for the RBAC
> security model where:
> - **User U** is assigned realm role **UR**
> - **User Y** is assigned realm role **YR**
>
> UR grants access to A and through A to T.  YR does not.

---

## 9. RBAC Model Overview

Keycloak's RBAC wiring connects four layers in a directed graph:

```
Realm role (UR / YR)
    │  composite role mapping
    ▼
Client role  (e.g. agent-A:github-agent, github-tool:github-tool-aud)
    │  scope-to-role mapping
    ▼
Client scope  (e.g. agent-A-aud, tool-T-aud)
    │  oidc-audience-mapper
    ▼
Token aud claim  (A-SPIFFE, T-SPIFFE)
```

The key principle: **a user's realm role determines which client roles they hold; client roles gate
which scopes appear in their tokens; scopes carry the audience claims that AuthBridge validates**.

### Object inventory for the RBAC model

| Object | Type | Key Properties |
|--------|------|----------------|
| Realm role `UR` | Realm Role | Assigned to User U; made composite of A's and T's client roles |
| Realm role `YR` | Realm Role | Assigned to User Y; no composite mappings toward A or T |
| Client `agent-A` | Keycloak Client | `clientId=A-SPIFFE`; `standard.token.exchange.enabled=true`; `fullScopeAllowed=false` |
| Client `tool-T` | Keycloak Client | `clientId=T-SPIFFE`; `standard.token.exchange.enabled=true`; `fullScopeAllowed=false` |
| Client role `agent-A:github-agent` | Client Role | Defined on A's client; gates the `agent-A-aud` scope |
| Client role `tool-T:tool-T-aud` | Client Role | Defined on T's client; gates the `tool-T-aud` scope |
| Client scope `agent-A-aud` | Client Scope | `oidc-audience-mapper` → A-SPIFFE; `fullScopeAllowed=false`; scope-mapped to `agent-A:github-agent` |
| Client scope `tool-T-aud` | Client Scope | `oidc-audience-mapper` → T-SPIFFE; `fullScopeAllowed=false`; scope-mapped to `tool-T:tool-T-aud` |
| User U | Realm User | `assign_realm_roles(U, [UR])` |
| User Y | Realm User | `assign_realm_roles(Y, [YR])` |

### Policy file shape (YAML applied via `apply_policy.py`)

```yaml
# access_control_policy.yaml
policy:
  UR:                                   # realm role for User U
    - client: "agent-A"                 # grants access to A
      role:   "github-agent"
    - client: "github-tool"             # grants access to T (via A)
      role:   "github-tool-aud"
  YR:                                   # realm role for User Y
    []                                  # no client role mappings → no access to A or T
```

Applied with:
```python
# keycloak_ops/apply_policy.py — add_client_role_to_realm_role_composite()
url = f"/admin/realms/{realm}/roles-by-id/{realm_role['id']}/composites"
admin.connection.raw_post(url, data=json.dumps([client_role]))
```

---

## 10. RBAC Q1 — What ensures User U (role UR) can access Agent A?

### Full wiring

```
U authenticates → token₁ request
Keycloak evaluates for each scope assigned to U's client:
  agent-A-aud scope:
    fullScopeAllowed=false → check scope-mappings
    scope-mapping requires: agent-A:github-agent
    Does U hold agent-A:github-agent?
      U holds realm role UR
      UR is composite of: agent-A:github-agent  ✓
    → include aud mapper → token₁ gets aud=[A-SPIFFE]
```

Three setup steps enable this:

**① Create client role on A's client:**
```python
admin.create_client_role(agent_A_internal_id, {"name": "github-agent", "clientRole": True})
```

**② Create scope `agent-A-aud` with `fullScopeAllowed=false` and scope-map it to that role:**
```python
scope_representation["fullScopeAllowed"] = False
admin.update_client_scope(scope_id, scope_representation)
# POST /admin/realms/{realm}/client-scopes/{scope_id}/scope-mappings/clients/{agent_A_id}
# Body: [github-agent role representation]
```

**③ Make realm role UR a composite of `agent-A:github-agent`:**
```python
# POST /admin/realms/{realm}/roles-by-id/{UR_id}/composites
# Body: [github-agent client role representation]
```

AuthBridge at A then validates as before: signature + issuer + `aud == A-SPIFFE`. Token₁ passes;
the request is forwarded to A's process.

---

## 11. RBAC Q2 — What ensures Agent A can access Tool T on behalf of User U (role UR)?

The exchange works at two levels simultaneously:

**Level 1 — A's client is allowed to request `tool-T-aud`** (gate 2):
```python
admin.add_client_optional_client_scope(agent_A_internal_id, tool_T_aud_scope_id, {})
```
This is driven by `client_audience_targets` in `config.yaml`:
```yaml
client_audience_targets:
  spiffe://…/sa/agent-A:
    - github-tool    # → assigns tool-T-aud as optional to A's client
```

**Level 2 — U's token (subject) carries the right role to pass gate 3:**
```
Keycloak evaluates gate 3 during exchange:
  tool-T-aud scope:
    fullScopeAllowed=false → check scope-mappings
    scope-mapping requires: tool-T:tool-T-aud
    Does U hold tool-T:tool-T-aud?
      U holds realm role UR
      UR is composite of: tool-T:tool-T-aud  ✓
    → include aud mapper → token₂ gets aud=[T-SPIFFE]
```

The composite role mapping on UR for the tool-side client role is what grants U's token the right
to pass through the tool boundary. Without it, the exchange returns `400 invalid_scope` even if A
is correctly configured.

---

## 12. RBAC Q3 — Why can't User Y (role YR) access Agent A?

### The denial chain

```
Y authenticates → token request
Keycloak evaluates agent-A-aud scope:
  fullScopeAllowed=false → check scope-mappings
  scope-mapping requires: agent-A:github-agent
  Does Y hold agent-A:github-agent?
    Y holds realm role YR
    YR has NO composite mapping to agent-A:github-agent  ✗
  → scope excluded → token has no aud=A-SPIFFE
```

Y's token is issued without `aud=A-SPIFFE`. AuthBridge at A calls `a.verifier.Verify(ctx, token,
[A-SPIFFE])` — the `aud` claim does not contain A-SPIFFE → `DENY_JWT_FAILED` → **HTTP 401**.

### What makes U different from Y

The only difference is the composite role mapping on their respective realm roles:

| Realm role | Composite: `agent-A:github-agent` | Result |
|------------|-----------------------------------|--------|
| **UR** (U's role) | ✓ present | `agent-A-aud` scope included → `aud=[A-SPIFFE]` in token |
| **YR** (Y's role) | ✗ absent | `agent-A-aud` scope excluded → token has no A-SPIFFE audience |

### How to grant U but deny Y

The entire distinction lives in the policy YAML applied via `apply_policy.py`. No code changes, no
client reconfiguration — only the composite mappings on UR and YR:

```yaml
policy:
  UR:
    - client: "agent-A"
      role:   "github-agent"      # ← present: U gets aud=A-SPIFFE
  YR:
    []                            # ← absent: Y never gets aud=A-SPIFFE
```

Applied:
```python
# For UR → adds agent-A:github-agent to UR's composites
add_client_role_to_realm_role_composite(admin, realm, "UR", agent_A_id, "github-agent")
# YR: no call → YR has no composites → Y's tokens never reach A
```

---

## 13. RBAC Q4 — Why can't Agent A access Tool T on behalf of User Y (role YR)?

### The denial chain

Even if Y somehow reaches A (e.g. Y is granted access to A but not to T), when A's AuthBridge
performs the exchange:

```
RFC 8693 exchange POST:
  subject_token = Y's token
  audience      = T-SPIFFE
  scope         = openid tool-T-aud
  client_id     = A-SPIFFE  (acting client — A is still allowed to exchange)

Keycloak evaluates gate 3:
  tool-T-aud scope:
    fullScopeAllowed=false → check scope-mappings
    scope-mapping requires: tool-T:tool-T-aud
    Does Y hold tool-T:tool-T-aud?
      Y holds realm role YR
      YR has NO composite mapping to tool-T:tool-T-aud  ✗
  → scope denied → HTTP 400 invalid_scope
```

AuthBridge receives `400 invalid_scope`. The outbound call to T fails with `503` back to Y.
T is never contacted.

### What makes U different from Y at the T boundary

| Realm role | Composite: `tool-T:tool-T-aud` | Exchange result |
|------------|--------------------------------|-----------------|
| **UR** (U's role) | ✓ present | Gate 3 passes → token₂ issued with `aud=[T-SPIFFE]` |
| **YR** (Y's role) | ✗ absent | Gate 3 fails → `400 invalid_scope` |

### How to grant U but deny Y at T

Again, the entire distinction is in the policy YAML:

```yaml
policy:
  UR:
    - client: "agent-A"
      role:   "github-agent"      # access to A
    - client: "github-tool"
      role:   "github-tool-aud"   # ← present: U's token passes gate 3 at T
  YR:
    - client: "agent-A"
      role:   "github-agent"      # access to A (if desired)
    # github-tool mapping absent  # ← absent: Y's token fails gate 3 → 400
```

This separation means:
- YR can be granted access to A without gaining any access to T
- Revoking Y's access to T requires only removing `tool-T:tool-T-aud` from YR's composites
- No redeployment of A or T is needed

---

## 14. RBAC Q5 — Why can't Agent B access Tool T? (RBAC model, same answer)

The RBAC model adds no new mechanism here; the answer is identical to §7. The gate that blocks B
is **gate 2** (acting-client check), which is about B's Keycloak client not having `tool-T-aud`
as an optional scope. This is independent of realm roles — realm roles gate the subject token
(gates 3), while the acting-client check (gate 2) gates the exchanger.

The `client_audience_targets` map in `config.yaml` is still the single source of truth:

```yaml
client_audience_targets:
  spiffe://…/sa/agent-A:
    - github-tool     # A can call T
  # agent-B absent → B's client never gets tool-T-aud as optional scope
```

---

## 15. RBAC End-to-End: Full Object Map

```
Keycloak Realm: rossoctl
│
├── Realm Roles
│   ├── UR  ──composite──► agent-A:github-agent
│   │        ──composite──► tool-T:tool-T-aud
│   └── YR  (no composites toward A or T)
│
├── Clients
│   ├── kagenti (UI / E2E)
│   │   └── default scope: agent-A-aud
│   │
│   ├── agent-A  [clientId=A-SPIFFE]
│   │   ├── standard.token.exchange.enabled=true
│   │   ├── fullScopeAllowed=false
│   │   ├── client role: github-agent
│   │   ├── default scope:  agent-A-aud
│   │   └── optional scope: tool-T-aud
│   │
│   └── tool-T  [clientId=T-SPIFFE]
│       ├── standard.token.exchange.enabled=true
│       ├── fullScopeAllowed=false
│       └── client role: tool-T-aud
│
├── Client Scopes
│   ├── agent-A-aud
│   │   ├── oidc-audience-mapper → A-SPIFFE
│   │   ├── fullScopeAllowed=false
│   │   └── scope-mapping → agent-A:github-agent
│   │
│   └── tool-T-aud
│       ├── oidc-audience-mapper → T-SPIFFE
│       ├── fullScopeAllowed=false
│       └── scope-mapping → tool-T:tool-T-aud
│
└── Users
    ├── U  ──realm role──► UR
    └── Y  ──realm role──► YR
```

---

## 16. Consolidated Summary: All Boundaries, Both Models

### Flat model (no RBAC)

| Boundary | Gate | Keycloak mechanism | Failure |
|----------|------|--------------------|---------|
| Y → A | AuthBridge inbound | `agent-A-aud` scope not assigned to Y's client | `401` |
| A→T on Y's behalf | RFC 8693 gate 3 | `tool-T-aud` scope-role gate excludes Y's role | `400 invalid_scope` |
| B → T (any subject) | RFC 8693 gate 2 | `tool-T-aud` not optional on B's client | `400 invalid_scope` |

### RBAC model (UR / YR)

| Boundary | Gate | Keycloak mechanism | Failure |
|----------|------|--------------------|---------|
| Y (role YR) → A | AuthBridge inbound | YR has no composite → `agent-A:github-agent` → scope `agent-A-aud` excluded from Y's token | `401` |
| A → T on Y's behalf | RFC 8693 gate 3 | YR has no composite → `tool-T:tool-T-aud` → scope `tool-T-aud` excluded from Y's token | `400 invalid_scope` |
| B → T (any subject) | RFC 8693 gate 2 | B's client not in `client_audience_targets` → `tool-T-aud` never assigned to B's client | `400 invalid_scope` |

### Where each policy decision lives

| Question | Policy object | API call |
|----------|--------------|----------|
| Can U reach A? | UR composite → `agent-A:github-agent` | `POST /roles-by-id/{UR_id}/composites` |
| Can Y reach A? | YR composite (absent) | (no call for YR) |
| Can U's token pass gate 3 at T? | UR composite → `tool-T:tool-T-aud` | `POST /roles-by-id/{UR_id}/composites` |
| Can Y's token pass gate 3 at T? | YR composite (absent) | (no call for YR) |
| Can A act as exchanger for T? | A's optional scope: `tool-T-aud` | `add_client_optional_client_scope(A, tool-T-aud)` |
| Can B act as exchanger for T? | B's optional scope (absent) | (no call for B) |

### Design principle

In the RBAC model, all policy decisions are encoded in **composite role mappings** on realm roles.
AuthBridge remains a pure PEP and never changes. A, T, and their client configurations remain
static. Changing who can access what — granting U access to T, revoking Y's access to A — is a
single `POST /roles-by-id/{role_id}/composites` call. No pod restarts, no client reconfiguration,
no AuthBridge changes required.

---

## 17. Key Files Reference

| File | Relevance |
|------|-----------|
| `authbridge/demos/weather-agent/setup_keycloak_weather_advanced.py` | Concrete scope + client setup for a single agent + tool scenario |
| `authbridge/demos/github-issue/setup_keycloak.py` | Full RBAC mode: clients, roles, scope-role gating (`map_scopes_to_roles`), users |
| `authbridge/demos/github-issue/aiac/config.yaml` | Declarative `client_audience_targets`, `realm_roles`, `users` with role assignments |
| `authbridge/demos/github-issue/aiac/keycloak_ops/apply_policy.py` | Applies composite role mappings: realm role → client role (`add_client_role_to_realm_role_composite`) |
| `authbridge/demos/github-issue/aiac/keycloak_ops/delete_policy.py` | Removes composite mappings from realm roles without touching user assignments |
| `authbridge/demos/github-issue/aiac/policies/regular_policy.txt` | Example natural-language policy the AIAC agent translates into composite mappings |
| `authbridge/docs/plugin-reference.md` | `jwt-validation` and `token-exchange` plugin contracts |
| `authlib/auth/auth.go` | `HandleInbound`: the three JWT checks (signature, issuer, audience) |
| `authlib/plugins/tokenexchange/exchange/client.go` | RFC 8693 exchange POST construction |
| `authbridge/demos/token-exchange-routes/README.md` | `authproxy-routes` ConfigMap shape and troubleshooting |

---

---

# OPA-as-PDP Architecture: Minimal Keycloak Configuration

## 18. OPA as PDP — Design Principle

In the OPA-as-PDP architecture, two OPA plugins are attached to the AuthBridge inbound and
outbound pipelines:

- **Inbound OPA plugin** — evaluates whether the incoming request's subject (JWT `sub` claim,
  roles, and other token claims) is permitted to call this service. Replaces the audience-only
  check of the Keycloak-native model with full policy evaluation.
- **Outbound OPA plugin** — evaluates whether the subject token is permitted to be exchanged
  for a target-audience token before AuthBridge performs the RFC 8693 exchange.

OPA owns **all access control rule formulation and validation**. Keycloak is responsible only for:

1. **Token issuance** — issuing tokens with the correct `aud` claim so AuthBridge inbound can
   verify token authenticity (gate 1: signature + issuer; gate 2: audience anchor).
2. **Token exchange facilitation** — performing RFC 8693 exchanges, enforcing only that the
   acting client is registered and has the target scope assigned (gates 1 and 2). Gate 3
   (scope-to-role evaluation) is **bypassed**: scopes are created with `fullScopeAllowed=true`
   so Keycloak never evaluates role membership — that decision belongs to OPA.

---

## 19. What Keycloak No Longer Needs (OPA Model)

The following Keycloak objects and configuration steps from the RBAC model (§9–§17) are
**not required** in the OPA model:

| Object / Operation | Old purpose | OPA model |
|---|---|---|
| `fullScopeAllowed=false` on scopes | Gate 3: role-based scope inclusion | **Not needed** — scopes use `fullScopeAllowed=true` |
| Scope-to-role mappings (`scope-mappings/realm`) | Gate 3: which roles unlock which scopes | **Not needed** — OPA evaluates this |
| Scope-to-client-role mappings (`scope-mappings/clients/{id}`) | Gate 3 via client role intermediary | **Not needed** |
| Client roles (`agent-A:github-agent`, `tool-T:tool-T-aud`) | Scope-to-role mapping target | **Not needed** |
| Realm roles as composites of client roles | Policy application lever | **Not needed** as Keycloak mechanism |
| `POST /roles-by-id/{role_id}/composites` | Apply composite mapping | **Not needed** |

> **Realm roles may still be useful as OPA input data.** If users carry realm role claims in
> their JWT (e.g. `roles: ["developer"]`), OPA can use those claims as policy input. But
> their presence in the token is incidental — they are no longer the mechanism that gates
> scope inclusion.

---

## 20. Minimal Keycloak Configuration (OPA Model)

```
Realm: rossoctl
│
├── Clients
│   ├── kagenti-ui  (or per-user client)
│   │   └── default scope: agent-A-aud      ← token₁ always has aud=A-SPIFFE
│   │
│   ├── agent-A  [clientId=A-SPIFFE]
│   │   ├── standard.token.exchange.enabled=true
│   │   └── optional scope: tool-T-aud      ← gate 2: A can request T audience
│   │
│   └── tool-T  [clientId=T-SPIFFE]
│       └── standard.token.exchange.enabled=true
│
├── Client Scopes
│   ├── agent-A-aud  (oidc-audience-mapper → A-SPIFFE, fullScopeAllowed=true)
│   └── tool-T-aud   (oidc-audience-mapper → T-SPIFFE, fullScopeAllowed=true)
│
└── Users
    ├── U  (realm roles populated as JWT claims for OPA input)
    └── Y
```

Each audience scope (`X-aud`) is created once when the service is registered and assigned as:
- **Default** client scope on the callee's own client — a *default* client scope is added to
  **every** token issued for that client automatically, without the caller having to request it via
  the `scope` parameter, so those tokens always carry `aud=X-SPIFFE` (from the scope's audience
  protocol mapper).
- **Optional** client scope on each caller agent's client — an *optional* scope is included **only
  when explicitly requested** (here, during the token exchange), which is what enables gate 2 for
  that agent's token exchange calls.

`fullScopeAllowed=true` everywhere — Keycloak never gates by role. OPA receives the full
subject token and makes all access decisions.

---

## 21. The Two Remaining Keycloak Gates (OPA Model)

| Gate | Enforced by | What it checks | What must exist |
|------|-------------|----------------|-----------------|
| **Gate 1** | Keycloak | `standard.token.exchange.enabled=true` on the acting client | Set at service registration |
| **Gate 2** | Keycloak | Is `tool-T-aud` assigned (default or optional) to the caller's client? | `add_client_optional_client_scope(caller, tool-T-aud)` |
| **Gate 3** | ~~Keycloak~~ **OPA** | Does the subject have permission to reach this target? | OPA policy — no Keycloak configuration |

Gate 2 remains a Keycloak-enforced boundary but is now **topological** (which agents can
attempt an exchange toward which tools) rather than **subject-based** (which users can call
which tools through which agents). OPA handles subject-based policy on top.

---

## 22. Service Registration and Wiring (OPA Model)

The two operations that produce the required Keycloak state for a new service are:

### `register_service_audience(service)` — callee setup

Creates the service's canonical audience scope `{serviceId}-aud` and assigns it as a
**default** scope to the service's Keycloak client. Called once when a new agent or tool
is registered.

```
POST /scopes          {"name": "github-tool-aud", "protocol": "openid-connect"}
POST /services/{id}/scopes/{scope_id}/default
```

After this call, any user authenticating through `github-tool`'s client receives a token
with `aud=github-tool-SPIFFE` — AuthBridge inbound at `github-tool` will accept it.

### `allow_service_to_call(caller, callee)` — gate 2 wiring

Assigns the callee's canonical audience scope as an **optional** scope to the caller's
Keycloak client. Called whenever a new caller→callee edge is permitted.

```
POST /services/{caller.id}/scopes/{callee_aud_scope.id}/optional
```

After this call, `caller`'s AuthBridge can perform an RFC 8693 exchange requesting
`scope=github-tool-aud`, passing gate 2. OPA's outbound plugin then makes the final
allow/deny decision before the exchange is forwarded.

Both operations are **idempotent** — safe to call on every operator reconciliation.

---

## 23. Summary: Both Models Side by Side

| Dimension | Keycloak-native RBAC (§9–§17) | OPA-as-PDP (§18–§22) |
|---|---|---|
| **Gate 3 enforcement** | Keycloak scope-to-role evaluation | OPA outbound plugin |
| **Policy change mechanism** | `POST /roles-by-id/{id}/composites` | OPA Rego policy update |
| **Client roles required** | Yes (scope-to-role mapping targets) | No |
| **Composite role mappings** | Yes (policy lever) | No |
| **`fullScopeAllowed`** | `false` (role gate enabled) | `true` (OPA owns the gate) |
| **Keycloak objects per boundary** | Realm role + client role + scope-mapping + composite | Audience scope only |
| **Policy redeployment on change** | No (Keycloak API call only) | No (OPA policy update only) |
| **AuthBridge changes on policy change** | None | None |
