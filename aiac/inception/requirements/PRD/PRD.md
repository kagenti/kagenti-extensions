# PRD: AI-based Access Control (AIAC) for Keycloak

## 1. Purpose

Automate Keycloak RBAC management using a natural-language access control policy enforced by an AI agent. The system has three concerns:

1. **Configuration accessor** — a REST service and Python library that expose Keycloak user, role, client, and role-mapping data for both read and write operations.
2. **Policy knowledge base** — a ChromaDB RAG store holding the access control policy in persistent, queryable form, populated via a co-located ingest service.
3. **AIAC Agent** — a LangGraph-based AI agent triggered by Keycloak state-change events. It retrieves the current policy from the RAG store, interprets it against the live Keycloak state, and applies the required role assignments and revocations immediately.

## 2. Architecture Overview

Six components across three Kubernetes Pods plus a Python library layer, all implemented in Python 3.14. External dependencies: Keycloak Admin API, an LLM API, and an embedding API.

### Deployment topology

```
┌──────────────────────────────────────────────────────────┐
│  Keycloak Configuration Service Pod                      │
│                                                          │
│  ┌────────────────────────┐                              │
│  │  Keycloak Configuration│  :7070  ClusterIP            │
│  │  Service (FastAPI)     │  aiac-keycloak-service       │
│  └────────────────────────┘                              │
│              ▲                                           │
└──────────────┼───────────────────────────────────────────┘
               │
┌──────────────┼───────────────────────────────────────────┐
│  Agent Pod   |                                           │
│              │                                           │
│  ┌────────────────────────┐                              │
│  │  AIAC Agent (FastAPI)  │  :7071  ClusterIP            │
│  │  LangGraph-based       │                              │
│  └────────────────────────┘                              │
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
│  Python library  (aiac/src/)                    │
│                                                          │
│  aiac.library.models  — Pydantic only           │
│  aiac.library.api     — HTTP client →           │
│                          Keycloak Configuration Service  │
└──────────────────────────────────────────────────────────┘
```

### Call flow

```
Policy / domain knowledge ingestion (operator-driven):

  Developer ──(kubectl port-forward)──► RAG Ingest Service ──► ChromaDB aiac-policies          [policy rules]
                                                           ├──► ChromaDB aiac-domain-knowledge  [org/business context]
                                                           └──► Embedding API (external)

Role enforcement (event-driven):

  Trigger ──► AIAC Agent ──┬──► ChromaDB aiac-policies         [retrieve policy chunks]
                           ├──► ChromaDB aiac-domain-knowledge  [retrieve domain context chunks]
                           ├──► library.api ──► Keycloak Configuration Service ──► Keycloak Admin API  [read state]
                           │
                           ├──► LLM API (external)              [propose diff from policy + domain context + state]
                           ├──► LLM API (external)              [validate diff]
                           └──► library.api ──► Keycloak Configuration Service ──► Keycloak Admin API  [apply diff]
```

### Component dependencies

| Component | Called by | Calls | Returns |
|-----------|-----------|-------|---------|
| Keycloak Configuration Service | `aiac.library.api` | Keycloak Admin REST API | Raw Keycloak JSON |
| `aiac.library.models` | `aiac.library.api`, AIAC Agent, other agents | — | Pydantic model definitions |
| `aiac.library.api` | AIAC Agent, Python scripts, LangGraph agents | Keycloak Configuration Service (HTTP) | Pydantic model instances |
| ChromaDB | RAG Ingest Service (writes), AIAC Agent (reads) | — | Policy and domain knowledge vectors |
| RAG Ingest Service | Developer (via `kubectl port-forward`) | ChromaDB, Embedding API | — |
| AIAC Agent | Keycloak event handlers, orchestrators | `aiac.library.api`, ChromaDB, LLM API | Applied/revoked role diff |

### Key architectural decisions

- **Keycloak Configuration Service binds to `0.0.0.0`.** Exposed as a Kubernetes ClusterIP Service (`aiac-keycloak-service`) so that the Agent Pod can reach it over the cluster network. Also accessible via `kubectl port-forward`.
- **RAG Pod runs ChromaDB and RAG Ingest Service together.** Exposed as a Kubernetes ClusterIP Service (`aiac-rag-service`) on ports 7080 (ChromaDB) and 7072 (RAG Ingest Service). Developer ingestion is done via `kubectl port-forward`.
- **AIAC Agent is stateless.** Changes are applied immediately on trigger — no pending session or human confirmation step.
- **`aiac.library.models` is dependency-free** (only `pydantic`). Agents can import it without pulling in `requests` or `python-dotenv`.
- **`aiac.__init__`, `aiac.library.__init__`, and `aiac.service.__init__` are empty.** Callers use explicit submodule paths: `from aiac.library.models import User`, `from aiac.library.api import get_users`.
- **ChromaDB hosts two collections: `aiac-policies` and `aiac-domain-knowledge`.** The legal collection set is governed by `AIAC_RAG_COLLECTIONS` on the RAG Ingest Service (default: `policy,domain-knowledge`). Collection slug to ChromaDB name mapping: `policy` → `aiac-policies`, `domain-knowledge` → `aiac-domain-knowledge`.

---

## 3. Component: Keycloak Configuration Service

FastAPI service (`0.0.0.0:7070`) that proxies the Keycloak Admin REST API. Exposes 8 endpoints (6 reads + assign + revoke). Stateless, no caching. Supports per-request realm override via optional `?realm=` query parameter.

**Full spec:** [components/keycloak-service.md](components/keycloak-service.md)

---

## 4. Component: Library

Python package at `aiac/src/`. Two submodules:

- **`aiac.library.models`** — dependency-free Pydantic models for all Keycloak entities (`User`, `RealmRole`, `Client`, `ClientRole`, `ClientScope`, `RoleMappings`).
- **`aiac.library.api`** — HTTP client wrapping the Keycloak Configuration Service; returns typed Pydantic instances; all functions require a `realm: str` parameter.

**Full spec:** [components/library.md](components/library.md)

---

## 5. Component: AIAC Agent

LangGraph `StateGraph` (`0.0.0.0:7071`). Six `/apply/*` endpoints trigger a conditional workflow: three-way parallel fan-out (policy fetch from `aiac-policies` + domain knowledge fetch from `aiac-domain-knowledge` + Keycloak state fetch) → LLM propose diff → LLM validate diff → apply or abort. Stateless; changes are applied immediately. Integrated retry with differentiated error codes per upstream.

**Full spec:** [components/aiac-agent.md](components/aiac-agent.md)

---

## 6. Component: RAG Knowledge Base

ChromaDB vector store (`aiac-rag-service:7080`) hosting two collections: `aiac-policies` (access control policy rules) and `aiac-domain-knowledge` (org/business context such as team rosters, application ownership, and department mappings). Both collections are managed by the RAG Ingest Service and read by the AIAC Agent. Co-located with the RAG Ingest Service in the RAG Pod.

**Full spec:** [components/rag-knowledge-base.md](components/rag-knowledge-base.md)

---

## 7. Component: RAG Ingest Service

FastAPI service (`0.0.0.0:7072`) co-located with ChromaDB. Thirteen collection-parameterized endpoints across three semantics: complete collection replacement (`POST /ingest/{collection}/{text|file|url}`), document-level upsert (`POST /ingest/{collection}/update/{text|file|url}`), and explicit removal (`DELETE /ingest/{collection}/{doc_id}`). The `{collection}` slug is validated against `AIAC_RAG_COLLECTIONS` (default: `policy,domain-knowledge`). Developer access via `kubectl port-forward`.

**Full spec:** [components/rag-ingest-service.md](components/rag-ingest-service.md)

---

## 8. Deployment

### Kubernetes manifests

Three separate manifest files:

| File | Contents |
|------|----------|
| `aiac/k8s/keycloak-service-deployment.yaml` | `aiac-keycloak-config` ConfigMap + Keycloak Configuration Service Pod Deployment + ClusterIP Service |
| `aiac/k8s/rag-deployment.yaml` | RAG Pod Deployment (ChromaDB + RAG Ingest Service containers) + ClusterIP Service |
| `aiac/k8s/agent-deployment.yaml` | Agent Pod Deployment + ClusterIP Service |

The Keycloak Configuration Service Pod mounts `aiac-keycloak-config` (KEYCLOAK_URL, KEYCLOAK_REALM) and `keycloak-admin-secret` (KEYCLOAK_ADMIN_USERNAME, KEYCLOAK_ADMIN_PASSWORD) as env vars.

### Docker images

Built independently. No entry in the repo's `build.yaml` CI matrix.

```bash
# Build Keycloak Configuration Service
docker build -t ac-configuration-service:latest aiac/service/

# Build Agent
docker build -t aiac-agent:latest aiac/agent/

# Build RAG Ingest Service
docker build -t aiac-rag-ingest:latest aiac/rag-ingest/
```

### `aiac-keycloak-config` ConfigMap template

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: aiac-keycloak-config
data:
  KEYCLOAK_URL: "http://keycloak-service.keycloak.svc:8080"
  KEYCLOAK_REALM: "kagenti"
```

Update `KEYCLOAK_URL` and `KEYCLOAK_REALM` for the target environment before applying.

---

## 9. Testing

Tests live in `tests/` alongside the existing client-registration and keycloak_sync tests.

### Unit tests

| Target | What to mock | What to assert |
|--------|-------------|----------------|
| Keycloak Configuration Service endpoints | `KeycloakAdmin` methods (return fixture dicts) | Correct JSON response, 204 on write success, 502 on Keycloak error |
| `aiac.library.models` | No mock needed | `extra='ignore'` drops unknown fields, required fields validated, `model_validate` round-trips correctly |
| `aiac.library.api` functions | Keycloak Configuration Service HTTP endpoints | Returns correct Pydantic model instances; `RuntimeError` on non-2xx; default URL fallback |
| AIAC Agent | TBD | TBD |

### Integration tests

Require a live Keycloak instance. Controlled by env vars:

| Variable | Description |
|----------|-------------|
| `KEYCLOAK_URL` | Keycloak base URL |
| `KEYCLOAK_REALM` | Realm to query |
| `KEYCLOAK_ADMIN_USERNAME` | Admin username |
| `KEYCLOAK_ADMIN_PASSWORD` | Admin password |

Integration tests call the live Keycloak Configuration Service (running locally or via port-forward) and assert that results are non-empty lists of the correct type.

Use a pytest marker (e.g. `@pytest.mark.integration`) so unit tests and integration tests can be run independently:

```bash
pytest tests/ -m "not integration"   # unit only
pytest tests/ -m integration          # integration only
```

---

## 10. Conventions and constraints

- Python version: 3.14
- Base Docker image: `python:3.14-slim`
- Linting: ruff (line length 120, target py312 per root `pyproject.toml`)
- Commits: DCO sign-off required (`git commit -s`); use `Assisted-By` not `Co-Authored-By`
- No auth on Keycloak Configuration Service or RAG Ingest Service — network isolation (ClusterIP + `kubectl port-forward`) is the access control mechanism
- Keycloak Configuration Service, Agent, and RAG Ingest Service are not registered with the repo's `build.yaml` CI matrix; they have independent build processes
- The `aiac` directory is a namespace package — do not create `aiac/__init__.py`
