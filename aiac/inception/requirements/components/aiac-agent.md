# Component PRD: AIAC Agent

## Description

A LangGraph-based AI agent service that enforces a natural-language access control policy against the live PDP state. Triggered via the **Event Broker** (NATS JetStream) for all automated triggers, and directly via HTTP for the operator-only `rebuild` command:

- **Event Broker** → `aiac.apply.service.{id}` subject (originated by Keycloak SPI `CLIENT_CREATED`)
- **Event Broker** → `aiac.apply.realm-role.{id}` subject (originated by Keycloak SPI realm role created/updated)
- **Event Broker** → `aiac.apply.build` subject (originated by RAG Ingest Service post-ingest)
- **Operator/admin call** → `POST /apply/rebuild` directly via `kubectl port-forward` (HTTP only — not routed through Event Broker)

The Agent subscribes to the Event Broker as a durable competing consumer (`aiac-agent-consumer` queue group). It acknowledges each message only after successful processing — ensuring at-least-once delivery and automatic replay on pod restart.

The `/apply/*` HTTP endpoints are retained as a debugging escape hatch. The **NATS consumer is a thin adapter layer** that receives events from the Event Broker and calls the same internal `/apply/*` handler functions — there is no duplicated business logic.

The service is structured as a **Controller** (FastAPI routes) that dispatches to three **Orchestrators**, each owning one or more compiled `StateGraph` sub-agents:

| Orchestrator | Trigger(s) | Sub-agents |
|---|---|---|
| Service Onboarding | `service/{id}` | Service Provision → Service Policy (sequential) |
| Policy Update | `build`, `rebuild` | Build sub-agent or Rebuild sub-agent (alternative) |
| Role Update | `realm-role/{id}` | Realm Role sub-agent |

All components are **logically separated modules within a single pod and process** — no inter-service network calls between orchestrators and sub-agents.

```mermaid
flowchart TD
    NATS["Event Broker\nNATS JetStream\naiac.apply.>"]
    NATS_CONSUMER["NATS Consumer\nasyncio background task\nthin adapter"]
    TRIGGERS["HTTP Triggers\nPOST /apply/*\n(debugging + rebuild)"]
    CTRL["Controller\nroutes.py"]

    NATS -->|"durable queue group\naiac-agent-consumer"| NATS_CONSUMER
    NATS_CONSUMER -->|"calls internal handler"| CTRL
    TRIGGERS --> CTRL

    subgraph CO["Service Onboarding"]
        ORC1["Orchestrator"]
        SA1["Service Provision"]
        SA2["Service Policy"]
        ORC1 --> SA1
        ORC1 --> SA2
    end

    subgraph PU["Policy Update"]
        ORC2["Orchestrator"]
        SA3["Build"]
        SA4["Rebuild"]
        ORC2 --> SA3
        ORC2 --> SA4
    end

    subgraph RR["Role Update"]
        ORC3["Orchestrator"]
        SA5["Realm Role"]
        ORC3 --> SA5
    end

    TRIGGERS --> CTRL
    CTRL -->|"realm-role/:id"| ORC3
    CTRL -->|"build / rebuild"| ORC2
    CTRL -->|"service/:id"| ORC1
```

---

## NATS Consumer

A thin adapter started as an **asyncio background task** in the FastAPI `lifespan` handler. It subscribes to the `aiac.apply.>` wildcard on the `aiac-events` NATS JetStream stream using the `aiac-agent-consumer` durable queue group.

### Dispatch table

| Subject pattern | Internal handler |
|---|---|
| `aiac.apply.service.{id}` | Service Onboarding Orchestrator |
| `aiac.apply.realm-role.{id}` | Role Update Orchestrator |
| `aiac.apply.build` | Policy Update Orchestrator (Build) |

### Ack contract

The consumer **awaits** the internal handler before issuing the NATS acknowledgement. On handler success → ack. On handler exception → do not ack; NATS redelivers after `AckWait`. After 5 unacknowledged redeliveries, NATS routes the message to `aiac.apply.dlq`.

Fire-and-forget (`asyncio.create_task`) is explicitly prohibited — acking before handler completion would break at-least-once guarantees.

### Failure isolation

The consumer and the FastAPI HTTP server share the same process. If the Agent pod crashes mid-processing, the in-flight message was never acked and NATS redelivers it to the next pod instance. This prevents the consumer from exhausting retry counts against an unavailable handler (which would occur if they were separate containers).

### Configuration

| Variable | Default | Source |
|---|---|---|
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | ConfigMap (`aiac-pdp-config`) |

---

## Controller

