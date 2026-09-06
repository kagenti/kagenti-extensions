# Component PRD: AIAC Agent

## Description

A LangGraph-based AI agent service that enforces a natural-language access control policy against the live PDP state. Triggered via the **Event Broker** (NATS JetStream) for all automated triggers, and directly via HTTP for the operator-only `rebuild` command:

- **Event Broker** → `aiac.apply.service.{id}` subject (originated by Keycloak SPI `CLIENT_CREATED`)
- **Event Broker** → `aiac.apply.role.{id}` subject (originated by Keycloak SPI role created/updated)
- **Event Broker** → `aiac.apply.policy.build` subject (originated by RAG Ingest Service post-ingest)
- **Operator/admin call** → `POST /apply/policy/rebuild` directly via `kubectl port-forward` (HTTP only — not routed through Event Broker)

The Agent subscribes to the Event Broker as a durable competing consumer (`aiac-agent-consumer` queue group). It acknowledges each message only after successful processing — ensuring at-least-once delivery and automatic replay on pod restart.

The `/apply/*` HTTP endpoints are retained as a debugging escape hatch. The **NATS consumer is a thin adapter layer** that receives events from the Event Broker and calls the same internal `/apply/*` handler functions — there is no duplicated business logic.

The service is structured as a **Controller** (FastAPI routes) that dispatches to the **Service Onboarding Orchestrator** (UC1) or directly to the Policy Update and Role Update sub-agents (UC2, UC3). Each producing sub-agent calls the **shared Policy Rules Builder** (`agent/policy_rules_builder/`) directly, merges the results internally, and returns a single `list[PolicyRule]` to the Controller. The Controller calls `compute_and_apply(merged_rules)` from `aiac.policy.computation` (PCE) once.

| Use Case | Dispatch | Sub-agents | Sub-agent output |
|---|---|---|---|
| Service Onboarding (UC1) | via Orchestrator | Service Provision + Service Policy Builder | `list[PolicyRule]` |
| Policy Update (UC2) | Controller → sub-agent directly | Build or Rebuild (TBD) | `list[PolicyRule]` |
| Role Update (UC3) | Controller → sub-agent directly | Role sub-agent | `list[PolicyRule]` |

Each producing sub-agent calls the **shared Policy Rules Builder** (`agent/policy_rules_builder/`) for each applicable (roles, scope) or (role, scopes) pair, merges the results, and returns a single `list[PolicyRule]` to the Controller. The Controller calls `compute_and_apply(merged_rules)` from `aiac.policy.computation` (PCE) once — no shared apply node exists. The PCE owns all Policy Model Store ↔ PDP Policy Writer coordination. Neither sub-agents nor the Policy Rules Builder call `aiac.pdp.policy.library` or `aiac.policy.model_store.library` directly.

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
        SA2["Service Policy Builder"]
        ORC1 --> SA1
        ORC1 --> SA2
    end

    subgraph PU["Policy Update"]
        SA4["Build"]
        SA5["Rebuild"]
        SA5 -->|"delegates"| SA4
    end

    subgraph RR["Role Update"]
        SA6["Role"]
    end

    PRB["Policy Rules Builder (shared)\nagent/policy_rules_builder/"]
    PCE["Policy Computation Engine\naiac.policy.computation\ncompute_and_apply(rules)"]

    CTRL -->|"service/:id"| ORC1
    CTRL -->|"build"| SA4
    CTRL -->|"rebuild"| SA5
    CTRL -->|"role/:id"| SA6

    SA2  -->|"calls"| PRB
    SA4  -->|"calls"| PRB
    SA6  -->|"calls"| PRB

    ORC1 -->|"list[PolicyRule]"| CTRL
    SA4  -->|"list[PolicyRule]"| CTRL
    SA5  -->|"list[PolicyRule]"| CTRL
    SA6  -->|"list[PolicyRule]"| CTRL

    CTRL -->|"merged rules"| PCE
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

> **Follow-up:** `aiac.apply.offboard.{id}` (Service Offboarding, UC4) is the intended subject for event-driven offboard. It is **not yet wired** into the consumer — offboard is reachable today only via the `POST /apply/offboard/{service_id}` HTTP route.

### Ack contract

The consumer **awaits** the internal handler before it issues the NATS acknowledgement. On handler success → ack.

On handler failure, the consumer classifies the exception by **type**, not by HTTP status code:

- **Permanent** — `PolicyConflictError`, `PolicyContradictionError`, `PolicyRulesBuilderError`, and `UnparseableLLMResponseError`. The consumer calls `term()` and routes the message to `aiac.apply.dlq` **immediately**. There is no redelivery, because the same input cannot succeed on a retry.
- **Retryable** — `LLMAccessError`, plus any genuinely unknown or transient error. The consumer does **not** ack. NATS redelivers after `AckWait`, up to `MAX_DELIVER` (5) deliveries, then routes the message to `aiac.apply.dlq`.

Both entry paths log through the shared `log_by_type` helper (`agent/shared/error_logging.py`), because FastAPI exception handlers do **not** fire on the NATS path.

Fire-and-forget (`asyncio.create_task`) is explicitly prohibited — an ack before handler completion would break the at-least-once guarantee.

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
- Receive the `list[PolicyRule]` returned by the Orchestrator or sub-agent (already merged by the sub-agent).
- Call `compute_and_apply(merged_rules)` from `aiac.policy.computation` (PCE) once.
- Return a bare HTTP status code to the caller; write summary and debug info to the log.

No per-use-case business logic, retry handling, or state assembly lives in the Controller. PRB calls are owned by the producing sub-agents; the Controller's shared step is the single PCE call.

---

## Use Cases

Each use case (and the UC1 Orchestrator) is specified in a dedicated sub-PRD:

| Use Case | Sub-PRD | Trigger(s) | Notes |
|---|---|---|---|
| Service Onboarding | [aiac-agent/uc1-service-onboarding.md](aiac-agent/uc1-service-onboarding.md) | `aiac.apply.service.{id}`, `POST /apply/service/{id}` | Orchestrator sequences: Service Provision → Service Policy Builder (IdP reader + PRB invoker) |
| Policy Update | [aiac-agent/uc2-policy-update.md](aiac-agent/uc2-policy-update.md) | `aiac.apply.policy.build`, `POST /apply/policy/build`, `POST /apply/policy/rebuild` | |
| Role Update | [aiac-agent/uc3-role-update.md](aiac-agent/uc3-role-update.md) | `aiac.apply.role.{id}`, `POST /apply/role/{id}` | |
| Service Offboarding | (see PCE `decommission`) | `POST /apply/offboard/{service_id}` (`aiac.apply.offboard.{id}` — NATS wiring is a follow-up) | Thin sub-agent; calls the PCE's `decommission(service_id)` **directly** (whole-service teardown, not a rule fold — bypasses the PRB and `compute_and_apply`). Keyed by **clientId, not UUID** (an offboarded client is gone from `get_services()`). |

> **Note:** Each producing sub-agent (UC1–UC3) calls the **shared Policy Rules Builder** directly, merges the results, and returns `list[PolicyRule]` to the Controller. The Controller calls `compute_and_apply(merged_rules)` from `aiac.policy.computation` (PCE) once. Policy rule application is fully specified in [policy-computation-engine.md](policy-computation-engine.md). The Policy Rules Builder is specified in [aiac-agent/policy-rules-builder.md](aiac-agent/policy-rules-builder.md). **UC4 (Service Offboarding) is the exception:** it produces no rules — its handler resolves the clientId and calls the PCE's authoritative `decommission(service_id)` (specified in [policy-computation-engine.md → Decommission](policy-computation-engine.md#decommission-service-offboard)) to tear down the service's entire policy footprint.

### IdP access — library, not service

Every sub-agent (UC1 Provision + Service Policy Builder, UC2 Build + Rebuild, UC3 Role) performs **all** IdP reads and writes through the **idp-library** API — `aiac.idp.configuration.api.Configuration` — and **never** calls the IdP Configuration **service** (`aiac.idp.service.configuration.*`) or its HTTP endpoints directly. The library owns the HTTP transport, retry/backoff, and Keycloak↔model mapping; sub-agents depend only on its typed `Configuration` methods (e.g. `get_service`, `get_services`, `get_subjects`, `get_scopes`, `create_service_role`, `create_service_scope`, `set_service_type`). The shared service-type vocabulary is `aiac.idp.configuration.models.ServiceType` (`Agent`/`Tool`) — the same enum used by `Service.type`. See [library-idp.md](library-idp.md).

---

## Endpoints

| Method | Path | Orchestrator | Sub-agent |
|---|---|---|---|
| GET | `/health` | — | — (liveness/readiness) |
| POST | `/apply/policy/build` | Policy Update | Build |
| POST | `/apply/policy/rebuild` | Policy Update | Rebuild |
| POST | `/apply/role/{role_id}` | Role Update | Role |
| POST | `/apply/service/{service_id}` | Service Onboarding | Provision |
| POST | `/apply/offboard/{service_id}` | Service Offboarding | Offboard (calls PCE `decommission` directly) |

