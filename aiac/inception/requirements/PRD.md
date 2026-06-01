# PRD: AI-based Access Control (AIAC)

## 1. Purpose

Kagenti AI agents call services across a shared platform. Each call must carry a token narrowed to exactly the permissions the caller's role entitles on the target service. The challenge is where access policy lives: without a dedicated policy management layer, the natural design is a hybrid where AuthBridge (the per-pod enforcement sidecar) declares `token_scopes` in its route configuration — spreading policy intent across per-deployment ConfigMaps rather than maintaining it in a single authoritative place.

AIAC solves this by automating RBAC/ABAC management using a natural-language policy enforced by an AI agent, built around a generic **Policy Decision Point (PDP)** abstraction with Keycloak as the Phase 1 backend. The system comprises six components:

1. **PDP Configuration Service** — a REST service that exposes PDP entity data (subjects, roles, services, scopes, permissions, composite mappings) for read operations. Backed by Keycloak in both phases; the read interface is stable across phases.
2. **PDP Policy Service** — a REST service that applies policy changes to the active PDP backend. Phase 1 implementation writes Keycloak composite role mappings (realm role → service permissions). Phase 2 implementation writes LLM-generated Rego rules to OPA. Both implementations expose the same Kubernetes ClusterIP service name — switching phases is a deployment swap only.
3. **RAG Knowledge Base** — a ChromaDB vector store holding the access control policy and domain knowledge in persistent, queryable form, populated via a co-located RAG Ingest Service.
4. **Event Broker** — a NATS JetStream pod that decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides durable, at-least-once delivery with automatic replay on Agent pod restart. Competing consumer model ensures each event is processed exactly once.
5. **AIAC Agent** — a LangGraph-based AI agent triggered by Event Broker subscriptions (`aiac.apply.>` subjects) and directly by the operator (`rebuild` only). It retrieves the current policy from the RAG store, interprets it against the live PDP state, and applies the required policy changes immediately.
6. **Python library** — `aiac.pdp.library` provides typed access to both PDP services via `read_api` and `write_api` modules backed by generic Pydantic models.

### Design principle: PDP/PEP separation

AIAC enforces a strict three-layer model across both phases:

| Layer | Component | Role |
|---|---|---|
| **Policy Management** | AIAC Agent | Translates natural-language policy into PDP configuration on every trigger |
| **Policy Decision (PDP)** | Keycloak (Phase 1) / OPA (Phase 2) | Decides what a caller may access; issues scoped tokens |
| **Policy Enforcement (PEP)** | AuthBridge | Intercepts traffic; exchanges tokens; carries no policy knowledge |

The PEP (AuthBridge) is a pure enforcement layer. It performs RFC 8693 token exchanges sending only the target `audience` — no `scope` parameter. The PDP evaluates the caller's realm role and issues a token containing exactly the entitlements that role grants on the target service.

This means `token_scopes` is absent from `authproxy-routes`. Route configuration carries routing intent only (`host` → `target_audience`). Policy intent lives entirely in the PDP, kept current by AIAC.

### Implementation phases

| Phase | PDP Policy write target | Write operation | PEP behaviour |
|---|---|---|---|
| Phase 1 | Keycloak | Composite role mappings (realm role → service permissions) | `audience` only — Keycloak resolves entitlements from composites |
| Phase 2 | OPA | LLM-generated Rego rules | `audience` only — OPA evaluates Rego; PEP is unchanged |

Phase transition: before Phase 2 is activated, the agent clears all composite mappings from Keycloak, then the PDP Policy pod is replaced with the OPA implementation. AuthBridge requires no changes — the PEP is identical in both phases.

---

## 2. Architecture Overview

Six components across six Kubernetes Pods plus a Python library layer, all implemented in Python 3.12. External dependencies: Keycloak Admin API, an LLM API, and an embedding API. The Keycloak SPI listener is defined in a separate PRD.

### Deployment topology

