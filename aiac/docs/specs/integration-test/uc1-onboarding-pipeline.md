# Integration Test: uc1-onboarding-pipeline — a ladder of UC-1 onboarding tests

> **One spec among several.** This document specifies the **UC-1 onboarding** integration tests.
> Integration-test specs live under `docs/specs/integration-test/` (a sibling of `components/`), indexed
> by the master PRD's *Integration test specifications* section ([../PRD.md](../PRD.md)). This is the
> phase-1 service-onboarding demo driven end-to-end through the **real UC-1 agent** against
> **really-deployed** demo workloads, and **enforced by the deployed AuthBridge OPA plugin** — not the
> definition of integration testing in general.

> **Ladder, not one test.** This spec was previously a single "complete two-policy" test that assumed a
> **two-stack** topology (one AIAC stack per `policy.md` variant) which is **not deployed** and so could
> never run. It is now a **ladder** of three gradual, runnable tests against **one** AIAC stack, plus a
> **deferred** two-policy rung:
>
> | Rung | Issue | Onboards | Proves |
> |---|---|---|---|
> | 1 | `testing/5.4.1-uc1-onboard-agent-only.md` | agent only | agent discovery + inbound enforcement stand alone; outbound empty (all deny) with no tool |
> | 2 | `testing/5.4.2-uc1-onboard-agent-then-tool.md` | agent → tool | onboarding the tool **after** the agent completes the agent's outbound gate (PCE additive merge) |
> | 3 | `testing/5.4.3-uc1-onboard-tool-then-agent.md` | tool → agent | the happy path; **and, vs rung 2, onboarding-order-independence** |
> | 4 | `testing/5.4.4-uc1-onboard-two-policies.md` | two policies | **deferred / TBD**; two-stack impl discarded |

> **Relationship to `policy-pipeline`.** This is the **onboarding-order-focused sibling** of
> [policy-pipeline.md](policy-pipeline.md). Identical *scenario facts and truth tables* (same three users,
> same role→access facts, same inbound/outbound matrices) and the **same** live enforcement loop — both
> onboard through the real in-cluster UC-1 Controller and assert the **deployed OPA plugin's** allow/deny
> over real HTTP through AuthBridge. `policy-pipeline` is the **full happy-path matrix + negative
> controls** over the fully onboarded stack; this ladder **isolates onboarding-order properties** across
> three rungs (agent-only; agent→tool; tool→agent + order-independence).

## Location

`aiac/test/integration/` — pytest modules marked `@pytest.mark.integration`, one per rung
(`test_uc1_onboard_agent_only.py`, `test_uc1_onboard_agent_then_tool.py`,
`test_uc1_onboard_tool_then_agent.py`). Each is a thin module that wraps the shared harness in a
one-line session fixture and supplies only its own rung's oracle (verdicts computed from
`scenario_uc1.py`) and live assertions. They import three shared modules:

- `scenario_uc1.py` — the pure-data scenario (users/roles + the pair-lists expressed over the
  **discovered, workload-prefixed** names `github-tool.source-read`, `github-agent.source_operations`, …,
  plus the **bare** runtime names `source-read` the oracle keys on). The old two-variant machinery
  (`VARIANTS`, `POLICY_EXPLICIT`, per-variant URLs/pods) is gone; the truth tables (`USERS`,
  `USER_ROLES`, `INBOUND_PAIRS`, `OUTBOUND_SUBJECT_PAIRS`, `OUTBOUND_TARGET_PAIRS`, `TOOL_SCOPES`,
  `AGENT_SCOPES`, `TOOL_REQUEST_NAMES`) and the single **abstract** `policy.md` remain.
- `uc1_onboard.py` — the shared live harness: config, Keycloak provisioning/cleanup
  (`provision_realm_and_users` / `resolve_service_id` / `cleanup_provisioned` / `clear_policy_store`),
  the onboard trigger (`onboard`), the outbound token-exchange-leg prep (`ensure_github_tool_route` /
  `grant_exchange_scope` / `restart_agent`), the bundle-convergence poll, the live decision oracle
  (`expected_inbound` / `expected_outbound_bare`, `inbound_decision` / `outbound_decision`), and
  `onboarded_stack(workloads)` — the whole per-rung fixture flow parameterised by the ordered workload
  list.
