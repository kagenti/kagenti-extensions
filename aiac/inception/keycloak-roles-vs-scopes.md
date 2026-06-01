# Keycloak: Client Roles vs Client Scopes

## Definitions

**Client Role** — a named permission defined within a specific client (e.g., `my-app:admin`). Represents what a user is authorized to do inside that application. Assigned to users or groups.

**Client Scope** — a reusable container that bundles claims, role mappers, and protocol mappers. Controls what gets included in a token when a client requests it. Defined at the realm level and shared across clients.

| | Client Role | Client Scope |
|---|---|---|
| **What it is** | A named permission/authority | A reusable claim bundle |
| **Defined on** | A specific client | Realm level, shared across clients |
| **Controls** | What a user is authorized to do | What appears in the access token |
| **Assigned to** | Users / groups | Clients (as default or optional scope) |

## Role-to-Scope Mapping

The relationship is not one-to-one. It is flexible in both directions.

- **One scope → many roles**: a `reporting` scope can inject `analytics-service:read`, `data-warehouse:export`, and `audit-log:view` in a single grant.
- **One role → many scopes**: `user-service:read` can appear in both a `basic-access` (default) and a `user-management` (optional) scope.
- **Scope with no roles**: carries only protocol mappers (e.g., `email`, `given_name`). The built-in OIDC `profile` and `email` scopes work this way.
- **Role with no scope**: a role assigned directly to a user appears in the token without any scope involvement, as long as the default `roles` mapper is active.
- **Many clients, one scope**: a `service-account` scope defined once at the realm level can be assigned as a default to many backend clients, avoiding per-client configuration.

## Are Scopes Required for RBAC or ABAC?

No.

### RBAC without scopes

Assign client roles directly to users or groups. The roles land in the token via the default `roles` mapper. The application reads `resource_access["my-app"].roles` and gates access.

```json
{ "resource_access": { "my-app": { "roles": ["admin"] } } }
```

### ABAC without scopes

Add a custom user attribute mapper directly on the client. It injects attributes into the token, which the app or a policy engine (e.g., OPA) evaluates for access decisions.

```json
{ "department": "finance", "clearance": "secret" }
```

## When Scopes Are Worth Using

- **Multi-client reuse** — same role bundle or attribute set needed across many clients
- **OAuth2 consent flows** — user explicitly grants a third-party app access to specific capabilities
- **Least-privilege token narrowing** — client requests a reduced scope to get a token with fewer permissions than the user holds
- **Federation / external IdP** — normalizing incoming claims from an external provider across all clients

## Summary

For internal applications, direct role and attribute mappers on the client are simpler and sufficient. Scopes become worthwhile when many clients share the same access model, or when doing OAuth2 delegation flows where the user consents to what a client may access on their behalf.

---

## How a Client Requests a Scope (Technical Detail)

The `scope` parameter is a space-separated string sent in the POST body to Keycloak's token endpoint. It is a *ceiling request* — Keycloak returns only the claims/roles mapped by those scopes, even if the subject holds broader permissions.

```
POST /realms/kagenti/protocol/openid-connect/token
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=<caller-jwt>
audience=github-tool
scope=openid github-tool-aud github-full-access
```

Keycloak evaluates three constraints before issuing the narrowed token:

| Constraint | What Keycloak checks |
|---|---|
| Client allowed? | Is `token-exchange` enabled on the acting client? |
| Scope registered? | Is the requested scope configured on the target client? |
| Subject holds it? | Does the subject token's holder actually have access to that scope? |

## How It Works in Kagenti / AuthBridge

The requesting entity is never the application developer manually — it is **AuthBridge's token-exchange plugin** acting transparently on behalf of the workload.

### 1. Static route configuration (`authproxy-routes` ConfigMap)

An operator declares per outbound target what scopes to request:

```yaml
routes:
  - host: "github-tool-mcp"
    target_audience: "github-tool"
    token_scopes: "openid github-tool-aud github-full-access"
```

### 2. Intercept outbound request

When the workload makes an HTTP call to `github-tool-mcp`, AuthBridge's forward proxy intercepts it, matches the host against the route table, and resolves the target audience and scopes.

### 3. RFC 8693 token exchange call

AuthBridge calls Keycloak's token endpoint with the subject token, audience, and scope. Keycloak mints a new, narrowed access token. AuthBridge injects it into the outbound request — the workload itself is unaware.

### 4. End result

Even if the calling agent holds `read`, `write`, and `admin` roles, the token delivered to `github-tool` contains only the claims mapped by the requested scopes. The token is valid for exactly one target audience, for the duration of that call.

---

## Keycloak as PDP, AuthBridge as Pure PEP

Without automated policy management, the natural Kagenti design is a hybrid PDP/PEP: AuthBridge influences policy by declaring `token_scopes` in the route config. Keycloak validates and issues accordingly, but the *intent* lives in AuthBridge. This is the baseline to reason from.

