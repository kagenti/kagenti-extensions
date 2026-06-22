# Component PRD: PDP Library (`aiac.pdp.library`)

**Phase 2 only.** These modules replace the Phase 1 `aiac.pdp.library.policy.*` submodules. They have no dependency on Keycloak — all IdP operations use `aiac.idp.library.configuration`.

## Location
`aiac/src/aiac/pdp/library/`

## Package structure

```
aiac/src/aiac/pdp/
├── __init__.py         # empty
└── library/
    ├── __init__.py     # empty
    ├── models.py       # PolicyRule, AgentPolicyModel, PolicyModel
    └── policy.py       # apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
```

All `__init__.py` files are empty. Callers use explicit submodule paths:

```python
from aiac.pdp.library.models import PolicyModel, AgentPolicyModel, PolicyRule
from aiac.pdp.library.policy import apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
```

---

## Submodule: `aiac.pdp.library.models`

### Description
Dependency-free Pydantic `BaseModel` subclasses for Phase 2 policy representation. Importable by any consumer without pulling in `requests` or `python-dotenv`. Consumed by the AIAC Agent's policy-producing sub-agents and the `aiac.pdp.library.policy` HTTP client.

**Replaces:** `aiac.pdp.library.policy.models` (Phase 1, which held a TBD `PolicyStatement` / `PolicyModel`).

### Dependencies
```
pydantic
```

### Pydantic models

All models use `model_config = ConfigDict(extra='ignore')`.

#### `PolicyRule`

A single access rule: a `(role, scope)` tuple. Used in both inbound and outbound rule sets.

| Field | Type |
|-------|------|
| `role` | `str` |
| `scope` | `str` |

#### `AgentPolicyModel`

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

#### `PolicyModel`

A partial or full system policy model. When sent to `POST /policy`, contains only the agents whose policies have changed.

| Field | Type |
|-------|------|
| `agents` | `list[AgentPolicyModel]` |

### Usage

```python
from aiac.pdp.library.models import PolicyModel, AgentPolicyModel, PolicyRule

rule = PolicyRule(role="weather-reader", scope="read")
agent_model = AgentPolicyModel(
    agent_id="weather-agent",
    agent_roles=["weather-reader"],
    agent_scopes=["read"],
    source_roles={"dashboard-agent": ["weather-reader"]},
    scope_targets={"read": ["weather-api"]},
    inbound_rules=[rule],
    outbound_rules=[PolicyRule(role="weather-reader", scope="read")],
)
model = PolicyModel(agents=[agent_model])
```

---

## Submodule: `aiac.pdp.library.policy`

### Description
HTTP client module wrapping the PDP Policy Service (OPA) REST API. Exposes four module-level functions. Service URL is read from the `AIAC_PDP_POLICY_URL` environment variable (default: `http://127.0.0.1:7072`). All functions raise `RuntimeError` on non-2xx response.

No `realm` parameter — the OPA service operates on a Kubernetes CR, not a Keycloak realm.

**Replaces:** `aiac.pdp.library.policy.api` (Phase 1, which had a realm-bound `Policy` class with a single `apply_policy` method).

### Dependencies
```
requests
pydantic
python-dotenv
```

### Functions

```python
def apply_policy(model: PolicyModel) -> None
    # POST /policy — upsert Rego packages for all agents in the partial model

def apply_agent_policy(agent_id: str, model: AgentPolicyModel) -> None
    # POST /policy/agents/{agent_id} — upsert Rego packages for a single agent

def delete_agent_policy(agent_id: str) -> None
    # DELETE /policy/agents/{agent_id} — remove all Rego packages for agent (off-boarding)

def delete_policy() -> None
    # DELETE /policy — clear all Rego packages (rebuild pre-step)
```

### Configuration

Read from `AIAC_PDP_POLICY_URL` environment variable (or `.env` file co-located with `policy.py`). Falls back to the default if absent.

| Variable | Default |
|----------|---------|
| `AIAC_PDP_POLICY_URL` | `http://127.0.0.1:7072` |

### Usage

```python
from aiac.pdp.library.policy import apply_policy, apply_agent_policy, delete_agent_policy, delete_policy
from aiac.pdp.library.models import PolicyModel, AgentPolicyModel, PolicyRule

# Single-agent update (service onboarding / role change)
apply_agent_policy("weather-agent", agent_model)

# Full rebuild pre-step: clear all, then reapply
delete_policy()
apply_policy(full_model)

# Off-boarding
delete_agent_policy("weather-agent")
```
