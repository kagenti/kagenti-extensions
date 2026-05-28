# Component PRD: AIAC Agent

## Description
A LangGraph-based AI agent that enforces a natural-language access control policy against the live Keycloak state. Triggered via HTTP by Keycloak state change events or a full rebuild request. On each invocation the Agent:

1. In parallel: retrieves policy chunks from ChromaDB (`aiac-policies`), retrieves domain context chunks from ChromaDB (`aiac-domain-knowledge`), and reads the relevant Keycloak state via `aiac.library.api`.
2. Interprets the policy and domain context against the current state using an LLM, producing a typed `ProposedDiff`.
3. Validates the diff (existence check, safety guard rails, LLM re-confirmation, scope check).
4. On validation pass: applies changes immediately via `assign_client_roles` and `revoke_client_roles`. On failure: returns a structured error with no changes applied.

## Graph design

Structured conditional workflow (`StateGraph`) — not ReAct. LLM is confined to `propose_diff` and `validate_diff` nodes only. A single compiled graph instance is shared by all six endpoints; trigger type and entity ID are passed as initial state.

```
START → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_keycloak_state] → propose_diff → validate_diff → [apply_diff | abort] → format_response → END
```

### State schema (`AgentState`)

| Field | Type | Description |
|-------|------|-------------|
| `trigger` | `TriggerContext` | Endpoint type + entity ID |
| `realm` | `str` | Keycloak realm (from `KEYCLOAK_REALM`) |
| `policy_chunks` | `list[str]` | Policy text chunks from `aiac-policies` ChromaDB collection |
| `domain_knowledge_chunks` | `list[str]` | Org/business context chunks from `aiac-domain-knowledge` ChromaDB collection; `[]` when the collection is empty — non-fatal |
| `keycloak_snapshot` | `KeycloakSnapshot` | Scoped Keycloak data for this trigger |
| `proposed_diff` | `ProposedDiff \| None` | LLM output |
| `validation_errors` | `list[str]` | Errors from `validate_diff` |
| `applied` | `list[RoleAssignment]` | Executed assignments |
| `revoked` | `list[RoleAssignment]` | Executed revocations |
| `summary` | `str` | Human-readable explanation (from LLM `reasoning` field) |

### State types (`state.py`)

`KeycloakSnapshot` is a Pydantic `BaseModel` that reuses model classes from `aiac.library.models`. All fields are optional with empty defaults — each trigger type populates only the relevant subset:

```python
class KeycloakSnapshot(BaseModel):
    users: list[User] = []
    realm_roles: list[RealmRole] = []
    clients: list[Client] = []
    client_roles: dict[str, list[ClientRole]] = {}   # client_id → roles
    client_scopes: list[ClientScope] = []
    user_role_mappings: dict[str, RoleMappings] = {} # user_id → mappings
```

### LLM output schema (`ProposedDiff`)

```python
class RoleAssignment(BaseModel):
    user_id: str
    client_id: str
    role_id: str
    role_name: str

class ProposedDiff(BaseModel):
    assign: list[RoleAssignment]
    revoke: list[RoleAssignment]
    reasoning: str
```

LLM is called via `llm.with_structured_output(ProposedDiff)` using `langchain-openai` (`ChatOpenAI`). Target endpoint must support tool calling.

**Planner prompt** (`propose_diff` node):
- System message (stable, cacheable): role definition + `AIAC_AC_MODEL` framing + output instructions ("enforce {AC_MODEL} policy, compute minimal role diff, be conservative").
- User message (per-request): trigger description + policy chunks (from `aiac-policies`) + domain knowledge section (from `aiac-domain-knowledge`; omitted or rendered as empty section when `domain_knowledge_chunks` is `[]`) + scoped Keycloak snapshot summary (structured text, not raw JSON).

**Auditor prompt** (`validate_diff` re-confirmation):
- System message: auditor role ("verify this diff correctly implements the policy").
- User message: proposed diff + policy chunks + domain knowledge chunks (auditor receives the same context as the planner to enable full re-confirmation).

### `fetch_policy` and `fetch_domain_knowledge` — RAG query strings per trigger type

Both nodes use the same trigger-type-keyed query strings. The query is derived from the **trigger TYPE only** (not the entity ID).

| Trigger | ChromaDB similarity query |
|---------|--------------------------|
| `rebuild` | `"all access control rules"` |
| `user/{id}` | `"user role assignment rules"` |
| `realm-role/{id}` | `"realm role assignment rules"` |
| `client/{id}` | `"client access control rules"` |
| `client/{id}/role/{id}` | `"client role assignment rules"` |
| `client-scope/{id}` | `"client scope access control rules"` |

- `fetch_policy` queries `aiac-policies` and stores results in `AgentState.policy_chunks`.
- `fetch_domain_knowledge` queries `aiac-domain-knowledge` and stores results in `AgentState.domain_knowledge_chunks`. Returns `[]` when the collection is empty — non-fatal, the agent continues with empty domain context.
- Both nodes share `UPSTREAM_MAX_RETRIES` (tenacity retry policy) and return HTTP 503 when ChromaDB is unavailable.

Number of results capped by `CHROMA_N_RESULTS` (default `10`), shared across both nodes.