```
┌──────────────────────────────────────────────────────────┐
│  PDP Configuration Pod                                   │
│                                                          │
│  ┌────────────────────────┐                              │
│  │  PDP Configuration     │  :7070  ClusterIP            │
│  │  Service (FastAPI)     │  aiac-pdp-config-service     │
│  └────────────────────────┘                              │
│              ▲                                           │
└──────────────┼───────────────────────────────────────────┘
               │
┌──────────────┼───────────────────────────────────────────┐
│  PDP Policy Pod (Phase 1: Keycloak | Phase 2: OPA)       │
│              │                                           │
│  ┌────────────────────────┐                              │
│  │  PDP Policy Service    │  :7073  ClusterIP            │
│  │  (FastAPI)             │  aiac-pdp-policy-service     │
│  └────────────────────────┘                              │
│              ▲                                           │
└──────────────┼───────────────────────────────────────────┘
               │
┌──────────────┼───────────────────────────────────────────┐
│  Event Broker Pod                                        │
│              │                                           │
│  ┌────────────────────────┐                              │
│  │  NATS JetStream        │  :4222  ClusterIP            │
│  │                        │  aiac-event-broker-service   │
│  │  stream: aiac-events   │                              │
│  │  subjects: aiac.apply.>│                              │
│  │  dlq: aiac.apply.dlq   │                              │
│  └────────────────────────┘                              │
│              ▲                ▲                          │
│    (publish) │                │ (publish)                │
└──────────────┼────────────────┼──────────────────────────┘
               │  (subscribe)   │
┌──────────────┼───────────────────────────────────────────┐
│  Agent Pod   │                                           │
│              │                                           │
│  ┌────────────────────────────────────────────────────┐  │
│  │  aiac-init (init container)                        │  │
│  │  python:3.12-slim + nats-py + httpx                │  │
│  │  gates: NATS + PDP Config + PDP Policy + RAG       │  │
│  │  creates: aiac-events JetStream stream             │  │
│  └────────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────────┐  │
│  │  AIAC Agent (FastAPI)  :7071  ClusterIP            │  │
│  │  LangGraph-based                                   │  │
│  │  + NATS consumer (asyncio background task)         │  │
│  │    consumer: aiac-agent-consumer queue group       │  │
│  └────────────────────────────────────────────────────┘  │
│              │                                           │
└──────────────┼───────────────────────────────────────────┘
               │
┌──────────────┼───────────────────────────────────────────┐
│  RAG Pod     │                                           │
│              ▼                                           │
│  ┌──────────────────────────┐  ┌──────────────────────┐  │
│  │  ChromaDB  :7080         │  │  RAG Ingest Service  │  │
│  │  collections:            │  │  (FastAPI) :7072     │  │
│  │    aiac-policies         │  │                      │  │
│  │    aiac-domain-knowledge │  │                      │  │
│  └──────────────────────────┘  └──────────────────────┘  │
│  ClusterIP: aiac-rag-service (7080 + 7072)               │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Python library  (aiac/src/)                             │
│                                                          │
│  aiac.pdp.library.models   — Pydantic only               │
│  aiac.pdp.library.read_api — HTTP client →               │
│                          PDP Configuration Service       │
│  aiac.pdp.library.write_api — HTTP client →              │
│                          PDP Policy Service              │
└──────────────────────────────────────────────────────────┘
```

### Call flow

