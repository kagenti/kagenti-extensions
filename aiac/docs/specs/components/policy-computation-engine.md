# Component PRD: Policy Computation Engine (`aiac.policy.computation`)

## Problem Statement

AIAC Agent sub-agents produce `list[PolicyRule]` objects representing partial policy updates — a new onboarding event may produce a handful of rules covering one agent's inbound and outbound access. `compute_and_apply(rules, override)` merges those rules into persisted policy.

The **original design persisted only per-agent `AgentPolicyModel` (APM) records**, storing rules denormalised onto the agent that reaches or is reached. Because a rule was only ever attached to an agent that already existed in the store, the merge outcome depended on the **order** in which services were onboarded.

### The order-dependence bug (repro)

Let:

- `UR` = a user (realm) role, mapped to agent `A`'s scope `AS` **and** tool `T`'s scope `TS`.
- `AR` = agent `A`'s (client) role, mapped to `TS`.

Onboarding **A then T** yields `APM(A)` outbound `{AR→TS, UR→TS}` — correct. Onboarding **T then A** **loses `UR→TS`**: at T-onboarding no agent yet targets `TS`, so the `(UR → TS)` outbound-subject rule has nowhere to attach and is dropped; at A-onboarding it is never re-emitted. The two orders diverge.

The same shape produces a **latent sibling bug**: a user role added *later* (UC3) to an already-onboarded agent+tool pair could not be reconstructed onto the agent, because nothing re-derived the agent's gates from durable facts.

## Solution

A **two-layer** model (see the policy-model component spec, handoff 01):

- **`ServicePolicyModel` (SPM)** — one per service, **persistent**, the **source of truth**. It carries the service's own identity (`owned_roles` / `owned_scopes` / `service_type`) and its `inbound_rules`: every `(role → scope)` rule whose `scope` this service owns. `UR→TS` lives durably on `SPM(T)`.
- **`AgentPolicyModel` (APM)** — **derived on demand** from the relevant SPMs and **partial-upserted** to the PDP. Never persisted as source of truth.

`compute_and_apply` routes each incoming rule to `SPM(scope.serviceId).inbound_rules`, persists the changed SPMs, computes the set of **affected agents** from the batch, re-derives each affected agent's APM **entirely from SPMs (zero IdP)**, and partial-upserts them to the PDP in a single `apply_policy` call.

Because `UR→TS` is durable on `SPM(T)` and is reconstructed onto `A` whenever `A` is derived, both onboarding orders converge to the same `APM(A)` = inbound `{UR→AS}`, outbound `{AR→TS, UR→TS}`. The latent sibling bug is fixed too: a late UC3 user role routes to `SPM(T)`, marks `A` affected, and re-derives `A`'s subject gate.

The module is pure Python (`aiac.policy.computation`), imported directly into the calling sub-agent's process. No FastAPI service, no Kubernetes deployment, no container image.

---

## Assumptions

These AIAC invariants (from the policy-model spec, handoff 01) are relied on by the PCE and are **enforced upstream at the Keycloak IdP boundary** (handoff 02), not re-checked here:

1. **No role spans both kinds.** A role is held by users *or* by agent service accounts, never both. This is what lets `Role.actorIds` be a single list and lets the PCE split inbound rules cleanly by `role.kind`. AIAC invariant, *not* a Keycloak guarantee.
2. **No scope shared across services.** A scope has exactly one owner, so `Scope.serviceId` is single-valued and `SPM(scope.serviceId)` is unambiguous. (Keycloak client scopes are realm-level and assignable to many clients; for AIAC-managed scopes the owner set is always length 1.)
3. **Agent role ⇔ Keycloak client role; user role ⇔ Keycloak realm role.** `Role.kind` is populated from Keycloak's `clientRole` flag by the IdP config service; agent roles come from a service's **client** roles (`Service.roles`).

---

## User Stories

