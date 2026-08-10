# Component PRD: AIAC Policy Store

## Problem Statement

The AIAC Agent's Policy Computation Engine produces and merges `ServicePolicyModel` (SPM) objects — one per service, keyed by `serviceId` — representing the inbound/outbound access control policy for each service. The PDP Policy Writer translates these into Rego packages and writes them to an `AuthorizationPolicy` Kubernetes CR — but this derived artifact cannot be reverse-engineered back into structured SPM data. Without a durable structured policy store:

- The Policy Computation Engine cannot read current policy state for additive merging — it must re-derive the full state from the PDP snapshot on every trigger.
- Override-purge cannot find stale role→service mappings that the live IdP no longer reflects — there is no record of what was previously granted.
- Pod restarts lose any in-flight policy construction context.

The `AgentPolicyModel` (APM) is **derived and never persisted** — it is computed on demand from SPMs. The store therefore has no per-agent surface; it persists SPMs only.

## Solution

A dedicated **AIAC Policy Store** owns an in-memory cache of `ServicePolicyModel` rows (keyed by `serviceId`) backed by a SQLite database for durability. A companion library [`aiac.policy.store.library`](library-policy-store.md) exposes module-level typed functions matching the `aiac.pdp.policy.library` pattern, used by the Policy Computation Engine to read and write SPM state without any storage-layer boilerplate.

The SPM is the **source of truth**. The PDP Policy Writer retains sole ownership of the `AuthorizationPolicy` CR (Rego packages) and has no dependency on the Policy Store. The two persistence artifacts serve distinct purposes and are owned by distinct services:

| Artifact | Owner | Contents |
|---|---|---|
| SQLite `service_policies` table | Policy Store | Structured `ServicePolicyModel`, keyed by `serviceId` — source of truth (cache-first, write-through) |
| `AuthorizationPolicy` CR (one total) | PDP Policy Writer | Derived Rego packages — OPA runtime artifact |

---

## User Stories

1. As the Policy Computation Engine, I want to read the current `ServicePolicyModel` for a specific service by id, so that I can additively append new rules without overwriting existing ones — receiving a fresh empty SPM the first time a service is seen.
2. As the Policy Computation Engine, I want to resolve the single SPM that owns a given scope, so that I can attach outbound/inbound rules to the correct service.
3. As the Policy Computation Engine, I want to list every SPM whose inbound rules reference a given role — including stale mappings the IdP no longer reflects — so that override-purge can remove access that should no longer exist.
4. As the Policy Computation Engine, I want to upsert a `ServicePolicyModel`, so that the current policy state survives pod restarts.
5. As a consumer of the Policy Store library, I want a typed Python library that returns `ServicePolicyModel` objects directly, so that I can work with structured policy data without writing storage client code.
6. As an operator, I want the Policy Store deployed as its own single-replica StatefulSet with a dedicated PVC, so that its storage and restart lifecycle is decoupled from the stateless policy services.

---

## Implementation Decisions

### Policy Store Service

**Location:** `aiac/src/aiac/policy/store/service/`

**Port:** `0.0.0.0:7074`

**ClusterIP Service:** `aiac-policy-store-service:7074`

**Deployment:** dedicated single-replica `StatefulSet` `aiac-policy-store`, with a `volumeClaimTemplate` PVC (1 Gi, `ReadWriteOnce`, cluster-default StorageClass) mounted at `/data`. Fronted by a headless Service for stable pod DNS plus the `aiac-policy-store-service:7074` ClusterIP for clients. Not co-located with IdP Configuration / PDP Policy Writer.

**Framework:** FastAPI + uvicorn. **Base image:** `python:3.12-slim`.

