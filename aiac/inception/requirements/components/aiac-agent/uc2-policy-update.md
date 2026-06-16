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

Dispatches to one sub-agent based on trigger type:
- `build` trigger → Build sub-agent
- `rebuild` trigger → Rebuild sub-agent

The Policy Update agent compares the **current composite role mappings** (authoritative record of previously applied rules) against the **current policy in ChromaDB** and applies the delta: adding missing composite mappings and removing stale ones.

---

## Sub-agents

### Build Sub-agent

`policy_update/build/`

```
START → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_pdp_state] → propose_diff → validate_diff → apply_diff → format_response → END
```

#### Nodes

- **`fetch_pdp_state`**: fetches all roles and their current composites, all services and their permissions, all scopes.
- **`propose_diff`**: LLM node; produces `ProposedDiff` — minimal delta between ChromaDB policy and live composite state.
- **`validate_diff`**: existence check + safety guard rails + auditor LLM re-confirmation + scope check. See [Validate Node common checks](../aiac-agent.md#validate-node--common-checks-all-agents).
- **`apply_diff`**: calls `add_role_composites` / `remove_role_composites` from `aiac.pdp.library.policy`.
- **`format_response`**: assembles the build result.

#### Graph

```mermaid
flowchart TD
    START(("START"))

    START --> FP["fetch_policy\nChromaDB"]
    START --> FDK["fetch_domain_knowledge\nChromaDB"]
    START --> FKC["fetch_pdp_state\nall roles + composites,\nall services + permissions"]

    FP & FDK & FKC --> PROPOSE["propose_diff\nPlanner LLM -> ProposedDiff\nminimal delta vs live composites"]

    PROPOSE --> VALIDATE["validate_diff\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check"]

    VALIDATE --> APPLY["apply_diff\nadd_role_composites\nremove_role_composites"]
    APPLY --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    style FP fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FDK fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FKC fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style PROPOSE fill:#713f12,color:#fef3c7,stroke:#d97706
    style VALIDATE fill:#713f12,color:#fef3c7,stroke:#d97706
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
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
START → clear_composites → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_pdp_state] → propose_diff → validate_diff → apply_diff → format_response → END
```

#### Delta from Build

- **`clear_composites`**: calls `clear_all_composites(realm)` from `aiac.pdp.library.policy` before the fetch fan-out. Removes all composite mappings from all roles.
- **`fetch_pdp_state`**: receives a `PDPSnapshot` with empty `role_composites` after the wipe.
- **`propose_diff`**: produces an add-only diff (no removals — composites are empty).
- All remaining nodes (`validate_diff`, `apply_diff`, `format_response`): identical contract to Build.

#### Graph

```mermaid
flowchart TD
    START(("START")) --> CLEAR["clear_composites\nclear_all_composites\nrealm-wide wipe"]

    CLEAR --> FP["fetch_policy\nChromaDB"]
    CLEAR --> FDK["fetch_domain_knowledge\nChromaDB"]
    CLEAR --> FKC["fetch_pdp_state\nempty role_composites\nafter wipe"]

    FP & FDK & FKC --> PROPOSE["propose_diff\nPlanner LLM -> ProposedDiff\nadd-only: composites are empty"]

    PROPOSE --> VALIDATE["validate_diff\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check"]

    VALIDATE --> APPLY["apply_diff\nadd_role_composites only"]
    APPLY --> FORMAT["format_response"]
    FORMAT --> END(("END"))

    style CLEAR fill:#7f1d1d,color:#fee2e2,stroke:#f87171
    style FP fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FDK fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style FKC fill:#1e3a8a,color:#e2e8f0,stroke:#5a9fd4
    style PROPOSE fill:#713f12,color:#fef3c7,stroke:#d97706
    style VALIDATE fill:#713f12,color:#fef3c7,stroke:#d97706
    style APPLY fill:#14532d,color:#dcfce7,stroke:#4ade80
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
├── orchestrator.py                  ← dispatches to build or rebuild sub-agent
├── build/
│   ├── __init__.py
│   ├── graph.py                     ← Build StateGraph
│   ├── nodes.py                     ← fetch_pdp_state, propose_diff, validate_diff, apply_diff, format_response
│   └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
└── rebuild/
    ├── __init__.py
    ├── graph.py                     ← Rebuild StateGraph
    ├── nodes.py                     ← clear_composites, fetch_pdp_state, propose_diff, validate_diff, apply_diff, format_response
    └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
```

---

## Open Questions

_None currently._
