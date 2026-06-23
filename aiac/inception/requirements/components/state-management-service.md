# Component PRD: AIAC State Management Service

## Problem Statement

The AIAC Agent's Policy sub-agent and Policy Builder sub-agent produce and merge `AgentPolicyModel` objects representing the access control policy for each service. The PDP Policy Service translates these into Rego packages and writes them to an `AuthorizationPolicy` Kubernetes CR — but this derived artifact cannot be reverse-engineered back into structured `AgentPolicyModel` data. Without a durable structured policy store:

- The Policy Builder sub-agent cannot read current policy state for diff computation — it must re-derive the full state from the PDP snapshot on every trigger.
- Off-boarded agents leave no structured record of their removal.
- Policy state is not inspectable via standard Kubernetes tooling.
- Pod restarts lose any in-flight policy construction context.

## Solution

A dedicated **AIAC State Management Service** owns `AgentPolicy` Kubernetes Custom Resources (one per `AgentPolicyModel`) as the authoritative structured policy store. A companion library [`aiac.pdp.library.state.api`](library-state.md) exposes module-level typed functions matching the `aiac.pdp.library.policy` pattern, used by AIAC Agent sub-agents to read and write policy state without Kubernetes client boilerplate.

The PDP Policy Service retains sole ownership of the `AuthorizationPolicy` CR (Rego packages) and has no dependency on the State Management Service. The two CRs serve distinct purposes and are written by distinct services:

| CR | Owner | Contents |
|---|---|---|
| `AgentPolicy` (one per agent) | State Management Service | Structured `AgentPolicyModel` — source of truth |
| `AuthorizationPolicy` (one total) | PDP Policy Service | Derived Rego packages — OPA runtime artifact |

---

## User Stories

1. As the Policy Builder sub-agent, I want to read the current `AgentPolicyModel` for a specific agent, so that I can compute an accurate delta without re-deriving state from the PDP snapshot.
2. As the Policy Builder sub-agent, I want to read the full `PolicyModel` (all agents), so that I can execute a whole-system policy rebuild.
3. As the Policy Builder sub-agent, I want to write an `AgentPolicyModel` to persistent storage, so that the current policy state survives pod restarts.
4. As the Policy Builder sub-agent, I want to delete a specific agent's policy on off-boarding, so that decommissioned services are removed from the structured policy store.
5. As the Policy Builder sub-agent, I want to clear all agent policies in a single call, so that a full policy rebuild can start from a clean state.
6. As an AIAC sub-agent developer, I want a typed Python library that returns `AgentPolicyModel` and `PolicyModel` objects directly, so that I can work with structured policy data without writing Kubernetes client code.
7. As an operator, I want to inspect current policy state using `kubectl get agentpolicies`, so that I can audit access control configuration without specialized tooling.
8. As an operator, I want the State Management Service to be co-located in the PDP Interface Pod, so that policy-related services are deployed together and share the same Kubernetes ServiceAccount.

---

## Implementation Decisions

### State Management Service

**Location:** `aiac/src/aiac/pdp/service/state/`

**Port:** `0.0.0.0:7074`

**ClusterIP Service:** `aiac-pdp-state-service:7074`

**Deployment:** third container in the PDP Interface Pod (alongside IdP Configuration Service `:7071` and PDP Policy Service `:7072`)

**Framework:** FastAPI + uvicorn. **Base image:** `python:3.12-slim`.

**Kubernetes client:** `kubernetes.client.CustomObjectsApi` with `load_incluster_config()` falling back to `load_kube_config()`.

**CRD identity:**

| Field | Value |
|---|---|
| Group | `aiac.kagenti.io` |
| Version | `v1` |
| Kind | `AgentPolicy` |
| Plural | `agentpolicies` |
| CR name | `agent_id` value |
| CR `spec` | `AgentPolicyModel.model_dump()` (snake_case) |

**Upsert strategy:** server-side apply — `patch_namespaced_custom_object` with `Content-Type: application/apply-patch+yaml`, `field_manager`, and `force=True`. Idempotent; no separate create/update logic.

**DELETE /policy:** list all CRs via `list_namespaced_custom_object`, then delete each via `delete_namespaced_custom_object`. No collection delete.