`GET /health` is a bare liveness/readiness probe: the Controller is stateless (no local state, no connection held at rest), so it answers `200 {"status": "ok"}` whenever the process is serving, dispatching to no handler and touching no upstream. Upstream reachability (IdP, PCE, NATS) is validated per-request by the handlers. The k8s Deployment wires both the readiness and liveness probes to it.

The `/apply/offboard/{service_id}` path uses the `{service_id:path}` converter (slash-bearing SPIFFE-URI clientIds) and is keyed on the **clientId (SPM key)**, not the Keycloak UUID that `/apply/service/{service_id}` carries — an offboarded client is gone from `get_services()`, so UUID→clientId resolution is impossible.

The `/apply/*` endpoints return bare HTTP status codes: `200 OK` on success (no response body), and the status codes from the Error Handling table on upstream failure. Success responses carry no body; upstream failures and PRB exceptions are raised as FastAPI `HTTPException`s, so error responses carry a sanitized JSON error body (`{"detail": <safe summary>}`; see [Error Handling → Sanitized body vs. full log](#sanitized-body-vs-full-log)) alongside the status code. Summary, applied-rule details, and debug information are written to the service log. Validation failures surface as an error status and log entry; detailed reporting is specified in [policy-rules-builder.md](aiac-agent/policy-rules-builder.md). A genuine grant/prohibit conflict surfaces on `/apply` as a `422` with a `ConflictReport` body (verbatim policy quotes; see [Error Handling](#error-handling)). There is no separate pre-commit `/policy/check` route — it is retired ([ADR 0001](../../adr/0001-identify-never-reconcile.md) / #2503), and the conflict diagnostic is folded into `/apply`.

---

## Configuration

| Variable | Default | Source |
|---|---|---|
| `NATS_URL` | `nats://aiac-event-broker-service:4222` | ConfigMap (`aiac-pdp-config`) |
| `AIAC_PDP_CONFIG_URL` | `http://aiac-pdp-config-service:7071` | ConfigMap (`aiac-pdp-config`) — used by `aiac.idp.configuration.api` (in-process via PCE) |
| `AIAC_PDP_POLICY_URL` | `http://aiac-pdp-policy-service:7072` | ConfigMap (`aiac-pdp-config`) — used by `aiac.pdp.policy.library` (in-process via PCE) |
| `AIAC_POLICY_MODEL_STORE_URL` | `http://aiac-policy-model-store-service:7074` | ConfigMap (`aiac-pdp-config`) — used by `aiac.policy.model_store.library` (in-process via PCE) |
| `AIAC_CHROMADB_URL` | `http://aiac-rag-service:8000` | ConfigMap (`aiac-pdp-config`) |
| `KEYCLOAK_REALM` | — | ConfigMap (`aiac-pdp-config`) |
| `LLM_BASE_URL` | — | ConfigMap |
| `LLM_MODEL` | — | ConfigMap |
| `LLM_API_KEY` | — | Kubernetes Secret |
| `AIAC_AC_MODEL` | `RBAC` | ConfigMap (accepted: `RBAC`, `ABAC`, `REBAC`) |
| `CHROMA_N_RESULTS` | `10` | ConfigMap |
| `MAX_CHANGES_PER_RUN` | `50` | ConfigMap |
| `UPSTREAM_MAX_RETRIES` | `3` | ConfigMap |
| `LLM_MAX_RETRIES` | `3` | ConfigMap |
| `LLM_RETRY_BACKOFF_MIN` | `1` | ConfigMap |
| `LLM_RETRY_BACKOFF_MAX` | `30` | ConfigMap |

`UPSTREAM_MAX_RETRIES` governs the IdP, MCP, and Kubernetes transport seams only. The `LLM_*` knobs govern the PRB's LLM seam (see [Error Handling → Two retry layers](#two-retry-layers)).

ChromaDB collections: `aiac-policies` and `aiac-domain-knowledge`.

---

## Error Handling

### Two retry layers

The Agent keeps two retry layers distinct.

**Transport retries.** The Agent retries each upstream transport call up to `UPSTREAM_MAX_RETRIES` times with exponential backoff (`tenacity`) before the error propagates. The retry primitive is the project-level shared `run_upstream(fn)` helper (`aiac/shared/upstream.py`). It is transport-agnostic: it re-raises the original exception after the final attempt. The Agent applies retry at the **transport boundary**, not at the agent call sites — inside the idp-library `Configuration` (its `_request` helper), inside the provision MCP helper (`_mcp_tools_list`), and inside the provision Kubernetes seam (`uc/onboarding/provision/kube.py`). Each caller then maps the re-raised failure to the upstream status below.

**LLM-seam retries.** The Policy Rules Builder (PRB) retries its own LLM seam with dedicated knobs — `LLM_MAX_RETRIES`, `LLM_RETRY_BACKOFF_MIN`, and `LLM_RETRY_BACKOFF_MAX` (specified in [`aiac-agent/policy-rules-builder.md`](aiac-agent/policy-rules-builder.md)). `UPSTREAM_MAX_RETRIES` does **not** govern LLM calls. It stays for the IdP, MCP, and Kubernetes transport seams only.

### Upstream → HTTP status

| Upstream | HTTP status on final failure |
|---|---|
| ChromaDB | `503 Service Unavailable` |
| IdP Configuration Service | `502 Bad Gateway` |
| PDP Policy Writer | `502 Bad Gateway` |
| Kubernetes API | `502 Bad Gateway` |
| LLM API | `502 Bad Gateway` |

### Exception → HTTP status

The PRB raises a typed exception hierarchy (specified in [`aiac-agent/policy-rules-builder.md`](aiac-agent/policy-rules-builder.md)). Each consuming caller maps the exception to an HTTP status.

| Exception | Raised where | HTTP status |
|---|---|---|
| `PolicyRulesBuilderBaseError` (base) | — | `500` (safety net) |
| `PolicyRulesBuilderError` | PRB `_audit`, after `MAX_AUDIT_RETRIES` | `422` |
| `LLMAccessError` | PRB `_structured_call`, transient retries exhausted | `502` |
| `UnparseableLLMResponseError` | PRB `_structured_call`, reachable but unparseable | `502` |
| `PolicyContradictionError` | PRB `_audit`, genuine contradiction | `422` |
| `PolicyConflictError` (carries a `ConflictReport`) | `ServicePolicyBuilder.build` (UC1) | `422` |

The base class `PolicyRulesBuilderBaseError` is a `500` safety net: any unforeseen PRB error still returns a defined status, not an untyped `500`. Both `LLMAccessError` and `UnparseableLLMResponseError` map to `502`, but they differ on the async path (see [Async failure classification](#async-failure-classification)). The HTTP status is decoupled from the async retry class.

### Sanitized body vs. full log

An error response body carries a safe summary only — `{"detail": <safe summary>}` — with no internal endpoint, host, or key. The full detail (endpoint, root cause, and traceback) goes to the named loggers only. The `PolicyConflictError` body is the one exception: its `ConflictReport` is already safe, because it carries policy quotes only.

Upstream failures and PRB exceptions propagate as HTTP error responses on the synchronous `/apply/*` paths, raised as FastAPI `HTTPException`s. The status code is authoritative.

### Async failure classification

On the NATS path the failure class is decided by **exception type**, never by HTTP status code. Permanent failures route straight to the dead-letter subject; retryable failures are redelivered. See [NATS Consumer → Ack contract](#ack-contract).

---

## Runtime

- Framework: FastAPI with uvicorn
- Bind: `0.0.0.0:7070`
- State: stateless — changes applied immediately, no pending session required
- Base image: `python:3.12-slim`

---

## File Structure

```
aiac/src/aiac/
├── shared/                             ← project-level shared: run_upstream (upstream.py) — transport retry primitive
└── agent/
    ├── controller/
    ├── shared/                         ← flatten_role (roles.py); focal_entities.py (resolve_focal_entities — D13, shared by live build() + diagnostic); error_logging.py (log_by_type — per-persona named-logger router)
    ├── uc/
    │   ├── onboarding/
    │   │   ├── orchestrator.py         ← sequences provision → policy_builder, returns list[PolicyRule]
    │   │   ├── provision/              ← LLM sub-agent: classify, analyze, write to IdP; kube.py = retrying K8s seam
    │   │   └── policy_builder/         ← IdP reader + PRB invoker: read IdP, call PRB, return list[PolicyRule]
    │   ├── policy_update/
    │   │   ├── build/                  ← calls PRB, returns list[PolicyRule]; TBD internals
    │   │   └── rebuild/                ← delegates to Build; TBD internals
    │   └── role_update/                ← calls PRB with (role, all_scopes), returns list[PolicyRule]
    └── policy_rules_builder/           ← shared; called by Service Policy Builder, Build, and Role sub-agent
        ├── diagnostic.py               ← parallel diagnostic assembly (START-seeds-text, _audit_diagnostic record-not-raise, terminal _explain)
        └── diagnostic_models.py        ← ConflictReport + conflict/unevaluated row models
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
