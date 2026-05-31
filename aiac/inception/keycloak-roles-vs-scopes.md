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