The Controller is a thin FastAPI routes layer (`controller/routes.py`). Its sole responsibilities are:

- Parse the trigger type and entity ID from the request path.
- Dispatch to the appropriate orchestrator.
- Return the orchestrator's response to the caller.

No business logic, retry handling, or state assembly lives in the Controller.

---

## Use Cases

Each orchestrator and its sub-agents are specified in a dedicated sub-PRD:

| Use Case | Sub-PRD | Trigger(s) |
|---|---|---|
| Service Onboarding | [aiac-agent/uc1-service-onboarding.md](aiac-agent/uc1-service-onboarding.md) | `aiac.apply.service.{id}`, `POST /apply/service/{id}` |
| Policy Update | [aiac-agent/uc2-policy-update.md](aiac-agent/uc2-policy-update.md) | `aiac.apply.build`, `POST /apply/build`, `POST /apply/rebuild` |
| Role Update | [aiac-agent/uc3-role-update.md](aiac-agent/uc3-role-update.md) | `aiac.apply.realm-role.{id}`, `POST /apply/realm-role/{id}` |

---

## Shared Module

Lives at `aiac/src/aiac/agent/shared/`.

```mermaid
flowchart TD
    subgraph SHARED["shared - used by all policy-applying sub-agents"]
        FP["fetch_policy\nQuery: aiac-policies collection\nReturns: policy_chunks\nFails: 503 after UPSTREAM_MAX_RETRIES"]
        FDK["fetch_domain_knowledge\nQuery: aiac-domain-knowledge collection\nReturns: domain_knowledge_chunks\nFails: non-fatal"]
    end

    subgraph STATE["BaseAgentState"]
        S1["trigger: TriggerContext"]
        S2["realm: str"]
        S3["policy_chunks: list of str"]
        S4["domain_knowledge_chunks: list of str"]
        S5["pdp_snapshot: PDPSnapshot"]
        S6["proposed_diff: ProposedDiff or None"]
        S7["validation_errors: list of str"]
        S8["added / removed: list of CompositeMapping"]
        S9["summary: str"]
    end

    subgraph QUERY_KEYS["ChromaDB query strings by trigger"]
        Q1["build / rebuild -> all access control rules"]
        Q2["realm-role/:id -> realm role assignment rules"]
        Q3["service/:id -> service access control rules"]
    end

    FP & FDK --> STATE
    QUERY_KEYS --> FP
    QUERY_KEYS --> FDK
```

### `shared/nodes.py`

Two node functions shared by all policy-applying sub-agents:

- `fetch_policy`: queries `aiac-policies` ChromaDB collection; stores results in `BaseAgentState.policy_chunks`. Returns `503` when ChromaDB is unavailable after `UPSTREAM_MAX_RETRIES` retries.
- `fetch_domain_knowledge`: queries `aiac-domain-knowledge` ChromaDB collection; stores results in `BaseAgentState.domain_knowledge_chunks`. Returns `[]` when collection is empty — non-fatal.

Both nodes use the same trigger-type-keyed query strings:

| Trigger | ChromaDB similarity query |
|---|---|
| `build` | `"all access control rules"` |
| `rebuild` | `"all access control rules"` |
| `realm-role/{id}` | `"realm role assignment rules"` |
| `service/{id}` | `"service access control rules"` |

Number of results capped by `CHROMA_N_RESULTS` (default `10`).

### `shared/state.py`

All type definitions shared across agents:

#### `BaseAgentState`

| Field | Type | Description |
|---|---|---|
| `trigger` | `TriggerContext` | Endpoint type + entity ID |
| `realm` | `str` | Realm name (from `KEYCLOAK_REALM`) |
| `policy_chunks` | `list[str]` | Policy text chunks from `aiac-policies` |
| `domain_knowledge_chunks` | `list[str]` | Domain context chunks from `aiac-domain-knowledge` |
| `pdp_snapshot` | `PDPSnapshot` | Scoped PDP data for this trigger |
| `proposed_diff` | `ProposedDiff \| None` | LLM output |
| `validation_errors` | `list[str]` | Errors from validate node |
| `added` | `list[CompositeMapping]` | Executed composite additions |
| `removed` | `list[CompositeMapping]` | Executed composite removals |
| `summary` | `str` | Human-readable explanation |

#### `PDPSnapshot`

```python
class PDPSnapshot(BaseModel):
    subjects: list[Subject] = []
    roles: list[Role] = []
    services: list[Service] = []
    service_permissions: dict[str, list[Permission]] = {}  # service_id → permissions
    service_scopes: list[Scope] = []
    subject_assignments: dict[str, Assignments] = {}       # subject_id → assignments
    role_composites: dict[str, list[Permission]] = {}      # role_name → current composite permissions
```

