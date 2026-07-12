# feat(kagenti-operator): stamp `protocol.kagenti.io/mcp` on MCP tool Services at deploy time

> **For:** Kagenti platform / kagenti-operator team.
> **Filed by:** AIAC (kagenti-extensions). This is a cross-repo dependency of AIAC Service Onboarding.

## Summary

AIAC Service Onboarding discovers an MCP tool's capabilities by calling `tools/list` on the tool's
MCP endpoint. It locates that endpoint by finding the tool's Kubernetes `Service` and confirming the
Service is an MCP server via the **`protocol.kagenti.io/mcp`** label. Today that label is applied
**manually** — the operator only *reads* `protocol.kagenti.io/*` and never *stamps* the MCP label —
so tool onboarding silently depends on deployment hygiene the platform does not enforce.

**Request:** have the platform apply `protocol.kagenti.io/mcp` automatically to MCP tool Services at
deploy time.

## Problem description

AIAC's `analyze_tool` node resolves a tool's MCP endpoint as
`http://{workload}.{namespace}.svc.cluster.local:{port}/mcp` and **gates** on the Service carrying
`protocol.kagenti.io/mcp` as the correctness signal that the Service is truly an MCP server (rather
than blindly POSTing `tools/list` to an arbitrary Service).

Current operator behavior:

- It applies **`kagenti.io/type`** (`agent`/`tool`) to workloads via the `AgentRuntime` CR
  (`internal/controller/agentruntime_controller.go`).
- It **reads** `protocol.kagenti.io/*` labels for protocol/agent-card discovery
  (`internal/controller/agentcard_controller.go`).
- It does **not** stamp `protocol.kagenti.io/mcp` anywhere.

Consequence: a tool can have `kagenti.io/type=tool` **and** an operator-registered Keycloak client
(both operator-managed) yet be missing the MCP label — so AIAC onboarding fails with a `502` until
someone runs `kubectl label` by hand. This was exactly the manual step required to onboard
`github-tool-mcp` in the `team1` namespace during AIAC bring-up.

## Proposed change

Apply `protocol.kagenti.io/mcp` to a tool's `Service` automatically at deploy time. Reasonable
homes (team's choice):

- the tool Helm chart / deployment template, or
- the `AgentRuntime` reconcile path when the workload is `kagenti.io/type=tool`, or
- the operator when it manages a tool workload's Service.

Keep the existing **presence/empty-string** semantics (the label value is `""`; consumers match on
key presence, not `=true`).

## Acceptance criteria

- [ ] MCP tool Services carry `protocol.kagenti.io/mcp` **without** a manual `kubectl label` step.
- [ ] Where/how the label is applied is documented.
- [ ] Existing manually-labelled Services (e.g. `github-tool-mcp`) remain compatible (idempotent).

## Context / references

- AIAC design — hybrid MCP endpoint lookup and the label gate: `analyze_tool` node in the AIAC UC1
  Service Onboarding spec; AIAC issue 6.2 (`analyze_tool` service lookup strategy).
- Operator today: reads `protocol.kagenti.io/*` (`internal/controller/agentcard_controller.go`);
  applies `kagenti.io/type` via `AgentRuntime` (`internal/controller/agentruntime_controller.go`);
  no MCP-label stamping.
- AIAC MCP-discovery handoff, open item #1 ("automate label stamping").