```
Policy / domain knowledge ingestion (operator-driven):

  Developer ──(kubectl port-forward)──► RAG Ingest Service ──► ChromaDB aiac-policies          [policy rules]
                                                           ├──► ChromaDB aiac-domain-knowledge  [org/business context]
                                                           ├──► Embedding API (external)
                                                           └──► Event Broker aiac.apply.build   [trigger policy recompute]

Role enforcement (event-driven):

  Policy build trigger (aiac.apply.build) → Policy Update Orchestrator:

  Event Broker ──► AIAC Agent (NATS consumer) ──┬──► ChromaDB aiac-policies         [retrieve policy chunks]
                                                ├──► ChromaDB aiac-domain-knowledge  [retrieve domain context chunks]
                                                ├──► read_api ──► PDP Configuration Service ──► Keycloak Admin API  [read state + composites]
                                                ├──► LLM API (external)              [propose diff from policy + domain context + state]
                                                ├──► LLM API (external)              [validate diff]
                                                ├──► write_api ──► PDP Policy Service ──► Keycloak Admin API  [apply composite diff]
                                                └──► NATS ack                        [message removed from stream]

  Rebuild trigger (operator-only, HTTP direct):

  Operator ──(kubectl port-forward)──► AIAC Agent /apply/rebuild ──┬── write_api ──► PDP Policy Service  [clear all composite mappings]
                                                                   ├──► ChromaDB aiac-policies
                                                                   ├──► ChromaDB aiac-domain-knowledge
                                                                   ├──► read_api ──► PDP Configuration Service ──► Keycloak Admin API
                                                                   ├──► LLM API (external)
                                                                   ├──► LLM API (external)
                                                                   └──► write_api ──► PDP Policy Service ──► Keycloak Admin API

  Realm role trigger (aiac.apply.realm-role.{id}) → Realm Roles Orchestrator:

  Event Broker ──► AIAC Agent (NATS consumer) ──┬──► ChromaDB aiac-policies         [retrieve policy chunks]
                                                ├──► ChromaDB aiac-domain-knowledge  [retrieve domain context chunks]
                                                ├──► read_api ──► PDP Configuration Service ──► Keycloak Admin API  [read scoped state]
                                                ├──► LLM API (external)              [propose composite mappings scoped to affected role]
                                                ├──► LLM API (external)              [validate mappings]
                                                ├──► write_api ──► PDP Policy Service ──► Keycloak Admin API  [apply composite mappings]
                                                └──► NATS ack

  Service onboarding trigger (aiac.apply.service.{id}) → Service Onboarding Orchestrator:

  Event Broker ──► AIAC Agent (NATS consumer) ──┬──► Kubernetes API (in-cluster)    [retrieve AgentRuntime/AgentCard CR → ServiceInfo]
                                                ├──► LLM API (external)              [analyze agent/tool → ServiceProvision]
                                                ├──► write_api ──► PDP Policy Service ──► Keycloak Admin API  [provision permissions + scopes]
                                                ├──► ChromaDB aiac-policies         [retrieve policy chunks]
                                                ├──► ChromaDB aiac-domain-knowledge  [retrieve domain context chunks]
                                                ├──► read_api ──► PDP Configuration Service ──► Keycloak Admin API  [read state]
                                                ├──► LLM API (external)              [propose composite mappings for new service]
                                                ├──► LLM API (external)              [validate mappings]
                                                ├──► write_api ──► PDP Policy Service ──► Keycloak Admin API  [apply composite mappings]
                                                └──► NATS ack
```

### Component dependencies

| Component | Called by | Calls | Returns |
|-----------|-----------|-------|---------|
| PDP Configuration Service | `aiac.pdp.library.read_api` | Keycloak Admin REST API | Raw Keycloak JSON (generic endpoint names) |
| PDP Policy Service (Keycloak) | `aiac.pdp.library.write_api` | Keycloak Admin REST API | 204/201 on success |
| `aiac.pdp.library.models` | `aiac.pdp.library.read_api`, `aiac.pdp.library.write_api`, AIAC Agent | — | Pydantic model definitions |
| `aiac.pdp.library.read_api` | AIAC Agent, Python scripts | PDP Configuration Service (HTTP) | Typed Pydantic instances |
| `aiac.pdp.library.write_api` | AIAC Agent, Python scripts | PDP Policy Service (HTTP) | Typed Pydantic instances or None |
| ChromaDB | RAG Ingest Service (writes), AIAC Agent (reads) | — | Policy and domain knowledge vectors |
| RAG Ingest Service | Developer (via `kubectl port-forward`) | ChromaDB, Embedding API, Event Broker | — |
| Event Broker (NATS JetStream) | Keycloak SPI listener, RAG Ingest Service (publishers); NATS JetStream (DLQ routing) | — | Durable event delivery to AIAC Agent; DLQ on max retries |
| AIAC Agent | Event Broker (NATS consumer), operator (`/apply/rebuild` HTTP direct) | Policy Update / Realm Roles / Service Onboarding orchestrators → `aiac.pdp.library.*`, ChromaDB, LLM API, Kubernetes API | Applied composite diff; provisioned service permissions/scopes (onboarding) |

