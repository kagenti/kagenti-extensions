# PRD: AI-based Access Control (AIAC)

## Abstract

AI-based Access Control (AIAC) is a Kagenti platform extension that automates RBAC/ABAC policy
enforcement for AI agents running on Kubernetes. A LangGraph-based AI agent continuously translates
a natural-language access control policy — stored in a vector knowledge base — into concrete
permission configurations in the active Policy Decision Point (PDP), eliminating manual policy
administration and preventing policy drift as services and roles evolve. Phase 1 targets Keycloak
as the PDP backend; Phase 2 (planned) replaces only the policy-write layer with OPA/Rego while
leaving all other components unchanged.

---

## 1. Problem Description

Kagenti AI agents call services across a shared platform. Every call must carry a token scoped to
exactly the permissions the caller's role entitles on the target service. Without a dedicated
policy management layer, access policy ends up scattered across per-deployment configuration,
creating three compounding problems:

1. **Policy drift** — new services and roles are onboarded without corresponding permission
   updates because there is no automated mechanism to apply them.
2. **Distributed policy intent** — no single authoritative source declares what roles may do;
   policy knowledge is fragmented across deployments.
3. **Manual administration overhead** — keeping Keycloak composite role mappings consistent with
   a growing fleet of agents and tools requires ongoing human attention with no audit trail.

---

## 2. Problem Solution

AIAC introduces a strict three-layer model that cleanly separates policy concerns: a **Policy
Management** layer (AIAC Agent) that translates natural-language policy into PDP configuration, a
**Policy Decision** layer (Keycloak / OPA) that evaluates caller entitlements, and a **Policy
Enforcement** layer (AuthBridge) that intercepts traffic and exchanges tokens but carries no policy
knowledge of its own.

The AIAC Agent subscribes to an event stream (NATS JetStream) and reacts to entity lifecycle
events — new services, role changes, policy updates — by retrieving the current policy from a RAG
knowledge base, querying live PDP state, and applying the minimal required diff via a dedicated
PDP Policy Service. **Policy intent lives entirely in the PDP, not in per-pod configuration.**

---

## 3. Design Principles

### PDP/PEP separation

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

## 4. Major Use-Cases

### UC-1 · Continuous Access Reconciliation (On-boarding / Off-boarding)

**Trigger:** A Realm Role or Keycloak Client is created, updated, or removed.

The Keycloak SPI listener publishes a scoped event to the Event Broker. The AIAC Agent retrieves
relevant context from the RAG store, reads the current composite role
state from the PDP, and asks the LLM to compute the minimal permission diff scoped to the affected
entity. The diff is validated by a second LLM pass and applied to Keycloak. Supports both
**auto-apply** (fully automated, least-privilege) and **recommendation + human review** modes.

**Two-phase implementation — trigger, RAG retrieval, LLM reasoning, and validation are identical in both phases; only the policy-write target differs:**

- **Phase 1 (current):** diff applied as Keycloak composite role mappings (realm role → service permissions)
- **Phase 2 (planned):** diff applied as LLM-generated Rego rules written to OPA; PDP Policy container image swap only — no other component changes

### UC-2 · Policy Update Reconciliation

**Trigger:** An operator ingests updated documents into the RAG store.

After ingestion the RAG Ingest Service publishes a build event. The AIAC Agent retrieves all
relevant context, computes a full composite role diff against current PDP state, and applies the
delta. A `rebuild` variant (operator-only, direct HTTP) first clears all composite mappings before
recomputing from scratch — used when policy changes are too broad for incremental diff.

### UC-3 · Entitlements Review

**Trigger:** Operator request (on-demand or scheduled).

The agent evaluates all current Keycloak composite mappings — including manually added ones that
AIAC did not create — against the policy. It reports compliant, non-compliant, and
policy-agnostic entitlements, enabling audit and remediation workflows.

### UC-4 · Access Request

**Trigger:** User request via chatbot.

A user requests an entitlement grant. The agent verifies the request against the policy
(permissive approach) and either auto-grants or routes to a human approver (man-in-the-loop).
Manually granted entitlements are flagged as policy-agnostic and surfaced during UC-3 reviews.

---

## 5. Architecture Overview

