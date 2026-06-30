# Component Sub-PRD: UC2 — Policy Update

> **Status: TBD.** The internal design of the Build and Rebuild sub-agents is not yet defined. A dedicated grill session is required.

> **Depends on:** [`../aiac-agent.md`](../aiac-agent.md) — NATS Consumer, Controller, Shared Module, Configuration, Error Handling, Runtime.

## Triggers

| Source | Subject / Path |
|---|---|
| Event Broker (NATS) | `aiac.apply.policy.build` (originated by RAG Ingest Service post-ingest) |
| HTTP (debug / operator) | `POST /apply/policy/build` |
| HTTP (operator only) | `POST /apply/policy/rebuild` (not routed through Event Broker) |

## Architecture

```mermaid
flowchart TD
    NATS["Event Broker\nNATS JetStream\naiac.apply.policy.build"]
    NATS_CONSUMER["NATS Consumer\nasyncio background task\nthin adapter"]
    TRIGGERS["HTTP Triggers\nPOST /apply/policy/build\nPOST /apply/policy/rebuild\n(debug / operator)"]
    CTRL["Controller\nroutes.py"]

    NATS -->|"durable queue group\naiac-agent-consumer"| NATS_CONSUMER
    NATS_CONSUMER -->|"calls internal handler"| CTRL
    TRIGGERS --> CTRL

    subgraph PU["Policy Update (TBD)"]
        SA_BUILD["Build sub-agent\n(TBD)"]
        SA_REBUILD["Rebuild sub-agent\n(TBD)"]
    end

    PRB["Policy Rules Builder (shared)\nagent/policy_rules_builder/"]
    PCE["Policy Computation Engine\naiac.policy.computation\ncompute_and_apply(merged_rules)"]

    SA_REBUILD -->|"delegates"| SA_BUILD
    SA_BUILD -->|"calls"| PRB

    CTRL -->|"build"| SA_BUILD
    CTRL -->|"rebuild"| SA_REBUILD
    SA_BUILD -->|"list[PolicyRule]"| CTRL
    SA_REBUILD -->|"list[PolicyRule]"| CTRL
    CTRL -->|"merged rules"| PCE
```

## What is known

- **Two sub-agents:** Build (responds to `aiac.apply.policy.build` + `POST /apply/policy/build`) and Rebuild (responds to `POST /apply/policy/rebuild` only).
- Build calls the PRB directly, merges the results, and returns `list[PolicyRule]` to the Controller.
- Rebuild delegates to Build and returns Build's `list[PolicyRule]` to the Controller.
- The Controller calls `compute_and_apply(merged_rules)` via the PCE — the same pattern as all other UCs.
- Internal behavior (how Build/Rebuild sub-agents derive their tuple content, what IdP data they read, whether any LLM node is involved) is **deferred** — to be resolved in a dedicated grill session.

## Out of scope (this stub)

- Build sub-agent internal design.
- Rebuild sub-agent internal design.
- PRB internals — see [`policy-rules-builder.md`](policy-rules-builder.md).
- PCE reconcile mechanics — see [`../policy-computation-engine.md`](../policy-computation-engine.md).
- Response body shape — no response bodies; handlers return bare HTTP status codes. Summary + debug go to the log.
