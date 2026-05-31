# Component PRD: AIAC Agent

## Description
A LangGraph-based AI agent that enforces a natural-language access control policy against the live Keycloak state. Triggered via HTTP by Keycloak state change events or full build/rebuild requests. The agent runs two compiled `StateGraph` instances:

- **Policy Update Graph** — handles `build`, `rebuild`, `user/{id}`, and `realm-role/{id}` triggers. Computes role assignment diffs and applies them.
- **Client Onboarding Graph** — handles `client/{id}` triggers. Classifies the new client, provisions its roles and scopes in Keycloak via an LLM-derived `ClientProvision`, then computes and applies mapping diffs.

On each invocation both graphs:

1. In parallel: retrieve policy chunks from ChromaDB (`aiac-policies`), retrieve domain context chunks from ChromaDB (`aiac-domain-knowledge`), and read the relevant Keycloak state via `aiac.library.api`.
2. Interpret the policy and domain context against the current state using an LLM, producing a typed `ProposedDiff`.
3. Validate the diff (existence check, safety guard rails, LLM re-confirmation, scope check).
4. On validation pass: apply changes immediately via `assign_client_roles` and `revoke_client_roles`. On failure: return a structured error with no changes applied.

The Client Onboarding Graph additionally runs a preprocessing subgraph before step 1: it classifies the client type (Agent or Tool), derives its Keycloak roles and scopes via an LLM call, and provisions them via `create_client_role` and `create_client_scope` before the policy diff phase begins.

## Graph design

Two compiled `StateGraph` instances sharing a common set of node functions. Both graphs share `BaseAgentState` as the base state schema. Trigger type and entity ID are passed as initial state.

### Policy Update Graph

```
START → (if rebuild: clear_assignments →) [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_keycloak_state] → propose_diff → validate_diff → [apply_diff | abort] → format_response → END
```

Handles triggers: `build`, `rebuild`, `user/{id}`, `realm-role/{id}`.

- For `rebuild`: `clear_assignments` runs first (calls `revoke_all_role_assignments(realm)`), then the parallel fetch fan-out proceeds against the now-empty state. The `propose_diff` node receives an empty `keycloak_snapshot` and produces an assign-only diff.
- For `build` and all other triggers: no `clear_assignments`; proceeds directly to the parallel fetch fan-out and computes a minimal diff against live state.

### Client Onboarding Graph

```
START → classify_client → [analyze_agent | analyze_tool] → provision_client → [fetch_policy ‖ fetch_domain_knowledge ‖ fetch_keycloak_state] → propose_diff → validate_diff → [apply_diff | abort] → format_response → END
```

Handles trigger: `client/{id}`.

- `classify_client`: queries Keycloak and the kagenti-operator to determine client type and retrieve `ClientInfo`. See **kagenti-operator dependency note** below.
- `analyze_agent` / `analyze_tool`: LLM node that produces a `ClientProvision` from `ClientInfo`. `analyze_agent` uses agent description + skill list; `analyze_tool` uses description only. Routing is a conditional edge on `ClientInfo.client_type`.
- `provision_client`: non-LLM node; calls `create_client_role` and `create_client_scope` for each entry in `ClientProvision`. Runs before `fetch_keycloak_state` so provisioned roles/scopes are visible in the snapshot.

> **kagenti-operator dependency:** `classify_client` queries the kagenti-operator API to retrieve the Agent Card (for `ClientType.agent`) or tool description (for `ClientType.tool`). The kagenti-operator API contract for this query is defined in a separate investigation.

### State schema

#### `BaseAgentState`

Shared by both graphs.