### Key architectural decisions

- **PDP services bind to `0.0.0.0`.** Exposed as Kubernetes ClusterIP Services so that the Agent Pod can reach them over the cluster network.
- **PDP Policy Service ClusterIP name is stable across phases.** `aiac-pdp-policy-service` on `:7073` is used in both Phase 1 (Keycloak) and Phase 2 (OPA). Phase transition = deployment swap only.
- **Phase 1 RBAC via composite roles.** AIAC manages realm role → service permission mappings at the role level, not per-user. Users inherit permissions automatically when assigned a realm role.
- **RAG Pod runs ChromaDB and RAG Ingest Service together.** Exposed as `aiac-rag-service` on ports 7080 (ChromaDB) and 7072 (RAG Ingest Service).
- **AIAC Agent is stateless.** Changes are applied immediately on trigger — no pending session or human confirmation step.
- **Event Broker decouples all automated triggers from the Agent.** The Keycloak SPI listener and RAG Ingest Service publish to NATS subjects; the Agent subscribes as a durable competing consumer. This removes all direct dependencies between trigger sources and the Agent.
- **`rebuild` bypasses the Event Broker.** It is an operator-only command issued directly via HTTP (`kubectl port-forward`). It is never published to NATS and has no NATS listener.
- **NATS consumer is a thin adapter.** It receives events from the Event Broker and calls the same internal `/apply/*` handler functions used by the debug HTTP endpoints. No business logic lives in the consumer.
- **Agent `/apply/*` HTTP endpoints are retained for debugging.** They are not the primary trigger path; the NATS consumer is. `kubectl port-forward` to the Agent is used only for `rebuild` and debugging.
- **Event Broker uses WorkQueuePolicy.** Messages are removed from the stream after acknowledgement. Unacknowledged messages survive Agent pod restarts and are redelivered automatically. After 5 failed deliveries, messages are routed to `aiac.apply.dlq`.
- **AIAC init container gates Agent startup.** Before the Agent container starts, the init container waits for NATS, PDP Configuration Service, PDP Policy Service, and RAG Ingest Service to be healthy, then creates the `aiac-events` JetStream stream idempotently.
- **`aiac.pdp.library.models` is dependency-free** (only `pydantic`). Agents can import it without pulling in `requests` or `python-dotenv`.
- **`aiac.__init__`, `aiac.pdp.__init__`, `aiac.pdp.library.__init__`, `aiac.pdp.configuration.__init__`, `aiac.pdp.policy.__init__`, and `aiac.pdp.policy.keycloak.__init__` are empty.** Callers use explicit submodule paths: `from aiac.pdp.library.models import Subject`, `from aiac.pdp.library.read_api import get_subjects`.
- **ChromaDB hosts two collections: `aiac-policies` and `aiac-domain-knowledge`.** Collection slug to ChromaDB name mapping: `policy` → `aiac-policies`, `domain-knowledge` → `aiac-domain-knowledge`.
- **`user/{id}` trigger removed.** Composite role mappings are realm-role-scoped; individual user creation/update does not require agent intervention — composites apply automatically.

---

## 3. Component: PDP Configuration Service