Six components across four Kubernetes Pods plus a Python library layer, all implemented in Python 3.12. External dependencies: Keycloak Admin API, an LLM API, and an embedding API. The Keycloak SPI listener is defined in a separate PRD.

### Component Summary

| # | Component | Description |
|---|-----------|-------------|
| 1 | **PDP Configuration Service** | REST service that exposes PDP entity data (subjects, roles, services, scopes, permissions, composite mappings) for read operations. Backed by Keycloak in both phases; the read interface is stable across phases. |
| 2 | **PDP Policy Service** | REST service that applies policy changes to the active PDP backend. Phase 1 writes Keycloak composite role mappings (realm role → service permissions). Phase 2 writes LLM-generated Rego rules to OPA. Both implementations expose the same Kubernetes ClusterIP service name — switching phases is a container image swap only. |
| 3 | **Policy and Domain Knowledge RAG** | ChromaDB vector store holding the access control policy and domain knowledge in persistent, queryable form, populated via a co-located RAG Ingest Service. |
| 4 | **Event Broker** | NATS JetStream pod that decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides durable, at-least-once delivery with automatic replay on Agent pod restart. Competing consumer model ensures each event is processed exactly once. |
| 5 | **AIAC Agent** | LangGraph-based AI agent triggered by Event Broker subscriptions (`aiac.apply.>` subjects) and directly by the operator (`rebuild` only). Retrieves the current policy from the RAG store, interprets it against live PDP state, and applies the required policy changes immediately. |
| 6 | **Python library** | Python API library provides typed access to both PDP services via `configuration` and `policy` modules backed by generic Pydantic models. |

```
               (𝗞𝗲𝘆𝗰𝗹𝗼𝗮𝗸 𝗔𝗱𝗺𝗶𝗻 𝗥𝗘𝗦𝗧 𝗔𝗣𝗜)
                             ▲
               ┌─────────────┴────────────┐
               │                          │
(𝘨𝘦𝘵 𝘳𝘰𝘭𝘦𝘴, 𝘴𝘤𝘰𝘱𝘦𝘴, 𝘢𝘨𝘦𝘯𝘵𝘴, 𝘵𝘰𝘰𝘭𝘴) (𝘴𝘦𝘵 𝘳𝘰𝘭𝘦-𝘴𝘤𝘰𝘱𝘦 𝘮𝘢𝘱𝘱𝘪𝘯𝘨𝘴)
┌──────────────┼──────────────────────────┼────────────────┐
│  PDP Interface Pod                      │                │
│              │                          │                │
│  ┌───────────┴────────────┐  ┌──────────┴─────────────┐  │
│  │  PDP Configuration     │  │  PDP Policy Service    │  │
│  │  Service               │  │  (Phase 1: Keycloak)   │  │
│  └────────────────────────┘  └────────────────────────┘  │
│              ▲                          ▲                │
└──────────────┼──────────────────────────┼────────────────┘
   (𝘨𝘦𝘵 𝘳𝘰𝘭𝘦𝘴, 𝘴𝘤𝘰𝘱𝘦𝘴, 𝘴𝘦𝘳𝘷𝘪𝘤𝘦𝘴)       (𝘴𝘦𝘵 𝘢𝘤𝘤𝘦𝘴𝘴 𝘳𝘶𝘭𝘦𝘴)
┌──────────────┼──────────────────────────┼────────────────┐  ┌──────────────────────────────────┐
│  Agent Pod   │    ┌─────────────────────┘                │  │  Event Broker Pod                │
│              │    │                                      │  │                                  │
│      ┌────────────────┐                                  │  │  ┌──────────────────────────┐    │
│      │   AIAC Agent   │◄─────────────────────────────────┼──┼──│      NATS JetStream      │    │
│      └────────────────┘         (𝘯𝘰𝘵𝘪𝘧𝘺)                  │  │  └──────────────────────────┘    │
│              │                                           │  │         ▲              ▲         │
│              │                                           │  │         │              │         │
└──────────────┼───────────────────────────────────────────┘  └─────────┼──────────────┼─────────┘
               │                                                    (𝘱𝘶𝘣𝘭𝘪𝘴𝘩)        (𝘱𝘶𝘣𝘭𝘪𝘴𝘩)
┌──────────────┼───────────────────────────────────────────┐            │              │
│  Policy and  │ Domain Knowledge RAG Pod                  │      (𝗞𝗲𝘆𝗰𝗹𝗼𝗮𝗸 𝗦𝗣𝗜)  (𝗥𝗔𝗚 𝗜𝗻𝗴𝗲𝘀𝘁)
│              ▼                                           │
│  ┌──────────────────────────┐  ┌──────────────────────┐  │
│  │  ChromaDB (vector store) │  │  RAG Ingest Service  │  │
│  └──────────────────────────┘  └──────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

All inter-pod traffic is Kubernetes ClusterIP. External access is exclusively via
`kubectl port-forward` (operator/developer) or NATS publish (Keycloak SPI, RAG Ingest).

### Deployment topology

```
┌──────────────────────────────────────────────────────────┐
│  PDP Interface Pod                                       │
│                                                          │
│  ┌────────────────────────┐  ┌────────────────────────┐  │
│  │  PDP Configuration     │  │  PDP Policy Service    │  │
│  │  Service (FastAPI)     │  │  (FastAPI)             │  │
│  │  :7071  ClusterIP      │  │  :7072  ClusterIP      │  │
│  │  aiac-pdp-config-svc   │  │  aiac-pdp-policy-svc   │  │
│  └────────────────────────┘  └────────────────────────┘  │
│              ▲                          ▲                │
└──────────────┼──────────────────────────┼────────────────┘
               │                          │