#### `ProposedDiff` and `CompositeMapping`

```python
class CompositeMapping(BaseModel):
    role_name: str
    service_id: str
    permission_id: str
    permission_name: str

class ProposedDiff(BaseModel):
    add: list[CompositeMapping]
    remove: list[CompositeMapping]
    reasoning: str
```

#### `ValidationVerdict`

```python
class ValidationVerdict(BaseModel):
    approved: bool
    reason: str
```

Service Onboarding types (`ServiceType`, `ServiceInfo`, `ServiceProvision`, `OnboardingProvisionState`, etc.) are defined in `onboarding/provision/state.py` — see [UC1: Service Onboarding](aiac-agent/uc1-service-onboarding.md).

---

## LLM Integration

All `propose_*` and `validate_*` nodes use `langchain-openai` (`ChatOpenAI`) via `llm.with_structured_output()`. Target endpoint must support tool calling.

Each sub-agent defines its own `PLANNER_SYSTEM` and `AUDITOR_SYSTEM` constants in its `prompts.py`:

- **Planner prompt**: system message (stable, cacheable) — role definition + `AIAC_AC_MODEL` framing scoped to the agent's context; user message (per-request) — trigger description + policy chunks + domain knowledge section + scoped PDP snapshot summary.
- **Auditor prompt**: system message — auditor role for the specific agent's scope; user message — proposed diff + policy chunks + domain knowledge chunks.

Service Provision sub-agent additionally defines `ANALYZE_AGENT_SYSTEM` and `ANALYZE_TOOL_SYSTEM` in `onboarding/provision/prompts.py`.

---

## Validate Node — Common Checks (All Agents)

All `validate_*` / `validate_mappings` nodes perform the same four checks. Binary abort on any failure:

```mermaid
flowchart TD
    IN["proposed_diff\n+ pdp_snapshot"] --> C1

    C1{"1. Existence check\nEvery role_name, service_id,\npermission_id in diff exists\nin pdp_snapshot"}
    C1 -->|"fail"| ABORT["ABORT\nvalidation_errors populated\nadded and removed empty"]
    C1 -->|"pass"| C2

    C2{"2. Safety guard rails\ntotal changes\nadd + remove\n<= MAX_CHANGES_PER_RUN"}
    C2 -->|"fail"| ABORT
    C2 -->|"pass"| C3

    C3{"3. LLM re-confirmation\nAuditor system prompt\n-> ValidationVerdict\napproved bool + reason str"}
    C3 -->|"approved=false"| ABORT
    C3 -->|"approved=true"| C4

    C4{"4. Scope check\nDiff bounded to entities\nreferenced by trigger\nno over-reach"}
    C4 -->|"fail"| ABORT
    C4 -->|"pass"| APPLY["proceed to apply_*"]

    style ABORT fill:#fee2e2
    style APPLY fill:#dcfce7
    style C3 fill:#fef9c3
```

1. **Existence check** — every `role_name`, `service_id`, `permission_id` in the diff exists in `pdp_snapshot`.
2. **Safety guard rails** — total changes (`add` + `remove`) ≤ `MAX_CHANGES_PER_RUN`.
3. **LLM re-confirmation** — second LLM call with auditor system prompt; returns `ValidationVerdict(approved, reason)`.
4. **Scope check** — diff is bounded to entities referenced by the trigger; no over-reach on partial updates.

---

## Endpoints

| Method | Path | Orchestrator | Sub-agent |
|---|---|---|---|
| POST | `/apply/build` | Policy Update | Build |
| POST | `/apply/rebuild` | Policy Update | Rebuild |
| POST | `/apply/realm-role/{role_id}` | Role Update | Realm Role |
| POST | `/apply/service/{service_id}` | Service Onboarding | Provision → Policy |

**Success response (Service Onboarding):**
```json
{ "added": [...], "removed": [...], "summary": "...", "provisioned": { "roles": [...], "scopes": [...] } }
```

**Success response (all other agents):**
```json
{ "added": [...], "removed": [...], "summary": "...", "provisioned": null }
```

**Abort response (validation failure, all agents):**
```json
{ "added": [], "removed": [], "summary": "...", "validation_errors": [...], "provisioned": null }
```

---

## Configuration

