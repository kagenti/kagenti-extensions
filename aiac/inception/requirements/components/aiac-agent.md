# Component PRD: AIAC Agent

## Description

A LangGraph-based AI agent service that enforces a natural-language access control policy against the live PDP state. Triggered via the **Event Broker** (NATS JetStream) for all automated triggers, and directly via HTTP for the operator-only `rebuild` command:

- **Event Broker** → `aiac.apply.service.{id}` subject (originated by Keycloak SPI `CLIENT_CREATED`)
- **Event Broker** → `aiac.apply.role.{id}` subject (originated by Keycloak SPI role created/updated)
- **Event Broker** → `aiac.apply.policy.build` subject (originated by RAG Ingest Service post-ingest)
- **Operator/admin call** → `POST /apply/policy/rebuild` directly via `kubectl port-forward` (HTTP only — not routed through Event Broker)

The Agent subscribes to the Event Broker as a durable competing consumer (`aiac-agent-consumer` queue group). It acknowledges each message only after successful processing — ensuring at-least-once delivery and automatic replay on pod restart.

The `/apply/*` HTTP endpoints are retained as a debugging escape hatch. The **NATS consumer is a thin adapter layer** that receives events from the Event Broker and calls the same internal `/apply/*` handler functions — there is no duplicated business logic.

The service is structured as a **Controller** (FastAPI routes) that dispatches to the **Service Onboarding Orchestrator** (UC1) or directly to the Policy Update and Role Update sub-agents (UC2, UC3). Each sub-agent emits a `tuple[list[Role], list[Scope]]` scoped to the trigger. The Controller passes this tuple to the **shared Policy Rules Builder** (`agent/shared/policy_rules_builder/`), which emits a `list[PolicyRule]`, and then calls `compute_and_apply(rules)` directly via the PCE.

| Use Case | Dispatch | Sub-agents | Sub-agent output |
|---|---|---|---|
| Service Onboarding (UC1) | via Orchestrator | Service Provision → Service Policy (sequential) | `tuple[list[Role], list[Scope]]` (new service roles + scopes) |
| Policy Update (UC2) | Controller → sub-agent directly | Build sub-agent or Rebuild sub-agent (alternative) | `tuple[list[Role], list[Scope]]` (all roles + scopes) |
| Role Update (UC3) | Controller → sub-agent directly | Role sub-agent | `tuple[list[Role], list[Scope]]` (specific role + all scopes) |

Each producing sub-agent emits a `tuple[list[Role], list[Scope]]` scoped to the trigger. The Controller passes this tuple to a **shared Policy Rules Builder sub-agent** (`agent/shared/policy_rules_builder/`), which uses the natural-language policy to emit a `list[PolicyRule]` scoped to the trigger. The Controller then calls `compute_and_apply(rules)` from `aiac.policy.computation` directly — no shared apply node exists. The PCE owns all Policy Store ↔ PDP Policy Writer coordination. Neither sub-agents nor the Policy Rules Builder call `aiac.pdp.policy.library` or `aiac.policy.store.library` directly.

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
        ORC1 --> SA1
    end

    subgraph PU["Policy Update"]
        SA4["Build"]
        SA5["Rebuild"]
    end

    subgraph RR["Role Update"]
        SA6["Role"]
    end

    PRB["Policy Rules Builder (shared)\nagent/shared/policy_rules_builder/"]
    PCE["Policy Computation Engine\naiac.policy.computation\ncompute_and_apply(rules)"]

    CTRL -->|"service/:id"| ORC1
    CTRL -->|"build"| SA4
    CTRL -->|"rebuild"| SA5
    CTRL -->|"role/:id"| SA6

    ORC1 -->|"tuple"| PRB
    SA4 -->|"tuple"| PRB
    SA5 -->|"tuple"| PRB
    SA6 -->|"tuple"| PRB

    PRB -->|"rules"| CTRL
    CTRL -->|"rules"| PCE
