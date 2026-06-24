# PRD: AI-based Access Control (AIAC)

## Abstract

AI-based Access Control (AIAC) is a Kagenti platform extension that automates RBAC/ABAC policy
enforcement for AI agents running on Kubernetes. A LangGraph-based AI agent continuously translates
a natural-language access control policy — stored in a vector knowledge base — into concrete
permission configurations in the active Policy Decision Point (PDP), eliminating manual policy
administration and preventing policy drift as services and roles evolve. The PDP backend is OPA,
which evaluates LLM-generated Rego rules; Keycloak remains the identity provider for entity
management (subjects, roles, services).

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
3. **Manual administration overhead** — keeping OPA policy rules consistent with a growing fleet
   of agents and tools requires ongoing human attention with no audit trail.

---

## 2. Problem Solution

AIAC introduces a strict three-layer model that cleanly separates policy concerns: a **Policy
Management** layer (AIAC Agent) that translates natural-language policy into PDP configuration, a
**Policy Decision** layer (OPA) that evaluates caller entitlements, and a **Policy Enforcement**
layer (AuthBridge) that intercepts traffic and exchanges tokens but carries no policy knowledge of
its own.

The AIAC Agent subscribes to an event stream (NATS JetStream) and reacts to entity lifecycle
events — new services, role changes, policy updates — by retrieving the current policy from a RAG
knowledge base, querying live PDP state, and applying the minimal required diff via a dedicated
PDP Policy Writer. **Policy intent lives entirely in the PDP, not in per-pod configuration.**

---

## 3. Design Principles

### PDP/PEP separation

AIAC enforces a strict three-layer model:

| Layer | Component | Role |
|---|---|---|
| **Policy Management** | AIAC Agent | Translates natural-language policy into PDP configuration on every trigger |
| **Policy Decision (PDP)** | OPA | Evaluates LLM-generated Rego rules; decides what a caller may access |
| **Policy Enforcement (PEP)** | AuthBridge | Intercepts traffic; exchanges tokens; carries no policy knowledge |

The PEP (AuthBridge) is a pure enforcement layer. It performs RFC 8693 token exchanges sending only the target `audience` — no `scope` parameter. OPA evaluates the caller's role against the Rego rules and issues a token containing exactly the entitlements that role grants on the target service.

This means `token_scopes` is absent from `authproxy-routes`. Route configuration carries routing intent only (`host` → `target_audience`). Policy intent lives entirely in OPA, kept current by AIAC.

---

## 4. Major Use-Cases

### UC-1 · Continuous Access Reconciliation (On-boarding / Off-boarding)

**Trigger:** A Role or Keycloak Client is created, updated, or removed.

The Keycloak SPI listener publishes a scoped event to the Event Broker. The AIAC Agent retrieves
relevant context from the RAG store, reads the current OPA policy state, and asks the LLM to
compute the minimal permission diff scoped to the affected entity. The diff is validated by a
second LLM pass and applied to OPA as updated Rego rules. Supports both **auto-apply** (fully
automated, least-privilege) and **recommendation + human review** modes.

### UC-2 · Policy Update Reconciliation

**Trigger:** An operator ingests updated documents into the RAG store.

After ingestion the RAG Ingest Service publishes a build event. The AIAC Agent retrieves all
relevant context, computes a full policy diff against current OPA state, and applies the delta.
A `rebuild` variant (operator-only, direct HTTP) first clears all OPA policy rules before
recomputing from scratch — used when policy changes are too broad for incremental diff.

### UC-3 · Entitlements Review

**Trigger:** Operator request (on-demand or scheduled).

The agent evaluates all current OPA policy rules — including manually added ones that AIAC did not
create — against the natural-language policy. It reports compliant, non-compliant, and
policy-agnostic entitlements, enabling audit and remediation workflows.

### UC-4 · Access Request

**Trigger:** User request via chatbot.

A user requests an entitlement grant. The agent verifies the request against the policy
(permissive approach) and either auto-grants or routes to a human approver (man-in-the-loop).
Manually granted entitlements are flagged as policy-agnostic and surfaced during UC-3 reviews.

---

## 5. Architecture Overview

Seven components across five Kubernetes Pods plus a Python library layer, all implemented in Python 3.12. External dependencies: Keycloak Admin API, an LLM API, and an embedding API. The Keycloak SPI listener is defined in a separate PRD.