**Endpoints:**

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET` | `/policy` | — | `PolicyModel` (all agents) |
| `GET` | `/policy/agents/{agent_id}` | — | `AgentPolicyModel` |
| `POST` | `/policy` | `PolicyModel` | `204 No Content` |
| `POST` | `/policy/agents/{agent_id}` | `AgentPolicyModel` | `204 No Content` |
| `DELETE` | `/policy/agents/{agent_id}` | — | `204 No Content` |
| `DELETE` | `/policy` | — | `204 No Content` |
| `GET` | `/health` | — | `200` / `503` |

**Error responses:**
- `404 Not Found` with `{"error": "agent {id} not found"}` when GET /policy/agents/{agent_id} finds no CR.
- `502 Bad Gateway` with `{"error": "..."}` on Kubernetes API failure for all write endpoints.
- `503 Service Unavailable` if GET /health cannot reach the Kubernetes API.

**`main.py` functions:**

- `_cr_body(model: AgentPolicyModel) -> dict` — build server-side apply body with `apiVersion`, `kind`, `metadata.name`, `metadata.namespace`, `spec = model.model_dump()`.
- `_upsert_agent(agent_id: str, model: AgentPolicyModel)` — call `patch_namespaced_custom_object` with server-side apply.
- `_get_agent(agent_id: str) -> AgentPolicyModel` — call `get_namespaced_custom_object`; raise 404 if not found.
- `_list_all() -> PolicyModel` — call `list_namespaced_custom_object`; deserialize each item's `spec` to `AgentPolicyModel`.
- `_delete_agent(agent_id: str)` — call `delete_namespaced_custom_object`.
- `_delete_all()` — call `_list_all()`, then `_delete_agent` for each.

**Configuration:**

| Variable | Source | Default |
|---|---|---|
| `AGENTPOLICY_NAMESPACE` | ConfigMap (`aiac-pdp-config`) | Required |

**Dependencies:** `fastapi`, `uvicorn[standard]`, `kubernetes`, `pydantic`

**File structure:**

```
aiac/src/aiac/pdp/service/state/
├── __init__.py
├── Dockerfile
├── requirements.txt
└── main.py
```

Build command (run from repo root):
```bash
docker build -f aiac/src/aiac/pdp/service/state/Dockerfile \
  -t aiac-pdp-state:latest aiac/src/
```

---

## Testing Decisions

Good tests assert external behavior at the system boundary — not internal implementation details such as private helpers or field serialization choices.

### State Management Service

**Seam:** `kubernetes.client.CustomObjectsApi` — mock this class entirely.

**Prior art:** `2.13-unit-tests-pdp-policy-opa-service.md` (mock `CustomObjectsApi`; all endpoints + error paths).

Key behaviors to assert:
- `GET /policy/agents/{id}`: `get_namespaced_custom_object` called with correct args; CR `spec` deserialized to `AgentPolicyModel`; `404` returned when CR not found.
- `GET /policy`: `list_namespaced_custom_object` called; all items deserialized; empty list yields `PolicyModel(agents=[])`.
- `POST /policy/agents/{id}`: `patch_namespaced_custom_object` called with server-side apply body; `spec` contains `AgentPolicyModel.model_dump()`.
- `POST /policy`: `patch_namespaced_custom_object` called once per agent.
- `DELETE /policy/agents/{id}`: `delete_namespaced_custom_object` called with correct name.
- `DELETE /policy`: `list_namespaced_custom_object` called, then `delete_namespaced_custom_object` per item.
- Kubernetes API exception on write endpoints → `502`.
- Kubernetes API exception on `GET /health` → `503`.

See [library-state.md](library-state.md) for the companion library testing decisions.

---

## Out of Scope

- **Controller/watch pattern:** the State Management Service is a REST service, not a Kubernetes controller. It does not watch `AgentPolicy` CRs or react to changes.
- **CRD schema validation (OpenAPI v3):** deferred to the K8s manifests issue.
- **Triggering Rego generation:** the State Management Service writes structured data only. Triggering Rego generation in the PDP Policy Service remains the AIAC Agent's responsibility via `aiac.pdp.library.policy`.
- **Pagination:** `GET /policy` returns all agents without pagination. At target scale (hundreds of agents), the full CR list fits within a single Kubernetes API list response.
- **In-cluster mTLS between AIAC Agent and State Management Service:** secured by Kubernetes network policy; no application-layer auth.

---

## Further Notes

- The `AgentPolicy` CRD must exist in the cluster before the State Management Service starts. The CRD manifest and RBAC (`get`, `list`, `patch`, `delete` on `agentpolicies` in the `aiac.kagenti.io` API group) should be created in the K8s manifests issue that extends the PDP Interface Pod.
- `spec` fields use snake_case (matching Pydantic's `model_dump()`) rather than Kubernetes camelCase convention. This avoids a translation layer and is consistent with the approach in the `AuthorizationPolicy` CR.
- The `agent_id` is used as the CR `metadata.name`. Since Kubernetes resource names must be valid DNS subdomain names, `agent_id` values must be lowercase alphanumeric with hyphens only — this is already the convention for service IDs in the AIAC trigger events (`aiac.apply.service.{id}`).