1. As an AIAC Agent sub-UC agent, I want to submit a list of `PolicyRule` objects and have them durably recorded on the right service and reflected in every affected agent's policy, without implementing routing or storage merge logic myself.
2. As an AIAC Agent sub-UC agent, I want to submit the rules and get no return value to unpack on success, so I stay decoupled from routing, storage, and derivation — while a failure still surfaces to me (US 7).
3. As the Policy Computation Engine, I want each rule recorded on the SPM of the service that **owns the rule's scope**, so the fact survives regardless of which services already exist.
4. As the Policy Computation Engine, I want to derive an affected agent's APM purely from the persisted SPMs, so the result is **independent of onboarding order**.
5. As the Policy Computation Engine, I want to skip duplicate rules on append, so re-processing the same event does not create redundant entries.
6. As the Policy Computation Engine, I want to partial-upsert only the affected agents' packages to the PDP, so unaffected agents are left untouched.
7. As a developer, I want exceptions from the computation logged **and re-raised**, so a failed IdP / store / PDP interaction surfaces to the caller (the Controller returns HTTP 500; a NATS consumer nacks → at-least-once redelivery) instead of being silently dropped while nothing is applied.
8. As a developer, I want a stable import path, so the calling convention does not change as the module grows.

---

## Implementation Decisions

### Module Identity

**Namespace:** `aiac.policy.computation`

**Location:** `aiac/src/aiac/policy/computation/`

```
aiac/src/aiac/policy/
└── computation/
    ├── __init__.py   # empty
    └── engine.py     # compute_and_apply
```

No FastAPI. No Kubernetes deployment. No container image. Imported as a library by AIAC Agent sub-UC agents.

### Public API

Two entry points — an incremental fold and an authoritative offboard:

```python
def compute_and_apply(rules: list[PolicyRule], override: bool = False) -> None
def decommission(service_id: str) -> None   # service_id = clientId (SPM key), not the Keycloak UUID
```

