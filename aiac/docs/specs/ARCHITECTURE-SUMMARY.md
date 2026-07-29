# AIAC Architectural Summary

## Abstract

AI-based Access Control (AIAC) is a Kagenti platform extension that automates RBAC/ABAC policy
enforcement for AI agents running on Kubernetes. A LangGraph-based AI agent continuously translates
a natural-language access control policy — stored in a vector knowledge base — into concrete
permission configurations in the active Policy Decision Point (PDP), eliminating manual policy
administration and preventing policy drift as services and roles evolve. The PDP backend is OPA,
which evaluates LLM-generated Rego rules; Keycloak remains the identity provider for entity
management (subjects, roles, services).

---

## Problem Description

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

## Problem Solution

AIAC introduces a strict three-layer model that cleanly separates policy concerns:

| Layer | Component | Responsibility |
|---|---|---|
| **Policy Management** | AIAC Agent | Translates natural-language policy into PDP configuration on every trigger |
| **Policy Decision (PDP)** | OPA | Evaluates LLM-generated Rego rules; returns the caller's entitlements/decision |
| **Policy Enforcement (PEP)** | AuthBridge | Intercepts traffic; exchanges tokens; carries no policy knowledge |

The AIAC Agent subscribes to an event stream (NATS JetStream) and reacts to entity lifecycle
events — new services, role changes, policy updates — by retrieving the current policy from a RAG
knowledge base, querying live PDP state, and applying the minimal required diff via a dedicated
PDP Policy Writer. AuthBridge performs RFC 8693 token exchanges sending only the target
`audience` — no `scope` parameter. OPA evaluates the caller's role against the Rego rules and
returns the entitlements that role grants on the target service; the IdP (Keycloak, via
AuthBridge) issues the exchanged token scoped to exactly those entitlements.
**Policy intent lives entirely in OPA, kept current by AIAC.**

---

## Major Use-Cases

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

The agent evaluates all current OPA policy rules — including manually added ones that AIAC did
not create — against the natural-language policy. It reports compliant, non-compliant, and
policy-agnostic entitlements, enabling audit and remediation workflows.

### UC-4 · Access Request

**Trigger:** User request via chatbot.

A user requests an entitlement grant. The agent verifies the request against the policy
(permissive approach) and either auto-grants or routes to a human approver (man-in-the-loop).
Manually granted entitlements are flagged as policy-agnostic and surfaced during UC-3 reviews.

---

## AIAC Component Architecture

Eight components across five Kubernetes Pods plus a Python library layer, all implemented in Python 3.12. External dependencies: Keycloak Admin API, an LLM API, and an embedding API. The Keycloak SPI listener is defined in a separate PRD.

| # | Component | Description |
|---|-----------|-------------|
| 1 | **IdP Configuration Service** | REST service that exposes IdP entity data (subjects, roles, services, scopes) for read and write operations. Read methods enrich services with assigned roles/scopes and enrich roles with child roles. Backed by Keycloak. Python library: `aiac.idp.configuration`. |
| 2 | **PDP Policy Writer** | REST service that applies LLM-generated Rego rules to the OPA backend. Writes derived Rego packages to an `AuthorizationPolicy` Kubernetes CR. Exposed as ClusterIP service `aiac-pdp-policy-service:7072`. Python library: `aiac.pdp.policy.library`. |
| 3 | **Policy Store** | REST service that owns an in-memory `PolicyModel` cache backed by SQLite as the authoritative structured policy store. Enables the Policy Computation Engine to read current `AgentPolicyModel` state for additive merging. Deployed as a dedicated single-replica StatefulSet (`aiac-policy-store`) at `:7074`. Python library: `aiac.policy.store.library`. |
| 4 | **Policy Computation Engine** | Pure Python library module (`aiac.policy.computation`). No service, no Kubernetes deployment. Receives `list[PolicyRule]` from AIAC Agent sub-agents, queries IdP to resolve owning services, additively merges rules into `AgentPolicyModel` objects in the Policy Store, and pushes the updated `PolicyModel` to the PDP Policy Writer. Single entry point: `compute_and_apply(rules)`. |
| 5 | **Policy and Domain Knowledge RAG** | ChromaDB vector store holding the access control policy and domain knowledge in persistent, queryable form, populated via a co-located RAG Ingest Service. |
| 6 | **Event Broker** | NATS JetStream pod that decouples event producers (Keycloak SPI listener, RAG Ingest Service) from the AIAC Agent. Provides durable, at-least-once delivery with automatic replay on Agent pod restart. Competing consumer model ensures each event is processed exactly once. |
| 7 | **AIAC Agent** | LangGraph-based AI agent triggered by Event Broker subscriptions (`aiac.apply.>` subjects) and directly by the operator (`rebuild` only). Retrieves the current policy from the RAG store, interprets it against live PDP state, and applies the required policy changes immediately. |
| 8 | **Python library** | Python API library provides typed access to IdP and policy services via `aiac.idp.configuration`, `aiac.policy.model`, `aiac.policy.store.library`, `aiac.pdp.policy.library`, and `aiac.policy.computation` modules backed by generic Pydantic models. |

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
               │   │  Policy Store Pod                    │
               │   │                                      │
               │   │  ┌───────────────────────────────┐   │
               │   │  │  Policy Store Service         │   │
               │   │  │                               │   │
               │   │  │     (SQLite policy.db)        │   │
               │   │  └───────────────────────────────┘   │
               │   │                  ▲                   │
               │   └──────────────────┼───────────────────┘
               │                      │