FastAPI service (`0.0.0.0:7070`) that proxies Keycloak Admin REST API read endpoints using generic PDP entity names. Exposes 7 read endpoints. Stateless, no caching. Supports per-request realm override via optional `?realm=` query parameter. Backed by Keycloak in both Phase 1 and Phase 2.

**Full spec:** [components/pdp-configuration-service.md](components/pdp-configuration-service.md)

---

## 4. Component: PDP Policy Service

FastAPI service (`0.0.0.0:7073`) that applies policy changes to the active PDP backend. Two implementations share the same Kubernetes ClusterIP name (`aiac-pdp-policy-service`):

- **Phase 1 — Keycloak:** manages composite role mappings (realm role → service permissions) via Keycloak Admin API. 5 write endpoints.
- **Phase 2 — OPA:** writes LLM-generated Rego rules to OPA. Interface TBD (separate PRD).

**Phase 1 full spec:** [components/pdp-policy-keycloak-service.md](components/pdp-policy-keycloak-service.md)

---

## 5. Component: Library

Python package at `aiac/src/`. Three submodules:

- **`aiac.pdp.library.models`** — dependency-free Pydantic models for all PDP entities (`Subject`, `Role`, `Service`, `Permission`, `Scope`, `Assignments`).
- **`aiac.pdp.library.read_api`** — HTTP client wrapping the PDP Configuration Service; returns typed Pydantic instances; all functions require a `realm: str` parameter.
- **`aiac.pdp.library.write_api`** — HTTP client wrapping the PDP Policy Service; abstracts Phase 1 (Keycloak composite mappings) and Phase 2 (OPA Rego) behind a stable function interface.

**Full spec:** [components/library.md](components/library.md)

---

## 6. Component: Event Broker

NATS JetStream pod (`aiac-event-broker-service:4222`). Decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides at-least-once delivery, replay on pod restart via `WorkQueuePolicy`, and a dead-letter subject (`aiac.apply.dlq`) after 5 failed deliveries. No authentication — ClusterIP network isolation is the access control mechanism. Stream: `aiac-events`, subjects `aiac.apply.>`, consumer group `aiac-agent-consumer`.

**Full spec:** [components/event-broker.md](components/event-broker.md)

---

## 7. Component: AIAC Agent

FastAPI + LangGraph service (`0.0.0.0:7071`). Receives automated triggers via the **Event Broker** (NATS JetStream durable consumer, `aiac-agent-consumer` queue group) and the operator-only `rebuild` command directly via HTTP. Structured as a thin **Controller** (`controller/routes.py`) that dispatches `/apply/*` handlers to three **Orchestrators**, each owning one or more compiled `StateGraph` sub-agents. A **NATS consumer** (asyncio background task in the FastAPI `lifespan` handler) is a thin adapter that receives NATS events and calls the same internal handler functions used by the HTTP endpoints:

| Orchestrator | Trigger(s) | Sub-agents |
|---|---|---|
| Service Onboarding | `aiac.apply.service.{id}` | Service Provision → Service Policy (sequential) |
| Policy Update | `aiac.apply.build`, `/apply/rebuild` (HTTP) | Build sub-agent or Rebuild sub-agent (alternative) |
| Realm Roles | `aiac.apply.realm-role.{id}` | Realm Role sub-agent |

All sub-agent `StateGraph` instances are logically separated modules running within a single pod and process. The **Policy Update** sub-agents compute a minimal delta between the current ChromaDB policy and live composite role state. The **Rebuild** variant additionally clears all composite mappings before computing the diff. The **Realm Roles** sub-agent applies scoped composite mappings for a single affected realm role. The **Service Onboarding** orchestrator first provisions service permissions/scopes (via the Kubernetes in-cluster API to read `AgentRuntime`/`AgentCard` CRs), then maps realm roles to the new service's permissions via composite role additions. Stateless; changes are applied immediately. Integrated retry with differentiated error codes per upstream.

**Full spec:** [components/aiac-agent.md](components/aiac-agent.md)

---

## 8. Component: RAG Knowledge Base

