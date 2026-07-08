# Component Sub-PRD: UC1 — Service Onboarding

> **Depends on:** [`../aiac-agent.md`](../aiac-agent.md) — NATS Consumer, Controller, Shared Module, Configuration, Error Handling, Runtime.

## Triggers

| Source | Subject / Path |
|---|---|
| Event Broker (NATS) | `aiac.apply.service.{id}` (originated by Keycloak SPI `CLIENT_CREATED`) |
| HTTP (debug) | `POST /apply/service/{service_id}` |

## Architecture overview

UC1 is the only use case with an Orchestrator, because it is a two-stage pipeline:

1. **Service Provision** (LLM-based): classify the new service, derive its roles + scopes, write them into the IdP.
2. **Service Policy** (deterministic): read the full IdP role + scope universe (excluding the new service's own entities), package into `list[tuple]`, return to the Orchestrator.

The Orchestrator returns `(list[PolicyRule], override=False)` to the Controller. The Controller calls the PCE with that `override` flag; the PCE owns all rule reconciliation. UC1 is **incremental** — existing roles receive a partial new mapping and must not lose their other access — so the mode is always append (`override=False`).

```mermaid
flowchart TD
    NATS["Event Broker\nNATS JetStream\naiac.apply.service.{id}"]
    NATS_CONSUMER["NATS Consumer\nasyncio background task\nthin adapter"]
    TRIGGERS["HTTP Triggers\nPOST /apply/service/{service_id}\n(debug)"]
    CTRL["Controller\nroutes.py"]

    NATS -->|"durable queue group\naiac-agent-consumer"| NATS_CONSUMER
    NATS_CONSUMER -->|"calls internal handler"| CTRL
    TRIGGERS --> CTRL

    subgraph CO["Service Onboarding"]
        ORC["Orchestrator"]
        SA_PROV["Service Provision\n(LLM)"]
        SA_POL["Service Policy\n(deterministic)"]
        ORC --> SA_PROV
        ORC --> SA_POL
    end

    PRB["Policy Rules Builder (shared)\nagent/policy_rules_builder/"]
    PCE["Policy Computation Engine\naiac.policy.computation\ncompute_and_apply(merged_rules, override)"]

    CTRL -->|"service/:id"| ORC
    SA_POL -->|"calls"| PRB
    ORC -->|"(list[PolicyRule], override=False)"| CTRL
    CTRL -->|"merged rules, override=False"| PCE
```

## Orchestrator

`onboarding/orchestrator.py`

**Sequence:**
1. Call `ServiceProvisionGraph.invoke()` → get back `ServiceProvision { roles, scopes }` + `service_type`.
2. Call `ServicePolicyUpdate.run(service_provision, service_type)` → get back `list[PolicyRule]`.
3. Return `(list[PolicyRule], override=False)` to the Controller.

No LLM calls, retry logic, or response assembly in the Orchestrator beyond sequencing.

**Replay safety (at-least-once delivery):** Service Provision IdP writes are **idempotent** (create-or-get by name: `create_service_role` / `create_service_scope` return the existing entity on a duplicate call). The PCE reconcile is also idempotent. If the pod crashes between Service Provision completing and the PCE call, NATS redelivers and the full pipeline re-runs safely to convergence. There is **no rollback logic**.

---

## Sub-agent: Service Provision

`onboarding/provision/`

**Nature:** LLM-based. Classifies the new service (agent or tool), derives roles + scopes from AgentCard / MCP manifest, and **writes them into the IdP**.

All IdP writes and reads target **`aiac.idp.configuration.api`**:
- `create_service_role(service_id, role)` — idempotent (create-or-get)
- `create_service_scope(service_id, scope)` — idempotent (create-or-get)

### Graph

```
START → classify_service → [analyze_agent | analyze_tool] → provision_service → END
```

### Nodes

- **`classify_service`**: determines service type.
  1. Store `service_id = trigger.entity_id` (Keycloak `client_id`).
  2. Check format:
     - **SPIFFE format** `spiffe://{domain}/ns/{namespace}/sa/{serviceAccount}` → extract `namespace` and `workload_name = serviceAccount`; continue to step 3.
     - **Any other format** → `service_type = tool`; `namespace = None`; `workload_name = None`; route to `analyze_tool`. No K8s access.
  3. LIST pods in `namespace`, find one whose `spec.serviceAccountName == workload_name`. Returns `502` on Kubernetes API failure or pod not found.
  4. Validate `kagenti.io/type` label on the pod (applied by kagenti-operator):
     - `agent` → `service_type = agent`; route to `analyze_agent`.
     - Absent or any other value → `502` (inconsistent deployment).

  > K8s access: `list` on `pods` in the target namespace. Agent path only.
  > `kagenti.io/type` is authoritative — applied exclusively by the kagenti-operator admission webhook.

- **`analyze_agent`**: non-LLM node; reads AgentCard CR.
  1. LIST `AgentCard` CRs (`agent.kagenti.dev/v1alpha1`) in `namespace`; find the one matching `workload_name`.
  2. **AgentCard found** → produce `ServiceProvision`:
     - `roles`: `[RoleDefinition(name=f"{workloadName}.agent", description="Agent role")]`
     - `scopes`: `[ScopeDefinition(name=f"{workloadName}.{skill.name}", description=skill.description) for skill in card.skills]`
     - `reasoning`: `f"derived from AgentCard: {len(skills)} skills"`
  3. **AgentCard not found** (legacy deployment) → produce minimal `ServiceProvision`:
     - `roles`: `[RoleDefinition(name=f"{workloadName}.agent", description="Agent role")]`
     - `scopes`: `[ScopeDefinition(name=f"{workloadName}.access", description="Default access scope")]`
     - `reasoning`: `"partial: no AgentCard found, default scope assigned"`

  > K8s access: `list` on `agentcards.agent.kagenti.dev` in the target namespace.

- **`analyze_tool`**: non-LLM node; discovers MCP tools.
  1. Resolve `workload_name`: call `get_service(service_id)` from `aiac.idp.configuration.api` → `workload_name = client.name`.
  2. Locate MCP endpoint: **TBD** — see issue [`inception/issues/6.2-analyze-tool-lookup-strategy.md`](../../issues/6.2-analyze-tool-lookup-strategy.md).
  3. Call `tools/list` (HTTP POST, MCP protocol) on the resolved endpoint.
  4. Produce `ServiceProvision`:
     - `roles`: `[]` (tools do not initiate further calls)
     - `scopes`: `[ScopeDefinition(name=f"{workload_name}.{tool.name}", description=tool.description) for tool in manifest.tools]`
     - `reasoning`: `f"derived from MCP manifest: {len(tools)} tools"`
  5. Returns `502` on config API failure, endpoint lookup failure, or MCP call failure.

  > K8s access: none. Tool path uses config API only (pending issue 6.2).
  > MCP path convention: all MCP tool services must serve at `/mcp`.

- **`provision_service`**: non-LLM node; calls `create_service_role` and `create_service_scope` from `aiac.idp.configuration.api` for each entry in `ServiceProvision`. Reads `service_id` from state. Writes are **idempotent** (create-or-get).
  - Also persists the discovered `service_type` onto the Keycloak client via `Configuration.set_service_type(service, service_type)`, which stores it as the **`client.type`** attribute. This is the **authoritative origin** of the attribute that the IdP library's `Service._resolve_keycloak_fields` reads back (see the IdP library spec's type-resolution precedence). Case must be normalized at this persistence step: `ServiceType` is lowercase (`agent`/`tool`) but `client.type` is capitalized (`Agent`/`Tool`) to match `Literal["Agent", "Tool"]` — map `agent`→`Agent`, `tool`→`Tool`.