**Storage backend:** SQLite via `sqlite3` stdlib (zero extra dependency — `sqlite3` ships with the Python standard library). Database file: `SERVICEPOLICY_DB_PATH` (default `/data/policy_model.db`).

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS service_policies (
    service_id TEXT PRIMARY KEY,
    spec       TEXT NOT NULL   -- ServicePolicyModel.model_dump_json() as JSON
);
```

**In-memory cache:** the service owns all SPM rows in memory as the authoritative serving layer:
- All read requests served from memory (storage never queried at runtime).
- Every mutation writes through to SQLite synchronously before returning `204`.
- On pod restart: load all rows from SQLite → populate cache → begin serving.

**Concurrency (single write lock):** the mutating endpoints (`POST` / `DELETE /policy/services/{service_id}`)
serialize their cache **and** DB writes under a single process-wide write lock, so the SQLite row and the
in-memory cache entry are updated together as one critical section. This prevents interleaved
upsert/delete requests from leaving the cache and DB inconsistent (e.g. a cache entry present after its
row was deleted, or a lost update when two upserts for the same `service_id` race). Reads serve from the
cache and do not take the write lock; the single-writer discipline is what keeps concurrent mutations
consistent.

**Transaction strategy:**
- Per-service upsert (`POST /policy/services/{service_id}`): `INSERT OR REPLACE INTO service_policies VALUES (?, ?)`.
- Per-service delete (`DELETE /policy/services/{service_id}`): `DELETE FROM service_policies WHERE service_id = ?`; evict the cache entry.

**By-role query:** `GET /policy/services?role={role_id}` scans the cache and returns every SPM whose `inbound_rules` contains a rule referencing `role_id`. **Why a store query and not an IdP lookup:** the SPM is the source of truth, so this must return *stored* rows — including stale role→service mappings that the live IdP no longer reflects, which override-purge depends on to remove access that should no longer exist. It may start as a full scan; a `role.id -> {service_id}` index can be added later behind the same route/signature without changing callers.

**Future normalization:** migrate to `service_policies` + `policy_rules(service_id, role, scope)` tables once `ServicePolicyModel`/rule schema stabilizes — a future observability UI (and a native by-role index) will benefit from queryable columns. JSON column in the current schema avoids migration churn during active development.

**Endpoints:**

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/policy/services/{service_id}` | — | `ServicePolicyModel` (from cache) |
| `GET` | `/policy/services?role={role_id}` | — | `list[ServicePolicyModel]` (SPMs referencing the role) |
| `POST` | `/policy/services/{service_id}` | `ServicePolicyModel` | `204 No Content` (upsert) |
| `DELETE` | `/policy/services/{service_id}` | — | `204 No Content` (off-board) |
| `GET` | `/health` | — | `200` / `503` |

The by-scope lookup has **no dedicated route** — it collapses to the by-id read via `scope.serviceId` and is implemented entirely in the library.

`service_id` is the Keycloak clientId, which is slash-bearing (`{ns}/{workload}`, or a SPIFFE URI under
SPIRE) and cannot be a single URL path segment as-is. The `{service_id}` path segment on the three
per-id routes above is base64url-encoded on the wire (`aiac.policy.store.keying.encode_service_id` /
`decode_service_id`); the service decodes it immediately on entry, and the cache/DB stays keyed by the
decoded real id — every `service_id` in a request/response *body* (including the by-role list) is
always the decoded, real form. The by-role query's `role={role_id}` param is unaffected (not a path
segment).

`DELETE /policy/services/{service_id}` removes a single SPM row (SQLite `DELETE` + cache eviction) so a service can be off-boarded when it is decommissioned. Deleting a service that is not present is a no-op (`204`). Override-purge still edits `inbound_rules` in place via the upsert; the delete route is for whole-service removal, not per-rule purging.

**Error responses:**
- `404 Not Found` with `{"error": "service {id} not found"}` when `GET /policy/services/{service_id}` finds no entry in cache. The library's `get_service_policy` catches this and returns a fresh empty SPM (per the "engine creates a fresh model on 404" convention); the by-role query never 404s (empty list on no match).
- `502 Bad Gateway` with `{"error": "..."}` on SQLite write error for the write and delete endpoints.
- `503 Service Unavailable` if `GET /health` cannot open or query the SQLite file.

**`main.py` functions:**