```

---

## NATS Consumer

A thin adapter started as an **asyncio background task** in the FastAPI `lifespan` handler. It subscribes to the `aiac.apply.>` wildcard on the `aiac-events` NATS JetStream stream using the `aiac-agent-consumer` durable queue group.

### Dispatch table

| Subject pattern | Internal handler |
|---|---|
| `aiac.apply.service.{id}` | Service Onboarding Orchestrator (UC1) |
| `aiac.apply.role.{id}` | Role Update sub-agent (UC3, via Controller) |
| `aiac.apply.policy.build` | Policy Update Build sub-agent (UC2, via Controller) |

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

The Controller is a FastAPI routes layer (`controller/routes.py`). Its responsibilities are:

- Parse the trigger type and entity ID from the request path.
- Dispatch to the Service Onboarding Orchestrator (UC1) or directly to the Policy Update / Role Update sub-agents (UC2, UC3).
- Receive the `tuple[list[Role], list[Scope]]` returned by the Orchestrator or sub-agent.
- Pass the tuple to the **shared Policy Rules Builder** and receive `list[PolicyRule]`.
- Call `compute_and_apply(rules)` from `aiac.policy.computation` (PCE).
- Return the final response to the caller.

No per-use-case business logic, retry handling, or state assembly lives in the Controller. The Policy Rules Builder and PCE calls are shared steps driven uniformly by the Controller across all use cases.

---

## Use Cases

Each use case (and the UC1 Orchestrator) is specified in a dedicated sub-PRD:

| Use Case | Sub-PRD | Trigger(s) |
|---|---|---|
| Service Onboarding | [aiac-agent/uc1-service-onboarding.md](aiac-agent/uc1-service-onboarding.md) | `aiac.apply.service.{id}`, `POST /apply/service/{id}` |
| Policy Update | [aiac-agent/uc2-policy-update.md](aiac-agent/uc2-policy-update.md) | `aiac.apply.policy.build`, `POST /apply/policy/build`, `POST /apply/policy/rebuild` |
| Role Update | [aiac-agent/uc3-role-update.md](aiac-agent/uc3-role-update.md) | `aiac.apply.role.{id}`, `POST /apply/role/{id}` |

> **Note:** After the Orchestrator or sub-agent returns a `tuple[list[Role], list[Scope]]`, the Controller calls the **shared Policy Rules Builder** to produce `list[PolicyRule]`, then calls `compute_and_apply(rules)` from `aiac.policy.computation` (PCE). Policy rule application is fully specified in [policy-computation-engine.md](policy-computation-engine.md). The Policy Rules Builder is specified in the [Shared Module](#shared-module) section of this document.

---

## Shared Module

Lives at `aiac/src/aiac/agent/shared/`.

```mermaid
flowchart TD
    subgraph SHARED["shared/nodes.py — used by all sub-agents and the Policy Rules Builder"]
        FP["fetch_policy\nQuery: aiac-policies collection\nReturns: policy_chunks\nFails: 503 after UPSTREAM_MAX_RETRIES"]
        FDK["fetch_domain_knowledge\nQuery: aiac-domain-knowledge collection\nReturns: domain_knowledge_chunks\nFails: non-fatal"]
    end

    subgraph STATE["BaseAgentState (sub-agent fields)"]
        S1["trigger: TriggerContext"]
        S2["realm: str"]
        S3["policy_chunks: list of str"]
        S4["domain_knowledge_chunks: list of str"]
        S5["pdp_snapshot: PDPSnapshot"]
        S8["summary: str"]
    end

    subgraph QUERY_KEYS["ChromaDB query strings by trigger"]
        Q1["build / rebuild -> all access control rules"]
        Q2["role/:id -> role assignment rules"]
        Q3["service/:id -> service access control rules"]
    end

    FP & FDK --> STATE
    QUERY_KEYS --> FP
    QUERY_KEYS --> FDK
```

### `shared/nodes.py`

Two node functions used by all sub-agents and the Policy Rules Builder:

- `fetch_policy`: queries `aiac-policies` ChromaDB collection; stores results in `BaseAgentState.policy_chunks`. Returns `503` when ChromaDB is unavailable after `UPSTREAM_MAX_RETRIES` retries.
- `fetch_domain_knowledge`: queries `aiac-domain-knowledge` ChromaDB collection; stores results in `BaseAgentState.domain_knowledge_chunks`. Returns `[]` when collection is empty — non-fatal.

Both nodes use the same trigger-type-keyed query strings:

| Trigger | ChromaDB similarity query |
|---|---|
| `build` | `"all access control rules"` |
| `rebuild` | `"all access control rules"` |
| `role/{id}` | `"role assignment rules"` |
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
| `summary` | `str` | Human-readable explanation |

> **Note:** `rules: list[PolicyRule]` and `validation_errors: list[str]` are produced and consumed within the Policy Rules Builder. Whether they remain in `BaseAgentState` or move to a dedicated PRB state is pending the PRB design grill.

#### `PDPSnapshot`

```python
class PDPSnapshot(BaseModel):
    subjects: list[Subject] = []
    roles: list[Role] = []
    services: list[Service] = []
    service_scopes: list[Scope] = []