### State: `OnboardingProvisionState`

Extends `BaseAgentState` with:

| Field | Type | Description |
|---|---|---|
| `service_id` | `str \| None` | Keycloak `client_id` = `trigger.entity_id` |
| `namespace` | `str \| None` | Parsed from SPIFFE URI; agents only |
| `workload_name` | `str \| None` | From SPIFFE URI (agents) or config API (tools) |
| `service_type` | `ServiceType \| None` | `agent` or `tool`; routing field |
| `service_provision` | `ServiceProvision \| None` | Populated by `analyze_agent` or `analyze_tool` |

### Types

```python
class ServiceType(str, Enum):
    agent = "agent"
    tool = "tool"

class RoleDefinition(BaseModel):
    name: str
    description: str

class ScopeDefinition(BaseModel):
    name: str
    description: str

class ServiceProvision(BaseModel):
    roles: list[RoleDefinition]
    scopes: list[ScopeDefinition]
    reasoning: str  # machine-generated provenance string
```

---

## Sub-agent: Service Policy

`onboarding/service_policy/`

**Nature:** deterministic IdP reader + PRB invoker.

**Purpose:** given the just-provisioned service's own roles + scopes (from Provision output), read the full IdP universe **excluding the new service's own entities**, call the PRB for each applicable (roles, scope) or (role, scopes) pair, and return a merged `list[PolicyRule]` to the Orchestrator.

