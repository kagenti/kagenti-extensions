# UC3: Role Update

## Depends on

- [`../aiac-agent.md`](../aiac-agent.md) — NATS Consumer, Controller, Shared Module, Validate Node common checks, Configuration, Error Handling, Runtime.

---

## Architecture

```mermaid
flowchart TD
    NATS["Event Broker\nNATS JetStream\naiac.apply.role.{id}"]
    NATS_CONSUMER["NATS Consumer\nasyncio background task\nthin adapter"]
    TRIGGERS["HTTP Triggers\nPOST /apply/role/{role_id}\n(debug)"]
    CTRL["Controller\nroutes.py"]

    NATS -->|"durable queue group\naiac-agent-consumer"| NATS_CONSUMER
    NATS_CONSUMER -->|"calls internal handler"| CTRL
    TRIGGERS --> CTRL

    subgraph RR["Role Update"]
        ORC3["Orchestrator"]
        SA5["Role"]
        ORC3 --> SA5
    end

    CTRL -->|"role/:id"| ORC3
```

---

## Trigger(s)

| Source | Subject / Path |
|---|---|
| Event Broker (NATS) | `aiac.apply.role.{id}` (originated by Keycloak SPI role created/updated) |
| HTTP (debug) | `POST /apply/role/{role_id}` |

---

## Orchestrator

`roles/orchestrator.py`

Dispatches to the Role sub-agent.

---

## Sub-agents

### Role Sub-agent

`roles/role/`

```
START → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_pdp_state] → propose_mappings → validate_mappings → apply_mappings → format_response → END
```

#### Nodes

- **`fetch_pdp_state`**: fetches all services and their permissions, all roles, and the current composites for the affected role.
- **`propose_mappings`**: LLM node; produces `ProposedDiff` scoped to the affected role.
- **`validate_mappings`**: existence check + safety guard rails + auditor LLM re-confirmation + scope check (bounded to the affected role). See [Validate Node common checks](../aiac-agent.md#validate-node--common-checks-all-agents).
- **`apply_mappings`**: calls `add_role_composites` / `remove_role_composites` from `aiac.pdp.library.policy`.
- **`format_response`**: assembles the result.

#### Graph

```mermaid
flowchart TD
    START(("START"))

    START --> FP["fetch_policy\nChromaDB"]
    START --> FDK["fetch_domain_knowledge\nChromaDB"]
    START --> FKC["fetch_pdp_state\naffected role composites,\nall services + permissions"]

    FP & FDK & FKC --> PROPOSE["propose_mappings\nPlanner LLM -> ProposedDiff\nscoped to affected role"]

    PROPOSE --> VALIDATE["validate_mappings\n1. Existence check\n2. Safety guard rails\n3. Auditor LLM\n4. Scope check\n   affected role only"]

    VALIDATE --> APPLY["apply_mappings\nadd_role_composites\nremove_role_composites"]
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

#### Prompts (`roles/role/prompts.py`)

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
aiac/src/aiac/agent/roles/
├── __init__.py
├── orchestrator.py                  ← dispatches to role sub-agent
└── role/
    ├── __init__.py
    ├── graph.py                     ← Role StateGraph
    ├── nodes.py                     ← fetch_pdp_state, propose_mappings, validate_mappings, apply_mappings, format_response
    └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
```

---

## Open Questions

_None currently._