```

#### `ValidationVerdict`

```python
class ValidationVerdict(BaseModel):
    approved: bool
    reason: str
```

Service Onboarding types (`ServiceType`, `RoleDefinition`, `ScopeDefinition`, `ServiceProvision`, `OnboardingProvisionState`) are defined in `onboarding/provision/state.py`. `PolicyRule`, `AgentPolicyModel`, and `PolicyModel` are defined in `aiac.policy.model` — see [policy-model.md](policy-model.md). See [UC1: Service Onboarding](aiac-agent/uc1-service-onboarding.md).

---

### `policy_rules_builder/` (shared)

The **Policy Rules Builder** receives `tuple[list[Role], list[Scope]]` from the producing sub-agent (or UC1 Orchestrator) and the natural-language policy, and emits `list[PolicyRule]` scoped to the trigger. It does **not** call `aiac.pdp.policy.library` or `aiac.policy.store.library` directly; only the PCE does.

> **Detailed design TBD — pending dedicated grill.** Open questions: Is the Policy Rules Builder a LangGraph `StateGraph` or a simpler callable? Does it use a single LLM call or a `propose_*` + `validate_*` node pattern? What context does it receive beyond the tuple — policy chunks, domain knowledge chunks, PDP snapshot? Does it need a `realm` parameter? Does it read current Policy Store state to avoid duplicates, or does the PCE handle dedup?

#### Validate Node — Common Checks (pending PRB grill)

> **Note:** The exact placement of validation (inside the Policy Rules Builder vs. a separate Controller step) is pending the PRB design grill. The four checks below are confirmed requirements.

The validation step performs four checks. Binary abort on any failure:

```mermaid
flowchart TD
    IN["policy_model\n+ pdp_snapshot"] --> C1

    C1{"1. Existence check\nEntities referenced by rules\nstatements resolved via\naiac.idp.configuration.api"}
    C1 -->|"fail"| ABORT["ABORT\nvalidation_errors populated\nadded and removed empty"]
    C1 -->|"pass"| C2

    C2{"2. Safety guard rails\ntotal statements\nin PolicyModel\n<= MAX_CHANGES_PER_RUN"}
    C2 -->|"fail"| ABORT
    C2 -->|"pass"| C3

    C3{"3. LLM re-confirmation\nAuditor system prompt\n-> ValidationVerdict\napproved bool + reason str"}
    C3 -->|"approved=false"| ABORT
    C3 -->|"approved=true"| C4

    C4{"4. Scope check\nDiff bounded to entities\nreferenced by trigger\nno over-reach"}
    C4 -->|"fail"| ABORT
    C4 -->|"pass"| EMIT["emit list[PolicyRule] to Controller"]