- `_get_db() -> sqlite3.Connection` — open `SERVICEPOLICY_DB_PATH` with `check_same_thread=False`; run `CREATE TABLE IF NOT EXISTS` on first open.
- `_upsert_service(service_id: str, model: ServicePolicyModel)` — under the write lock: `INSERT OR REPLACE INTO service_policies VALUES (?, ?)` with `model.model_dump_json()`, then update cache (DB + cache write as one locked critical section).
- `_delete_service(service_id: str)` — under the write lock: `DELETE FROM service_policies WHERE service_id = ?`, then evict the cache entry (no-op if absent) — DB + cache eviction as one locked critical section.
- `_get_service(service_id: str) -> ServicePolicyModel` — read from in-memory cache; raise `404` if absent.
- `_list_by_role(role_id: str) -> list[ServicePolicyModel]` — return every cached SPM whose `inbound_rules` references `role_id`.
- `_load_cache()` — on startup, load all rows from SQLite into the in-memory cache.

**Configuration:**

| Variable | Source | Default |
|---|---|---|
| `SERVICEPOLICY_DB_PATH` | ConfigMap (`aiac-policy-store-config`) | `/data/policy_model.db` |

**Dependencies:** `fastapi`, `uvicorn[standard]`, `pydantic`. `sqlite3` is stdlib (no new dependency).

**Imports:** `from aiac.policy.model.models import ServicePolicyModel, Scope, Role`

**File structure:**

```
aiac/src/aiac/policy/store/service/
├── __init__.py
├── Dockerfile
├── requirements.txt
└── main.py
```

Build command (run from repo root):
```bash
docker build -f aiac/src/aiac/policy/store/service/Dockerfile \
  -t aiac-policy-store:latest aiac/src/
```

---

## Testing Decisions

Good tests assert external behavior at the system boundary — not internal implementation details such as private helpers or field serialization choices.

### Policy Store Service

**Seam:** SQLite `:memory:` database — pass an in-memory connection to the service on startup instead of opening `SERVICEPOLICY_DB_PATH`. All behavioral assertions remain valid; only the storage seam changes.

Key behaviors to assert:
- `GET /policy/services/{id}`: returns `ServicePolicyModel` deserialized from cache (hit); `404 {"error": "service {id} not found"}` when the service is not in cache (miss).
- `GET /policy/services?role={role_id}`: returns every SPM whose `inbound_rules` references the role; `[]` when none match; multiple when several match.
- `POST /policy/services/{id}`: `spec` stored in SQLite; cache updated; `204` returned. Upsert round-trip: a second `POST` for the same id replaces the row.
- `DELETE /policy/services/{id}`: row removed from SQLite; cache entry evicted; `204` returned. Deleting an absent service is a no-op (`204`).
- SQLite write error on the write or delete endpoint → `502`.
- SQLite file cannot be opened/queried on `GET /health` → `503`.

See [library-policy-store.md](library-policy-store.md) for the companion library testing decisions.

---

## Out of Scope

- **APM persistence:** APMs are derived on demand and never stored; the store has no per-agent surface.
- **Triggering Rego generation:** the Policy Store writes structured data only. Triggering Rego generation in the PDP Policy Writer is the responsibility of `aiac.pdp.policy.library` (called by `aiac.policy.computation`).
- **Pagination:** the by-role query returns all matching SPMs without pagination. At target scale (hundreds of services), the full result fits within one query and one HTTP response.
- **In-cluster mTLS between Policy Computation Engine and Policy Store:** secured by Kubernetes network policy; no application-layer auth.
- **Multi-writer / replica scale-out:** the current design is single-writer (single-replica StatefulSet, RWO PVC, SQLite). Future migration to a shared DB (e.g. PostgreSQL) is a backend swap; the HTTP contract is unchanged.

---

## Further Notes

- The K8s manifests issue must create the `aiac-policy-store` StatefulSet, its `volumeClaimTemplate` PVC (1 Gi, `ReadWriteOnce`), and a headless Service. No CRD or RBAC is needed — the service does not touch the Kubernetes API.
- `spec` fields use snake_case (matching Pydantic's `model_dump()`) — consistent with the `AuthorizationPolicy` CR convention. The JSON column avoids a translation layer.
- `service_id` is the SQLite `PRIMARY KEY`. The `aiac.apply.service.{id}` naming convention (lowercase alphanumeric + hyphens) should be maintained for consistency with trigger events.
- K8s resource names: StatefulSet `aiac-policy-store`, ClusterIP Service `aiac-policy-store-service:7074`, env var `AIAC_POLICY_STORE_URL`.