### Component Summary

| # | Component | Description |
|---|-----------|-------------|
| 1 | **IdP Configuration Service** | REST service that exposes IdP entity data (subjects, roles, services, scopes) for read and write operations. Read methods enrich services with assigned roles/scopes and enrich roles with child roles and mapped scopes. Backed by Keycloak. Python library: `aiac.idp.library.configuration`. |
| 2 | **PDP Policy Writer** | REST service that applies LLM-generated Rego rules to the OPA backend. Writes derived Rego packages to an `AuthorizationPolicy` Kubernetes CR. Exposed as ClusterIP service `aiac-pdp-policy-service:7072`. Python library: `aiac.pdp.library.policy`. |
| 3 | **Policy Management Service** | REST service that owns an in-memory `PolicyModel` cache backed by SQLite as the authoritative structured policy store. Enables the Policy Builder sub-agent to read and diff `AgentPolicyModel` state without re-deriving it from the PDP snapshot. Deployed as a dedicated single-replica StatefulSet (`aiac-pdp-state`) at `:7074`. Python library: `aiac.pdp.library.state`. |
| 4 | **Policy and Domain Knowledge RAG** | ChromaDB vector store holding the access control policy and domain knowledge in persistent, queryable form, populated via a co-located RAG Ingest Service. |
| 5 | **Event Broker** | NATS JetStream pod that decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides durable, at-least-once delivery with automatic replay on Agent pod restart. Competing consumer model ensures each event is processed exactly once. |
| 6 | **AIAC Agent** | LangGraph-based AI agent triggered by Event Broker subscriptions (`aiac.apply.>` subjects) and directly by the operator (`rebuild` only). Retrieves the current policy from the RAG store, interprets it against live PDP state, and applies the required policy changes immediately. |
| 7 | **Python library** | Python API library provides typed access to the three policy services via `configuration`, `policy`, and `state` modules backed by generic Pydantic models. |

