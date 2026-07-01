# Component PRD: PDP Policy Writer (OPA)

## Location
`aiac/src/aiac/pdp/service/policy/opa/`

## Description
A FastAPI web service that translates a **Policy Model** into OPA Rego packages and writes them to an `AuthorizationPolicy` Kubernetes Custom Resource. The OPA plugin embedded in each AuthBridge instance fetches the Rego packages relevant to its pod from this CR at startup.

The service is deployed as a container in the **Kagenti Interface Pod** alongside the IdP Configuration Service, behind the `aiac-pdp-policy-writer-service:7072` ClusterIP.

The service has no dependency on Keycloak. All Keycloak operations (entity reads) are handled by the **IdP Configuration Service** and its library (`aiac.idp.library.configuration`).

---

## Pydantic models (`aiac.pdp.library.models`)

Dependency-free (only `pydantic`). Importable by any consumer without pulling in HTTP client dependencies.

All models use `model_config = ConfigDict(extra='ignore')`.

### `PolicyRule`

A single access rule: a `(role, scope)` tuple. Used in both inbound and outbound rule sets.

| Field | Type |
|-------|------|
| `role` | `str` |
| `scope` | `str` |

### `AgentPolicyModel`

Complete policy definition for a single agent (service). Contains two sets of `PolicyRule` entries plus supporting data maps used by the Rego packages.

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `str` | Service ID from the AIAC trigger event (`aiac.apply.service.{id}`) |
| `agent_roles` | `list[str]` | Realm roles assigned to this agent |
| `agent_scopes` | `list[str]` | Scopes this agent exposes |
| `source_roles` | `dict[str, list[str]]` | Maps inbound source service ID → list of realm roles |
| `scope_targets` | `dict[str, list[str]]` | Maps outbound scope → list of permitted target service IDs |
| `inbound_rules` | `list[PolicyRule]` | Who may call this agent: `(caller_role, requested_scope)` tuples |
| `outbound_rules` | `list[PolicyRule]` | What this agent may call: `(this_agent_role, requested_scope)` tuples |

**Inbound rule semantics:** a caller with realm role `role` is permitted to invoke this agent requesting scope `scope`.

**Outbound rule semantics:** this agent acting as realm role `role` is permitted to request scope `scope` on a target service.

### `PolicyModel`

A partial or full system policy model. When sent to the PDP Policy Writer, contains only the agents whose policies have changed.

| Field | Type |
|-------|------|
| `agents` | `list[AgentPolicyModel]` |

### Usage

```python
from aiac.pdp.library.models import PolicyModel, AgentPolicyModel, PolicyRule
```

---

## Endpoints

No `?realm=` parameter — the service operates on a Kubernetes CR, not a Keycloak realm.

| Method | Path | Body | Operation |
|--------|------|------|-----------|
| `POST` | `/policy` | `PolicyModel` | Upsert Rego packages for all agents in the partial model |
| `POST` | `/policy/agents/{agent_id}` | `AgentPolicyModel` | Upsert Rego packages for a single agent |
| `DELETE` | `/policy/agents/{agent_id}` | — | Remove all Rego packages for a specific agent (off-boarding) |
| `DELETE` | `/policy` | — | Clear all Rego packages from the CR (rebuild pre-step) |
| `GET` | `/health` | — | Readiness probe |

### Status codes

| Endpoint | Success | Error |
|----------|---------|-------|
| `POST /policy` | `204 No Content` | `502 Bad Gateway` with `{"error": "..."}` if CR write fails |
| `POST /policy/agents/{agent_id}` | `204 No Content` | `502 Bad Gateway` if CR write fails |
| `DELETE /policy/agents/{agent_id}` | `204 No Content` | `502 Bad Gateway` if CR write fails |
| `DELETE /policy` | `204 No Content` | `502 Bad Gateway` if CR write fails |
| `GET /health` | `200 OK` `{"status": "ok"}` | `503 Service Unavailable` if CR is unreachable |

---

## Rego package structure

For each `AgentPolicyModel`, the service generates **two Rego packages**: one for the inbound pipeline and one for the outbound pipeline. The `agent_id` is slugified for use in the package name (hyphens → underscores, lowercase).

### Inbound package: `authz.{agent_slug}.inbound`

Evaluated by the AuthBridge OPA plugin in the **inbound pipeline**. Input document: `{subject: str, source: str, scope: str}` where `subject` is the end-user ID, `source` is the calling service ID, and `scope` is the requested scope.

```rego
package authz.{agent_slug}.inbound

source_roles := {
    "{source_id}": ["{role}", ...],
    ...
}

default allow := false

allow if {
    some role in source_roles[input.source]
    role == "{rule.role}"
    input.scope == "{rule.scope}"
}
# ... one allow block per inbound PolicyRule
```

### Outbound package: `authz.{agent_slug}.outbound`

Evaluated by the AuthBridge OPA plugin in the **outbound pipeline**. Input document: `{subject: str, target: str, role: str, scope: str}` where `subject` is the end-user ID, `target` is the called service ID, `role` is this agent's own realm role, and `scope` is the requested scope.