┌──────────────┼──────────────────────┼───────────────────┐  ┌────────────────────────────────┐
│  Agent Pod   └───────────────────┐  │                   │  │  Event Broker Pod              │
│                                  │  │                   │  │                                │
│  ┌──────────────────────┐   ┌────────────────┐          │  │  ┌──────────────────────────┐  │
│  │ Policy Compute Engn  │◄──│   AIAC Agent   │◄─────────┼──┼──│      NATS JetStream      │  │
│  └──────────────────────┘   └────────────────┘  (𝘯𝘰𝘵𝘪𝘧𝘺) │  │  └──────────────────────────┘  │
│                                     │                   │  │         ▲              ▲       │
│                                     │                   │  │         │              │       │
└─────────────────────────────────────┼───────────────────┘  └─────────┼──────────────┼───────┘
                                      │                            (𝘱𝘶𝘣𝘭𝘪𝘴𝘩)        (𝘱𝘶𝘣𝘭𝘪𝘴𝘩)
┌─────────────────────────────────────┼───────────────────┐            │              │
│  Policy / Domain Knowledge RAG Pod  │                   │       (𝗞𝗲𝘆𝗰𝗹𝗼𝗮𝗸 𝗦𝗣𝗜)  (𝗥𝗔𝗚 𝗜𝗻𝗴𝗲𝘀𝘁)
│                                     ▼                   │
│  ┌─────────────────────┐   ┌─────────────────────────┐  │
│  │ RAG Ingest Service  │──►│ ChromaDB (vector store) │  │
│  └─────────────────────┘   └─────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

All inter-pod traffic is Kubernetes ClusterIP. External access is exclusively via
`kubectl port-forward` (operator/developer) or NATS publish (Keycloak SPI, RAG Ingest).

---

## Kagenti / Keycloak / OPA Interfaces

**AIAC ↔ Kagenti platform**
The AIAC Agent reads `AgentRuntime` and `AgentCard` custom resources from the Kubernetes API to
extract service metadata during UC-1 service onboarding. The `aiac.idp.configuration` and
`aiac.pdp.policy.library` Python packages are the integration surface for other Kagenti components
needing typed access to IdP configuration and PDP policy state.

**AIAC ↔ Keycloak**
The IdP Configuration Service proxies Keycloak Admin REST endpoints under generic IdP entity
names (subjects, roles, services, scopes). The Keycloak SPI listener publishes entity lifecycle
events to NATS; it is a separate component outside the AIAC codebase.

**AIAC ↔ OPA**
The PDP Policy Writer writes LLM-generated Rego rules to an `AuthorizationPolicy` Kubernetes CR.
Each agent pod's OPA plugin fetches its Rego packages from the CR at startup.

**AIAC ↔ Policy Management Service**
The Policy Management Service writes structured `AgentPolicyModel` data to a SQLite store
(in-memory cache + write-through to `/data/state.db` on a dedicated PVC) — the source of truth
for policy state that the AIAC Agent diffs against before writing updated Rego rules to OPA.

**AIAC ↔ Event Broker (NATS JetStream)**
The Agent subscribes to the event stream as a durable consumer with at-least-once delivery.
Unacknowledged messages survive pod restarts; failed messages are routed to a dead-letter subject.

---

## Call Flows

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

---