- `launcher.py` — the shared live-cluster half: `kubectl` wrappers, `port_forward`, `resolve_pod`,
  `mint_token`, `jwt_claim`, `inbound_probe` / `outbound_probe`, `inbound_outcome` / `outbound_outcome`
  (OPA denial classified by body, not status), `poll_until`, and the skip gates (`require_pipeline`,
  `require_env_or_skip`, `verify_subject_mapper`). There is **no** `opa_eval`, no `kubectl_cp` of
  `/rego`, and no standalone probe module: the evaluator is the deployed AuthBridge OPA plugin, and the
  input documents are built by AuthBridge's own parsers.

## Description

`@pytest.mark.integration` tests that validate the **phase-1 deliverable** and confirm the runnable demo:
they drive the **real UC-1 Service Onboarding agent** (`POST /apply/service/{id}` on the in-cluster AIAC
Controller, which upserts the `AuthorizationPolicy` CR on the live Kubernetes API) against
**already-deployed** `github-agent` + simplified `github-tool`, and then assert the **enforced decision**
is correct by driving **real HTTP requests through AuthBridge** and reading the **deployed OPA plugin's**
allow/deny.

Live enforcement is now **in scope and is the whole point**: each rung onboards, enables the outbound
token-exchange leg where a tool is present (Part B), waits for `bundle-service` + the AuthBridge OPA
sidecars to recompose and reload the bundle, then drives real requests through AuthBridge on both legs
(`jwt-validation` builds `input.identity` inbound; `token-exchange` + `mcp-parser` build the outbound
`input.identity` + `input.mcp.params.name`). The agent's own CrewAI reasoning flow is **not** triggered —
the probes are synthetic requests through AuthBridge (an inbound `ping` / `nonexistent`; an outbound bare
`tools/call`) — but the traffic is real and the deployed plugin enforces it.

The enforced decision is the **artifact under test** — the LLM/PCE that produced the policy might be
wrong — so the tests never trust it. Expected verdicts are **computed from** the `scenario_uc1.py`
pair-lists (the intended policy), keyed on the **bare** runtime tool names AuthBridge sends. A mismatch
fails the test and names the exact cell.

Because they need a live rossoctl/Kind cluster with the AuthBridge OPA pipeline wired into both legs +
operator + Keycloak + a real LLM, they are `@pytest.mark.integration` (out of the default unit run,
`-m "not integration"`) and **skip cleanly** when the cluster/pipeline is not wired or the env is unset
(they never false-pass).

## Topology

- **One in-cluster AIAC stack + the deployed AuthBridge OPA pipeline.** A single AIAC agent (Controller,
  `POST /apply/service/{id}`) + Policy Model Store + **OPA Policy Writer**, mounting the **single
  abstract** `policy.md`. AIAC runs in-cluster so UC-1's `analyze_tool` can reach the tool's MCP endpoint
  at its cluster-internal DNS name (`github-tool.{ns}.svc.cluster.local`); the tests trigger the
  Controller over `kubectl port-forward`.
- **The deployed OPA plugin is the evaluator.** Onboarding upserts the agent's `AuthorizationPolicy` CR
  on the live Kubernetes API; `bundle-service` (in `rossoctl-system`) recomposes the namespace bundle,
  and each workload pod's AuthBridge OPA sidecar polls + reloads it (~20–30 s). There is **no** `/rego`
  dump and **no** `kubectl cp` — the artifact under test is the enforced decision, not a file.
- **Convergence by polling real decisions.** After the CR is upserted (and, for the outbound leg, after
  Part B + the agent restart), `onboarded_stack` polls real requests through AuthBridge until this run's
  policy is reflected in the plugin's decisions, up to `AIAC_BUNDLE_TIMEOUT`.

## Preconditions (assumed, not performed by the tests)

- **Pipeline wired.** The AuthBridge OPA plugin is wired into both legs (`k8s/opa-kind-enable.sh`);
  `require_pipeline` skips cleanly if not (no `kubectl`, `AuthorizationPolicy` CRD not served,
  `bundle-service` not Running, the `opa` plugin not present on both legs, or a workload pod not Running).