┌──────────────┼──────────────────────────┼────────────────┐
│  Event Broker Pod                       │                │
│              │                          │                │
│  ┌────────────────────────┐             │                │
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
│  │  AIAC Agent (FastAPI)  :7070  ClusterIP            │  │
│  │  LangGraph-based                                   │  │
│  │  + NATS consumer (asyncio background task)         │  │
│  │    consumer: aiac-agent-consumer queue group       │  │
│  └────────────────────────────────────────────────────┘  │
│              │                                           │
└──────────────┼───────────────────────────────────────────┘
               │
┌──────────────┼───────────────────────────────────────────┐
│  RAG Pod (StatefulSet)                                   │
│              ▼                                           │
│  ┌──────────────────────────┐  ┌──────────────────────┐  │
│  │  ChromaDB  :8000         │  │  RAG Ingest Service  │  │
│  │  collections:            │  │  (FastAPI) :7073     │  │
│  │    aiac-policies         │  │                      │  │
│  │    aiac-domain-knowledge │  │                      │  │
│  │  PVC: 1Gi /chroma/chroma │  │                      │  │
│  └──────────────────────────┘  └──────────────────────┘  │
│  ClusterIP: aiac-rag-service (8000 + 7073)               │
└──────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────┐
│  Python library  (aiac/src/)                             │
│                                                          │
│  aiac.pdp.library.models   — Pydantic only               │
│  aiac.pdp.library.configuration — HTTP client            │
│                    (read/write) PDP Configuration Svc    │
│  aiac.pdp.library.policy — HTTP client →                 │
│                    (read/write) PDP Policy Service       │
└──────────────────────────────────────────────────────────┘
```

### Call Flows

#### UC-1a · Service On-boarding (`aiac.apply.service.{id}`)

```
 Keycloak SPI
      │  CLIENT_CREATED
      │ 1. publish aiac.apply.service.{id}
      ▼
 NATS JetStream
      │  (durable consumer, at-least-once delivery)
      │ 2. deliver event
      ▼
 AIAC Agent
      │ 3. GET /services, /roles, /assignments        ──► PDP Configuration Service ──► Keycloak Admin REST
      │ 4. GET /scopes, /permissions (target service) ──► PDP Configuration Service ──► Keycloak Admin REST
      │ 5. semantic query (policy + domain knowledge) ──► ChromaDB
      │ 6. [LLM] compute minimal permission diff for affected service
      │ 7. [LLM] validate diff against retrieved policy (second pass)
      │ 8. POST /permissions        (if new scope/permission required) ──► PDP Policy Service ──► Keycloak Admin REST
      │ 9. POST /composite-roles    (add mappings from diff)           ──► PDP Policy Service ──► Keycloak Admin REST
      │ 10. DELETE /composite-roles (remove mappings from diff)        ──► PDP Policy Service ──► Keycloak Admin REST
      │ 11. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-1b · Realm Role On-boarding (`aiac.apply.realm-role.{id}`)