| Variable | Default | Source |
|---|---|---|
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | ConfigMap (`aiac-pdp-config`) |
| `AIAC_PDP_CONFIG_URL` | `http://aiac-pdp-config-service:7071` | ConfigMap (`aiac-pdp-config`) |
| `AIAC_PDP_POLICY_URL` | `http://aiac-pdp-policy-service:7072` | ConfigMap (`aiac-pdp-config`) |
| `AIAC_CHROMADB_URL` | `http://aiac-rag-service:8000` | ConfigMap (`aiac-pdp-config`) |
| `KEYCLOAK_REALM` | — | ConfigMap (`aiac-pdp-config`) |
| `LLM_BASE_URL` | — | ConfigMap |
| `LLM_MODEL` | — | ConfigMap |
| `LLM_API_KEY` | — | Kubernetes Secret |
| `AIAC_AC_MODEL` | `RBAC` | ConfigMap (accepted: `RBAC`, `ABAC`, `REBAC`) |
| `CHROMA_N_RESULTS` | `10` | ConfigMap |
| `MAX_CHANGES_PER_RUN` | `50` | ConfigMap |
| `UPSTREAM_MAX_RETRIES` | `3` | ConfigMap |

ChromaDB collections: `aiac-policies` and `aiac-domain-knowledge`.

---

## Error Handling

All upstream calls are retried up to `UPSTREAM_MAX_RETRIES` times with exponential backoff (`tenacity`) before propagating the error.

| Upstream | HTTP status on final failure |
|---|---|
| ChromaDB | `503 Service Unavailable` |
| PDP Configuration Service | `502 Bad Gateway` |
| PDP Policy Service | `502 Bad Gateway` |
| Kubernetes API | `502 Bad Gateway` |
| LLM API | `504 Gateway Timeout` |

---

## Runtime

- Framework: FastAPI with uvicorn
- Bind: `0.0.0.0:7070`
- State: stateless — changes applied immediately, no pending session required
- Base image: `python:3.12-slim`

---

## File Structure

```
aiac/src/aiac/agent/
├── controller/
│   ├── __init__.py
│   └── routes.py                        ← FastAPI app + four /apply/* route handlers
│
├── onboarding/
│   ├── __init__.py
│   ├── orchestrator.py                  ← sequences provision → policy, assembles combined response
│   ├── provision/
│   │   ├── __init__.py
│   │   ├── graph.py                     ← Service Provision StateGraph
│   │   ├── nodes.py                     ← classify_service, analyze_agent, analyze_tool, provision_service, format_response
│   │   ├── prompts.py                   ← ANALYZE_AGENT_SYSTEM, ANALYZE_TOOL_SYSTEM
│   │   └── state.py                     ← ServiceType, Skill, ServiceInfo, RoleDefinition, ScopeDefinition, ServiceProvision, OnboardingProvisionState
│   └── policy/
│       ├── __init__.py
│       ├── graph.py                     ← Service Policy StateGraph
│       ├── nodes.py                     ← fetch_pdp_state, propose_mappings, validate_mappings, apply_mappings, format_response
│       └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│
├── policy_update/
│   ├── __init__.py
│   ├── orchestrator.py                  ← dispatches to build or rebuild sub-agent
│   ├── build/
│   │   ├── __init__.py
│   │   ├── graph.py                     ← Build StateGraph
│   │   ├── nodes.py                     ← fetch_pdp_state, propose_diff, validate_diff, apply_diff, format_response
│   │   └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│   └── rebuild/
│       ├── __init__.py
│       ├── graph.py                     ← Rebuild StateGraph
│       ├── nodes.py                     ← clear_composites, fetch_pdp_state, propose_diff, validate_diff, apply_diff, format_response
│       └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│
├── realm_roles/
│   ├── __init__.py
│   ├── orchestrator.py                  ← dispatches to realm_role sub-agent
│   └── realm_role/
│       ├── __init__.py
│       ├── graph.py                     ← Realm Role StateGraph
│       ├── nodes.py                     ← fetch_pdp_state, propose_mappings, validate_mappings, apply_mappings, format_response
│       └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│
└── shared/
    ├── __init__.py
    ├── nodes.py                         ← fetch_policy, fetch_domain_knowledge
    └── state.py                         ← BaseAgentState, TriggerContext, PDPSnapshot, ProposedDiff, CompositeMapping, ValidationVerdict
```

Docker build command (run from repo root):

```bash
docker build -f aiac/src/aiac/agent/controller/Dockerfile \
             -t aiac-agent:latest \
             aiac/src/
```

---

## Dependencies (`requirements.txt`)

```
langgraph
langchain-openai
chromadb
tenacity
fastapi
uvicorn[standard]
requests
python-dotenv
kubernetes
nats-py
```
