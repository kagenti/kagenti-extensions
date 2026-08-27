# Runbook — Policy Model Store state reset (ALLOW/DENY rollout, no back-compat)

> **Status: HITL / operational decision.** This runbook documents the *procedure*.
> The **go / no-go decision** — when to actually nuke the persisted Policy Model
> Store state in a given environment — belongs to a human operator. Tracking
> issue: **#121** (under Feature **#116 — Policy Model: ALLOW/DENY**, Wave 2 **#131**).

## When to run this

Run this **once per environment** when rolling out the ALLOW/DENY model change
(#117: `RuleEffect` + split rule/target fields). The change **renames** the
`ServicePolicyModel` inbound-rule field:

| Before (single list) | After (#117, split by effect) |
|---|---|
| `inbound_rules: list[PolicyRule]` | `inbound_allow_rules: list[PolicyRule]` **+** `inbound_deny_rules: list[PolicyRule]` |

There is **deliberately no alias, no dual-read shim, and no record-migration
script.** The persisted store must be **cleared and re-seeded by re-onboarding**.

## Why there is no back-compat / migration (the rationale — read this)

The models declare `model_config = ConfigDict(extra="ignore")`
(`src/aiac/policy/model/models.py`). The Policy Model Store persists each
`ServicePolicyModel` as JSON in SQLite and rehydrates it on startup with
`ServicePolicyModel.model_validate_json(spec)`
(`src/aiac/policy/model_store/service/main.py`, `_load_cache`).

An **old** persisted row carries `"inbound_rules": [ … grants … ]` in its JSON
`spec`. On load against the **new** model:

1. `inbound_rules` is no longer a declared field, so `extra="ignore"`
   **silently discards it** — no error, no warning.
2. `inbound_allow_rules` / `inbound_deny_rules` are absent from the old JSON, so
   they **default to `[]`**.

**Result:** the store comes up *healthy* with every prior grant silently gone —
a stale, half-migrated read. Worse, a legitimately-empty SPM is now
indistinguishable from a silently-emptied one, so you cannot even detect the
damage after the fact. A partial/aliased migration would only make this failure
mode quieter. **Clearing and re-seeding from the authoritative onboarding inputs
is the only safe path.**

The Policy Model Store is a **rebuildable projection of onboarding inputs**
(Keycloak clients/roles/scopes + agent cards), not an irreplaceable system of
record — which is what makes a clean reset acceptable.

## What holds the state

| Fact | Value |
|---|---|
| Backend | SQLite, table `service_policies (service_id TEXT PRIMARY KEY, spec TEXT NOT NULL)` |
| DB path | `SERVICEPOLICY_DB_PATH`, default **`/data/policy_model.db`** (ConfigMap `aiac-policy-model-store-config`) |
| Storage | PVC from `volumeClaimTemplates` **`policy-model-store-data`** (1Gi, RWO), mounted at `/data` |
| Workload | StatefulSet **`aiac-policy-model-store`**, pod `aiac-policy-model-store-0`, namespace **`aiac-system`** |
| Service / port | `aiac-policy-model-store-service` → **7074** (`/health`, `/policy/services`) |
| Serving layer | In-memory `_cache` loaded from SQLite at startup; **all reads are served from `_cache`** |

> **Cache caveat:** because reads are served from the in-memory `_cache` (loaded
> once at startup), deleting rows/files on disk **without** also clearing the
> cache (or restarting the pod) leaves stale grants being served. Each method
> below accounts for this.

---

## Reset procedure

Pick **one** method. **Method A** is preferred (surgical, no pod churn, clears
durable rows *and* cache atomically). Methods B/C are full volume/file wipes for
when you want belt-and-suspenders certainty that nothing file-level survives.

Namespace is `aiac-system` throughout; adjust if you deploy elsewhere.

### Method A — Programmatic truncate (preferred)

The store exposes `DELETE /policy/services`, which runs
`DELETE FROM service_policies` **and** `_cache.clear()` in one locked write
(returns `204`; clearing an already-empty store is a no-op). This is the
cleanest reset — no restart, cache and durable rows cleared together.

```bash
# Port-forward the store service (leave running in a second terminal)
kubectl -n aiac-system port-forward svc/aiac-policy-model-store-service 7074:7074 &

# Truncate all SPMs (durable rows + in-memory cache)
curl -fsS -X DELETE http://127.0.0.1:7074/policy/services -o /dev/null -w '%{http_code}\n'
# expect: 204

# Verify empty (any known service id should now 404)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7074/policy/services/<some-known-service-id>
# expect: 404
```

Then **re-seed** (see below).

### Method B — Full PVC wipe (StatefulSet reset)

Use when you want a guaranteed-fresh volume (e.g. suspected file-level cruft,
leftover journals, or you are also changing storage). This deletes the durable
volume; the StatefulSet recreates an empty one on the next pod start, and
`_init_db` creates a fresh empty table.

```bash
# 1. Scale the StatefulSet down so the PVC is released
kubectl -n aiac-system scale statefulset/aiac-policy-model-store --replicas=0
kubectl -n aiac-system wait --for=delete pod/aiac-policy-model-store-0 --timeout=60s

# 2. Delete the PVC (name = <claimTemplate>-<statefulset>-<ordinal>)
kubectl -n aiac-system delete pvc policy-model-store-data-aiac-policy-model-store-0

# 3. Scale back up — a fresh empty PVC + empty DB is created
kubectl -n aiac-system scale statefulset/aiac-policy-model-store --replicas=1
kubectl -n aiac-system rollout status statefulset/aiac-policy-model-store --timeout=120s
```

Then **re-seed** (see below).

### Method C — In-pod DB file delete + restart

Least disruptive of the file-level options (keeps the PVC). `/data` is writable
even though the root filesystem is read-only.

```bash
# Remove the SQLite file (and any journal sidecars) inside the pod
kubectl -n aiac-system exec aiac-policy-model-store-0 -- \
  sh -c 'rm -f /data/policy_model.db /data/policy_model.db-wal /data/policy_model.db-shm'

# Restart so the (now empty) DB is recreated and the cache reloads empty
kubectl -n aiac-system rollout restart statefulset/aiac-policy-model-store
kubectl -n aiac-system rollout status statefulset/aiac-policy-model-store --timeout=120s
```

Then **re-seed** (see below).

---

## Re-seed by re-onboarding

The store is repopulated by **re-onboarding every managed service** through the
stateless Controller. Each call runs the onboarding use-case → `(rules,
override)` → PCE `compute_and_apply`, which writes fresh SPMs (now with
`inbound_allow_rules` populated) and rebuilds all **derived** state — the
per-agent `AgentPolicyModel`s and the generated OPA Rego — automatically. No
separate PDP step is needed.

Controller onboarding surface (`src/aiac/agent/controller/routes.py`):

```
POST /apply/service/{service_id}     # onboard (or re-onboard) one service
```

Re-onboard **every** managed agent and tool. Reference onboarding drivers live
at `demo/use-cases/uc1-onboarding/onboard/` (`04-onboard-agent.py`,
`05-onboard-tool.py`); in a real environment, drive the same
`POST /apply/service/{service_id}` for each service id in your catalog, e.g.:

```bash
# Port-forward the Controller (adjust svc name/port to your deployment)
# then, for every managed service id:
for sid in $(< managed-service-ids.txt); do
  curl -fsS -X POST "http://127.0.0.1:<controller-port>/apply/service/${sid}" \
    -o /dev/null -w "${sid}: %{http_code}\n"
done
```

> Re-onboarding is **order-independent** by design (a `UR→TS` grant lands
> durably on the target's SPM at tool onboarding, independent of agent order),
> so you may re-onboard services in any order.

## Verification

```bash
# 1. Store healthy
curl -fsS http://127.0.0.1:7074/health          # {"status":"ok"}

# 2. A re-seeded SPM now carries the NEW split field (spot-check one service)
curl -s http://127.0.0.1:7074/policy/services/<a-re-onboarded-service-id> \
  | python -m json.tool | grep -E 'inbound_allow_rules|inbound_deny_rules'
#   -> inbound_allow_rules should be populated for a service that had grants;
#      the OLD "inbound_rules" key must NOT appear.

# 3. Derived Rego regenerated for agents (confirm via your PDP/OPA surface).
```

**Success criteria:** the store returns SPMs whose grants live under
`inbound_allow_rules` (not the dropped `inbound_rules`), and downstream OPA
policy reflects those grants.

## Explicit non-goals (state these when executing)

- **No field alias** (`inbound_rules` → `inbound_allow_rules`).
- **No dual-read / back-fill shim** on load.
- **No record-migration script.**

These are intentional: given `ConfigDict(extra="ignore")`, any of them would
mask the silent-drop rather than fix it. The clean nuke-and-reseed above is the
supported path.

---

_Part of Feature #116 (Policy Model: ALLOW/DENY), Wave 2 (#131). Depends on the
Wave 1 model change #117._