```
 Keycloak SPI
      │  REALM_ROLE_CREATED / REALM_ROLE_UPDATED
      │ 1. publish aiac.apply.realm-role.{id}
      ▼
 NATS JetStream
      │ 2. deliver event
      ▼
 AIAC Agent
      │ 3. GET /roles, /services, /assignments        ──► PDP Configuration Service ──► Keycloak Admin REST
      │ 4. semantic query (policy + domain knowledge) ──► ChromaDB
      │ 5. [LLM] compute minimal permission diff scoped to affected realm role
      │ 6. [LLM] validate diff against retrieved policy (second pass)
      │ 7. POST /composite-roles    (add mappings from diff)    ──► PDP Policy Service ──► Keycloak Admin REST
      │ 8. DELETE /composite-roles  (remove mappings from diff) ──► PDP Policy Service ──► Keycloak Admin REST
      │ 9. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-2a · Incremental Policy Update (`aiac.apply.build`)

```
 Operator
      │ 1. POST /ingest/policy/{text|file|url}
      ▼
 RAG Ingest Service
      │ 2. upsert documents ──► ChromaDB
      │ 3. publish aiac.apply.build
      ▼
 NATS JetStream
      │ 4. deliver event
      ▼
 AIAC Agent
      │ 5. GET /roles, /services, /assignments ──► PDP Configuration Service ──► Keycloak Admin REST
      │ 6. retrieve full policy context        ──► ChromaDB
      │ 7. [LLM] compute full composite role diff against current PDP state
      │ 8. POST /composite-roles    (add delta mappings)    ──► PDP Policy Service ──► Keycloak Admin REST
      │ 9. DELETE /composite-roles  (remove delta mappings) ──► PDP Policy Service ──► Keycloak Admin REST
      │ 10. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-2b · Full Rebuild (`POST /apply/rebuild`, operator-only)

```
 Operator
      │ 1. POST /apply/rebuild  (kubectl port-forward → Agent pod)
      ▼
 AIAC Agent
      │ 2. DELETE /composite-roles/all  (clear entire mapping table) ──► PDP Policy Service ──► Keycloak Admin REST
      │ 3. GET /roles, /services        (read fresh entity state)    ──► PDP Configuration Service ──► Keycloak Admin REST
      │ 4. retrieve full policy context                              ──► ChromaDB
      │ 5. [LLM] compute complete composite role set from scratch
      │ 6. POST /composite-roles  (write full mapping set)           ──► PDP Policy Service ──► Keycloak Admin REST
      ▼
 (synchronous HTTP response to operator)
```

### Component dependencies

| Component | Called by | Calls | Returns |
|-----------|-----------|-------|---------|
| PDP Configuration Service (in PDP Interface Pod) | `aiac.pdp.library.configuration` | Keycloak Admin REST API | Raw Keycloak JSON (generic endpoint names) |
| PDP Policy Service (in PDP Interface Pod) | `aiac.pdp.library.policy` | Keycloak Admin REST API | 204/201 on success |
| `aiac.pdp.library.models` | `aiac.pdp.library.configuration`, `aiac.pdp.library.policy`, AIAC Agent | — | Pydantic model definitions |
| `aiac.pdp.library.configuration` | AIAC Agent, Python scripts | PDP Configuration Service (HTTP) | Typed Pydantic instances (reads and writes configuration entities) |
| `aiac.pdp.library.policy` | AIAC Agent, Python scripts | PDP Policy Service (HTTP) | Typed Pydantic instances or None (writes policy rules/mappings) |
| ChromaDB | RAG Ingest Service (writes), AIAC Agent (reads) | — | Policy and domain knowledge vectors |
| RAG Ingest Service | Developer (via `kubectl port-forward`) | ChromaDB, Embedding API, Event Broker | — |
| Event Broker (NATS JetStream) | Keycloak SPI listener, RAG Ingest Service (publishers); NATS JetStream (DLQ routing) | — | Durable event delivery to AIAC Agent; DLQ on max retries |
| AIAC Agent | Event Broker (NATS consumer), operator (`/apply/rebuild` HTTP direct) | Policy Update / Realm Roles / Service Onboarding orchestrators → `aiac.pdp.library.*`, ChromaDB, LLM API, Kubernetes API | Applied composite diff; provisioned service permissions/scopes (onboarding) |