ChromaDB vector store (`aiac-rag-service:7080`) hosting two collections: `aiac-policies` (access control policy rules) and `aiac-domain-knowledge` (org/business context such as team rosters, application ownership, and department mappings). Both collections are managed by the RAG Ingest Service and read by the AIAC Agent. Co-located with the RAG Ingest Service in the RAG Pod.

**Full spec:** [components/rag-knowledge-base.md](components/rag-knowledge-base.md)

---

## 9. Component: RAG Ingest Service

FastAPI service (`0.0.0.0:7072`) co-located with ChromaDB. Thirteen collection-parameterized endpoints across three semantics: complete collection replacement (`POST /ingest/{collection}/{text|file|url}`), document-level upsert (`POST /ingest/{collection}/update/{text|file|url}`), and explicit removal (`DELETE /ingest/{collection}/{doc_id}`). The `{collection}` slug is validated against `AIAC_RAG_COLLECTIONS` (default: `policy,domain-knowledge`). After every successful ingest the service publishes to `aiac.apply.build` on the Event Broker (`NATS_URL`). Developer access via `kubectl port-forward`.

**Full spec:** [components/rag-ingest-service.md](components/rag-ingest-service.md)

---

## 10. Component: Keycloak SPI Listener

A custom Keycloak Event Listener SPI (Java) that listens to Keycloak's internal event bus and translates entity-scoped events into NATS publish calls to the Event Broker. The AIAC Agent subject schema is authoritative; the SPI PRD references it.

| Keycloak Event | Event Broker subject |
|---|---|
| `REGISTER`, `UPDATE_PROFILE` (user events) | — (dropped; composite roles handle user permission inheritance automatically) |
| `CLIENT_CREATED` | `aiac.apply.service.{id}` |
| Realm role created/updated | `aiac.apply.realm-role.{id}` |

**Full spec:** TBD (separate PRD).

---

## 11. Deployment

### Kubernetes manifests

Six separate manifest files:

| File | Contents |
|------|----------|
| `aiac/k8s/pdp-config-deployment.yaml` | `aiac-pdp-config` ConfigMap + PDP Configuration Service Pod Deployment + ClusterIP Service |
| `aiac/k8s/pdp-policy-keycloak-deployment.yaml` | PDP Policy Service Pod Deployment (Keycloak implementation) + ClusterIP Service |
| `aiac/k8s/event-broker-deployment.yaml` | Event Broker Pod Deployment (NATS JetStream) + ClusterIP Service |
| `aiac/k8s/rag-deployment.yaml` | RAG Pod Deployment (ChromaDB + RAG Ingest Service containers) + ClusterIP Service |
| `aiac/k8s/agent-deployment.yaml` | Agent Pod Deployment (aiac-init container + AIAC Agent container) + ClusterIP Service |
| `aiac/k8s/pdp-policy-opa-deployment.yaml` | PDP Policy Service Pod Deployment (OPA implementation) — Phase 2, TBD |

The PDP Configuration and PDP Policy (Keycloak) Pods mount `aiac-pdp-config` (KEYCLOAK_URL, KEYCLOAK_REALM) and `keycloak-admin-secret` (KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD) as env vars.

### Docker images

Built independently. No entry in the repo's `build.yaml` CI matrix.

```bash
# Build PDP Configuration Service
docker build -f aiac/src/aiac/pdp/configuration/Dockerfile -t aiac-pdp-config:latest aiac/src/

# Build PDP Policy Service (Keycloak)
docker build -f aiac/src/aiac/pdp/policy/keycloak/Dockerfile -t aiac-pdp-policy-keycloak:latest aiac/src/

# Build Agent (includes aiac-init container)
docker build -f aiac/src/aiac/agent/controller/Dockerfile -t aiac-agent:latest aiac/src/

# Build RAG Ingest Service
docker build -t aiac-rag-ingest:latest aiac/rag-ingest/
```

