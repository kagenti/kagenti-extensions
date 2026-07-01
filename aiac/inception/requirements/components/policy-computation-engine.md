# Component PRD: Policy Computation Engine (`aiac.policy.computation`)

## Problem Statement

AIAC Agent sub-agents produce `list[PolicyRule]` objects representing partial policy updates — a new onboarding event may produce a handful of rules covering one agent's inbound and outbound access. Before this component, merging those rules into full `AgentPolicyModel` objects required each sub-agent to independently:

1. Query the IdP Configuration Service to resolve which services own each role and scope.
2. Read the current `AgentPolicyModel` from the Policy Store.
3. Additively merge the new rules into the existing model.
4. Write the updated model back to the Policy Store.
5. Build a `PolicyModel` and push it to the PDP Policy Writer.

This bespoke logic was duplicated across every sub-agent that produced policy rules, making the merge semantics inconsistent and the IdP query pattern scattered.

## Solution

A pure Python library module `aiac.policy.computation` centralises all policy computation. Sub-agents call a single function `compute_and_apply(rules: list[PolicyRule]) -> None`, which handles IdP resolution, additive merging, Policy Store read/write, and PDP Policy Writer invocation. No FastAPI service, no Kubernetes deployment — the module is imported directly into the calling sub-agent's process.

---

## User Stories

1. As an AIAC Agent sub-UC agent, I want to submit a list of `PolicyRule` objects and have them automatically merged into the relevant `AgentPolicyModel` records, so that I do not need to implement IdP resolution or storage merge logic.
2. As an AIAC Agent sub-UC agent, I want the computation to be fire-and-forget, so that my sub-agent is not blocked waiting for Rego generation to complete.
3. As the Policy Computation Engine, I want to resolve which services own a given `Role`, so that I know which `AgentPolicyModel` records receive new outbound rules.
4. As the Policy Computation Engine, I want to resolve which services expose a given `Scope`, so that I know which `AgentPolicyModel` records receive new inbound rules.
5. As the Policy Computation Engine, I want to read each affected agent's current `AgentPolicyModel` before merging, so that additive append does not lose previously established rules.
6. As the Policy Computation Engine, I want to skip duplicate rules on append, so that re-processing the same event does not create redundant entries.
7. As the Policy Computation Engine, I want to push the updated `PolicyModel` to the PDP Policy Writer after all store writes succeed, so that OPA reflects the latest policy state.
8. As a developer, I want exceptions from the computation to be logged without propagating, so that a transient IdP or Policy Store failure does not crash the calling sub-agent.
9. As a developer, I want to import the engine from a stable path, so that the calling convention does not change as the module grows.

---

## Implementation Decisions

### Module Identity

**Namespace:** `aiac.policy.computation`

**Location:** `aiac/src/aiac/policy/computation/`

**Package structure:**

```
aiac/src/aiac/policy/
└── computation/
    ├── __init__.py   # empty
    └── engine.py     # compute_and_apply
```

No FastAPI. No Kubernetes deployment. No container image. Imported as a library by AIAC Agent sub-UC agents.

### Public API

Single entry point:

```python
def compute_and_apply(rules: list[PolicyRule]) -> None
```

- **Fire-and-forget:** the caller receives no return value. The function logs exceptions and does not propagate them — a transient failure in IdP resolution, Policy Store I/O, or PDP Policy Writer push must not crash the calling sub-agent.
- Import path: `from aiac.policy.computation.engine import compute_and_apply`

### Algorithm

Given `rules: list[PolicyRule]`, the engine executes these steps:

1. **Composite role flattening:** for each rule's `role`, recursively collect the role and all descendant roles from `role.childRoles` into a flat list of leaf roles, de-duplicated by `role.id`. (`Role` is not hashable, so de-duplication tracks seen `id`s rather than adding `Role` objects to a `set`.) All subsequent role-based queries operate on this flattened list. A non-composite role yields a list containing only itself.

2. **Scope → inbound services:** for each rule's `scope`, call `Configuration.get_services_by_scope(rule.scope) -> list[Service]`. Add the rule to `inbound_rules` of each returned service's `AgentPolicyModel`.

3. **Role → outbound services + `source_roles` + `target_scopes`:** for each flattened role R, call `Configuration.get_services_by_role(R) -> list[Service]`. For each returned service S:
   - Add the rule to `outbound_rules` of S's `AgentPolicyModel`.
   - Append R to `source_roles[S.id]` (creating the entry if absent). The map is keyed by the service's string `id`, not the `Service` object; the appended value is the typed `Role`.
   - For each target service T resolved in step 2 (services exposing `rule.scope`), append `rule.scope` to `target_scopes[T.id]` on S's `AgentPolicyModel` (creating the entry if absent). This records the outbound direction — S acting as R may request `rule.scope` on target T — keyed by the target service's string `id` with the typed `Scope` as the value.

4. **Role → subjects + `subject_roles`:** for each flattened role R, call `Configuration.get_subjects_by_role(R) -> list[Subject]`. For each returned subject S:
   - Append R to `subject_roles[S.id]` (creating the entry if absent). The map is keyed by the subject's string `id`; the appended value is the typed `Role`.

5. **Realm-level roles (no owning service):** if `get_services_by_role(R)` returns an empty list for a flattened role R, the role is realm-level. No outbound assignment or `source_roles` entry is made for that role. `subject_roles` entries are still recorded if `get_subjects_by_role(R)` returns subjects.