### Key architectural decisions

- **PDP services are co-located in a single PDP Interface Pod.** PDP Configuration Service and PDP Policy Service run as two containers in one pod, sharing the same Keycloak credentials. Two separate ClusterIP Services (`aiac-pdp-config-service:7071`, `aiac-pdp-policy-service:7072`) select the same pod. This eliminates the separate PDP Configuration and PDP Policy pods without changing the library's service URL interface.
- **PDP Interface Pod phase transition is a container image swap.** Phase 2 replaces the PDP Policy container image (`aiac-pdp-policy-keycloak` → `aiac-pdp-policy-opa`) within the same pod. The `aiac-pdp-policy-service` ClusterIP name and port `:7072` remain unchanged. No new pod or manifest is required — `pdp-policy-opa-deployment.yaml` does not exist.
- **PDP services bind to `0.0.0.0`.** Exposed as Kubernetes ClusterIP Services so that the Agent Pod can reach them over the cluster network.
- **Phase 1 RBAC via composite roles.** AIAC manages realm role → service permission mappings at the role level, not per-user. Users inherit permissions automatically when assigned a realm role.
- **RAG Pod is a StatefulSet with persistent ChromaDB storage.** ChromaDB data is stored on a 1 Gi `ReadWriteOnce` PersistentVolumeClaim mounted at `/chroma/chroma` (ChromaDB default). On pod recreation, the StatefulSet rebinds the same PVC and ChromaDB resumes from persisted state without re-ingestion. The pod runs a single replica.
- **RAG Pod runs ChromaDB and RAG Ingest Service together.** Exposed as `aiac-rag-service` on ports 8000 (ChromaDB default) and 7073 (RAG Ingest Service).
- **AIAC Agent is stateless.** Changes are applied immediately on trigger — no pending session or human confirmation step.
- **Event Broker decouples all automated triggers from the Agent.** The Keycloak SPI listener and RAG Ingest Service publish to NATS subjects; the Agent subscribes as a durable competing consumer. This removes all direct dependencies between trigger sources and the Agent.
- **`rebuild` bypasses the Event Broker.** It is an operator-only command issued directly via HTTP (`kubectl port-forward`). It is never published to NATS and has no NATS listener.
- **NATS consumer is a thin adapter.** It receives events from the Event Broker and calls the same internal `/apply/*` handler functions used by the debug HTTP endpoints. No business logic lives in the consumer.
- **Agent `/apply/*` HTTP endpoints are retained for debugging.** They are not the primary trigger path; the NATS consumer is. `kubectl port-forward` to the Agent is used only for `rebuild` and debugging.
- **Event Broker uses WorkQueuePolicy.** Messages are removed from the stream after acknowledgement. Unacknowledged messages survive Agent pod restarts and are redelivered automatically. After 5 failed deliveries, messages are routed to `aiac.apply.dlq`.
- **AIAC init container gates Agent startup.** Before the Agent container starts, the init container waits for NATS, PDP Configuration Service, PDP Policy Service, and RAG Ingest Service to be healthy, then creates the `aiac-events` JetStream stream idempotently.
- **`aiac.pdp.library.models` is dependency-free** (only `pydantic`). Agents can import it without pulling in `requests` or `python-dotenv`.
- **`aiac.__init__`, `aiac.pdp.__init__`, `aiac.pdp.library.__init__`, `aiac.pdp.service.__init__`, `aiac.pdp.service.configuration.__init__`, `aiac.pdp.service.configuration.keycloak.__init__`, `aiac.pdp.service.policy.__init__`, and `aiac.pdp.service.policy.keycloak.__init__` are empty.** Callers use explicit submodule paths: `from aiac.pdp.library.models import Subject`, `from aiac.pdp.library.configuration import get_subjects`.
- **ChromaDB hosts two collections: `aiac-policies` and `aiac-domain-knowledge`.** Collection slug to ChromaDB name mapping: `policy` → `aiac-policies`, `domain-knowledge` → `aiac-domain-knowledge`.
- **`user/{id}` trigger removed.** Composite role mappings are realm-role-scoped; individual user creation/update does not require agent intervention — composites apply automatically.

---