```
        (𝗞𝗲𝘆𝗰𝗹𝗼𝗮𝗸 𝗔𝗣𝗜)       (𝗞𝘂𝗯𝗲𝗿𝗻𝗲𝘁𝗲𝘀 𝗖𝗥 𝗔𝗣𝗜)
               ▲                      ▲
               │                      |
    (𝘶𝘴𝘦𝘳𝘴, 𝘳𝘰𝘭𝘦𝘴, 𝘤𝘭𝘪𝘦𝘯𝘵𝘴)    (𝘈𝘶𝘵𝘩𝘰𝘳𝘪𝘻𝘢𝘵𝘪𝘰𝘯𝘗𝘰𝘭𝘪𝘤𝘺 𝘊𝘙)
┌──────────────┼──────────────────────┼───────────────────┐
│  Kagenti Interface Pod              │                   │
│              │                      │                   │
│      ┌───────┴──────┐      ┌────────┴───────┐           │
│      │  IdP Config  │      │  PDP Policy    │           │
│      │  Service     │      │  Writer (OPA)  │           │
│      └──────────────┘      └────────────────┘           │
│              ▲                      ▲                   │
└──────────────┼──────────────────────┼───────────────────┘
               │                      │
               │                      │
               │                      │
               │   ┌──────────────────────────────────────┐
               │   │  Policy Management Pod               │
               │   │                                      │
               │   │  ┌───────────────────────────────┐   │
               │   │  │  Policy Management Service    │   │
               │   │  │                               │   │
               │   │  │     (SQLite policy.db)        │   │
               │   │  └───────────────────────────────┘   │
               │   │                  ▲                   │
               │   └──────────────────┼───────────────────┘
               │                      │
┌──────────────┼──────────────────────┼───────────────────┐  ┌────────────────────────────────┐
│  Agent Pod   │  ┌───────────────────┘                   │  │  Event Broker Pod              │
│              │  │                                       │  │                                │
│      ┌────────────────┐                                 │  │  ┌──────────────────────────┐  │
│      │   AIAC Agent   │◄────────────────────────────────┼──┼──│      NATS JetStream      │  │
│      └────────────────┘         (𝘯𝘰𝘵𝘪𝘧𝘺)                 │  │  └──────────────────────────┘  │
│              │                                          │  │         ▲              ▲       │
│              │                                          │  │         │              │       │
└──────────────┼──────────────────────────────────────────┘  └─────────┼──────────────┼───────┘
               │                                                    (𝘱𝘶𝘣𝘭𝘪𝘴𝘩)        (𝘱𝘶𝘣𝘭𝘪𝘴𝘩)
┌──────────────┼───────────────────────────────────────────┐           │              │
│  Policy and  │ Domain Knowledge RAG Pod                  │      (𝗞𝗲𝘆𝗰𝗹𝗼𝗮𝗸 𝗦𝗣𝗜)  (𝗥𝗔𝗚 𝗜𝗻𝗴𝗲𝘀𝘁)
│              ▼                                           │
│  ┌──────────────────────────┐  ┌──────────────────────┐  │
│  │  ChromaDB (vector store) │  │  RAG Ingest Service  │  │
│  └──────────────────────────┘  └──────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

All inter-pod traffic is Kubernetes ClusterIP. External access is exclusively via
`kubectl port-forward` (operator/developer) or NATS publish (Keycloak SPI, RAG Ingest).

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
      │ 3. GET /services, /roles, /assignments             ──► IdP Configuration Service ──► Keycloak Admin REST
      │ 4. GET /services/{id}/roles, /services/{id}/scopes ──► IdP Configuration Service ──► Keycloak Admin REST
      │ 5. semantic query (policy + domain knowledge)      ──► ChromaDB
      │ 6. [LLM] compute AgentPolicyModel for new service (inbound + outbound rules)
      │ 7. [LLM] validate policy model against retrieved policy (second pass)
      │ 8. POST /policy/agents/{service_id}  (write agent policy) ──► PDP Policy Writer ──► AuthorizationPolicy CR
      │ 9. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-1b · Role On-boarding (`aiac.apply.role.{id}`)

```
 Keycloak SPI
      │  REALM_ROLE_CREATED / REALM_ROLE_UPDATED
      │ 1. publish aiac.apply.role.{id}
      ▼
 NATS JetStream
      │ 2. deliver event
      ▼
 AIAC Agent
      │ 3. GET /roles, /services, /assignments        ──► IdP Configuration Service ──► Keycloak Admin REST
      │ 4. semantic query (policy + domain knowledge) ──► ChromaDB
      │ 5. [LLM] compute PolicyModel delta for all services affected by the role change
      │ 6. [LLM] validate policy model against retrieved policy (second pass)
      │ 7. POST /policy  (write updated PolicyModel) ──► PDP Policy Writer ──► AuthorizationPolicy CR
      │ 8. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-2a · Incremental Policy Update (`aiac.apply.policy.build`)

```
 Operator
      │ 1. POST /ingest/policy/{text|file|url}
      ▼
 RAG Ingest Service
      │ 2. upsert documents ──► ChromaDB
      │ 3. publish aiac.apply.policy.build
      ▼
 NATS JetStream
      │ 4. deliver event
      ▼
 AIAC Agent
      │ 5. GET /roles, /services, /assignments ──► IdP Configuration Service ──► Keycloak Admin REST
      │ 6. retrieve full policy context        ──► ChromaDB
      │ 7. [LLM] compute full PolicyModel delta against current OPA state
      │ 8. POST /policy  (write updated PolicyModel) ──► PDP Policy Writer ──► AuthorizationPolicy CR
      │ 9. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-2b · Full Rebuild (`POST /apply/policy/rebuild`, operator-only)

```
 Operator
      │ 1. POST /apply/policy/rebuild  (kubectl port-forward → Agent pod)
      ▼
 AIAC Agent
      │ 2. DELETE /policy               (clear all OPA policy rules) ──► PDP Policy Writer ──► AuthorizationPolicy CR
      │ 3. GET /roles, /services        (read fresh entity state)    ──► IdP Configuration Service ──► Keycloak Admin REST
      │ 4. retrieve full policy context                              ──► ChromaDB
      │ 5. [LLM] compute complete PolicyModel from scratch
      │ 6. POST /policy  (write full PolicyModel)                    ──► PDP Policy Writer ──► AuthorizationPolicy CR
      ▼
 (synchronous HTTP response to operator)