```

1. **Existence check** — all entities referenced by `rules` statements exist; resolved via `aiac.idp.configuration.api`.
2. **Safety guard rails** — total statements in `PolicyModel` ≤ `MAX_CHANGES_PER_RUN`.
3. **LLM re-confirmation** — second LLM call with auditor system prompt; returns `ValidationVerdict(approved, reason)`.
4. **Scope check** — `PolicyModel` is bounded to entities referenced by the trigger; no over-reach on partial updates.

---

## LLM Integration

All `propose_*` and `validate_*` nodes use `langchain-openai` (`ChatOpenAI`) via `llm.with_structured_output()`. Target endpoint must support tool calling.

Each sub-agent defines its own `PLANNER_SYSTEM` and `AUDITOR_SYSTEM` constants in its `prompts.py`:

- **Planner prompt**: system message (stable, cacheable) — role definition + `AIAC_AC_MODEL` framing scoped to the agent's context; user message (per-request) — trigger description + policy chunks + domain knowledge section + scoped PDP snapshot summary.
- **Auditor prompt**: system message — auditor role for the specific agent's scope; user message — proposed diff + policy chunks + domain knowledge chunks.

The Policy Rules Builder's LLM usage pattern (prompts and node structure) is TBD — pending the PRB design grill.

---

## Endpoints

| Method | Path | Orchestrator | Sub-agent |
|---|---|---|---|
| POST | `/apply/policy/build` | Policy Update | Build |
| POST | `/apply/policy/rebuild` | Policy Update | Rebuild |
| POST | `/apply/role/{role_id}` | Role Update | Role |
| POST | `/apply/service/{service_id}` | Service Onboarding | Provision → Policy |

**Success response (Service Onboarding):**
```json
{ "summary": "...", "provisioned": { "roles": [...], "scopes": [...] } }
```

**Success response (all other agents):**
```json
{ "summary": "...", "provisioned": null }
```

**Abort response (validation failure, all agents):**
```json
{ "summary": "...", "validation_errors": [...], "provisioned": null }
```

---

## Configuration

| Variable | Default | Source |
|---|---|---|
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | ConfigMap (`aiac-pdp-config`) |
| `AIAC_PDP_CONFIG_URL` | `http://aiac-pdp-config-service:7071` | ConfigMap (`aiac-pdp-config`) — used by `aiac.idp.configuration.api` (in-process via PCE) |
| `AIAC_PDP_POLICY_URL` | `http://aiac-pdp-policy-service:7072` | ConfigMap (`aiac-pdp-config`) — used by `aiac.pdp.policy.library` (in-process via PCE) |
| `AIAC_POLICY_STORE_URL` | `http://aiac-policy-store-service:7074` | ConfigMap (`aiac-pdp-config`) — used by `aiac.policy.store.library` (in-process via PCE) |
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
| IdP Configuration Service | `502 Bad Gateway` |
| PDP Policy Writer | `502 Bad Gateway` |
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
│   └── routes.py                        ← FastAPI app + four route handlers
│
├── onboarding/
│   ├── __init__.py
│   ├── orchestrator.py                  ← sequences provision → policy; returns tuple[list[Role], list[Scope]] to Controller
│   ├── provision/
│   │   ├── __init__.py
│   │   ├── graph.py                     ← Service Provision StateGraph
│   │   ├── nodes.py                     ← classify_service, analyze_agent, analyze_tool, provision_service, format_response
│   │   └── state.py                     ← ServiceType, RoleDefinition, ScopeDefinition, ServiceProvision, OnboardingProvisionState
│   └── policy/
│       ├── __init__.py
│       ├── graph.py                     ← Policy StateGraph
│       ├── nodes.py                     ← fetch_pdp_state, propose_policy, validate_policy
│       └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│
├── policy_update/
│   ├── __init__.py
│   ├── build/
│   │   ├── __init__.py
│   │   ├── graph.py                     ← Build StateGraph
│   │   ├── nodes.py                     ← fetch_pdp_state, propose_diff, validate_diff, format_response
│   │   └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│   └── rebuild/
│       ├── __init__.py
│       ├── graph.py                     ← Rebuild StateGraph
│       ├── nodes.py                     ← clear_policy, fetch_pdp_state, propose_diff, validate_diff, format_response
│       └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│
├── roles/
│   ├── __init__.py
│   └── role/
│       ├── __init__.py
│       ├── graph.py                     ← Role StateGraph
│       ├── nodes.py                     ← fetch_pdp_state, propose_policy, validate_policy, format_response
│       └── prompts.py                   ← PLANNER_SYSTEM, AUDITOR_SYSTEM
│
└── shared/
    ├── __init__.py
    ├── nodes.py                         ← fetch_policy, fetch_domain_knowledge
    ├── state.py                         ← BaseAgentState, TriggerContext, PDPSnapshot, ValidationVerdict (PolicyRule imported from aiac.policy.model; exact rules/tuple field placement pending PRB grill)
    └── policy_rules_builder/            ← TBD — design pending dedicated grill
        ├── __init__.py
        ├── graph.py                     ← PolicyRulesBuilderGraph (TBD)
        ├── nodes.py                     ← TBD
        └── prompts.py                   ← TBD
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