| Field | Type | Description |
|-------|------|-------------|
| `trigger` | `TriggerContext` | Endpoint type + entity ID |
| `realm` | `str` | Keycloak realm (from `KEYCLOAK_REALM`) |
| `policy_chunks` | `list[str]` | Policy text chunks from `aiac-policies` ChromaDB collection |
| `domain_knowledge_chunks` | `list[str]` | Org/business context chunks from `aiac-domain-knowledge`; `[]` when empty — non-fatal |
| `keycloak_snapshot` | `KeycloakSnapshot` | Scoped Keycloak data for this trigger |
| `proposed_diff` | `ProposedDiff \| None` | LLM output |
| `validation_errors` | `list[str]` | Errors from `validate_diff` |
| `applied` | `list[RoleAssignment]` | Executed assignments |
| `revoked` | `list[RoleAssignment]` | Executed revocations |
| `summary` | `str` | Human-readable explanation (from LLM `reasoning` field) |

#### `PolicyUpdateState`

Extends `BaseAgentState`. No additional fields.

#### `OnboardingState`

Extends `BaseAgentState`. Additional fields:

| Field | Type | Description |
|-------|------|-------------|
| `client_info` | `ClientInfo \| None` | Client type, description, and skills — populated by `classify_client` |
| `client_provision` | `ClientProvision \| None` | Roles and scopes to create — populated by `analyze_agent` or `analyze_tool` |

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

Client onboarding types:

```python
class ClientType(str, Enum):
    agent = "agent"
    tool = "tool"

class Skill(BaseModel):
    id: str
    name: str
    description: str

class ClientInfo(BaseModel):
    client_type: ClientType
    description: str
    skills: list[Skill] = []

class RoleDefinition(BaseModel):
    name: str
    description: str

class ScopeDefinition(BaseModel):
    name: str
    description: str

class ClientProvision(BaseModel):
    roles: list[RoleDefinition]
    scopes: list[ScopeDefinition]
    reasoning: str
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

**Analyze Agent prompt** (`analyze_agent` node):
- System message: provisioner role ("derive the minimal set of Keycloak roles and scopes for this agent from its description and skills").
- User message: `ClientInfo` rendered as structured text — description paragraph followed by a skill list (id, name, description per skill).

**Analyze Tool prompt** (`analyze_tool` node):
- System message: provisioner role ("derive the minimal set of Keycloak roles and scopes for this tool from its description").
- User message: `ClientInfo.description`.

All four prompt templates are defined as named constants in `prompts.py`: `PLANNER_SYSTEM`, `AUDITOR_SYSTEM`, `ANALYZE_AGENT_SYSTEM`, `ANALYZE_TOOL_SYSTEM`.

### `fetch_policy` and `fetch_domain_knowledge` — RAG query strings per trigger type

Both nodes use the same trigger-type-keyed query strings. The query is derived from the **trigger TYPE only** (not the entity ID).

| Trigger | ChromaDB similarity query |
|---------|--------------------------|
| `build` | `"all access control rules"` |
| `rebuild` | `"all access control rules"` |
| `user/{id}` | `"user role assignment rules"` |
| `realm-role/{id}` | `"realm role assignment rules"` |
| `client/{id}` | `"client access control rules"` |

- `fetch_policy` queries `aiac-policies` and stores results in `BaseAgentState.policy_chunks`.
- `fetch_domain_knowledge` queries `aiac-domain-knowledge` and stores results in `BaseAgentState.domain_knowledge_chunks`. Returns `[]` when the collection is empty — non-fatal, the agent continues with empty domain context.
- Both nodes share `UPSTREAM_MAX_RETRIES` (tenacity retry policy) and return HTTP 503 when ChromaDB is unavailable.

Number of results capped by `CHROMA_N_RESULTS` (default `10`), shared across both nodes.

### `fetch_keycloak_state` — Keycloak data scope per trigger type

For the `client/{id}` trigger, `fetch_keycloak_state` runs after `provision_client` in the Client Onboarding Graph so that freshly created roles and scopes are visible in the snapshot.

| Trigger | Fetches via `aiac.library.api` |
|---------|----------------------------------------|
| `build` | All users; all clients + their roles; all realm roles; all role mappings for all users |
| `rebuild` | All users; all clients + their roles; all realm roles; all role mappings for all users |
| `user/{id}` | That user's current role mappings; all clients + their roles |
| `realm-role/{id}` | All users; all realm roles |
| `client/{id}` | All users; that client's roles; all role mappings for all users |

### `validate_diff` — validation checks (binary abort on any failure)

1. **Existence check** — every `user_id`, `client_id`, `role_id` in the diff exists in `keycloak_snapshot`.
2. **Safety guard rails** — total changes (`assign` + `revoke`) ≤ `MAX_CHANGES_PER_RUN`.
3. **LLM re-confirmation** — second LLM call (same `ChatOpenAI` instance, auditor system prompt) with diff + policy chunks; returns `ValidationVerdict(approved: bool, reason: str)` via `with_structured_output`.
4. **Scope validation** — diff is bounded to entities referenced by the trigger; no over-reach on partial updates.

## Endpoints

| Method | Path | Description | Graph |
|--------|------|-------------|-------|
| POST | `/apply/build` | Full diff-only rebuild — compute diff against live Keycloak state, apply only required changes | Policy update |
| POST | `/apply/rebuild` | Full nuke + rebuild — clear all role assignments, then recompute and apply from scratch | Policy update |
| POST | `/apply/user/{user_id}` | Recompute and apply mappings affected by a user addition or removal | Policy update |
| POST | `/apply/realm-role/{role_id}` | Recompute and apply mappings affected by a realm role addition or removal | Policy update |
| POST | `/apply/client/{client_id}` | Classify and provision a new client, then recompute and apply access mappings | Onboarding |

Success response (policy update graph):

```json
{ "applied": [...], "revoked": [...], "summary": "...", "provisioned": null }
```

Success response (onboarding graph):

```json
{ "applied": [...], "revoked": [...], "summary": "...", "provisioned": { "roles": [...], "scopes": [...] } }
```

Abort response (validation failure, both graphs):

```json
{ "applied": [], "revoked": [], "summary": "...", "validation_errors": [...], "provisioned": null }
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
├── __init__.py              ← empty
├── service/
│   ├── __init__.py          ← empty
│   ├── main.py              ← FastAPI app factory + uvicorn entrypoint
│   ├── routes.py            ← five /apply/* route handlers
│   ├── Dockerfile           ← Docker image (build context: aiac/src/)
│   └── requirements.txt     ← Python dependencies
└── agent/
    ├── __init__.py          ← empty
    ├── state.py             ← BaseAgentState, PolicyUpdateState, OnboardingState,
    │                           TriggerContext, KeycloakSnapshot, ProposedDiff, RoleAssignment,
    │                           ValidationVerdict, ClientType, Skill, ClientInfo,
    │                           RoleDefinition, ScopeDefinition, ClientProvision
    ├── prompts.py           ← PLANNER_SYSTEM, AUDITOR_SYSTEM,
    │                           ANALYZE_AGENT_SYSTEM, ANALYZE_TOOL_SYSTEM
    ├── nodes_shared.py      ← fetch_policy, fetch_domain_knowledge, fetch_keycloak_state,
    │                           propose_diff, validate_diff, apply_diff, format_response
    ├── nodes_policy.py      ← clear_assignments
    ├── nodes_onboarding.py  ← classify_client, analyze_agent, analyze_tool, provision_client
    ├── onboarding_graph.py  ← Client Onboarding StateGraph singleton + run_client helper
    └── policy_update_graph.py ← Policy Update StateGraph singleton + run_build, run_rebuild,
                                  run_user, run_realm_role helpers
```

Docker build command (run from repo root):

```bash
docker build -f aiac/src/aiac/agent/service/Dockerfile \
             -t aiac-agent:latest \
             aiac/src/
```

## Error handling

All upstream calls (`fetch_policy`, `fetch_keycloak_state`, `propose_diff`, `validate_diff`, `classify_client`, `provision_client`) are retried up to `UPSTREAM_MAX_RETRIES` times with exponential backoff (`tenacity`) before propagating the error.

| Upstream | HTTP status on final failure |
|----------|------------------------------|
| ChromaDB | `503 Service Unavailable` |
| Keycloak Configuration Service | `502 Bad Gateway` |
| kagenti-operator | `502 Bad Gateway` |
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