```rego
package authz.{agent_slug}.outbound

scope_targets := {
    "{scope}": ["{target_id}", ...],
    ...
}

default allow := false

allow if {
    input.role == "{rule.role}"
    input.scope == "{rule.scope}"
    input.target in scope_targets["{rule.scope}"]
}
# ... one allow block per outbound PolicyRule
```

---

## Library: `aiac.pdp.library.policy`

HTTP client module wrapping the PDP Policy Writer REST API. Exposes four module-level functions. Service URL is read from the `AIAC_PDP_POLICY_URL` environment variable (default: `http://127.0.0.1:7072`). All functions raise `RuntimeError` on non-2xx response.

```python
def apply_policy(model: PolicyModel) -> None
    # POST /policy

def apply_agent_policy(agent_id: str, model: AgentPolicyModel) -> None
    # POST /policy/agents/{agent_id}

def delete_agent_policy(agent_id: str) -> None
    # DELETE /policy/agents/{agent_id}

def delete_policy() -> None
    # DELETE /policy
```

### Dependencies

```
requests
pydantic
python-dotenv
```

### Usage

```python
from aiac.pdp.library.policy import apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
from aiac.pdp.library.models import PolicyModel, AgentPolicyModel, PolicyRule

apply_agent_policy("weather-agent", agent_model)
delete_policy()
apply_policy(full_model)
```

---

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `AUTHORIZATION_POLICY_NAME` | TBD | Name of the `AuthorizationPolicy` CR to patch |
| `AUTHORIZATION_POLICY_NAMESPACE` | TBD | Namespace of the `AuthorizationPolicy` CR |

Authentication to the Kubernetes API: in-cluster service account (auto-detected by the `kubernetes` Python client). The pod's `ServiceAccount` must be bound to a `ClusterRole` granting `get`/`patch`/`update` on `AuthorizationPolicy` resources. The `ServiceAccount`, `ClusterRole`, and `ClusterRoleBinding` are declared in `pdp-interface-deployment.yaml`.

For local development, the `kubernetes` client falls back to `~/.kube/config` automatically.

> **Note:** `AuthorizationPolicy` CR schema and ConfigMap source for env vars are TBD.

---

## Runtime

- Framework: FastAPI
- Server: uvicorn
- Bind: `0.0.0.0:7072`
- Base image: `python:3.12-slim`
- Kubernetes ClusterIP Service: `aiac-pdp-policy-writer-service:7072`
- Deployment: co-located with IdP Configuration Service as a container in the **Kagenti Interface Pod** (`pdp-interface-deployment.yaml`)

---

## Dependencies (`requirements.txt`)

```
fastapi
uvicorn[standard]
kubernetes
pydantic
```

---

## File structure

```
aiac/src/aiac/pdp/service/
├── __init__.py
└── policy/
    ├── __init__.py
    └── opa/
        ├── __init__.py
        ├── Dockerfile
        ├── requirements.txt
        └── main.py

aiac/src/aiac/pdp/
├── __init__.py
└── library/
    ├── __init__.py
    ├── models.py       # PolicyRule, AgentPolicyModel, PolicyModel
    └── policy.py       # apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
```

Build command:
```bash
docker build -f aiac/src/aiac/pdp/service/policy/opa/Dockerfile \
  -t aiac-pdp-policy-opa:latest aiac/src/
```

---

## `main.py` behaviour notes

- Load Kubernetes in-cluster config at startup via `kubernetes.config.load_incluster_config()`; fall back to `kubernetes.config.load_kube_config()` for local development.
- Instantiate a `kubernetes.client.CustomObjectsApi` for all CR operations.
- `_slugify(agent_id: str) -> str`: replace hyphens with underscores, lowercase — produces a valid Rego package name segment.
- `_generate_inbound_rego(model: AgentPolicyModel) -> str`: render the inbound Rego package string from the model's `source_roles` map and `inbound_rules`.
- `_generate_outbound_rego(model: AgentPolicyModel) -> str`: render the outbound Rego package string from the model's `scope_targets` map and `outbound_rules`.
- `_upsert_agent(agent_id: str, inbound_rego: str, outbound_rego: str)`: patch the `AuthorizationPolicy` CR to upsert the two packages for `agent_id`. Schema TBD.
- `_delete_agent(agent_id: str)`: patch the CR to remove all packages for `agent_id`.
- `_delete_all()`: patch the CR to remove all packages.
- `POST /policy`: iterate `model.agents`; for each call `_generate_inbound_rego` + `_generate_outbound_rego` + `_upsert_agent`; return `Response(status_code=204)`.
- `POST /policy/agents/{agent_id}`: call `_generate_inbound_rego` + `_generate_outbound_rego` + `_upsert_agent`; return `Response(status_code=204)`.
- `DELETE /policy/agents/{agent_id}`: call `_delete_agent(agent_id)`; return `Response(status_code=204)`.
- `DELETE /policy`: call `_delete_all()`; return `Response(status_code=204)`.
- On Kubernetes API error, return HTTP 502 with `{"error": str(e)}`.
- `GET /health`: attempt to `get` the `AuthorizationPolicy` CR; return `200 {"status": "ok"}` on success, `503` on failure.