6. **Additive merge:** for each affected service/agent, read the current `AgentPolicyModel` from the Policy Store via `get_agent_policy(agent_id)`. Append new rules and map entries that are not already present (de-duplicate rules by value; de-duplicate `source_roles`, `subject_roles`, and `target_scopes` list values by the entity's `id`). Because the maps are keyed by string `id`, merging is a plain dict-key lookup — no hashing of `Service` / `Subject` / `Scope` objects is involved. Write the updated model back via `apply_agent_policy(agent_id, model)`.

7. **PDP push:** once all Policy Store writes complete, build a `PolicyModel` from the updated agents and call `aiac.pdp.policy.library.apply_policy(model)` (fire-and-forget within this function).

### Merge Semantics

Rules are appended additively — existing `inbound_rules` and `outbound_rules` entries are preserved. De-duplication compares rules by value (`role.id` + `scope.id`). **Rule revocation is TBD** — removing individual rules from an `AgentPolicyModel` is not yet specified.

### Dependencies

| Module | Purpose |
|--------|---------|
| `aiac.policy.model` | `PolicyRule`, `AgentPolicyModel`, `PolicyModel` |
| `aiac.idp.configuration.library` | `Configuration` — `get_services_by_role`, `get_services_by_scope`, `get_subjects_by_role` |
| `aiac.policy.store.library` | `get_agent_policy`, `apply_agent_policy` |
| `aiac.pdp.policy.library` | `apply_policy` — push updated `PolicyModel` to OPA |

### Not Called By

The PCE is **not** called by:
- PDP Policy Writer — it is the downstream consumer, not a caller
- Policy Store — the store is pure CRUD with no computation
- IdP Configuration Service — the IdP service has no awareness of this module

### Not Responsible For

- Rule revocation (TBD)
- Bootstrapping `AgentPolicyModel` records for new agents (the store returns a 404; the engine creates a fresh model in that case)
- Translating `PolicyModel` → Rego packages (responsibility of `aiac.pdp.policy.library` / PDP Policy Writer)

---

## Testing Decisions

Good tests assert external behavior — what the engine does to the Policy Store and PDP Policy Writer — not internal merge logic directly.

**Seam:** mock all four downstream dependencies at their module-level import boundary:
- `aiac.idp.configuration.library` — mock `Configuration.get_services_by_role`, `Configuration.get_services_by_scope`, and `Configuration.get_subjects_by_role`
- `aiac.policy.store.library` — mock `get_agent_policy`, `apply_agent_policy`
- `aiac.pdp.policy.library` — mock `apply_policy`

Key behaviors to assert:
- Rules with a resolvable scope result in `apply_agent_policy` calls for each service returned by `get_services_by_scope`.
- Rules with a resolvable role result in `apply_agent_policy` calls for each service returned by `get_services_by_role`; `source_roles` on the written model is keyed by the service's string `id` with the typed `Role` in the value list.
- `get_subjects_by_role` is called for each flattened role; `subject_roles` on the written model is keyed by the subject's string `id` with the typed `Role` in the value list.
- `target_scopes` on the written model is keyed by the target service's string `id` (a service exposing the rule's scope) with the typed `Scope` in the value list.
- Every relationship map on the written model has string keys, so `model_dump(mode="json")` round-trips without a custom key serializer.
- A composite role is flattened: `get_services_by_role` and `get_subjects_by_role` are called for each child role, not the composite role itself.
- Realm-level roles (empty service list from `get_services_by_role`) do not produce `outbound_rules` or `source_roles` entries; `subject_roles` entries are still recorded for any subjects returned by `get_subjects_by_role`.
- Existing rules and map entries in the fetched `AgentPolicyModel` are preserved after merge.
- Duplicate rules (same role + scope already present) are not appended twice; duplicate `source_roles` / `subject_roles` / `target_scopes` list entries (same `id`) are not appended twice.
- `apply_policy` is called exactly once after all `apply_agent_policy` writes complete.
- An exception from any dependency is logged and does not propagate to the caller.

**Prior art:** `3.14-unit-tests-write-api.md` (mock HTTP boundary pattern — apply the same approach at the library import boundary here).

---

## Out of Scope

- **Rule revocation:** removing individual `PolicyRule` entries from an `AgentPolicyModel`. Not yet designed — marked TBD.
- **Full policy rebuild:** the PCE handles incremental updates only. Full rebuilds (clear + reapply all) are driven by higher-level orchestration outside this module.
- **Direct Keycloak calls:** all IdP queries go through `aiac.idp.configuration.library.Configuration`. The PCE never calls Keycloak directly.
- **Persistence of `PolicyRule` inputs:** the PCE does not store the input rule list — only the merged `AgentPolicyModel` output is persisted.

---

## Further Notes

- The PCE is the **only** caller of `aiac.pdp.policy.library.apply_policy` from AIAC Agent sub-agents. Sub-agents no longer call the PDP Policy Library directly; they call `compute_and_apply` instead.
- `aiac/src/aiac/agent/policy/api.py` retains `role_to_scopes` / `roles_to_scope` helpers used by AIAC Agent sub-UC agents. `PolicyRule` (now at `aiac.policy.model`) is imported from there. These helpers are not used by the PCE.
