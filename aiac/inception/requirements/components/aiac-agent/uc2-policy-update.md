# UC2: Policy Update

## Depends on

- [`../aiac-agent.md`](../aiac-agent.md) — NATS Consumer, Controller, Shared Module, Validate Node common checks, Configuration, Error Handling, Runtime.

---

## Architecture

```mermaid
flowchart TD
    NATS["Event Broker\nNATS JetStream\naiac.apply.policy.build"]
    NATS_CONSUMER["NATS Consumer\nasyncio background task\nthin adapter"]
    TRIGGERS["HTTP Triggers\nPOST /apply/policy/build (debug)\nPOST /apply/policy/rebuild (operator)"]
    CTRL["Controller\nroutes.py"]

    NATS -->|"durable queue group\naiac-agent-consumer"| NATS_CONSUMER
    NATS_CONSUMER -->|"calls internal handler"| CTRL
    TRIGGERS --> CTRL

    subgraph PU["Policy Update"]
        ORC2["Orchestrator"]
        SA3["Build"]
        SA4["Rebuild"]
        ORC2 -->|"build"| SA3
        ORC2 -->|"rebuild"| SA4
    end

    APPLY["Policy Apply\nagent/shared/apply/\nPolicyApplyGraph"]

    ORC2 -->|"policy_model"| APPLY

    CTRL -->|"build / rebuild"| ORC2
```

---

## Trigger(s)

| Source | Subject / Path | Sub-agent |
|---|---|---|
| Event Broker (NATS) | `aiac.apply.policy.build` (originated by RAG Ingest Service post-ingest) | Build |
| HTTP (operator only) | `POST /apply/policy/rebuild` (via `kubectl port-forward`) | Rebuild |
| HTTP (debug) | `POST /apply/policy/build` | Build |

---

## Orchestrator

`policy_update/orchestrator.py`

Dispatches to one sub-agent based on trigger type, then sequences `PolicyApplyGraph` (see [Shared Module: `shared/apply/`](../aiac-agent.md#sharedapply)):
- `build` trigger → Build sub-agent → Policy Apply
- `rebuild` trigger → Rebuild sub-agent → Policy Apply

If the sub-agent's `validate_policy` fails (`policy_model is None`), the orchestrator returns the abort response directly without calling `PolicyApplyGraph`.

The Policy Update agent compares the **current composite role mappings** (authoritative record of previously applied rules) against the **current policy in ChromaDB** and applies the delta: adding missing composite mappings and removing stale ones.

---

## Sub-agents

### Build Sub-agent

`policy_update/build/`

```
START → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_pdp_state] → propose_policy → validate_policy → END
```

#### Nodes

- **`fetch_pdp_state`**: fetches all roles and their current composites, all services and their permissions, all scopes.
- **`propose_policy`**: LLM node; produces `PolicyModel` — minimal delta between ChromaDB policy and live composite state. `PolicyModel` is defined in `aiac/pdp/library/policy/models.py` (see [`../aiac-agent.md`](../aiac-agent.md)).
- **`validate_policy`**: existence check + safety guard rails + auditor LLM re-confirmation + scope check. See [Validate Node common checks](../aiac-agent.md#validate-node--common-checks-all-agents). Writes `policy_model` to state on success; leaves it `None` on failure.

#### Graph

```mermaid
flowchart TD
    START(("START"))

    START --> FP["fetch_policy\nChromaDB"]
    START --> FDK["fetch_domain_knowledge\nChromaDB"]
    START --> FKC["fetch_pdp_state\nall roles + composites,\nall services + permissions"]

    FP & FDK & FKC --> PROPOSE["propose_policy\nPlanner LLM -> PolicyModel\nminimal delta vs live composites"]

    PROPOSE --> VALIDATE["validate_policy\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check"]

    VALIDATE --> END(("END"))
```

#### State

`BaseAgentState` (no extensions required).

#### Prompts (`policy_update/build/prompts.py`)

`PLANNER_SYSTEM`, `AUDITOR_SYSTEM`.

---

### Rebuild Sub-agent

`policy_update/rebuild/`

Identical to the Build sub-agent with one addition: a `clear_composites` node prepended before the fetch fan-out.

```
START → clear_composites → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_pdp_state] → propose_policy → validate_policy → END
```

#### Delta from Build

- **`clear_composites`**: calls `clear_all_composites(realm)` from `aiac.pdp.library.policy` before the fetch fan-out. Removes all composite mappings from all roles.
- **`fetch_pdp_state`**: receives a `PDPSnapshot` with empty `role_composites` after the wipe.
- **`propose_policy`**: produces an add-only `PolicyModel` (no removals — composites are empty).
- **`validate_policy`**: identical contract to Build.

#### Graph

```mermaid
flowchart TD
    START(("START")) --> CLEAR["clear_composites\nclear_all_composites\nrealm-wide wipe"]

    CLEAR --> FP["fetch_policy\nChromaDB"]
    CLEAR --> FDK["fetch_domain_knowledge\nChromaDB"]
    CLEAR --> FKC["fetch_pdp_state\nempty role_composites\nafter wipe"]

    FP & FDK & FKC --> PROPOSE["propose_policy\nPlanner LLM -> PolicyModel\nadd-only: composites are empty"]

    PROPOSE --> VALIDATE["validate_policy\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check"]

    VALIDATE --> END(("END"))
```

#### State

`BaseAgentState` (no extensions required).

#### Prompts (`policy_update/rebuild/prompts.py`)

`PLANNER_SYSTEM`, `AUDITOR_SYSTEM`.

---

## Response

**Success:**
```json
{ "added": [...], "removed": [...], "summary": "...", "provisioned": null }
```

**Abort (validation failure):**
```json
{ "added": [], "removed": [], "summary": "...", "validation_errors": [...], "provisioned": null }
```

---

## File Structure

```
aiac/src/aiac/agent/policy_update/
├── __init__.py
├── orchestrator.py                  ← dispatches to build or rebuild sub-agent, then sequences PolicyApplyGraph
├── build/
│   ├── __init__.py
│   ├── graph.py                     ← Build StateGraph
│   ├── nodes.py                     ← fetch_pdp_state, propose_policy, validate_policy
│   └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
└── rebuild/
    ├── __init__.py
    ├── graph.py                     ← Rebuild StateGraph
    ├── nodes.py                     ← clear_composites, fetch_pdp_state, propose_policy, validate_policy
    └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
```

---

## Open Questions

_None currently._