- **No return value; failures propagate:** on success the caller receives no return value. Both functions log exceptions and **re-raise** them — a failure in IdP resolution, Policy Store I/O, or PDP Policy Writer push surfaces to the caller (the Controller returns HTTP 500; a NATS consumer nacks → at-least-once redelivery) rather than being silently swallowed while nothing is applied.
- **`override`:** selects the merge mode (see [Merge Semantics](#merge-semantics)). `False` (default) appends additively at the SPM layer; `True` authoritatively replaces every input role's mappings **across all SPMs** (role-level revocation). Set by the caller (the Controller) from the producing UC's choice — UC1 = `False`, UC3 = `True`, UC2 Rebuild = `True`, UC2 Build = TBD.
- **`decommission`:** the authoritative service **offboard** — tears down a decommissioned service's entire policy footprint (see [Decommission (service offboard)](#decommission-service-offboard)). Keyed by the **clientId (SPM key)**, since an offboarded client is gone from `get_services()` and its UUID can no longer be resolved.
- Import path: `from aiac.policy.computation.engine import compute_and_apply, decommission`

### Rule-builder input contract (upstream)

Each incoming `PolicyRule` arrives with `scope.serviceId`, `role.kind`, and `role.actorIds` **already populated**, and with roles **already flattened** to their closure (role + descendants, dedup by `role.id`). The PCE performs **no IdP lookup for routing or classification** and **no role flattening** — it treats each rule's `role` and `scope` as-is.

- The boundary that **derives** `scope.serviceId` / `role.kind` / `role.actorIds` from Keycloak facts is the **Keycloak IdP config service (handoff 02)**.
- The rule-builder (`src/aiac/agent/policy/api.py` — `role_to_scopes` / `roles_to_scope`, `PolicyRule`) merely **carries those fields through**. Ensure it does (add the pass-through if missing); it does not compute them.

### Algorithm

Given `rules: list[PolicyRule]` and an `override` flag, `compute_and_apply` executes:

1. **Catalog once.** Call `Configuration.get_services()` — the **only** runtime IdP read. For every service touched this batch, seed its SPM's `service_type` / `owned_roles` / `owned_scopes` from its catalog `Service` record, keeping only **AIAC-provisioned** entities (the `aiac.managed` marker on `Role.aiac_managed` / `Scope.aiac_managed`; Keycloak built-ins — the default client scopes `profile`, `email`, `roles`, `web-origins`, `acr`, `basic`, `service_account`, and the `default-roles-<realm>` composite — are dropped). This seed drives **P2** identity and the **P4** "only agents modelled" rule. It is a seed, **not** a per-derive dependency.

2. **Route each rule to its owning service's SPM.** For each rule `(role, scope)`, append it to `SPM(scope.serviceId).inbound_rules` — fetch the SPM via `get_service_policy_by_scope` / `get_service_policy`. **Append-dedup by `role.id + scope.id`.** There is **no** write-time 3-way P5b classification (the old (user,agent-scope)/(user,tool-scope)/(agent,tool-scope) routing table is gone) — a rule always lands on the SPM of the service that owns its scope, whatever the kinds.

3. **Override (`override=True`) — role-level revocation.** *Before* appending, purge the **distinct input-role set** from **every** SPM that contains any of them: one up-front pass using `get_service_policies_by_role` per distinct input role, removing every stored rule whose `role.id` matches. Then append the fresh rules. Purging once, up-front, ensures a role shared across the input is not wiped after being added. The old algorithm's `target_scopes` reconciliation is **deleted** — `target_scopes` is a derived, never-stored quantity.

3b. **Reconcile (drift GC) — after routing/override, before persist.** Prune each **touched** SPM against the step-1 `get_services()` catalog (no additional IdP read) so drift cannot accumulate across re-onboarding. Runs under **both** merge modes and is order-independent (drops only edges whose entity no longer exists). See [Reconcile (drift GC)](#reconcile-drift-gc) under Merge Semantics for the keep rules.

4. **Persist** each changed SPM via `apply_service_policy`.

5. **Compute the affected-agent set from the batch** — from the batch's roles/scopes, **not** by scanning all agents:
   - For each input (or purged) role `r` with `r.kind == Agent`: the owning agents in `r.actorIds` are affected (their outbound changed).
   - For each touched scope `s` with owner `X = s.serviceId`:
     - if `X` is an **Agent**, `X` is affected (its inbound changed); **and**
     - every agent **targeting** `s` is affected — namely the owners (`actorIds`) of the **Agent-kind** inbound rules on `SPM(X)` whose scope is `s`.

6. **Derive** each affected agent's APM (see below), collect them into one `PolicyModel`, and **partial-upsert** via `aiac.pdp.policy.library.apply_policy` **exactly once**. Exceptions are logged and re-raised (they propagate to the caller). **Tools get an SPM but no APM** (P4).

### Derivation of `APM(A)` — 100% from SPMs, zero IdP

Let `R_A = SPM(A).owned_roles` (A's client roles) and `S_A = SPM(A).owned_scopes`.

- **Identity (P2):** `agent_roles` ← `R_A`; `agent_scopes` ← `S_A`.
- **Inbound:** `inbound_rules` ← all of `SPM(A).inbound_rules`. Split each by `role.kind`:
  - `User` → `subject_roles[username] += role` (usernames from `role.actorIds`);
  - `Agent` → `source_roles[serviceId] += role` (serviceIds from `role.actorIds`).
- **Outbound:** for each `r ∈ R_A`, find the `r`-rules in `get_service_policies_by_role(r)`. For each such `(r → s)`: add to `outbound_rules` and `target_scopes[s.serviceId] += s`.
- **Outbound subject gate:** for each target `(X, s)` in `target_scopes` — where `X` is the callee, a **tool or another agent** — take the **User**-kind inbound rules `(u → s)` on `SPM(X)`, append them to `outbound_subject_rules`, and `subject_roles += u.actorIds`. The gate's range is tool ∪ agent scopes.

**Relevance is directional.** An SPM contributes to `A` **iff** it *is* `SPM(A)` (contributes inbound) **or** it contains a rule whose role is one of A's **agent** roles `R_A` (contributes outbound). A merely *shared user role* never confers relevance — this is what prevents a **false outbound edge** to a target (a tool or another agent) `A` does not actually target. This is a **derivation-layer** relevance rule: it does **not** imply the outbound user gate is empty. When the agent holds a per-skill operator role that the PRB maps (by capability-match) to a target's scope, the agent *does* target that callee, and the nested derivation then surfaces the shared-user edges.

### P2 / P4 / P5b reconciliation

- **P2 (identity embed):** copy `owned_roles` / `owned_scopes` from `SPM(A)` onto the APM's `agent_roles` / `agent_scopes`. AIAC-managed filter applied at catalog-seed time. Without the embed both generated gates would deny-all (inbound `subject_ok` needs a non-empty `agent_scopes`; outbound `target_ok` needs a non-empty `agent_roles`).
- **P4 (only agents modelled):** emit an APM / Rego only for SPMs with `service_type == Agent`. Tools keep an SPM (durable `inbound_rules`) but never get an APM — no `github_tool.*.rego` is emitted.
- **P5b (classification):** now expressed purely as `role.kind` + `scope.serviceId`. The write-time 3-way routing table is gone; classification happens at **derive** time by splitting inbound rules on `role.kind`.

### Agent → agent access — in scope, for free

An agent-to-agent edge `AR→BS` (agent A's role → agent B's scope) is stored on `SPM(B)` and handled uniformly, with **no target-type branching anywhere**:

- A's derivation: `AR ∈ R_A`, so `get_service_policies_by_role(AR)` finds `AR→BS` on `SPM(B)` → `outbound_rules += AR→BS`, `target_scopes[B] += BS`, plus B's user gates as `outbound_subject_rules`.
- B's derivation: `AR→BS ∈ SPM(B).inbound_rules`, `AR.kind == Agent` → `source_roles[A] += AR`.

Add a test for this.

**Future-optimization note (document, do NOT build now):** a shared edge like `AR→BS` is stored **once** canonically on `SPM(B)` but **projected into two APMs** (A's `outbound_rules` and B's `source_roles`), so the generated Rego duplicates it across two packages. This is acceptable; a future optimization could share the representation.

### Two implementation-time verification gates

Confirm both while coding (they gate correctness of the whole approach):

1. **`apply_policy` must be a partial (per-agent) upsert.** The PCE upserts only the affected agents. If `apply_policy` rewrites the **whole** policy set instead of replacing per-agent packages, partial upsert would delete every non-affected agent. If so, either make `apply_policy` per-agent, or push a full snapshot. Check `aiac.pdp.policy.library` / the pdp-policy library + writer specs.
2. **Rego must consume `source_roles`** for the inbound gate (not only `subject_roles`), or agent→agent inbound is *modelled but not enforced*. `source_roles` already exists on `AgentPolicyModel`, so the path likely exists — confirm.

### Merge Semantics

The `override` flag (set by the caller from the producing UC's choice) selects the merge mode, applied at the **SPM layer**:

- **`override=False` (default — additive append):** each rule is appended to `SPM(scope.serviceId).inbound_rules` if not already present (dedup by `role.id + scope.id`). Existing SPM rules are preserved. Incremental path (e.g. UC1 Service Onboarding, where existing roles must not lose their other access).
- **`override=True` (authoritative role-keyed replace):** before appending, the engine purges the distinct input-role set from **every** SPM containing them (`get_service_policies_by_role`), once, up-front, so the fresh rules become each role's complete mapping. Used by role-scoped recomputes (UC3 Role Update) and full rebuilds (UC2 Rebuild).

`override=True` provides **role-level** revocation. Finer-grained single-rule revocation (removing one `PolicyRule` without replacing its whole role) is still **TBD**.

#### Reconcile (drift GC)

SPM identity keys on Keycloak UUIDs, which **churn on delete/recreate**. Because append-dedup keys on `role.id + scope.id`, a re-onboarded service whose Keycloak roles/scopes were recreated presents *new* UUIDs, so its edges are treated as new and pile up **beside** the superseded generations — nothing removes the old ones. (`override=True` does not close this: it purges by the *input* role's id, so a role whose UUID already churned out of the batch is never matched.) A live diagnostic once found a single agent SPM carrying 53 inbound edges across two role-id generations, retired `*-aud` scopes, an impossible self-reference, and duplicate same-name roles — all replayed into every regenerated APM/Rego.

**Reconcile** closes this. After routing (step 2) and any override purge (step 3), and **before** persist (step 4), each **touched** SPM is pruned against the step-1 catalog. It **reuses that same `get_services()` result** — no additional IdP read, so the *only-runtime-IdP-read-is-`get_services()`* invariant holds. It runs under **both** merge modes and is **order-independent** — it removes *only* edges whose entity genuinely no longer exists, never a live edge, so both onboarding orders still converge. "Touched SPMs only": at that point the SPM cache holds exactly the routed + override-purged SPMs (agent-derive SPMs aren't loaded yet).

For each touched `SPM(X)` whose owner `X` **is present in the catalog** (a catalog **miss ⇒ skip pruning**, never wipe on a transient outage), an inbound edge is kept iff:

1. **Scope still exists** — `edge.scope.id ∈ {s.id for s in owned_scopes}` (X's current `aiac.managed` scopes, seeded from the catalog). Drops retired/churned scopes (kills the `*-aud` species and scope-model cruft).
2. **Agent role still exists** — for `role.kind == Agent`, `edge.role.id ∈` the catalog's `aiac.managed` role ids (all services). Drops retired/churned agent client roles (kills self-references and agent-role UUID churn).
3. **User-role churn collapse** — user realm roles are membership-derived, absent from the catalog, and the PCE must not read `get_subjects()`; so among surviving `User` edges grouped by `(scope.id, role.name)`, a stale edge is dropped only when **this batch** carries a *different* id for that same `(scope, name)` (the fresh id supersedes the old generation). Two *co-existing* same-name realm roles both currently held are both kept (realm hygiene, not accumulation — out of scope).

#### Decommission (service offboard)

Reconcile is passive and catalog-anchored: it prunes only **touched** SPMs and skips any whose owner is absent from `get_services()`. That leaves the **onboard→offboard** drift species uncovered — once a service `X` is decommissioned (its Keycloak client + roles/scopes deleted), `X` is gone from the catalog forever, so (1) `SPM(X)`'s own inbound edges linger; (2) `X`'s **outbound footprint** (`X_role → other_scope` edges on *other* SPMs) is never pruned; (3) if `X` was an agent, its **APM/Rego stays in the PDP**. `decommission(service_id)` is the **authoritative** teardown for exactly this — it acts on an explicit offboard signal, not the catalog-miss guard.

**Keyed by the clientId, not the UUID.** An offboarded client is gone from `get_services()`, so UUID→clientId resolution is impossible; the offboard contract carries the clientId (`Service.serviceId`, the SPM key) directly. This is the documented asymmetry with onboard's `/apply/service/{uuid}`.

Steps:

1. **Catalog once** (`get_services()` — the same single allowed IdP read; `X` is absent, used only to seed/classify the still-live agents re-derived in step 8).
2. **Load `SPM(X)`.** **Content guard:** a 404 fresh-empty SPM (never onboarded / already removed) is a **no-op** — no spurious PDP delete.
3. **Targeters** — agents whose *outbound* loses `X`: the `actorIds` of every **Agent**-kind inbound edge on `SPM(X)` (they held `their_role → X_scope` on `SPM(X)`, deleted in step 5).
4. **Purge `X`'s outbound footprint.** For each `r ∈ SPM(X).owned_roles`, find the SPMs referencing it via `get_service_policies_by_role(r)`; on each such SPM `B` (skip `X`), drop edges where `edge.role.id == r.id`; mark `B` changed and, if `B` is an agent, affected (its inbound `source_roles[X]` vanished).
5. **Delete `SPM(X)`** (`delete_service_policy`) — removes every user→X and agent→X inbound edge at once — and evict it from the SPM cache so re-derive can't resurrect it.
6. **Persist** each changed (footprint-purged) SPM (`apply_service_policy`).
7. **Delete `APM(X)`** (`delete_agent_policy`) iff `SPM(X).service_type == Agent` (tools have an SPM but no APM).
8. **Re-derive** `affected = (targeters ∪ purged-agent-owners) − {X}`, filtered to agents; `apply_policy(PolicyModel(agents=…))` **once** if non-empty. Derivation is reused unchanged — it reads the freshly-persisted, `X`-deleted store, so `outbound` / `target_scopes` / `source_roles` referencing `X` drop automatically.

**Invariants preserved:** still exactly one IdP read (`get_services()`); still a per-agent partial upsert. **Not covered** (follow-ups): NATS `aiac.apply.offboard.{id}` consumer wiring; dropped-target GC where the source service survives (via `override=True` re-onboard); batch offboard.

### Dependencies

| Module | Purpose |
|--------|---------|
| `aiac.policy.model` | `PolicyRule`, `ServicePolicyModel`, `AgentPolicyModel`, `PolicyModel` |
| `aiac.idp.configuration` | `Configuration.get_services` — the **only** runtime IdP read (catalog: `service_type` + own roles/scopes for the P2 seed) |
| `aiac.policy.store.library` | `get_service_policy` / `get_service_policy_by_scope` (fetch SPM), `get_service_policies_by_role` (SPMs containing a role — override purge + outbound derivation), `apply_service_policy` (persist SPM), `delete_service_policy` (offboard) |
| `aiac.pdp.policy.library` | `apply_policy` — partial-upsert derived APMs to OPA; `delete_agent_policy` — remove an offboarded agent's APM/Rego |

Note: the PCE no longer calls `get_services_by_role` / `get_services_by_scope` / `get_subjects_by_role` at routing or classification time — those facts arrive on the rules (input contract) and derivation reads SPMs. The single IdP read is `get_services()` for the identity seed.

### Not Called By

- PDP Policy Writer — the downstream consumer, not a caller.
- Policy Store — pure CRUD, no computation.
- IdP Configuration Service — no awareness of this module.

### Not Responsible For

- Rule revocation beyond role-level `override=True` replace (single-rule revocation is TBD).
- Bootstrapping SPM records for brand-new services (the store returns 404; the engine seeds a fresh SPM from the catalog).
- Translating `PolicyModel` → Rego packages (responsibility of `aiac.pdp.policy.library` / PDP Policy Writer).

---

## Testing Decisions

Good tests assert external behavior — what the engine writes to the Policy Store (SPMs) and pushes to the PDP (derived APMs) — not internal merge logic directly.

**Seam:** mock all downstream dependencies at their module-level import boundary:

- `aiac.idp.configuration` — mock `Configuration.get_services` (the catalog: `service_type` + each service's own roles/scopes for the P2 seed).
- `aiac.policy.store.library` — mock `get_service_policy` / `get_service_policy_by_scope`, `get_service_policies_by_role`, `apply_service_policy`, `delete_service_policy`.
- `aiac.pdp.policy.library` — mock `apply_policy`, `delete_agent_policy`.

**Un-freeze `test/policy/computation/`.** These tests were excluded (frozen imports caused collection errors). With the SPM redesign landed, un-freeze the directory so the suite runs under `pytest test/ -m "not integration"`.

Key behaviors to assert:

- **Original repro, both orders → identical `APM(A)`.** Onboard **A then T** and **T then A**; assert the derived `APM(A)` is identical (inbound `{UR→AS}`, outbound `{AR→TS, UR→TS}`), compared as order-independent `(role, scope)` sets. This is the headline regression guard.
- **Latent sibling bug (late UC3 user role).** After A+T exist, a later user-role rule `(UR2 → TS)` routes to `SPM(T)`, marks A affected, and A's re-derived subject gate includes `UR2`.
- **Agent → agent (`AR→BS`).** Stored on `SPM(B)`; A's derived APM has `AR→BS` in `outbound_rules` + `target_scopes[B]`; B's derived APM has `source_roles[A] += AR`.
- **Override role-level purge across SPMs.** `override=True` with an input role already present on multiple SPMs → that role is purged from **every** SPM (via `get_service_policies_by_role`) once, up-front, before the fresh rules are appended; a role shared across the input is not wiped after being added.
- **Append dedup.** A rule already present on the target SPM (same `role.id + scope.id`) is not appended twice; map list entries (same `id`) are not duplicated.
- **No flattening.** Rules arrive pre-flattened; the PCE issues at most one `get_service_policies_by_role` call **per distinct role** — a rule carrying a composite role does not trigger per-child calls inside the PCE.
- **Tool gets an SPM but no APM (P4).** A Tool service accrues durable `inbound_rules` on its SPM but is never emitted as an APM/Rego; the agent→tool `target_scopes` edge still appears on the agent's derived APM.
- **P2 identity from `owned_*`.** Each derived APM's `agent_roles` / `agent_scopes` come from `SPM(A).owned_roles` / `owned_scopes`, AIAC-managed-filtered; an agent with no AIAC-managed catalog roles/scopes keeps `[]`.
- **Directional relevance — no false outbound edge.** A user role shared between `AS` and `TS` does **not** by itself make A "target" T; A's outbound edge to T appears only if one of A's **agent** roles maps to a T scope.
- **Affected set from the batch, not a full scan.** The affected-agent set is computed from the batch roles/scopes; agents unrelated to the batch are never derived or upserted.
- **`apply_policy` called exactly once** after all `apply_service_policy` writes complete (partial upsert of only the affected agents).
- **Reconcile (drift GC).** A touched SPM carrying dangling edges (retired scope, churned scope UUID, churned/duplicate user role, retired agent-role self-reference) is pruned against the catalog on re-onboarding; live edges survive and the pass is idempotent; a catalog miss (owner absent) leaves the SPM untouched.
- **Decommission (service offboard).** Onboard an agent A targeting tool T, then `decommission(T)`: `SPM(T)` is deleted, no `delete_agent_policy` (tool has no APM), and A is re-derived with an empty outbound while its inbound survives. `decommission(A)`: `SPM(A)` deleted, `delete_agent_policy(A)` called, A's outbound footprint (`AR→TS` on `SPM(T)`) purged while T keeps its user grant, and no APM re-derived for the deleted agent. A never-onboarded / 404 service is a no-op.
- **Failures propagate.** An exception from any dependency is logged and **re-raised** (it propagates to the caller, which surfaces it — e.g. the Controller returns HTTP 500); on success `compute_and_apply` / `decommission` return `None`.

**Prior art:** `3.14-unit-tests-write-api.md` (mock boundary pattern — apply the same approach at the library import boundary here).

---

## Out of Scope

- **Fine-grained rule revocation:** removing an individual `PolicyRule` without replacing its whole role. `override=True` covers role-level replace at the SPM layer (see [Merge Semantics](#merge-semantics)); single-rule revocation is not yet designed — **TBD**. (Full-service **decommission / package deletion** *is* now designed and implemented — see [Decommission (service offboard)](#decommission-service-offboard).)
- **Full policy rebuild orchestration:** the PCE handles incremental updates; full rebuilds (clear + reapply all) are driven by higher-level orchestration outside this module.
- **Direct Keycloak calls:** all IdP access goes through `aiac.idp.configuration.Configuration` (only `get_services()`). The PCE never calls Keycloak directly.
- **Persistence of `PolicyRule` inputs:** the PCE persists SPMs (source of truth); APMs are derived and pushed, never persisted as truth. The raw input rule list is not stored.
- **Model field definitions** (handoff 01), **IdP service / library** (handoffs 02/03), **store CRUD** (handoff 04).

---

## Further Notes

- The PCE is the **only** caller of `aiac.pdp.policy.library.apply_policy` from AIAC Agent sub-agents. Sub-agents call `compute_and_apply`, not the PDP Policy Library directly.
- `aiac/src/aiac/agent/policy/api.py` retains `role_to_scopes` / `roles_to_scope` helpers used by AIAC Agent sub-UC agents; they now carry `scope.serviceId` / `role.kind` / `role.actorIds` through on each `PolicyRule` (input contract above). These helpers are not used by the PCE.
</content>
</invoke>