The Event Broker uses the official `nats` Docker image with JetStream enabled (`-js` flag). No custom build required.

### `aiac-pdp-config` ConfigMap template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aiac-pdp-config
data:
  KEYCLOAK_URL: "http://keycloak-service.keycloak.svc:8080"
  KEYCLOAK_REALM: "kagenti"
  AIAC_PDP_CONFIG_URL: "http://aiac-pdp-config-service:7070"
  AIAC_PDP_POLICY_URL: "http://aiac-pdp-policy-service:7073"
  NATS_URL: "nats://aiac-event-broker-service:4222"
  AIAC_RAG_INGEST_URL: "http://aiac-rag-service:7072"
```

Update `KEYCLOAK_URL` and `KEYCLOAK_REALM` for the target environment before applying.

---

## 12. Testing

Tests live in `aiac/test/`.

### Unit tests

| Target | What to mock | What to assert |
|--------|-------------|----------------|
| PDP Configuration Service endpoints | `KeycloakAdmin` methods (return fixture dicts) | Correct JSON response, 502 on Keycloak error |
| PDP Policy Service (Keycloak) endpoints | `KeycloakAdmin` methods | 204 on write success, 201 on create, 502 on Keycloak error |
| `aiac.pdp.library.models` | No mock needed | `extra='ignore'` drops unknown fields, required fields validated, `model_validate` round-trips correctly |
| `aiac.pdp.library.read_api` functions | PDP Configuration Service HTTP endpoints | Returns correct Pydantic model instances; `RuntimeError` on non-2xx; default URL fallback |
| `aiac.pdp.library.write_api` functions | PDP Policy Service HTTP endpoints | Correct serialisation; `RuntimeError` on non-2xx; default URL fallback |
| Event Broker NATS consumer | NATS message delivery (mock `nats-py` subscription) | Correct handler dispatched per subject; ack issued on success; no ack on handler exception |
| Event Broker DLQ | NATS max redelivery exceeded | Message routed to `aiac.apply.dlq` after 5 failures |
| Init container health-check | HTTP 4xx then 200 sequence; NATS TCP refused then connected | Exits 0 only after all four dependencies healthy; `add_stream` called with correct config |
| AIAC Agent | TBD | TBD |

### Integration tests

Require a live Keycloak instance. Controlled by env vars:

| Variable | Description |
|----------|-------------|
| `KEYCLOAK_URL` | Keycloak base URL |
| `KEYCLOAK_REALM` | Realm to query |
| `KEYCLOAK_ADMIN_USERNAME` | Admin username |
| `KEYCLOAK_ADMIN_PASSWORD` | Admin password |

Integration tests call the live PDP Configuration Service (running locally or via port-forward) and assert that results are non-empty lists of the correct type. Event Broker integration tests require a live NATS JetStream instance.

Use a pytest marker (e.g. `@pytest.mark.integration`) so unit tests and integration tests can be run independently:

```bash
pytest aiac/ -m "not integration"   # unit only
pytest aiac/ -m integration          # integration only
```

---

## 13. Conventions and constraints

- Python version: 3.12
- Base Docker image: `python:3.12-slim`
- Linting: ruff (line length 120, target py312 per root `pyproject.toml`)
- Commits: DCO sign-off required (`git commit -s`); use `Assisted-By` not `Co-Authored-By`
- No auth on PDP Configuration Service, PDP Policy Service, RAG Ingest Service, or Event Broker — network isolation (ClusterIP + `kubectl port-forward`) is the access control mechanism
- PDP Configuration Service, PDP Policy Service, Agent, RAG Ingest Service, and Event Broker are not registered with the repo's `build.yaml` CI matrix; they have independent build processes
- `aiac/__init__.py` exists and is empty — `aiac` is a regular package, not a namespace package
- NATS consumer must **await** handler completion before issuing ack — fire-and-forget (`asyncio.create_task`) is prohibited; premature ack breaks at-least-once delivery guarantees