## 6. Kagenti / Keycloak / OPA Interfaces

**AIAC ↔ Kagenti platform**
The AIAC Agent reads `AgentRuntime` and `AgentCard` custom resources from the Kubernetes API to
extract service metadata during UC-1 service onboarding. The `aiac.pdp.library` Python package
is the integration surface for other Kagenti components needing typed access to the PDP.

**AIAC ↔ Keycloak (Phase 1)**
The PDP Configuration Service proxies Keycloak Admin REST read endpoints under generic PDP entity
names (subjects, roles, services, scopes, permissions, assignments). The PDP Policy Service writes
composite role mappings (realm role → service permissions) to Keycloak. The Keycloak SPI listener
publishes entity lifecycle events to NATS; it is a separate component outside the AIAC codebase.

**AIAC ↔ OPA (Phase 2, planned)**
The PDP Policy Service container image is swapped from the Keycloak implementation to an OPA
implementation. The Kubernetes ClusterIP service name and port are unchanged — no other component
is modified. The OPA implementation writes LLM-generated Rego rules in place of composite role
mappings. AuthBridge requires no changes.

**AIAC ↔ Event Broker (NATS JetStream)**
The Agent subscribes to the event stream as a durable consumer with at-least-once delivery.
Unacknowledged messages survive pod restarts; failed messages are routed to a dead-letter subject.
See Section 7.4 (Event Broker) and Section 8 (Deployment) for subject names and handler mapping.

---

## 7. AIAC System Components

### 7.1 PDP Configuration Service

FastAPI service (`0.0.0.0:7071`) co-located with the PDP Policy Service in the **PDP Interface Pod**. Manages PDP configuration entities (subjects, roles, services, scopes) via Keycloak Admin REST API. Exposes read and write endpoints for configuration entities. Stateless, no caching. Supports per-request realm override via optional `?realm=` query parameter. Backed by Keycloak in both Phase 1 and Phase 2.

**Full spec:** [components/pdp-configuration-service.md](components/pdp-configuration-service.md)

---

### 7.2 PDP Policy Service

FastAPI service (`0.0.0.0:7072`) co-located with the PDP Configuration Service in the **PDP Interface Pod**. Applies policy changes to the active PDP backend. Two container images share the same Kubernetes ClusterIP name (`aiac-pdp-policy-service:7072`):

- **Phase 1 — Keycloak** (`aiac-pdp-policy-keycloak`): manages composite role mappings (realm role → service permissions) via Keycloak Admin API. 5 write endpoints.
- **Phase 2 — OPA** (`aiac-pdp-policy-opa`): writes LLM-generated Rego rules to OPA. Interface TBD (separate PRD). Phase transition = container image swap within the PDP Interface Pod; no manifest change required.

**Phase 1 full spec:** [components/pdp-policy-keycloak-service.md](components/pdp-policy-keycloak-service.md)

---

### 7.3 Library

Python package at `aiac/src/`. Three submodules:

- **`aiac.pdp.library.models`** — dependency-free Pydantic models for all PDP entities (`Subject`, `Role`, `Service`, `Permission`, `Scope`, `Assignments`).
- **`aiac.pdp.library.configuration`** — HTTP client wrapping the PDP Configuration Service; read and write access to configuration entities (subjects, roles, services, scopes); returns typed Pydantic instances; all methods require a `realm: str` parameter.
- **`aiac.pdp.library.policy`** — HTTP client wrapping the PDP Policy Service; abstracts Phase 1 (Keycloak composite mappings) and Phase 2 (OPA Rego) behind a stable function interface.

**Full spec:** [components/library.md](components/library.md)

---

### 7.4 Event Broker

NATS JetStream pod (`aiac-event-broker-service:4222`). Decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides at-least-once delivery, replay on pod restart via `WorkQueuePolicy`, and a dead-letter subject (`aiac.apply.dlq`) after 5 failed deliveries. No authentication — ClusterIP network isolation is the access control mechanism. Stream: `aiac-events`, subjects `aiac.apply.>`, consumer group `aiac-agent-consumer`.

**Full spec:** [components/event-broker.md](components/event-broker.md)

---

### 7.5 AIAC Agent

