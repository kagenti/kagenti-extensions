# Expectation card — `<app-name>`

*Fill everything above **Results** BEFORE running the harness — the card states
what a correct run must produce; the run then confirms or deviates. One card
per app per validation run; copy this template to
`validation/<app-name>-<YYYY-MM-DD>.md`. Content-kind vocabulary = what the DG
`sidecar_interactions` processor derives (lab-data-governance
`derive.py` `_KIND_TABLE` + MCP lifecycle/discovery overrides).*

## Identity

| Field | Value |
|---|---|
| App | `<name>` (fleet.conf entry) |
| Entry protocol | `a2a` \| `mcp` |
| `SELF_ID` | `<self_id>` |
| Image ref | `docker.io/library/<name>-otel:latest` |
| Running image ID (record AT RUN TIME) | `<sha256:…>` |
| LLM wiring | `<env vars → endpoint/model>` \| none |

Record the running image ID **when you run**, not from the build log — agent
Deployments use `imagePullPolicy: Always` on `:latest`, so any pod roll can
silently pull a drifted upstream app:

```bash
kubectl get pod -n team1 -l app.kubernetes.io/name=<name> -o \
  jsonpath='{range .items[0].status.containerStatuses[*]}{.name}{"  "}{.imageID}{"\n"}{end}'
```

## Expected entities (kind + natural key)

| kind | natural_key | note |
|---|---|---|
| client | `client:<driver-pod-ip>` | anonymous caller (no JWT) — ip varies per run |
| agent \| tool | `agent:<self_id>` \| `tool:<self_id>` | the entry entity |
| agent | `agent:<self_id>` | MCP entry ONLY: bodyless http legs (SSE open / teardown) classify as inbound http, whose callee kind is `agent` — the same pod appears as BOTH `tool:` and `agent:`. Expected. |
| llm | `llm:host.containers.internal/qwen2.5:7b` | if LLM-wired |
| … | | downstream tools/services |

## Expected interactions per turn

One row per exchange. "hidden" = `mcp_lifecycle_*` / `tool_discovery_*`
content kinds the DG UI hides by default — expected, still first-class
interactions (an MCP entry also roots one interaction per session exchange —
see the harness header).

| leg (caller → callee) | dir/protocol | req / resp content_kind | count | hidden? |
|---|---|---|---|---|
| e.g. client → agent (entry) | inbound a2a | agent_request / agent_response | 1 | no |
| e.g. client → tool (initialize) | inbound mcp | mcp_lifecycle_request / mcp_lifecycle_result | 1 | yes |
| e.g. client → tool (tools/call) | inbound mcp | tool_call_arguments / tool_call_result | 1 | no |
| e.g. agent → llm | outbound inference | llm_chat_prompt / llm_completion | 1..k | no |
| e.g. client → agent (SSE open / teardown) | inbound http (bodyless on /mcp) | mcp_lifecycle_request / — (no payload rows) | 0..k | yes |

Totals per turn: `<T>` interactions = `<V>` visible + `<H>` hidden.

### `EXPECT_KINDS` line (derived content-kind counts)

Counts are **interaction legs**, read from the interactions API — the same
derivation the DG UI shows. So a bodyless exchange (SSE stream open, session
teardown) **does** count: it derives an interaction and a kind even though it
stores no payload. Count one entry per request leg and one per response leg,
and leave nondeterministic kinds (`llm_*` when the agent loops) unlisted.

Do not assert against `interaction_payloads.content_kind`: payloads are
content-addressed, so an agent that relays a body verbatim collapses two legs
into one row with one kind (see `dg-api.sh`, and the trivia card for live
evidence).

```bash
EXPECT_KINDS='tool_call_arguments=1,tool_call_result=1,mcp_lifecycle_request=2'
```

## Expected NO-interaction legs (capture gaps — expected, not failures)

| leg | why absent |
|---|---|
| e.g. agent → `api.frankfurter.dev` | HTTPS — TLS passthrough; the sidecar never sees plaintext (contract-documented) |
| e.g. wiki-service → git | local subprocess, no HTTP hop |

## Known quirks

- e.g. overlay pin, model-name allowlist, required fake env — see RUNBOOK
  troubleshooting table.

## Results

| Field | Value |
|---|---|
| Date / operator | |
| Harness + N | `concurrency-test-interactions.sh` \| `concurrency-test-mcp-interactions.sh`, N=6 |
| Trace ids | |
| Harness summary | `CLEAN FORESTS: ?/N   DISTINCT TRACES: ?/N` (+ root counts for MCP entry) |
| Deviations | each deviation: observed vs expected, and why |
| Verdict | **PASS** \| **FAIL** |