```

### Component dependencies

| Component | Called by | Calls | Returns |
|-----------|-----------|-------|---------|
| IdP Configuration Service (in Kagenti Interface Pod) | `aiac.idp.library.configuration.api` | Keycloak Admin REST API | Raw Keycloak JSON (generic endpoint names) |
| PDP Policy Writer — OPA (in Kagenti Interface Pod) | `aiac.pdp.library.policy` | Kubernetes CR (`AuthorizationPolicy`) | 204 on success |
| Policy Management Service (StatefulSet `aiac-pdp-state`) | `aiac.pdp.library.state` | SQLite (`agent_policies` table, in-memory cache) | `AgentPolicyModel` / `PolicyModel` on read; 204 on write |
| `aiac.idp.library.configuration.models` | `aiac.idp.library.configuration.api`, AIAC Agent | — | Pydantic model definitions for IdP entities (Subject, Role, Service, Scope) |
| `aiac.idp.library.configuration.api` | AIAC Agent, Python scripts | IdP Configuration Service (HTTP) | Typed Pydantic instances (reads and writes IdP configuration entities) |
| `aiac.pdp.library.models` | `aiac.pdp.library.policy`, `aiac.pdp.library.state`, AIAC Agent | — | Pydantic model definitions for OPA policy (PolicyRule, AgentPolicyModel, PolicyModel) |
| `aiac.pdp.library.policy` | AIAC Agent, Python scripts | PDP Policy Writer — OPA (HTTP) | None (writes Rego policy rules to AuthorizationPolicy CR) |
| `aiac.pdp.library.state` | AIAC Agent, Python scripts | Policy Management Service (HTTP) | `AgentPolicyModel` / `PolicyModel` on read; None on write/delete |
| ChromaDB | RAG Ingest Service (writes), AIAC Agent (reads) | — | Policy and domain knowledge vectors |
| RAG Ingest Service | Developer (via `kubectl port-forward`) | ChromaDB, Embedding API, Event Broker | — |
| Event Broker (NATS JetStream) | Keycloak SPI listener, RAG Ingest Service (publishers); NATS JetStream (DLQ routing) | — | Durable event delivery to AIAC Agent; DLQ on max retries |
| AIAC Agent | Event Broker (NATS consumer), operator (`/apply/policy/rebuild` HTTP direct) | Policy Update / Role Update / Service Onboarding orchestrators → `aiac.idp.library.configuration.api`, `aiac.pdp.library.policy`, `aiac.pdp.library.state`, ChromaDB, LLM API, Kubernetes API | Rego policy written to AuthorizationPolicy CR; structured policy written to Policy Management Service (SQLite); provisioned service permissions/scopes (onboarding) |

### Key architectural decisions

- **Stateless PDP services are co-located in the Kagenti Interface Pod; the stateful Policy Management Service is separate.** IdP Configuration Service and PDP Policy Writer run as two containers in the Interface Pod, sharing a Kubernetes ServiceAccount. The Policy Management Service is a dedicated single-replica StatefulSet (`aiac-pdp-state`) with its own PVC — decoupled from the Interface Pod's restart lifecycle. Three ClusterIP Services (`aiac-pdp-config-service:7071`, `aiac-pdp-policy-service:7072`, `aiac-pdp-state-service:7074`) provide stable addressing.
- **One CR + one SQLite store, distinct owners, distinct purposes.** The Policy Management Service owns a SQLite `agent_policies` table (backed by a 1 Gi RWO PVC) holding structured `AgentPolicyModel` data — the source of truth for policy state, served from an in-memory cache. The `AuthorizationPolicy` CR (one total, owned by the PDP Policy Writer) holds derived Rego packages for OPA runtime. The two services have no dependency on each other; both are driven by the AIAC Agent via their respective libraries.
- **Clean `idp` / `pdp` Python namespace split.** IdP-related code (Keycloak entity management) lives under `aiac.idp.*`; PDP policy code (OPA Rego) lives under `aiac.pdp.*`.
- **PDP services bind to `0.0.0.0`.** Exposed as Kubernetes ClusterIP Services so that the Agent Pod can reach them over the cluster network.
- **RBAC via OPA Rego rules.** AIAC manages role → service permission mappings by writing `AgentPolicyModel` instances to the `AuthorizationPolicy` CR. Each agent pod's OPA plugin fetches its packages from the CR at startup.
- **RAG Pod is a StatefulSet with persistent ChromaDB storage.** ChromaDB data is stored on a 1 Gi `ReadWriteOnce` PersistentVolumeClaim mounted at `/chroma/chroma` (ChromaDB default). On pod recreation, the StatefulSet rebinds the same PVC and ChromaDB resumes from persisted state without re-ingestion. The pod runs a single replica.
- **RAG Pod runs ChromaDB and RAG Ingest Service together.** Exposed as `aiac-rag-service` on ports 8000 (ChromaDB default) and 7073 (RAG Ingest Service).
- **AIAC Agent is stateless.** Changes are applied immediately on trigger — no pending session or human confirmation step.
- **Event Broker decouples all automated triggers from the Agent.** The Keycloak SPI listener and RAG Ingest Service publish to NATS subjects; the Agent subscribes as a durable competing consumer. This removes all direct dependencies between trigger sources and the Agent.
- **`rebuild` bypasses the Event Broker.** It is an operator-only command issued directly via HTTP (`kubectl port-forward`). It is never published to NATS and has no NATS listener.
- **NATS consumer is a thin adapter.** It receives events from the Event Broker and calls the same internal handler functions used by the debug HTTP endpoints. No business logic lives in the consumer.
- **Agent HTTP endpoints are retained for debugging.** They are not the primary trigger path; the NATS consumer is. `kubectl port-forward` to the Agent is used only for `rebuild` and debugging.
- **Event Broker uses WorkQueuePolicy.** Messages are removed from the stream after acknowledgement. Unacknowledged messages survive Agent pod restarts and are redelivered automatically. After 5 failed deliveries, messages are routed to `aiac.apply.dlq`.
- **AIAC init container gates Agent startup.** Before the Agent container starts, the init container waits for NATS, IdP Configuration Service, PDP Policy Writer, and RAG Ingest Service to be healthy, then creates the `aiac-events` JetStream stream idempotently.
- **`aiac.idp.library.configuration.models` and `aiac.pdp.library.models` are dependency-free** (only `pydantic`). Agents can import them without pulling in `requests` or `python-dotenv`.
- **All `__init__.py` files under `aiac.*` are empty.** Callers use explicit submodule paths: `from aiac.idp.library.configuration.models import Subject`, `from aiac.pdp.library.models import PolicyModel`.
- **ChromaDB hosts two collections: `aiac-policies` and `aiac-domain-knowledge`.** Collection slug to ChromaDB name mapping: `policy` → `aiac-policies`, `domain-knowledge` → `aiac-domain-knowledge`.
- **`user/{id}` trigger not implemented.** OPA rules are role-scoped; individual user creation/update does not require agent intervention — OPA rule evaluation resolves entitlements from the caller's role automatically.

---

## 6. Kagenti / Keycloak / OPA Interfaces

**AIAC ↔ Kagenti platform**
The AIAC Agent reads `AgentRuntime` and `AgentCard` custom resources from the Kubernetes API to
extract service metadata during UC-1 service onboarding. The `aiac.idp.library.configuration` and `aiac.pdp.library.policy` Python packages are the integration surface for other Kagenti components needing typed access to the IdP and PDP respectively.

**AIAC ↔ Keycloak**
The IdP Configuration Service proxies Keycloak Admin REST endpoints under generic entity names (subjects, roles, services, scopes, assignments). Read endpoints include per-service role and scope enrichment. The Keycloak SPI listener publishes entity lifecycle events to NATS; it is a separate component outside the AIAC codebase.

**AIAC ↔ OPA**
The PDP Policy Writer (`aiac-pdp-policy-opa`) writes LLM-generated Rego packages to an `AuthorizationPolicy` Kubernetes CR. Each agent pod embeds two OPA plugin instances inside AuthBridge (one for the inbound pipeline, one for the outbound pipeline); each plugin fetches its Rego packages from the CR at startup. AuthBridge requires no changes when policy rules are updated. Full spec: [components/pdp-policy-writer-opa.md](components/pdp-policy-writer-opa.md).

**AIAC ↔ Event Broker (NATS JetStream)**
The Agent subscribes to the event stream as a durable consumer with at-least-once delivery.
Unacknowledged messages survive pod restarts; failed messages are routed to a dead-letter subject.
See Section 7.4 (Event Broker) and Section 8 (Deployment) for subject names and handler mapping.

---

## 7. AIAC System Components

### 7.1 IdP Configuration Service

FastAPI service (`0.0.0.0:7071`) co-located with the PDP Policy Writer in the **Kagenti Interface Pod**. Manages IdP (Keycloak) entity data (subjects, roles, services, scopes) via Keycloak Admin REST API. Exposes read and write endpoints for configuration entities. Stateless. All endpoints except `/health` require a `?realm=<realm>` query parameter; returns `422` if absent. `/health` requires no realm parameter — it uses `KEYCLOAK_ADMIN_REALM` directly. `KeycloakAdmin` instances are created lazily per realm and cached in a thread-safe map; the admin always authenticates via the realm in `KEYCLOAK_ADMIN_REALM`.

**Full spec:** [components/idp-configuration-service.md](components/idp-configuration-service.md)

---

### 7.2 PDP Policy Writer

FastAPI service (`0.0.0.0:7072`, `aiac-pdp-policy-opa`) co-located with the IdP Configuration Service in the **Kagenti Interface Pod**. Writes LLM-generated Rego packages to an `AuthorizationPolicy` Kubernetes CR. Each AuthBridge OPA plugin instance fetches its Rego packages from the CR at startup.

**Full spec:** [components/pdp-policy-writer-opa.md](components/pdp-policy-writer-opa.md)

---

### 7.3 Policy Management Service

FastAPI service (`0.0.0.0:7074`, `aiac-pdp-state-service`) deployed as a dedicated single-replica StatefulSet (`aiac-pdp-state`) with a `volumeClaimTemplate` PVC (1 Gi, `ReadWriteOnce`) mounted at `/data`. Owns an in-memory `PolicyModel` cache backed by a SQLite database (`/data/state.db`) as the authoritative structured policy store. All GET requests are served from the in-memory cache; mutations write through to SQLite synchronously; on pod restart the cache is repopulated from SQLite. The Policy Builder sub-agent reads current `AgentPolicyModel` state for diff computation and writes updated state after each policy change, so that state survives pod restarts. The PDP Policy Writer has no dependency on the Policy Management Service; the SQLite store and `AuthorizationPolicy` CR are written by distinct services and serve distinct purposes.

**Full spec:** [components/policy-management-service.md](components/policy-management-service.md)

---

### 7.4 Library

Python package at `aiac/src/`. Clean `idp` / `pdp` namespace split:

**IdP library** (Keycloak entity management):
- **`aiac.idp.library.configuration.models`** — dependency-free Pydantic models for IdP entities (`Subject`, `Role`, `Service`, `Scope`).
- **`aiac.idp.library.configuration.api`** — HTTP client wrapping the IdP Configuration Service; read and write access to configuration entities; returns typed Pydantic instances; all methods require a `realm: str` parameter.

**PDP library** (OPA policy management):
- **`aiac.pdp.library.models`** — dependency-free Pydantic models for OPA policy entities (`PolicyRule`, `AgentPolicyModel`, `PolicyModel`).
- **`aiac.pdp.library.policy`** — HTTP client wrapping the PDP Policy Writer (OPA). Four module-level functions: `apply_policy`, `apply_agent_policy`, `delete_agent_policy`, `delete_policy`. No realm parameter.
- **`aiac.pdp.library.state`** — HTTP client wrapping the Policy Management Service. Six module-level functions: `get_policy`, `get_agent_policy`, `apply_policy`, `apply_agent_policy`, `delete_agent_policy`, `delete_policy`. Returns `PolicyModel` and `AgentPolicyModel` directly.

**Full specs:** [components/library-idp.md](components/library-idp.md) · [components/library-pdp.md](components/library-pdp.md) · [components/library-state.md](components/library-state.md)

---

### 7.5 Event Broker

NATS JetStream pod (`aiac-event-broker-service:4222`). Decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides at-least-once delivery, replay on pod restart via `WorkQueuePolicy`, and a dead-letter subject (`aiac.apply.dlq`) after 5 failed deliveries. No authentication — ClusterIP network isolation is the access control mechanism. Stream: `aiac-events`, subjects `aiac.apply.>`, consumer group `aiac-agent-consumer`.

**Full spec:** [components/event-broker.md](components/event-broker.md)

---

### 7.6 AIAC Agent

FastAPI + LangGraph service (`0.0.0.0:7070`). Receives automated triggers via the **Event Broker** (NATS JetStream durable consumer, `aiac-agent-consumer` queue group) and the operator-only `rebuild` command directly via HTTP. Structured as a thin **Controller** (`controller/routes.py`) that dispatches `/apply/*` handlers to three **Orchestrators**, each owning one or more compiled `StateGraph` sub-agents. A **NATS consumer** (asyncio background task in the FastAPI `lifespan` handler) is a thin adapter that receives NATS events and calls the same internal handler functions used by the HTTP endpoints:

| Orchestrator | Trigger(s) | Sub-agents |
|---|---|---|
| Service Onboarding | `aiac.apply.service.{id}` | Service Provision → Service Policy (sequential) |
| Policy Update | `aiac.apply.policy.build`, `/apply/policy/rebuild` (HTTP) | Build sub-agent or Rebuild sub-agent (alternative) |
| Role Update | `aiac.apply.role.{id}` | Role sub-agent |

All sub-agent `StateGraph` instances are logically separated modules running within a single pod and process. The **Policy Update** sub-agents compute a minimal `PolicyModel` delta between the current ChromaDB policy and live OPA state. The **Rebuild** variant additionally clears all OPA policy rules before recomputing the full `PolicyModel`. The **Role Update** orchestrator recomputes the `PolicyModel` for all services affected by the role change. The **Service Onboarding** orchestrator classifies the new service via the pod's `kagenti.io/type` label (for agents reads the `AgentCard` CR; for tools calls `tools/list` on the MCP endpoint discovered via K8s Service label lookup), then computes and writes an `AgentPolicyModel` for the new service. Stateless; changes are applied immediately. Integrated retry with differentiated error codes per upstream.

**Full spec:** [components/aiac-agent.md](components/aiac-agent.md)

---

### 7.7 RAG Knowledge Base

ChromaDB vector store (`aiac-rag-service:8000`) hosting two collections: `aiac-policies` (access control policy rules) and `aiac-domain-knowledge` (org/business context such as team rosters, application ownership, and department mappings). Both collections are managed by the RAG Ingest Service and read by the AIAC Agent. Co-located with the RAG Ingest Service in the RAG Pod. ChromaDB data is persisted on a 1 Gi PVC mounted at `/chroma/chroma`; the RAG Pod is a StatefulSet.

**Full spec:** [components/rag-knowledge-base.md](components/rag-knowledge-base.md)

---

### 7.8 RAG Ingest Service

FastAPI service (`0.0.0.0:7073`) co-located with ChromaDB. Thirteen collection-parameterized endpoints across three semantics: complete collection replacement (`POST /ingest/{collection}/{text|file|url}`), document-level upsert (`POST /ingest/{collection}/update/{text|file|url}`), and explicit removal (`DELETE /ingest/{collection}/{doc_id}`). The `{collection}` slug is validated against `AIAC_RAG_COLLECTIONS` (default: `policy,domain-knowledge`). After every successful ingest the service publishes to `aiac.apply.policy.build` on the Event Broker (`NATS_URL`). Developer access via `kubectl port-forward`.

**Full spec:** [components/rag-ingest-service.md](components/rag-ingest-service.md)

---

### 7.9 Keycloak SPI Listener

A custom Keycloak Event Listener SPI (Java) that listens to Keycloak's internal event bus and translates entity-scoped events into NATS publish calls to the Event Broker. The AIAC Agent subject schema is authoritative; the SPI PRD references it.

| Keycloak Event | Event Broker subject |
|---|---|
| `REGISTER`, `UPDATE_PROFILE` (user events) | — (dropped; OPA rules are role-scoped and resolve entitlements from the caller's role automatically) |
| `CLIENT_CREATED` | `aiac.apply.service.{id}` |
| Role created/updated | `aiac.apply.role.{id}` |

**Full spec:** TBD (separate PRD).

---

## 8. Deployment

### Kubernetes manifests

Four separate manifest files:

| File | Contents |
|------|----------|
| `aiac/k8s/pdp-interface-deployment.yaml` | `aiac-pdp-config` ConfigMap + Kagenti Interface Pod Deployment (IdP Configuration Service container + PDP Policy Writer container) + two ClusterIP Services (`aiac-pdp-config-service:7071`, `aiac-pdp-policy-service:7072`) |
| `aiac/k8s/state-statefulset.yaml` | `aiac-pdp-state` StatefulSet (Policy Management Service container) + `volumeClaimTemplate` (1 Gi, `ReadWriteOnce`, mounted at `/data`) + headless Service + `aiac-pdp-state-service:7074` ClusterIP Service |
| `aiac/k8s/event-broker-deployment.yaml` | Event Broker Pod Deployment (NATS JetStream) + ClusterIP Service |
| `aiac/k8s/rag-statefulset.yaml` | RAG StatefulSet (ChromaDB + RAG Ingest Service containers) + 1 Gi PVC template + ClusterIP Service |
| `aiac/k8s/agent-deployment.yaml` | Agent Pod Deployment (aiac-init container + AIAC Agent container) + ClusterIP Service |

The two Interface Pod containers mount `aiac-pdp-config` (KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_ADMIN_REALM) and `keycloak-admin-secret` (KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD) as env vars. The IdP Configuration Service uses `KEYCLOAK_ADMIN_REALM` (admin auth realm) and ignores `KEYCLOAK_REALM`; the PDP Policy Writer uses `KEYCLOAK_REALM` as its default operating realm. The Policy Management Service container mounts `aiac-pdp-config` for `AGENTPOLICY_DB_PATH` (default `/data/state.db`) — no Kubernetes API access or RBAC required.

### Docker images

Built independently. No entry in the repo's `build.yaml` CI matrix.

```bash
# Build IdP Configuration Service (deployed as a container in the Kagenti Interface Pod)
docker build -f aiac/src/aiac/idp/service/configuration/keycloak/Dockerfile -t aiac-pdp-config:latest aiac/src/

# Build PDP Policy Writer — OPA implementation (deployed as a container in the Kagenti Interface Pod)
docker build -f aiac/src/aiac/pdp/service/policy/opa/Dockerfile -t aiac-pdp-policy-opa:latest aiac/src/

# Build Policy Management Service (deployed as a StatefulSet aiac-pdp-state)
docker build -f aiac/src/aiac/pdp/service/state/Dockerfile -t aiac-pdp-state:latest aiac/src/

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
  KEYCLOAK_ADMIN_REALM: "master"
  AIAC_PDP_CONFIG_URL: "http://aiac-pdp-config-service:7071"
  AIAC_PDP_POLICY_URL: "http://aiac-pdp-policy-service:7072"
  AIAC_PDP_STATE_URL: "http://aiac-pdp-state-service:7074"
  AGENTPOLICY_DB_PATH: "/data/state.db"
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
| IdP Configuration Service endpoints | `KeycloakAdmin` methods (return fixture dicts) | Correct JSON response, 502 on Keycloak error |
| PDP Policy Writer (OPA) endpoints | Kubernetes CR write (`AuthorizationPolicy`) | 204 on success, 502 on CR write error |
| Policy Management Service endpoints | SQLite `:memory:` database | Correct read/write/delete; 404 on missing agent; 502 on SQLite write error; 503 on SQLite open/query failure at `/health` |
| `aiac.pdp.library.state` functions | Policy Management Service HTTP endpoints | Correct method + path per function; returns typed model on read; `RuntimeError` on non-2xx; default URL fallback |
| `aiac.pdp.library.models` | No mock needed | `extra='ignore'` drops unknown fields, required fields validated, `model_validate` round-trips correctly |
| `aiac.idp.library.configuration.api` functions | IdP Configuration Service HTTP endpoints | Returns correct Pydantic model instances; `RuntimeError` on non-2xx; default URL fallback |
| `aiac.pdp.library.policy` functions | PDP Policy Writer HTTP endpoints | Correct serialisation; `RuntimeError` on non-2xx; default URL fallback |
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

Integration tests call the live IdP Configuration Service (running locally or via port-forward) and assert that results are non-empty lists of the correct type. Event Broker integration tests require a live NATS JetStream instance.

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
- No auth on IdP Configuration Service, PDP Policy Writer, RAG Ingest Service, or Event Broker — network isolation (ClusterIP + `kubectl port-forward`) is the access control mechanism
- IdP Configuration Service, PDP Policy Writer, Agent, RAG Ingest Service, and Event Broker are not registered with the repo's `build.yaml` CI matrix; they have independent build processes
- `aiac/__init__.py` exists and is empty — `aiac` is a regular package, not a namespace package
- NATS consumer must **await** handler completion before issuing ack — fire-and-forget (`asyncio.create_task`) is prohibited; premature ack breaks at-least-once delivery guarantees