FastAPI + LangGraph service (`0.0.0.0:7070`). Receives automated triggers via the **Event Broker** (NATS JetStream durable consumer, `aiac-agent-consumer` queue group) and the operator-only `rebuild` command directly via HTTP. Structured as a thin **Controller** (`controller/routes.py`) that dispatches `/apply/*` handlers to three **Orchestrators**, each owning one or more compiled `StateGraph` sub-agents. A **NATS consumer** (asyncio background task in the FastAPI `lifespan` handler) is a thin adapter that receives NATS events and calls the same internal handler functions used by the HTTP endpoints:

| Orchestrator | Trigger(s) | Sub-agents |
|---|---|---|
| Service Onboarding | `aiac.apply.service.{id}` | Service Provision → Service Policy (sequential) |
| Policy Update | `aiac.apply.build`, `/apply/rebuild` (HTTP) | Build sub-agent or Rebuild sub-agent (alternative) |
| Realm Roles | `aiac.apply.realm-role.{id}` | Realm Role sub-agent |

All sub-agent `StateGraph` instances are logically separated modules running within a single pod and process. The **Policy Update** sub-agents compute a minimal delta between the current ChromaDB policy and live composite role state. The **Rebuild** variant additionally clears all composite mappings before computing the diff. The **Realm Roles** sub-agent applies scoped composite mappings for a single affected realm role. The **Service Onboarding** orchestrator first provisions service permissions/scopes (via the Kubernetes in-cluster API to read `AgentRuntime`/`AgentCard` CRs), then maps realm roles to the new service's permissions via composite role additions. Stateless; changes are applied immediately. Integrated retry with differentiated error codes per upstream.

**Full spec:** [components/aiac-agent.md](components/aiac-agent.md)

---

### 7.6 RAG Knowledge Base

ChromaDB vector store (`aiac-rag-service:8000`) hosting two collections: `aiac-policies` (access control policy rules) and `aiac-domain-knowledge` (org/business context such as team rosters, application ownership, and department mappings). Both collections are managed by the RAG Ingest Service and read by the AIAC Agent. Co-located with the RAG Ingest Service in the RAG Pod. ChromaDB data is persisted on a 1 Gi PVC mounted at `/chroma/chroma`; the RAG Pod is a StatefulSet.

**Full spec:** [components/rag-knowledge-base.md](components/rag-knowledge-base.md)

---

### 7.7 RAG Ingest Service

FastAPI service (`0.0.0.0:7073`) co-located with ChromaDB. Thirteen collection-parameterized endpoints across three semantics: complete collection replacement (`POST /ingest/{collection}/{text|file|url}`), document-level upsert (`POST /ingest/{collection}/update/{text|file|url}`), and explicit removal (`DELETE /ingest/{collection}/{doc_id}`). The `{collection}` slug is validated against `AIAC_RAG_COLLECTIONS` (default: `policy,domain-knowledge`). After every successful ingest the service publishes to `aiac.apply.build` on the Event Broker (`NATS_URL`). Developer access via `kubectl port-forward`.

**Full spec:** [components/rag-ingest-service.md](components/rag-ingest-service.md)

---

### 7.8 Keycloak SPI Listener

A custom Keycloak Event Listener SPI (Java) that listens to Keycloak's internal event bus and translates entity-scoped events into NATS publish calls to the Event Broker. The AIAC Agent subject schema is authoritative; the SPI PRD references it.

| Keycloak Event | Event Broker subject |
|---|---|
| `REGISTER`, `UPDATE_PROFILE` (user events) | — (dropped; composite roles handle user permission inheritance automatically) |
| `CLIENT_CREATED` | `aiac.apply.service.{id}` |
| Realm role created/updated | `aiac.apply.realm-role.{id}` |

**Full spec:** TBD (separate PRD).

---

## 8. Deployment

### Kubernetes manifests

Four separate manifest files:

| File | Contents |
|------|----------|
| `aiac/k8s/pdp-interface-deployment.yaml` | `aiac-pdp-config` ConfigMap + PDP Interface Pod Deployment (PDP Configuration Service container + PDP Policy Service container) + two ClusterIP Services |
| `aiac/k8s/event-broker-deployment.yaml` | Event Broker Pod Deployment (NATS JetStream) + ClusterIP Service |
| `aiac/k8s/rag-statefulset.yaml` | RAG StatefulSet (ChromaDB + RAG Ingest Service containers) + 1 Gi PVC template + ClusterIP Service |
| `aiac/k8s/agent-deployment.yaml` | Agent Pod Deployment (aiac-init container + AIAC Agent container) + ClusterIP Service |

