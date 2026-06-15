# AIAC Architectural Summary

## Abstract

AI-based Access Control (AIAC) is a Kagenti platform extension that automates RBAC/ABAC policy
enforcement for AI agents running on Kubernetes. A LangGraph-based AI agent continuously translates
a natural-language access control policy — stored in a vector knowledge base — into concrete
permission configurations in the active Policy Decision Point (PDP), eliminating manual policy
administration and preventing policy drift as services and roles evolve. Phase 1 targets Keycloak
as the PDP backend; Phase 2 (planned) replaces only the policy-write layer with OPA/Rego while
leaving all other components unchanged.

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
3. **Manual administration overhead** — keeping Keycloak composite role mappings consistent with
   a growing fleet of agents and tools requires ongoing human attention with no audit trail.

---

## Problem Solution

AIAC introduces a strict three-layer model that cleanly separates policy concerns:

| Layer | Component | Responsibility |
|---|---|---|
| **Policy Management** | AIAC Agent | Translates natural-language policy into PDP configuration on every trigger |
| **Policy Decision (PDP)** | Keycloak (Phase 1) / OPA (Phase 2) | Evaluates caller entitlements; issues scoped tokens |
| **Policy Enforcement (PEP)** | AuthBridge | Intercepts traffic; exchanges tokens; carries no policy knowledge |

The AIAC Agent subscribes to an event stream (NATS JetStream) and reacts to entity lifecycle
events — new services, role changes, policy updates — by retrieving the current policy from a RAG
knowledge base, querying live PDP state, and applying the minimal required diff via a dedicated
PDP Policy Service. AuthBridge performs RFC 8693 token exchanges sending only the target
`audience`; the PDP resolves entitlements from the composite role mappings AIAC maintains.
**Policy intent lives entirely in the PDP, not in per-pod configuration.**

---

## Major Use-Cases

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

## AIAC Component Architecture

Five components across four Kubernetes pods, plus a Python client library:

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
               │                          │
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

---

## Kagenti / Keycloak / OPA Interfaces

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
      │ 5. GET /roles, /services, /assignments ──► PDP Configuration Service ──► Keycloak Admin REST
      │ 6. retrieve full policy context        ──► ChromaDB
      │ 7. [LLM] compute full composite role diff against current PDP state
      │ 8. POST /composite-roles    (add delta mappings)    ──► PDP Policy Service ──► Keycloak Admin REST
      │ 9. DELETE /composite-roles  (remove delta mappings) ──► PDP Policy Service ──► Keycloak Admin REST
      │ 10. ACK message
      ▼
 NATS JetStream  (message removed from pending)
```

#### UC-2b · Full Rebuild (`POST /apply/policy/rebuild`, operator-only)

```
 Operator
      │ 1. POST /apply/policy/rebuild  (kubectl port-forward → Agent pod)
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

---

## Short-Term Objectives

| # | Objective | Detail |
|---|-----------|--------|
| 1 | **Kagenti integration (UC-1 implementation)** | Plug AIAC into Kagenti and define its lifecycle |
| 2 | **Improve AIAC decision reasoning** | Take into account richer context: User Role description, Agent card, Tool description, Policy digest |
| 3 | **Rego / OPA integration (initially with Keycloak)** | OPA as the PDP; outcome: Rego access rules |
| 4 | **GitHub demo reimplementation with Rego / OPA** | End-to-end demo using OPA as the Policy Decision Point |