### `fetch_keycloak_state` — Keycloak data scope per trigger type

| Trigger | Fetches via `aiac.library.api` |
|---------|----------------------------------------|
| `rebuild` | All users; all clients + their roles; all realm roles; all role mappings for all users |
| `user/{id}` | That user's current role mappings; all clients + their roles |
| `realm-role/{id}` | All users; all realm roles |
| `client/{id}` | All users; that client's roles; all role mappings for all users |
| `client/{id}/role/{id}` | All users; that client's roles; all role mappings for all users |
| `client-scope/{id}` | All client scopes; all clients; all users |

### `validate_diff` — validation checks (binary abort on any failure)

1. **Existence check** — every `user_id`, `client_id`, `role_id` in the diff exists in `keycloak_snapshot`.
2. **Safety guard rails** — total changes (`assign` + `revoke`) ≤ `MAX_CHANGES_PER_RUN`.
3. **LLM re-confirmation** — second LLM call (same `ChatOpenAI` instance, auditor system prompt) with diff + policy chunks; returns `ValidationVerdict(approved: bool, reason: str)` via `with_structured_output`.
4. **Scope validation** — diff is bounded to entities referenced by the trigger; no over-reach on partial updates.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/apply/rebuild` | Full rebuild — recompute all mappings from current Keycloak state and policy RAG, apply immediately |
| POST | `/apply/user/{user_id}` | Recompute and apply mappings affected by a user addition or removal |
| POST | `/apply/realm-role/{role_id}` | Recompute and apply mappings affected by a realm role addition or removal |
| POST | `/apply/client/{client_id}` | Recompute and apply mappings affected by a client addition or removal |
| POST | `/apply/client/{client_id}/role/{role_id}` | Recompute and apply mappings affected by a client role addition or removal |
| POST | `/apply/client-scope/{scope_id}` | Recompute and apply mappings affected by a client scope addition or removal |

Success response:

```json
{ "applied": [...], "revoked": [...], "summary": "..." }
```

Abort response (validation failure):

```json
{ "applied": [], "revoked": [], "summary": "...", "validation_errors": [...] }
```

## Configuration

| Variable | Default | Source |
|----------|---------|--------|
| `AC_SERVICE_URL` | `http://aiac-keycloak-service:7070` | ConfigMap |
| `CHROMA_URL` | `http://aiac-rag-service:7080` | ConfigMap |
| `KEYCLOAK_REALM` | — | ConfigMap (`aiac-keycloak-config`) |
| `LLM_BASE_URL` | — | ConfigMap |
| `LLM_MODEL` | — | ConfigMap |
| `LLM_API_KEY` | — | Kubernetes Secret |
| `AIAC_AC_MODEL` | `RBAC` | ConfigMap (accepted: `RBAC`, `ABAC`, `REBAC`) |
| `CHROMA_N_RESULTS` | `10` | ConfigMap |
| `MAX_CHANGES_PER_RUN` | `50` | ConfigMap |
| `UPSTREAM_MAX_RETRIES` | `3` | ConfigMap |

ChromaDB collections queried: `aiac-policies` (policy fetch) and `aiac-domain-knowledge` (domain knowledge fetch).

## Runtime

- Framework: FastAPI with uvicorn
- Bind: `0.0.0.0:7071`
- State: stateless — changes applied immediately, no pending session required
- Base image: `python:3.14-slim`

## File structure

```
aiac/src/aiac/agent/
├── __init__.py          ← empty
├── service/
│   ├── __init__.py      ← empty
│   ├── main.py          ← FastAPI app factory + uvicorn entrypoint
│   ├── routes.py        ← all six /apply/* route handlers
│   ├── Dockerfile       ← Docker image (build context: aiac/src/)
│   └── requirements.txt ← Python dependencies
└── agent/
    ├── __init__.py      ← empty
    ├── state.py         ← AgentState, TriggerContext, KeycloakSnapshot,
    │                      ProposedDiff, RoleAssignment, ValidationVerdict
    ├── prompts.py       ← planner system prompt template,
    │                      auditor system prompt template
    ├── nodes.py         ← fetch_policy, fetch_domain_knowledge, fetch_keycloak_state,
    │                      propose_diff, validate_diff, apply_diff, format_response
    └── graph.py         ← StateGraph definition, edge wiring, compiled graph instance (singleton);
                           per-trigger helpers: run_rebuild, run_user, run_realm_role,
                           run_client, run_client_role, run_client_scope
```

Docker build command (run from repo root):

```bash
docker build -f aiac/src/aiac/agent/service/Dockerfile \
             -t aiac-agent:latest \
             aiac/src/
```

## Error handling

All upstream calls (`fetch_policy`, `fetch_keycloak_state`, `propose_diff`, `validate_diff`) are retried up to `UPSTREAM_MAX_RETRIES` times with exponential backoff (`tenacity`) before propagating the error.

| Upstream | HTTP status on final failure |
|----------|------------------------------|
| ChromaDB | `503 Service Unavailable` |
| Keycloak Configuration Service | `502 Bad Gateway` |
| LLM API | `504 Gateway Timeout` |

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
```