Both containers in the PDP Interface Pod mount `aiac-pdp-config` (KEYCLOAK_URL, KEYCLOAK_REALM) and `keycloak-admin-secret` (KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD) as env vars.

### Docker images

Built independently. No entry in the repo's `build.yaml` CI matrix.

```bash
# Build PDP Configuration Service (deployed as a container in the PDP Interface Pod)
docker build -f aiac/src/aiac/pdp/service/configuration/keycloak/Dockerfile -t aiac-pdp-config:latest aiac/src/

# Build PDP Policy Service — Keycloak implementation (Phase 1 container in the PDP Interface Pod)
docker build -f aiac/src/aiac/pdp/service/policy/keycloak/Dockerfile -t aiac-pdp-policy-keycloak:latest aiac/src/

# Build Agent (includes aiac-init container)
docker build -f aiac/src/aiac/agent/controller/Dockerfile -t aiac-agent:latest aiac/src/

# Build RAG Ingest Service
docker build -t aiac-rag-ingest:latest aiac/rag-ingest/
```

The Event Broker uses the official `nats` Docker image with JetStream enabled (`-js` flag). No custom build required.

Phase 2 note: replacing the PDP Policy Service with an OPA implementation requires only building a new `aiac-pdp-policy-opa:latest` image and updating the Policy container image reference in `pdp-interface-deployment.yaml`. No other manifest changes are required.

### `aiac-pdp-config` ConfigMap template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aiac-pdp-config
data:
  KEYCLOAK_URL: "http://keycloak-service.keycloak.svc:8080"
  KEYCLOAK_REALM: "kagenti"
  AIAC_PDP_CONFIG_URL: "http://aiac-pdp-config-service:7071"
  AIAC_PDP_POLICY_URL: "http://aiac-pdp-policy-service:7072"
  NATS_URL: "nats://aiac-event-broker-service:4222"
  AIAC_RAG_INGEST_URL: "http://aiac-rag-service:7073"
  AIAC_CHROMADB_URL: "http://aiac-rag-service:8000"
```

Update `KEYCLOAK_URL` and `KEYCLOAK_REALM` for the target environment before applying.

---

## 9. Testing

Tests live in `aiac/test/`.

### Unit tests

| Target | What to mock | What to assert |
|--------|-------------|----------------|
| PDP Configuration Service endpoints | `KeycloakAdmin` methods (return fixture dicts) | Correct JSON response, 502 on Keycloak error |
| PDP Policy Service (Keycloak) endpoints | `KeycloakAdmin` methods | 204 on write success, 201 on create, 502 on Keycloak error |
| `aiac.pdp.library.models` | No mock needed | `extra='ignore'` drops unknown fields, required fields validated, `model_validate` round-trips correctly |
| `aiac.pdp.library.configuration` functions | PDP Configuration Service HTTP endpoints | Returns correct Pydantic model instances; `RuntimeError` on non-2xx; default URL fallback |
| `aiac.pdp.library.policy` functions | PDP Policy Service HTTP endpoints | Correct serialisation; `RuntimeError` on non-2xx; default URL fallback |
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

## 10. Conventions and constraints

- Python version: 3.12
- Base Docker image: `python:3.12-slim`
- Linting: ruff (line length 120, target py312 per root `pyproject.toml`)
- Commits: DCO sign-off required (`git commit -s`); use `Assisted-By` not `Co-Authored-By`
- No auth on PDP Configuration Service, PDP Policy Service, RAG Ingest Service, or Event Broker — network isolation (ClusterIP + `kubectl port-forward`) is the access control mechanism
- PDP Configuration Service, PDP Policy Service, Agent, RAG Ingest Service, and Event Broker are not registered with the repo's `build.yaml` CI matrix; they have independent build processes
- `aiac/__init__.py` exists and is empty — `aiac` is a regular package, not a namespace package
- NATS consumer must **await** handler completion before issuing ack — fire-and-forget (`asyncio.create_task`) is prohibited; premature ack breaks at-least-once delivery guarantees