- **Workloads deployed + registered.** Both `github-agent` and simplified `github-tool` are **already
  deployed** in `AIAC_DEMO_NAMESPACE` and **already registered as Keycloak clients**
  (`client.name = "{ns}/{workload}"`) into `AIAC_TEST_REALM`. The tests do **not** `kubectl apply`
  manifests or wait for operator registration / `rossoctl.io/type` labels / AgentCard / `tools/list` — that
  is deployment's job.
  > **Resolving `{service_id}`.** The `POST /apply/service/{service_id}` route is a **single path
  > segment**, and the Controller resolves the trigger via `admin.get_client(service_id)` — which keys
  > on the Keycloak **internal client UUID** (a slash-free GUID). It is **not** the `clientId`: the
  > operator sets `client.name = "{ns}/{workload}"`, and the `clientId` is slash-bearing either way
  > (`"{ns}/{workload}"` with SPIRE off, a SPIFFE URI under `--spire-trust-domain`), so it cannot be a
  > path segment. Resolve by looking up the client whose **name** is `"{ns}/github-tool"` /
  > `"{ns}/github-agent"`, then trigger with that client's **`id`** (the UUID) — `resolve_service_id`.
- **Users + realm roles.** The fixture provisions them (UC-1 does not) — see
  *[Scenario](#scenario)* — via `KeycloakAdmin` into `AIAC_TEST_REALM`, **before** onboarding;
  idempotent; left in place. `verify_subject_mapper` confirms the realm's `username → sub` mapper +
  Direct Access Grants (else skip).

## Per-rung flow

**Keycloak cleanup + policy-store clear → onboard (rung order) → enable outbound leg → poll bundle →
drive real requests + assert → Keycloak cleanup + CR delete.**

1. **Cleanup** (before and after each rung, all before any assertion). `cleanup_provisioned` deletes the
   **agent's and tool's** provisioned realm roles + client scopes (leaving the clients registered exactly
   as before the first run), delete the agent's `AuthorizationPolicy` CR, and `clear_policy_store` drops
   persisted SPMs from the in-cluster Policy Store (whose SQLite outlives redeploys, so pre-fix cruft
   would otherwise accumulate — onboarding appends with `override=False`). This gives every rung a clean
   slate and makes reruns converge. Then `provision_realm_and_users` (idempotent) + `ensure_agent_policy`
   (mount the abstract `policy.md` on the Controller pod).
2. **Onboard** in the rung's order via `POST /apply/service/{service_id}`, where `{service_id}` is the
   internal Keycloak UUID (`resolve_service_id`), **not** the clientId.
   - `POST /apply/service/{github-tool id}` → UC-1 classifies it a **Tool**, reads the MCP manifest,
     provisions scopes `github-tool.{source-read, source-write, issues-read, issues-write}`, sets
     `client.type=Tool`. **No rules are written for the tool directly.**
   - `POST /apply/service/{github-agent id}` → UC-1 classifies it an **Agent**, reads the AgentCard,
     provisions **one operator role per skill** `github-agent.{source_operations, issue_operations}`
     (mirroring the scopes) + scopes `github-agent.{source_operations, issue_operations}`, sets
     `client.type=Agent`; the Service Policy Builder maps roles→scopes via the real PRB (real LLM,
     `temperature=0`) and the Controller calls `compute_and_apply(rules, override=False)`; the OPA Policy
     Writer upserts the agent's `AuthorizationPolicy` CR.
3. **Enable the outbound token-exchange leg (Part B)** — only meaningful when a tool is onboarded (rungs 2
   and 3). `ensure_github_tool_route` adds the `github-tool` outbound route to `authproxy-routes`,
   `grant_exchange_scope` grants the agent's client the `github-tool` audience scope as optional, and
   `restart_agent` restarts the agent so it reloads the route (and its OPA sidecar re-fetches the
   recomposed bundle). Without this the outbound call passes through unexchanged and never reaches OPA.
4. **Poll until the pipeline converges.** `poll_until` drives real decisions until this run's CR is
   reflected (inbound `dev-user` allow, `devops-user` deny; outbound `dev-user` `source-read` at its
   terminal verdict), waiting out the bundle poll + post-restart token-exchange window.
5. **Validate two outcomes at the end** (no intermediate checks):
   1. **Keycloak provisioning.** The expected realm role(s) + client scopes exist with the expected
      names/descriptions (via `KeycloakAdmin`) — and, for rung 1, that **no** tool scopes were provisioned.
   2. **Enforced decisions.** Drive **real HTTP requests through AuthBridge** and read the **deployed OPA
      plugin's** allow/deny:
      - **Inbound** — per `subject`, `inbound_decision` (200 → `allow`, 403 → `deny`); expected from
        `expected_inbound`.
      - **Outbound (per-scope two-gate AND)** — per `(subject × bare tool name)`, a real MCP `tools/call`
        for the **bare** tool through AuthBridge's forward proxy (`outbound_decision`); a denial is a
        JSON-RPC error frame (`error.data.plugin: "opa"`) at HTTP 200 that the harness classifies as
        `deny`. Expected from `expected_outbound_bare` — allowed iff the subject **and** some agent role
        both reach that tool's scope.
      - Verdicts are **computed from** `scenario_uc1.py`, never from the policy. A failing node names the
        exact cell.
6. **Cleanup** — restore the clients to their pre-run state and delete this run's CR.

## Onboarding order is irrelevant (rungs 2 vs 3)

The **final** enforced policy must not depend on the order services are onboarded. This is a
**requirement**: if onboarding order changes the end state, that is a **bug** the ladder exists to catch —
not an accepted difference. Rung 3 (tool → agent) is the **live counterpart of the PCE's
order-independence unit test (8.11)** and the exact repro of the original order-dependence bug: under the
old APM-only design, tool-then-agent **lost** the outbound gate.

Why it holds: `compute_and_apply` is **affected-agent** oriented and **additive** (`override=False`, see
[../components/policy-computation-engine.md](../components/policy-computation-engine.md)). When the **tool**
is onboarded, its Service Policy Builder pairs the tool's scopes against the rest of the role universe,
producing `(agent-role, tool-scope)` and `(user-role, tool-scope)` rules; the PCE resolves those roles to
the **agent** and merges them onto the agent's stored `AgentPolicyModel`, re-upserting the agent's
`AuthorizationPolicy` CR. So:

- **Rung 2 (agent → tool):** agent onboarding leaves the outbound gate empty; **tool onboarding fills it
  in**.
- **Rung 3 (tool → agent):** the tool's scopes already exist, so **agent onboarding produces the full
  gate** in one pass.
- **Both converge** to the same enforced decisions. Rung 3 asserts, at the oracle level, that its intended
  end state is **identical** to rung 2's published expectations (`RUNG3_* == RUNG2_*`), then proves the
  **real plugin's decisions** match that in the tool→agent order — so onboarding order did not change what
  is enforced.

Rung 1 (agent only) is the exception by construction: with no tool onboarded there are no tool scopes in
the universe, so the outbound user gate is **empty** (all deny). Inbound is unaffected.

## Expected output

Verdicts are **computed from** the `scenario_uc1.py` pair-lists (these tables are the human-readable
rendering). They are **identical to policy-pipeline's** and to what the deployed OPA plugin enforces.

`USERS`: `dev-user`→`developer`, `test-user`→`tester`, `devops-user`→`devops`.

**Inbound allow** (the real plugin's inbound decision; all rungs):

| Subject | Inbound |
|---|---|
| dev-user | ✅ |
| test-user | ✅ |
| devops-user | ❌ |

**Outbound allow(subject, tool)** (the real plugin's outbound decision, per-scope two-gate AND over the
**bare** tool names; the agent reaches all four tool scopes, so the user gate discriminates) — **rungs 2
and 3** (with a tool onboarded):

| | source-read | source-write | issues-read | issues-write |
|---|---|---|---|---|
| dev-user | ✅ | ✅ | ✅ | ❌ |
| test-user | ❌ | ❌ | ✅ | ✅ |
| devops-user | ❌ | ❌ | ❌ | ❌ |

**Rung 1 (agent only):** the outbound table is **entirely deny** (empty user gate — no tool scopes).

The pipeline emits an agent `AuthorizationPolicy` CR only — explicitly **no** tool CR (the tool is a pure
target; "no rules written for the tool alone"). Each rung also asserts the expected Keycloak provisioning
end state (agent roles/scopes with the expected descriptions; rung 1 additionally asserts **no** tool
scopes exist).

### Prefixed provisioned names vs. bare runtime names

UC-1 names every scope `{workload}.{name}`, so what it **provisions** into Keycloak (and what the oracle's
grant-set constants hold) is **workload-prefixed** — `github-tool.source-read`,
`github-agent.source_operations`. But the request AuthBridge actually sends, and the name the OPA plugin
compares against, is the **bare** runtime tool name (`source-read`) that `mcp-parser` puts in
`input.mcp.params.name`. So the live oracle keys decisions on the **bare** names
(`expected_outbound_bare` / `outbound_decision`); the two naming registers meet in `scenario_uc1.py`
(prefixed provisioned truth + a `bare()` de-prefixer). The enforced decisions are therefore identical to
`policy-pipeline`'s — both share the same harness and enforce over the same bare names.

### The agent→tool gate (capability-matched)

Phase-1 states outbound access is the **per-scope intersection** of the user→tool gate and the
agent→tool gate. UC-1 provisions **one operator role per skill**
(`github-agent.source_operations` / `github-agent.issue_operations`), and the PRB maps those operator
roles to the tool scopes by domain (capability-match under `generic_policy.md`), so the agent's capability
gate is **populated over all four tool scopes**. Because the agent reaches every tool scope, the **user
gate discriminates** — the plugin enforces the real per-scope AND (subject gate AND capability gate on the
same `input.mcp.params.name`) and, for this scenario, its verdicts equal the user-gate slice. The AND is
genuine, not degenerate: if the agent reached only a subset of the tool's scopes, the request would be
denied for the scopes it does not reach.

## Scenario

Identical role→access facts to `policy-pipeline`, driven through real UC-1 onboarding of deployed
workloads and enforced by the deployed OPA plugin.

| Element | Value |
|---------|-------|
| Realm | `AIAC_TEST_REALM` (must match the deployed stack's `KEYCLOAK_REALM`; default `rossoctl`) |
| Agent | `github-agent` — **discovered** per-skill operator roles `github-agent.source_operations`, `github-agent.issue_operations` (mirroring the scopes); scopes `github-agent.source_operations`, `github-agent.issue_operations` (from AgentCard skills) |
| Tool | `github-tool` (simplified) — **discovered** scopes `github-tool.{source-read, source-write, issues-read, issues-write}` (from MCP `tools/list`) |
| Users | `dev-user` (`developer`), `test-user` (`tester`), `devops-user` (`devops`) |
| `developer` | source read/write + issues read |
| `tester` | issues read/write |
| `devops` | no access (inbound deny; denied every outbound tool) — conveyed by **role description only**, absent from the `policy.md` (deny-by-default) |

## Configuration (env)

The suite reads its config from `test/integration/.env` (gitignored); source it before running
(`set -a; . test/integration/.env; set +a`). The drivers read these:

| Variable | Purpose | Default |
|----------|---------|---------|
| `KUBECONFIG` | Kubeconfig for the live rossoctl/Kind cluster | — (required) |
| `KEYCLOAK_URL` | External Keycloak base URL | — (required) |
| `KEYCLOAK_ADMIN_USERNAME` / `KEYCLOAK_ADMIN_PASSWORD` | Keycloak admin creds (user/realm-role provisioning + cleanup) | — (required) |
| `KEYCLOAK_ADMIN_REALM` | Realm the admin creds live in | `master` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | PRB LLM (pinned `temperature=0`); consumed by the in-cluster AIAC pod | — (required) |
| `AIAC_TEST_REALM` | Realm the tests resolve/provision against. **Must match the deployed AIAC stack's `KEYCLOAK_REALM`** — the in-cluster Controller resolves the onboarding trigger in *its own* realm, so a harness on a different realm resolves a client UUID the Controller can't find (404 → onboard 502) | `rossoctl` |
| `AIAC_DEMO_NAMESPACE` | Namespace the demo workloads are deployed in (precondition) | `team1` |
| `AIAC_TRUST_DOMAIN` | SPIFFE trust domain the operator registers the demo workloads under | `localtest.me` |

> Cluster/stack knobs the harness also honors, with defaults matching the deployed stack (rarely
> overridden): the Controller target/namespace/ports (`AIAC_CONTROLLER_*`, default
> `svc/aiac-agent-service` in `aiac-system` on `7070`), the Policy Store target (`AIAC_STORE_*`,
> `svc/aiac-policy-model-store-service` on `7074`), the abstract-policy ConfigMap/mount
> (`AIAC_POLICY_CONFIGMAP` / `AIAC_POLICY_MOUNT_PATH`), the agent Deployment to restart
> (`AIAC_AGENT_DEPLOYMENT`), and the timeouts (`AIAC_ONBOARD_TIMEOUT`, `AIAC_BUNDLE_TIMEOUT`,
> `AIAC_BUNDLE_POLL_INTERVAL`). Single stack — one Controller, one policy; the two-variant env
> (`AIAC_EXPLICIT_URL`/`AIAC_ABSTRACT_URL`, per-variant OPA pods) is gone with the two-stack topology.

## Runbook

Runnable against a live rossoctl/Kind cluster (operator + Keycloak + SPIRE) with the AIAC stack + the
AuthBridge OPA pipeline wired into **both** legs, `github-agent` + `github-tool` **already deployed and
registered** into `AIAC_TEST_REALM`, and a real LLM in-pod. Stand the pipeline up with
`k8s/opa-kind-enable.sh`; the full prerequisites, wiring, and manual probe commands are in
`k8s/opa-kind-runbook.md`.

```bash
k8s/opa-kind-enable.sh          # one-time: wire the OPA plugin into both legs of the Kind cluster
set -a; . test/integration/.env; set +a
.venv/bin/pytest test/integration/ -m integration -k uc1_onboard -v
# A failing node names the exact cell, e.g.:
#   test_outbound[test-user-source-read] — expected deny, plugin allowed
```

Without `-m integration` the suite is not collected; when the cluster/pipeline is not wired or the env is
unset it **skips cleanly** (it never false-passes).

## Testing Decisions

- **Highest seam available, verified by the real evaluator.** Real deployed workloads + real operator +
  real UC-1 onboarding + real PRB/PCE + real Keycloak + real LLM, driven through the production trigger
  (`POST /apply/service/{id}`) and enforced by the **deployed AuthBridge OPA plugin**. Assert only
  **external behavior** — the allow/deny decisions the plugin makes — never internal policy structure.
- **The enforced decision is the artifact under test; the scenario is the oracle.** Verdicts computed from
  `scenario_uc1.py`, keyed on the bare runtime tool names — not from the policy itself.
- **Onboard, then enforce.** Live enforcement / token-exchange / real HTTP through AuthBridge is now the
  whole point (not out of scope). The agent's own CrewAI reasoning flow is not triggered — the probes are
  synthetic requests through AuthBridge — but the traffic is real and the deployed plugin enforces it.
- **Deployment is a precondition.** The tests do not deploy or wait for registration; they cleanup →
  onboard → enable the outbound leg → poll → validate → cleanup, so reruns are hermetic and cheap.
- **One stack, one policy, the deployed plugin.** Rungs 1–3 need only one AIAC stack; the deployed OPA
  plugin + the upserted `AuthorizationPolicy` CR are what make the pipeline observable.
- **Onboarding-order-independence is asserted, not assumed** (rungs 2 vs 3). Rung 3's intended end state
  is checked identical to rung 2's published expectations, and the real plugin's decisions are asserted in
  the tool→agent order. A divergence is a bug.
- **Per-scope two-gate AND.** UC-1's per-skill operator roles are mapped to the tool scopes by
  capability-match, so the capability gate is populated; the plugin enforces the real per-scope AND. The
  agent reaches all four tool scopes, so the user gate discriminates.
- **Stack's realm, leave-in-place; per-rung cleanup.** UC-1 resolves/provisions against the deployed
  stack's `KEYCLOAK_REALM` (default `rossoctl`) and **never deletes** the realm/users/roles; only the
  provisioned agent/tool roles/scopes (and this run's CR + policy-store SPMs) are cleaned up per rung so
  onboarding runs from a clean slate. `policy-pipeline` (`5.3`) shares this same live stack and the same
  leave-in-place realm.
- **LLM nondeterminism, contained.** PRB LLM pinned `temperature=0`; both cell-level and provisioning
  assertions; `@pytest.mark.integration`, out of default CI.
- **Prior art, shared not copied.** Reuses the `5.3` shape (skip gates, scenario-as-oracle, the live
  decision oracle) via `uc1_onboard.py` / `launcher.py` / `scenario_uc1.py`.

## Relationship to other integration tests

- **Onboarding-order sibling of `policy-pipeline`** ([policy-pipeline.md](policy-pipeline.md),
  `testing/5.3-policy-pipeline-integration-test.md`): identical scenario facts/tables and the **same**
  live enforcement loop (onboard through the Controller → real HTTP through AuthBridge → deployed OPA
  plugin's allow/deny). `policy-pipeline` is the **full happy-path matrix + negative controls** over the
  fully onboarded stack; this ladder **isolates onboarding-order properties** across three rungs. Both
  share the same harness and the same live stack (the `rossoctl` realm + the `team1` workloads); the
  former explicit-vs-abstract two-policy equivalence check is **deferred to rung 4** (`testing/5.4.4`),
  since only one `policy.md` is mounted on the live stack.
- Same `@pytest.mark.integration` + live-enforcement flavor as `testing/5.1-integration-tests.md`; runs
  outside the default unit run against live dependencies and skips cleanly when the cluster/env is not
  wired.

Tracking issues: `testing/5.4-uc1-onboarding-integration-test.md` (epic) + `5.4.1`/`5.4.2`/`5.4.3` (rungs)
+ `5.4.4` (deferred two-policy).

## Out of Scope

- **Writing the rung tests + `scenario_uc1.py` / harness edits** — this spec *describes* them; they are
  written under the `5.4.x` issues.
- **The UC-1 agent, PRB, PCE, OPA writer, the AuthBridge OPA plugin, and the demo `github-agent`** —
  specified/tested by their own components/issues. UC-1's discovery naming and per-skill operator-role
  behavior are **fixed**; these tests observe and enforce against them.
- **Deploying / registering the workloads and wiring the OPA pipeline** — preconditions
  (`k8s/opa-kind-enable.sh`), not part of the tests.
- **Two-policy (rung 4)** — deferred; the two-stack topology is discarded and the in-cluster approach is
  TBD (`testing/5.4.4-uc1-onboard-two-policies.md`).
- **The agent's CrewAI reasoning flow / real A2A message content** — the probes drive synthetic requests
  through AuthBridge to exercise the enforced gates; they do not run the agent's task graph.
- **Default-CI wiring** — `@pytest.mark.integration`; runs on demand.

## Scenario inputs

**Functional** inputs — the PRB reads the descriptions and the `policy.md` to produce the role→scope
mappings. Descriptions are **generic and keyword-free** and stay within Keycloak's 255-char cap (written
verbatim); client `type` is set by UC-1 from the `rossoctl.io/type` label.

### Discovered entities (what UC-1 provisions)

- **`github-tool`** (Tool) → scopes, from MCP `tools/list` (verbatim descriptions):
  - `github-tool.source-read` — "Read source repository contents: file listings and file bodies. Read-only."
  - `github-tool.source-write` — "Create, modify, or delete source repository contents; commit file changes."
  - `github-tool.issues-read` — "Read issues and their comment threads. Read-only."
  - `github-tool.issues-write` — "Create and update issues: open, edit, comment, and close."
- **`github-agent`** (Agent) → **one operator role per skill** (name + description mirror each scope) +
  scopes from the AgentCard skills:
  - `github-agent.source_operations` — "Browse and search code; read, create, and modify repository file contents, branches, and commits."
  - `github-agent.issue_operations` — "Read, search, create, and update issues, comments, sub-issues, and pull requests."

  The operator roles `github-agent.source_operations` / `github-agent.issue_operations` carry the same
  descriptions as the scopes they mirror; those descriptions drive the PRB capability-match. (This
  replaces the prior single generic `github-agent.agent` role.)

### Realm roles (provisioned by the fixture)

- `developer` — "Developer — an engineering user who develops the source codebase (writing and maintaining code) and fixes code defects reported in the issue tracker; works primarily in source and consults issues for defect reports."
- `tester` — "Tester — a quality-assurance user who verifies software quality and tracks defects through the issue tracker: filing, triaging, and updating issue reports; works in the issue tracker, not in source."
- `devops` — "DevOps — an operations user who manages deployment infrastructure and runtime environments; does not author source code and does not manage the issue tracker."

### `policy.md` — the single (abstract) variant

Phase-1's intent-only prose. The PRB/LLM expands intent into the discovered scopes via the entity/role
descriptions. It stays **user-intent-only** and **does not name the agent's operator roles** — the
agent's capability gate comes from the generic rubric (`generic_policy.md`) matching the operator-role
descriptions to the tool-scope descriptions, not from the policy naming them. Deny by default. Phrased
**purely positively** so absences are conveyed by silence + deny-by-default (keeping the fixture
ALLOW-only; deny-extraction deferred to #142, as in `policy-pipeline`).

```markdown
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
```

> The **explicit** enumerated variant and cross-variant equivalence are deferred to rung 4
> (`testing/5.4.4-uc1-onboard-two-policies.md`); the two-stack topology that served both variants is
> discarded.