AIAC (AI-based Access Control) is the system that replaces this hybrid with a clean PDP/PEP split. The mechanism is described in detail in [The Kagenti/AIAC Solution](#the-kagentiaiac-solution) below; this section establishes the structural change that AIAC implements.

### What changes

AuthBridge sends the exchange request **without a `scope` parameter** — only the `audience`:

```
POST /realms/kagenti/protocol/openid-connect/token
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=<caller-jwt>
audience=github-tool
```

Keycloak then applies its own configured policies to determine what goes into the issued token.

### How Keycloak carries the policy

| Mechanism | How it works |
|---|---|
| **Default scopes on the target client** | Scopes listed as default are always included in any token issued for that audience, regardless of what the requester asks. |
| **Token exchange policies (Keycloak 26+)** | Fine-grained policies define, per `(calling_client → target_client)` pair, exactly what scopes are granted. Policy lives entirely in Keycloak. |
| **Optional scopes** | A scope marked optional on the target client is only included if explicitly requested. Not requesting it = not included. The optional/default split is itself a policy instrument. |

### Structural comparison

| | Current (hybrid) | Pure PDP/PEP |
|---|---|---|
| Where scope policy lives | `authproxy-routes` in AuthBridge | Keycloak client config / exchange policies |
| AuthBridge sends | `audience` + `scope` | `audience` only |
| Keycloak role | Validates and enforces ceiling | Decides and issues |
| AuthBridge role | Influences and enforces | Enforces only |

### Tradeoffs

**Pro:** Keycloak becomes the single source of truth for what a service is allowed to receive. Policy is auditable in one place.

**Con:** When no `scope` is sent, Keycloak defaults to issuing all default scopes for the target client. If those defaults are broad, the least-privilege guarantee depends on careful default scope configuration on each target client — the discipline shifts from AuthBridge route config to Keycloak realm config.

**Con:** Requires Keycloak 26+ token exchange policies for per-caller-pair scope control. Below that, only per-target defaults are available.

### Implementation requirements

1. Remove `token_scopes` from `authproxy-routes`
2. Audit and tighten default scopes on every target client in Keycloak
3. Use Keycloak 26+ token exchange policies if per-caller-pair scope control is needed
4. AIAC owns this configuration — encoding access policy as composite role mappings (Phase 1) or Rego rules (Phase 2) rather than per-route scope declarations in AuthBridge

---

## The Kagenti/AIAC Solution

AIAC implements and maintains the clean PDP/PEP split described above. It introduces a **policy management layer** between the natural-language policy source and the PDP, removing the need for any policy knowledge inside AuthBridge.

### Three-Layer Architecture

| Layer | Component | Responsibility |
|---|---|---|
| **Policy Management** | AIAC Agent | Reads natural-language policy from RAG store; translates it to PDP configuration on every trigger |
| **Policy Decision (PDP)** | Keycloak (Phase 1) / OPA (Phase 2) | Evaluates what a caller may access; issues scoped tokens |
| **Policy Enforcement (PEP)** | AuthBridge | Intercepts traffic; exchanges tokens with `audience` only; forwards narrowed credentials; carries no policy knowledge |

AuthBridge is a pure PEP in both phases. The PDP backend is the only moving part between phases.

### Phase 1: Keycloak Composite Role Mappings as PDP Policy

AIAC manages **realm role → service permission composite mappings** in Keycloak. Each realm role (e.g., `data-analyst`) is a composite that bundles the exact client-level permissions it should grant on each downstream service. When the caller's realm role changes or a new service is onboarded, AIAC recomputes and applies a minimal mapping delta.

AuthBridge performs the token exchange with `audience` only:

```
POST /realms/kagenti/protocol/openid-connect/token
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=<caller-jwt>
audience=target-service
```

Keycloak applies the composite mappings managed by AIAC and issues a token containing exactly the permissions the caller's realm role entitles on `target-service`. The `token_scopes` field is absent from `authproxy-routes`; routes carry only routing intent (`host` → `target_audience`).

The discipline of keeping composite mappings tight is delegated entirely to AIAC. It reacts to four trigger types:

| Trigger | Scope | What AIAC does |
|---|---|---|
| `client/{id}` (new service onboarded) | Single new service | Provisions permissions/scopes on the client; maps all realm roles that should access it |
| `realm-role/{id}` (role created/updated) | Single affected role | Recomputes composite mappings for that role only |
| `build` (policy document updated, incremental) | Minimal delta | Retrieves updated policy from RAG store; applies diff to composite mappings |
| `rebuild` (operator-initiated, full reset) | All mappings | Clears all composites; recomputes from scratch |

This covers the "Con" identified above — broad default scopes are controlled not by per-operator discipline but by automated, policy-driven AIAC recomputation.

### Phase 2: OPA as PDP — LLM-Generated Rego

Phase 2 replaces composite role mappings with LLM-generated Rego rules evaluated by OPA. AIAC generates Rego from the same natural-language policy RAG store and writes it to OPA via the PDP Policy Service — the same stable interface, different backend.

**Transition sequence:**

1. AIAC clears all composite mappings from Keycloak (Keycloak reverts to a pure token issuer with no embedded access policy).
2. The PDP Policy Service pod is swapped to the OPA implementation — same `aiac-pdp-policy-service` ClusterIP, same API contract. This is a deployment swap only.
3. AIAC begins writing LLM-generated Rego rules to OPA instead of composite mappings to Keycloak.

AuthBridge is unaffected. It continues sending audience-only token exchange requests. The PDP has changed; the PEP has not.

Phase 2 achieves a stronger separation: Rego rules express policy in a purpose-built, human-auditable language rather than as implicit Keycloak composite graph traversals.

### Comparison

| Concern | Hybrid (pre-AIAC) | AIAC Phase 1 (Keycloak) | AIAC Phase 2 (OPA) |
|---|---|---|---|
| Where access policy lives | `authproxy-routes` in AuthBridge | RAG store → Keycloak composite mappings | RAG store → OPA Rego rules |
| AuthBridge sends | `audience` + `scope` | `audience` only | `audience` only |
| PDP | Keycloak (validates ceiling) | Keycloak (decides from composites) | OPA (decides from Rego) |
| Policy auditability | Fragmented across route configs | Single source: RAG store + Keycloak composites | Single source: RAG store + Rego |
| New service onboarding | Manual: add scopes to every caller's route | Automated: AIAC provisions on `CLIENT_CREATED` | Automated: AIAC provisions on `CLIENT_CREATED` |
| Phase transition cost | — | Baseline | PDP pod swap; AuthBridge and AIAC unchanged |