The two call directions prevent self-mapping and keep each PRB call's semantic intent crisp:
- `build_scope_rules(other_roles, agent_scope)` = *who else may call this skill*
- `build_role_rules(agent_role, other_scopes)` = *what else may this role call* (agent path only)

### Steps

1. Receive `service_provision: ServiceProvision` + `service_type: ServiceType` from the Orchestrator.
2. Read **all roles** from `aiac.idp.configuration.api`, **excluding** the new service's own roles (i.e. exclude `role.name in {r.name for r in service_provision.roles}`).
3. Read **all scopes** from `aiac.idp.configuration.api`, **excluding** the new service's own scopes (i.e. exclude `scope.name in {s.name for s in service_provision.scopes}`).
4. **Flatten roles to their closure** before any PRB call, via the shared `flatten_role` helper (see [Composite role flattening](#composite-role-flattening)): expand `other_roles` into the union of every role's closure, de-duplicated by `role.id` (call this `flattened_other_roles`); on the agent path, also expand each of `service_provision.roles`.
5. Call PRB and merge:
   - **`service_type = tool`:** call `build_scope_rules(flattened_other_roles, scope)` for each scope in `service_provision.scopes`. Merge results into a single `list[PolicyRule]`.
   - **`service_type = agent`:** call `build_scope_rules(flattened_other_roles, scope)` for each scope in `service_provision.scopes`; for each role in `service_provision.roles`, call `build_role_rules(r, other_scopes)` **once per role `r` in that role's closure**. Merge all results into a single `list[PolicyRule]`.
6. Return the merged `list[PolicyRule]` to the Orchestrator. (The Orchestrator pairs it with `override=False` for the Controller — see [Architecture overview](#architecture-overview).)

**Note on "all relevant scopes":** relevance (which of `other_scopes` maps to each `agent_role`) is determined by the PRB, not here. This module always passes the full excluded-self scope universe; the PRB emits only the relevant rule mappings. See [`policy-rules-builder.md`](policy-rules-builder.md).

### Composite role flattening

Every role passed to the PRB is first flattened to its **closure** via the shared
`flatten_role` helper (aiac-agent Shared Module): recursively collect the role and all
descendant roles from `role.childRoles` into a flat list, de-duplicated by `role.id`
(`Role` is not hashable, so de-duplication tracks seen `id`s rather than adding `Role`
objects to a `set`). A non-composite role yields a list containing only itself. The PRB
therefore receives already-flattened roles, and the PCE performs no further flattening.

## File structure

```
aiac/src/aiac/agent/uc/
└── onboarding/
    ├── orchestrator.py
    ├── provision/
    │   ├── __init__.py
    │   ├── graph.py      ← ServiceProvisionGraph (LLM-based StateGraph)
    │   ├── nodes.py      ← classify_service, analyze_agent, analyze_tool, provision_service
    │   ├── state.py      ← OnboardingProvisionState
    │   └── types.py      ← ServiceType, RoleDefinition, ScopeDefinition, ServiceProvision
    └── service_policy/
        ├── __init__.py
        └── runner.py     ← ServicePolicyUpdate.run(service_provision, service_type) → list[PolicyRule]
```

## Out of scope

- PRB internals — see [`policy-rules-builder.md`](policy-rules-builder.md).
- PCE reconcile mechanics — see [`../policy-computation-engine.md`](../policy-computation-engine.md).
- Response body shape — no success body; handlers return bare HTTP status codes (error responses carry FastAPI's default JSON error body from the raised `HTTPException`). Summary + debug go to the log.
- MCP endpoint lookup strategy for tools — tracked in `inception/issues/6.2-analyze-tool-lookup-strategy.md`.
