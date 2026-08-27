# Integration Test: policy-pipeline — `test_policy_pipeline.py`

> **One spec among several.** This document specifies a **single** integration test.
> Integration-test specs live **one spec per test** under `docs/specs/integration-test/`
> (a sibling of `components/`), and the master PRD's *Integration test specifications* section
> ([../PRD.md](../PRD.md)) is the index of them. This is the **policy-pipeline** integration test —
> the full identity→policy→**enforcement** pipeline — not the definition of integration testing in
> general, and not the only integration-test PRD.

## Location
`aiac/test/integration/test_policy_pipeline.py` — a pytest module marked `@pytest.mark.integration`.
It imports two shared modules: `aiac/test/integration/scenario_uc1.py` — the canonical `github-agent`
scenario as pure data (the role→access truth table the *Expected output* renders — the pair-lists,
expressed over the **discovered, workload-prefixed** names `github-tool.source-read`,
`github-agent.source_operations`, …) — and `aiac/test/integration/uc1_onboard.py` — the shared live
harness (Keycloak provisioning/cleanup, the `POST /apply/service/{id}` onboard trigger, the outbound
token-exchange-leg prep, the bundle-convergence poll, and the live decision oracle + probes). The
harness in turn builds on `aiac/test/integration/launcher.py`'s live-cluster half (`kubectl` wrappers,
`port_forward`, `resolve_pod`, `mint_token`, `inbound_probe` / `outbound_probe`, `inbound_outcome` /
`outbound_outcome`, `poll_until`, and the skip gates). There is **no** standalone Rego module and **no**
`opa` binary here anymore: the evaluator is the deployed AuthBridge OPA plugin (see *[What it
does](#what-it-does)*).

## Description

A `@pytest.mark.integration` test that drives the **whole identity→policy→enforcement pipeline** —
**Keycloak → UC-1 onboarding → PRB → PCE → OPA Policy Writer → AuthBridge OPA plugin** — end-to-end,
then **asserts** that the **enforced decision** is correct by driving a **real HTTP request through
AuthBridge** and reading the **deployed OPA plugin's** allow/deny. Nothing is mocked or dumped; the
artifact under test is the *enforced decision*, not a file on disk.

The generated policy is the **artifact under test** — the LLM/PCE that produced it might be wrong — so
the test never trusts it. Instead it sends requests derived from the **scenario spec (the intended
policy)** and asserts the real plugin admits/denies each one as the scenario truth table
(`scenario_uc1.py`) requires. A mismatch fails the test and names the exact `subject[ / tool]` cell.

This is the **umbrella full-matrix e2e** for the fixed `github-agent` scenario. It onboards **both** the
`github-agent` and the `github-tool` through the real in-cluster UC-1 Controller
(`POST /apply/service/{id}`, which upserts the `AuthorizationPolicy` CR on the live Kubernetes API),
enables the outbound token-exchange leg, waits for `bundle-service` + the AuthBridge OPA sidecars to
recompose and reload the bundle, then asserts the **full happy-path matrix + negative controls** over
the fully onboarded stack. Both gates are exercised through AuthBridge's own parsers: `jwt-validation`
builds `input.identity` on the inbound leg; `token-exchange` + `mcp-parser` build the outbound
`input.identity` + `input.mcp.params.name` (the **bare** tool name) — so the test never hand-builds an
input document and there is no standalone probe module.

Where this sits vs. the UC-1 ladder ([uc1-onboarding-pipeline.md](uc1-onboarding-pipeline.md)): rungs
1–3 isolate onboarding-**order** properties (agent-only; agent→tool; tool→agent + order-independence);
this module is the **full matrix + negative controls** over the fully onboarded stack. Both share the
same live stack — the `rossoctl` realm and the deployed `team1` workloads — so there is exactly one
deployed pipeline to enforce against; the former explicit-vs-abstract two-policy equivalence check is
therefore **deferred to the two-policy rung** `testing/5.4.4` (only one `policy.md` is mounted on the
live stack).

Because it needs a live rossoctl/Kind cluster with the AuthBridge OPA pipeline wired in, a real LLM,
and Keycloak admin creds, it is `@pytest.mark.integration` and stays out of the default unit-test run
(`-m "not integration"`); it **skips cleanly** when the cluster is not wired or the env is unset (it
never false-passes).

### What it does

A single session fixture (`uc1.onboarded_stack([AGENT_WORKLOAD, TOOL_WORKLOAD])`) drives the whole
identity→policy→enforcement pipeline once; the individual tests then assert the real plugin's decisions
over the fully onboarded stack.

1. **Skip gates first — before any cluster mutation.** `require_pipeline` skips cleanly if the live
   AuthBridge OPA pipeline is not wired (no `kubectl`, `AuthorizationPolicy` CRD not served,
   `bundle-service` not Running, the `opa` plugin not on **both** legs, or a workload pod not Running);
   `require_env_or_skip` skips if `KEYCLOAK_URL` / admin creds are unset. The suite never false-passes.
2. **Clean slate.** Delete the agent's `AuthorizationPolicy` CR, `cleanup_provisioned` (drop the
   `github-agent.` / `github-tool.`-prefixed realm roles + client scopes UC-1 provisions), and
   `clear_policy_store` (drop persisted SPMs from the in-cluster Policy Store, whose SQLite outlives
   redeploys). Then `provision_realm_and_users` idempotently ensures the scenario's three users +
   realm roles (`developer` / `tester` / `devops`) with the descriptions the PRB reads (the fixture
   provisions these; UC-1 does not), `verify_subject_mapper` confirms the realm's `username → sub`
   mapper + Direct Access Grants are in place (else skip), and `ensure_agent_policy` mounts the single
   abstract `policy.md` on the Controller pod.
3. **Onboard both workloads through the real in-cluster UC-1 Controller.** `POST /apply/service/{id}`
   for the `github-tool` and the `github-agent`, where `{id}` is the client's **internal Keycloak
   UUID** (`resolve_service_id`), not the slash-bearing `clientId`. UC-1 classifies each service,
   reads the MCP `tools/list` / AgentCard skills, provisions the **workload-prefixed** scopes
   (`github-tool.{source-read, source-write, issues-read, issues-write}`) and the agent's **one
   operator role per skill** (`github-agent.{source_operations, issue_operations}`), maps roles→scopes
   via the real PRB (real LLM, `temperature=0`), and the Controller calls
   `compute_and_apply(rules, override=False)`; the OPA Policy Writer upserts the agent's
   `AuthorizationPolicy` CR on the live Kubernetes API.
4. **Enable the outbound token-exchange leg (Part B).** `ensure_github_tool_route` adds the
   `github-tool` outbound route to `authproxy-routes`, `grant_exchange_scope` grants the agent's
   Keycloak client the `github-tool` audience scope as optional, and `restart_agent` restarts the
   agent so it reloads the route (and its OPA sidecar re-fetches the recomposed bundle). Without this
   the outbound call would pass through unexchanged and never reach OPA.
5. **Wait for the pipeline to converge.** `poll_until` drives real decisions until this run's CR is
   reflected: `dev-user` reaches the agent (inbound allow), `devops-user` is blocked (inbound deny —
   proving the restrictive client-scoped gate is live, not the allow-all baseline), and `dev-user`'s
   outbound `source-read` has reached its terminal `allow` (waiting out the post-restart
   token-exchange window). Keycloak cleanup + CR delete run **before and after**; the clients stay
   registered as before.
6. **Assert the enforced decisions over the full matrix.** Each test mints a fresh user token and
   sends a **real HTTP request through AuthBridge**:
   - **Inbound** — one node per `subject`. A request as `subject` reaches the agent iff the user's
     role may reach some agent scope. `jwt-validation` builds `input.identity`; the real OPA plugin
     decides (200 → `allow`, 403 → `deny`).
   - **Outbound** — one node per `(subject × bare tool name)`. A real MCP `tools/call` for the **bare**
     tool through AuthBridge's forward proxy (token-exchange → OPA) is allowed iff **both** the subject
     and some agent role are entitled to that tool's scope (the per-scope two-gate AND). `mcp-parser`
     surfaces `input.mcp.params.name`; a denial is a JSON-RPC error frame (`error.data.plugin: "opa"`)
     at HTTP 200 that the harness classifies as `deny`.
   - The expected verdict for every cell is **computed from** the `scenario_uc1.py` pair-lists
     (`INBOUND_PAIRS` / `OUTBOUND_SUBJECT_PAIRS` / `OUTBOUND_TARGET_PAIRS`), keyed on the **bare**
     runtime tool names AuthBridge sends — not a second hand-maintained copy. A wrong LLM/PCE mapping
     therefore fails the exact `subject / tool` cell.
7. **Negative controls.** An otherwise-allowed subject (`dev-user`) invoking a tool name in **no**
   allowed scope (`nonexistent-tool`) is denied — the outbound gate matches `input.mcp.params.name`
   exactly, so an unknown tool falls through to deny-by-default. A bogus, destructive-sounding tool
   name (`delete_everything`) matching no discovered scope is likewise denied — guarding against an
   over-broad match letting an unrecognized operation through.
8. **Oracle-contract tests (fixture-independent).** A handful of tests need neither the cluster nor
   the env: they assert the intended matrix itself — `expected_inbound` / `expected_outbound_bare`
   over the scenario pair-lists — the tracer bullet. If these are wrong, every live assertion is
   meaningless.

## Expected output

The test passes when the deployed OPA plugin decides every cell of the scenario truth table as follows.
Verdicts are **computed from** the `scenario_uc1.py` pair-lists (`INBOUND_PAIRS` /
`OUTBOUND_SUBJECT_PAIRS` / `OUTBOUND_TARGET_PAIRS`), keyed on the bare runtime tool names; this table
is the human-readable rendering of them.

`USERS`: `dev-user`→`developer`, `test-user`→`tester`, `devops-user`→`devops`.

**Inbound allow** (the real plugin's inbound decision, from `INBOUND_PAIRS`, user-role→agent-scope):

| Subject | Inbound |
|---|---|
| dev-user | ✅ |
| test-user | ✅ |
| devops-user | ❌ |

**Outbound allow(subject, tool)** (the real plugin's outbound decision, per-scope two-gate AND over
the **bare** tool names; the agent reaches all four tool scopes, so the user gate discriminates):

| | source-read | source-write | issues-read | issues-write |
|---|---|---|---|---|
| dev-user | ✅ | ✅ | ✅ | ❌ |
| test-user | ❌ | ❌ | ✅ | ✅ |
| devops-user | ❌ | ❌ | ❌ | ❌ |

Plus the negative controls: `dev-user` invoking `nonexistent-tool` or `delete_everything` is **denied**
(deny-by-default; no accidental allow on an unknown tool name).

The pipeline emits an agent `AuthorizationPolicy` CR only — explicitly **no** standalone tool policy
(the tool is a pure target; no rules are written for it directly). This fixture is **ALLOW-only** (see
*[Further Notes](#further-notes)*): the single `policy.md` carries only positive fine-grained grants and
no exclusivity / prohibition prose, and the entity/role descriptions stay deny-neutral, so the
DENY-aware PRB emits **no** `DENY` rules. Extending the fixture to exercise the PRB's ALLOW+DENY path
(explicit-prohibition prose and/or description-driven denies, plus a grant-set assertion that compares
**deny** sets) is deferred to the sibling "for later" issue (#142, ALLOW+DENY policy support); see the
*Deny-extraction interaction* note under *[Further Notes](#further-notes)*.

## Scenario

A single agent + tool + three users, fixed so the enforced decisions are reproducible and reviewable.
This is the canonical `github-agent` worked example, driven end to end through the real UC-1 onboarding
pipeline and enforced by the deployed OPA plugin, plus a third `devops-user` that exercises the
deny-by-default path. Entities are **discovered** by UC-1 (tool scopes from the MCP `tools/list`
manifest, agent roles/scopes from the AgentCard skills), so every scope is **workload-prefixed**.

| Element | Value |
|---------|-------|
| Realm | `AIAC_TEST_REALM` (must match the deployed stack's `KEYCLOAK_REALM`; default `rossoctl`) |
| Agent | `github-agent` — **discovered** per-skill operator roles `github-agent.source_operations`, `github-agent.issue_operations` (mirroring the scopes); scopes `github-agent.source_operations`, `github-agent.issue_operations` (from AgentCard skills) |
| Tool | `github-tool` — **discovered** scopes `github-tool.{source-read, source-write, issues-read, issues-write}` (from MCP `tools/list`) |
| Users | `dev-user` (role `developer`), `test-user` (role `tester`), `devops-user` (role `devops`) |
| `developer` | source read/write + issues read |
| `tester` | issues read/write |
| `devops` | no access (inbound deny; denied every outbound tool) |

Role → access (the fixed facts the single `policy.md` and the `scenario_uc1.py` pair-lists must agree
with — the generic descriptions are not part of this triad):

- `developer` — source read/write, issues read.
- `tester` — issues read/write.
- `devops` — no access. Conveyed by the **role description only** — it is absent from every pair-list
  and from the `policy.md` (deny-by-default), so it is denied inbound and on every outbound tool.

## Configuration (env)

The suite reads its config from `test/integration/.env` (gitignored); source it before running
(`set -a; . test/integration/.env; set +a`). The drivers read these:

| Variable | Purpose | Default |
|----------|---------|---------|
| `KUBECONFIG` | Kubeconfig for the live rossoctl/Kind cluster | — (required) |
| `KEYCLOAK_URL` | External Keycloak base URL | — (required) |
| `KEYCLOAK_ADMIN_USERNAME` / `KEYCLOAK_ADMIN_PASSWORD` | Keycloak admin creds (user/realm-role provisioning + cleanup) | — (required) |
| `KEYCLOAK_ADMIN_REALM` | Realm the admin creds live in | `master` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | PRB LLM (pinned `temperature=0`), consumed by the in-cluster AIAC pod | — (required) |
| `AIAC_TEST_REALM` | Realm the tests resolve/provision against. **Must match the deployed AIAC stack's `KEYCLOAK_REALM`** — the in-cluster Controller resolves the onboarding trigger in *its own* realm | `rossoctl` |
| `AIAC_DEMO_NAMESPACE` | Namespace the demo workloads are deployed in (precondition) | `team1` |
| `AIAC_TRUST_DOMAIN` | SPIFFE trust domain the operator registers the demo workloads under | `localtest.me` |

> Cluster/stack knobs the harness also honors, with defaults matching the deployed stack (rarely
> overridden): the Controller target/namespace/ports (`AIAC_CONTROLLER_*`, default
> `svc/aiac-agent-service` in `aiac-system` on `7070`), the Policy Store target
> (`AIAC_STORE_*`, `svc/aiac-policy-model-store-service` on `7074`), the policy ConfigMap/mount
> (`AIAC_POLICY_CONFIGMAP` / `AIAC_POLICY_MOUNT_PATH`), and the timeouts
> (`AIAC_ONBOARD_TIMEOUT`, `AIAC_BUNDLE_TIMEOUT`, `AIAC_BUNDLE_POLL_INTERVAL`).

## Runbook

Runnable against a live rossoctl/Kind cluster (operator + Keycloak + SPIRE) with the AuthBridge OPA
pipeline wired into **both** legs, `github-agent` + `github-tool` **deployed and registered** into
`AIAC_TEST_REALM`, and a real LLM in-pod. Stand the pipeline up with `k8s/opa-kind-enable.sh`; the full
prerequisites, wiring, and manual probe commands are in `k8s/opa-kind-runbook.md`.

```bash
k8s/opa-kind-enable.sh          # one-time: wire the OPA plugin into both legs of the Kind cluster
set -a; . test/integration/.env; set +a
.venv/bin/pytest test/integration/test_policy_pipeline.py -m integration -v
# Parametrized over subject inbound + (subject × bare tool) outbound + negative controls.
# A failing node names the exact cell, e.g.:
#   test_outbound[test-user-source-read] — expected deny, plugin allowed
```

Without `-m integration` the suite is not collected; when the cluster is not wired or the env is unset
it **skips cleanly** (it never false-passes). To eyeball the pipeline manually, follow
`k8s/opa-kind-runbook.md` (Part A inbound, Part B outbound) and inspect the upserted
`AuthorizationPolicy` CR and the provisioned Keycloak realm.

## Testing Decisions

- **Highest seam available, verified by the real evaluator.** Real deployed workloads + real operator
  + real UC-1 onboarding + real PRB/PCE + real Keycloak + real LLM, driven through the production
  trigger (`POST /apply/service/{id}`) and enforced by the **deployed AuthBridge OPA plugin**. The
  test asserts only **external behavior** — the allow/deny decisions the plugin makes for
  scenario-derived requests — never internal policy structure (which the OPA Policy Writer's own unit
  tests own).
- **The enforced decision is the artifact under test; the scenario is the oracle.** The LLM/PCE that
  produced the policy might be wrong, so the expected verdicts are **computed from** the
  `scenario_uc1.py` pair-lists, keyed on the bare runtime tool names — not from a second hand-maintained
  copy or from the policy itself. A wrong role→scope mapping therefore fails the exact cell.
- **Both gates go through AuthBridge's own parsers.** Inbound `input.identity` is built by
  `jwt-validation`; outbound `input.identity` + `input.mcp.params.name` (the **bare** tool name) are
  built by `token-exchange` + `mcp-parser`. The test never hand-builds an input document and there is
  no standalone probe module — the deployed plugin sees exactly what production sees.
- **Outbound needs the token-exchange leg live.** The outbound OPA gate is only reached if
  `token-exchange` first intercepts + exchanges the agent's call to `github-tool`; Part B (route +
  optional client scope + agent restart) enables it, and the fixture polls real decisions until it
  settles before asserting.
- **Negative controls.** Unknown/bogus tool names (`nonexistent-tool`, `delete_everything`) must be
  denied — guarding against an over-broad match or accidental allow on a name in no discovered scope.
- **Skip cleanly, never false-pass.** The suite skips (does not fail) when the pipeline is not wired
  or the integration env is unset, and it skips before any cluster mutation.
- **LLM nondeterminism, contained.** The in-cluster PRB LLM is pinned to `temperature=0`, and the
  single abstract `policy.md` leans on the LLM to expand prose + descriptions into concrete scopes;
  the enforced decisions are asserted cell-by-cell against the truth table. Some model-dependence
  remains, which is why the suite is `@pytest.mark.integration`, out of default CI.
- **Shared harness, one live stack.** The onboarding, Part-B prep, bundle-convergence poll, and live
  decision oracle live in `test/integration/uc1_onboard.py` and are shared with the UC-1 ladder; the
  fixed scenario lives in `test/integration/scenario_uc1.py`. Both suites enforce against the same
  deployed pipeline (the `rossoctl` realm + the `team1` workloads), left in place across runs with
  per-run cleanup of only the provisioned prefixed roles/scopes — neither suite deletes/recreates the
  realm.

## Relationship to other integration tests

This is **one** integration-test spec among several indexed by the master PRD
([../PRD.md](../PRD.md), § *Integration test specifications*).

- **Umbrella sibling of the UC-1 onboarding ladder** ([uc1-onboarding-pipeline.md](uc1-onboarding-pipeline.md),
  `testing/5.4.x`): identical scenario facts/tables and the **same** live enforcement loop (onboard
  through the Controller → real HTTP through AuthBridge → deployed OPA plugin's allow/deny). The ladder
  isolates onboarding-**order** properties across three rungs; this test is the **full happy-path
  matrix + negative controls** over the fully onboarded stack.
- Same `@pytest.mark.integration` + live-enforcement flavor as `testing/5.1-integration-tests.md`;
  runs outside the default unit run against live dependencies and skips cleanly when the cluster/env
  is not wired.

Tracking issue for this test: `testing/5.3-policy-pipeline-integration-test.md`.

## Out of Scope

- **Writing `test_policy_pipeline.py` or any pipeline code** — this spec *describes* the test; the
  implementation is owned by `testing/5.3-policy-pipeline-integration-test.md` and the prerequisite
  issues.
- **The Rego generator, the canonical policy model, the PRB, the PCE, and the AuthBridge OPA plugin
  implementations** — specified and unit-tested by their own components
  ([../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md),
  [../components/policy-model.md](../components/policy-model.md),
  [../components/policy-computation-engine.md](../components/policy-computation-engine.md), and the PRB
  component spec), not here. This test asserts only the **enforced decisions**, never the internal
  structure of the generated policy.
- **Deploying / registering the workloads and wiring the OPA pipeline** — preconditions
  (`k8s/opa-kind-enable.sh`), not part of the test.
- **Two-policy explicit-vs-abstract equivalence** — deferred to the two-policy rung
  `testing/5.4.4`; the live stack mounts a single `policy.md`.
- **Default-CI wiring** — the test is `@pytest.mark.integration` and requires a live cluster +
  Keycloak + LLM, so it runs on demand, not in the default `-m "not integration"` unit run.

## Further Notes

> **Note — ALLOW-only fixture for now (deny-extraction deferred to #142).** The Policy Rules Builder now
> emits explicit `DENY` rules from direct-prohibition / exclusivity prose (see
> [../components/aiac-agent/policy-rules-builder.md](../components/aiac-agent/policy-rules-builder.md),
> § *Deny extraction*). This fixture is deliberately kept **ALLOW-only for now** so the suite builds and
> passes under the DENY-aware PRB (split from #140; the ALLOW+DENY half is the sibling "for later"
> issue #142). The single `policy.md` therefore carries only positive grants — no `exclusively`, no
> `read-only`, no `no access to source` — and the entity/role descriptions stay deny-neutral, so the PRB
> emits **no** `DENY` rules and the enforced policy is allow-only. This keeps the two claims below
> intact: the descriptions stay generic and drop out of the fact triad, and `devops` stays the pure
> **deny-by-default / silence** exemplar.
>
> Exercising the PRB's ALLOW+DENY path against this fixture — explicit-prohibition prose, the
> description-driven denies the `tester` (*"…not in source"*) and `devops` descriptions would supply
> under the PRB's symmetric rule, and a grant-set assertion that compares **deny** sets as well as allow
> sets — is out of scope here and tracked by #142. Note that the enforced **verdicts** (the truth table)
> are the same either way: `tester` is denied source and `devops` is denied everywhere whether by
> explicit `DENY` or by deny-by-default; only the generated policy's **deny-map content** would differ.

- The scenario is deliberately fixed. The role→access facts are owned by artefacts that must agree:
  the *Scenario* table, the single `policy.md` (see *Scenario inputs*), and the `scenario_uc1.py`
  pair-lists (`INBOUND_PAIRS` / `OUTBOUND_SUBJECT_PAIRS` / `OUTBOUND_TARGET_PAIRS`). The
  entity/role/scope **descriptions no longer encode those facts** — they are generic and functional and
  drop out of the fact triad; they must stay generic and simply not contradict the facts. If the
  role→access facts change, update the *Scenario* table, the `policy.md`, and the pair-lists together.
- The least-privilege **deny-by-default** directive is supplied by the PRB prompt itself
  (`_GRANT_ACCESS` in `agent/policy_rules_builder/prompts.py`), which prepends it — followed by the
  bundled generic baseline policy (`generic_policy.md`) — ahead of the scenario `policy.md` on every
  call, so every policy decision gets it. The abstract `policy.md` relies on the prompt and does not
  restate the directive.
- The single mounted `policy.md` is **user-intent-only** (see *Scenario inputs*): it states only what
  users may do and does **not** name the agent's operator roles. The agent's own capability (the
  outbound target gate) comes from the generic rubric (`generic_policy.md`) applied to the operator-role
  descriptions, not from naming those roles in the policy. Keeping the policy purely fine-grained and
  positive is also what keeps the fixture ALLOW-only.
- Descriptions are ≤255 characters and written **verbatim** into Keycloak (Keycloak caps role and
  client descriptions at 255 chars, and the generic descriptions are authored to stay within that cap).
- The `devops` role's **zero access** is conveyed by its **role description only**. It is absent from
  every pair-list and from the `policy.md`, so deny-by-default alone denies it inbound and on every
  outbound tool — which is precisely what the truth table's `devops-user` row asserts.

## Prerequisites

The live enforcement loop is in place (drivers, `k8s/opa-kind-*` scripts + runbook, and the
AuthBridge OPA plugin), so this test is ready to run once the pipeline is stood up. It requires a wired
cluster (`k8s/opa-kind-enable.sh`); the components it exercises end-to-end are specified/unit-tested by
their own issues:

- PRB — `agent/3.20-policy-rules-builder.md`
- PCE — `policy/pce/8.10-policy-computation-engine.md`
- Policy model — `policy/model/8.1-policy-model.md`
- Rego package generator — `pdp-policy-writer/1.10-rego-package-generator.md`
- pdp-policy library — `library/pdp/8.9-pdp-policy-library-rename.md`
- Policy Model Store library / service — `policy/store/8.7-policy-store-library.md` /
  `policy/store/8.5-policy-store-service.md`

## Scenario inputs (PRB functional inputs)

These are **functional** inputs — the PRB reads the entity/role/scope descriptions and the `policy.md`
to produce the role→scope mappings, so they are part of the fixed scenario, not decoration. The
entity/role descriptions and the agent/tool scopes are **discovered by UC-1** from the deployed
workloads (MCP `tools/list`, AgentCard skills); the realm roles and the `policy.md` are provisioned by
the fixture. Keep them in sync with the *Scenario* table (see *Further Notes*).

### Discovered entities (what UC-1 provisions)

Descriptions are **generic and keyword-free** and stay within Keycloak's 255-char cap (written
verbatim). Client `type` is set by UC-1 from the `rossoctl.io/type` label — not inferred from
description prose.

- **`github-tool`** (Tool) → scopes, from MCP `tools/list` (verbatim descriptions):
  - `github-tool.source-read` — "Read source repository contents: file listings and file bodies. Read-only."
  - `github-tool.source-write` — "Create, modify, or delete source repository contents; commit file changes."
  - `github-tool.issues-read` — "Read issues and their comment threads. Read-only."
  - `github-tool.issues-write` — "Create and update issues: open, edit, comment, and close."
- **`github-agent`** (Agent) → **one operator role per skill** (name + description mirror each scope) +
  scopes from the AgentCard skills:
  - `github-agent.source_operations` — "Browse and search code; read, create, and modify repository file contents, branches, and commits."
  - `github-agent.issue_operations` — "Read, search, create, and update issues, comments, sub-issues, and pull requests."

  The operator roles carry the same descriptions as the scopes they mirror; those descriptions drive
  the PRB capability-match that populates the agent→tool gate.

### Realm roles (provisioned by the fixture)

- `developer` — "Developer — an engineering user who develops the source codebase (writing and maintaining code) and fixes code defects reported in the issue tracker; works primarily in source and consults issues for defect reports."
- `tester` — "Tester — a quality-assurance user who verifies software quality and tracks defects through the issue tracker: filing, triaging, and updating issue reports; works in the issue tracker, not in source."
- `devops` — "DevOps — an operations user who manages deployment infrastructure and runtime environments; does not author source code and does not manage the issue tracker."

> The `devops` description is deliberately **unrelated** to source and issue work, so the PRB derives no
> agent or tool scope for it and deny-by-default leaves `devops-user` denied everywhere — the inbound
> deny case.

### `policy.md` — the single (abstract) variant

Phase-1's intent-only prose. The PRB/LLM expands intent into the discovered scopes via the entity/role
descriptions. It stays **user-intent-only** and **does not name the agent's operator roles** — the
agent's capability gate comes from the generic rubric (`generic_policy.md`) matching the operator-role
descriptions to the tool-scope descriptions, not from the policy naming them. Deny by default. Phrased
**purely positively** — no `exclusively`, `read-only`, or `no access to source` prose — so absences
(developer's lack of issues-write, tester's lack of source access) are conveyed by **silence +
deny-by-default**, not by prohibition triggers that would drive the DENY-aware PRB to emit `DENY` rules
(kept ALLOW-only for now; see *Further Notes*).

```markdown
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
```

> The **explicit** enumerated variant and the cross-variant equivalence check are deferred to the
> two-policy rung `testing/5.4.4`; the two-stack topology that once served both variants is discarded.
