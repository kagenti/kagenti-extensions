# Component PRD: Policy Store Library (`aiac.policy.store.library`)

Companion library for the [AIAC Policy Store](policy-store.md). Follows the same pattern as `aiac.pdp.policy.library` — module-level functions, URL from env var via `python-dotenv`, `RuntimeError` on non-2xx.

## Location
`aiac/src/aiac/policy/store/library/`

## Package structure

```
aiac/src/aiac/policy/store/
└── library/
    ├── __init__.py     # empty
    └── api.py          # five module-level functions (SPM-centric surface)
```

All `__init__.py` files are empty. Callers use explicit submodule paths:

```python
from aiac.policy.store.library.api import (
    get_service_policy,
    get_service_policy_by_scope,
    get_service_policies_by_role,
    apply_service_policy,
    delete_service_policy,
)
from aiac.policy.model.models import ServicePolicyModel, Scope, Role
```

---

## SPM redesign context

The `ServicePolicyModel` (SPM), keyed by `serviceId`, is the **persistent source of truth**.
The `AgentPolicyModel` (APM) is now **derived and never persisted** — so the store no longer
exposes any per-agent read/write functions. The library surface is entirely SPM-centric.

---

## Submodule: `aiac.policy.store.library.api`

### Description
HTTP client module wrapping the [AIAC Policy Store](policy-store.md) REST API. Exposes five module-level functions returning `ServicePolicyModel` objects directly — no Kubernetes client boilerplate. Service URL is read from the `AIAC_POLICY_STORE_URL` environment variable (default: `http://127.0.0.1:7074`). All functions raise `RuntimeError` on an unexpected non-2xx response (a `404` on the by-id read is handled, not raised — see below).

### Dependencies
```
requests
pydantic
python-dotenv
```

### Functions

```python
def get_service_policy(service_id: str) -> ServicePolicyModel
    # GET /policy/services/{service_id}
    # On miss (service returns 404) the library returns a *fresh empty*
    # ServicePolicyModel for that service_id — never raises on 404.
    # (Matches the existing "engine creates a fresh model on 404" convention.)

def get_service_policy_by_scope(scope: Scope) -> ServicePolicyModel | None
    # Singular: a scope has exactly one owning service (Assumption 2).
    # Sugar over get_service_policy(scope.serviceId) — resolves the owner via
    # scope.serviceId; no dedicated HTTP route.

def get_service_policies_by_role(role: Role) -> list[ServicePolicyModel]
    # GET /policy/services?role={role.id}  (the one genuinely new route)
    # Plural: a role (especially a user role) appears across many SPMs.
    # Returns every SPM whose inbound_rules contains a rule referencing
    # role.id. Empty list when none match.

def apply_service_policy(service_id: str, spm: ServicePolicyModel) -> None
    # POST /policy/services/{service_id}  — upsert.

def delete_service_policy(service_id: str) -> None
    # DELETE /policy/services/{service_id}  — off-board a decommissioned service.
    # No-op on the server if the service is absent (still 204).
```

**Removed** (APMs are no longer persisted): `get_agent_policy`, `apply_agent_policy`, and the
prior whole-collection `get_policy` / `apply_policy` / `delete_policy` / `delete_agent_policy`
functions. The only legitimate consumer is the Policy Computation Engine, which is migrated to the
functions above.

### Why by-role must be a store query (not an IdP lookup)

`get_service_policies_by_role` must return **stored** rows — including stale role→service mappings
that the live IdP no longer reflects. The Policy Computation Engine's override-purge (handoff 05)
depends on seeing exactly those stale rows so it can remove them. Because the SPM store is the
source of truth and the IdP is not, this query cannot be answered from the IdP; it is a query over
persisted SPMs. It may start as a full scan and later gain a `role.id -> {service_id}` index behind
the same signature without changing callers.

### Configuration

Read from `AIAC_POLICY_STORE_URL` environment variable (or `.env` file co-located with `api.py`). Falls back to the default if absent.

| Variable | Default |
|----------|---------|
| `AIAC_POLICY_STORE_URL` | `http://127.0.0.1:7074` |

### Usage

```python
from aiac.policy.store.library.api import (
    get_service_policy,
    get_service_policy_by_scope,
    get_service_policies_by_role,
    apply_service_policy,
    delete_service_policy,
)
from aiac.policy.model.models import ServicePolicyModel, Scope, Role

# Read current state for additive merge (fresh empty SPM on first sight)
current = get_service_policy("weather-service")

# Resolve the owning SPM of a scope
owner = get_service_policy_by_scope(scope)

# Find every SPM that grants a role (incl. stale mappings, for override-purge)
affected = get_service_policies_by_role(role)

# Write updated state (upsert)
apply_service_policy("weather-service", updated_spm)
```

---

## Testing Decisions

**Seam:** HTTP boundary — mock responses from `AIAC_POLICY_STORE_URL`.

**Prior art:** `3.14-unit-tests-write-api.md` (mock PDP Policy Writer HTTP; cover module-level functions).

Key behaviors to assert:
- `get_service_policy(id)` issues `GET /policy/services/{id}`; response body deserialized to `ServicePolicyModel` (hit).
- `get_service_policy(id)` on `404` returns a fresh empty `ServicePolicyModel` for that `service_id` — no `RuntimeError` (miss).
- `get_service_policy_by_scope(scope)` resolves via `scope.serviceId` (sugar over the by-id read).
- `get_service_policies_by_role(role)` issues the by-role query; returns every SPM referencing `role.id`; returns `[]` when none match; returns multiple when several match.
- `apply_service_policy(id, spm)` issues `POST /policy/services/{id}` with serialized `ServicePolicyModel`; upsert round-trip (write then read back the same SPM).
- `delete_service_policy(id)` issues `DELETE /policy/services/{id}`; returns `None` on success.
- Any unexpected non-2xx response raises `RuntimeError`.
- `AIAC_POLICY_STORE_URL` is read from env; falls back to `http://127.0.0.1:7074`.
</parameter>
</invoke>
