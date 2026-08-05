# Integration Test: uc1-onboarding-pipeline — a ladder of UC-1 onboarding tests

> **One spec among several.** This document specifies the **UC-1 onboarding** integration tests.
> Integration-test specs live under `docs/specs/integration-test/` (a sibling of `components/`), indexed
> by the master PRD's *Integration test specifications* section ([../PRD.md](../PRD.md)). This is the
> phase-1 service-onboarding demo driven end-to-end through the **real UC-1 agent** against
> **really-deployed** demo workloads — not the definition of integration testing in general.

> **Ladder, not one test.** This spec was previously a single "complete two-policy" test that assumed a
> **two-stack** topology (one AIAC stack per `policy.md` variant) which is **not deployed** and so could
> never run. It is now a **ladder** of three gradual, runnable tests against **one** AIAC stack, plus a
> **deferred** two-policy rung:
>
> | Rung | Issue | Onboards | Proves |
> |---|---|---|---|
> | 1 | `testing/5.4.1-uc1-onboard-agent-only.md` | agent only | agent discovery + inbound generation stand alone; outbound empty with no tool |
> | 2 | `testing/5.4.2-uc1-onboard-agent-then-tool.md` | agent → tool | onboarding the tool **after** the agent completes the agent's outbound (PCE additive merge) |
> | 3 | `testing/5.4.3-uc1-onboard-tool-then-agent.md` | tool → agent | the happy path; **and, vs rung 2, onboarding-order-independence** |
> | 4 | `testing/5.4.4-uc1-onboard-two-policies.md` | two policies | **deferred / TBD**; two-stack impl discarded |

> **Relationship to `policy-pipeline`.** This is the **discovery-driven sibling** of
> [policy-pipeline.md](policy-pipeline.md). Identical *scenario facts and truth tables* (same three users,
> same role→access facts, same inbound/outbound matrices); the difference is provenance — `policy-pipeline`
> *hand-provisions* the agent/tool roles/scopes in process, this ladder *infers them via real UC-1
> onboarding* of deployed workloads. That inference makes the generated Rego **semantically similar but not
> byte-identical** to `policy-pipeline` (see *[Semantic similarity](#semantic-similarity-not-byte-identity)*).

## Location

`aiac/test/integration/` — pytest modules marked `@pytest.mark.integration`, one per rung (or one module
with one test per rung). They import two shared modules:

- `scenario_uc1.py` — the pure-data scenario (users/roles + the pair-lists expressed over the
  **discovered, workload-prefixed** names `github-tool.source-read`, `github-agent.source_operations`, …).
  The old two-variant machinery (`VARIANTS`, `POLICY_EXPLICIT`, per-variant URLs/pods) is removed; the
  truth tables (`USERS`, `USER_ROLES`, `INBOUND_PAIRS`, `OUTBOUND_SUBJECT_PAIRS`, `TOOL_SCOPES`,
  `AGENT_SCOPES`, `AGENT_ROLE`) and the single **abstract** `policy.md` remain.
- `launcher.py` — the shared `kubectl`/port-forward + `opa` helpers (`kubectl`, `kubectl_cp`,
  `port_forward`, `resolve_pod`, `opa_eval`, `require_env`, …).

They also ship `probe_uc1.rego` — the outbound verification query, matching `input.function_name` against
the generated data maps by **exact string equality** on full discovered names, binding **both** the user
(subject) gate and the agent capability gate (the per-scope two-gate AND).

## Description

`@pytest.mark.integration` tests that validate the **phase-1 deliverable** and confirm the runnable demo:
they drive the **real UC-1 Service Onboarding agent** (`POST /apply/service/{id}` on the in-cluster AIAC
Controller) against **already-deployed** `github-agent` + simplified `github-tool`, and assert the
generated Rego decides correctly using the standalone `opa eval` binary as the oracle.

Phase-1 is explicit that **live enforcement / live traffic is out of scope** — correctness is shown by
**evaluating the generated rules**, not by routing requests. So each rung is *onboard + evaluate*: the
workloads are really deployed and really discovered by UC-1 (classify from the `rossoctl.io/type` label,
read the AgentCard / MCP `tools/list`, provision roles/scopes into Keycloak, model access, emit Rego), but
**no A2A message is ever sent through the agent**.

The generated Rego is the **artifact under test** — the LLM/PCE that produced it might be wrong — so the
tests never trust it. Expected verdicts are **computed from** the `scenario_uc1.py` pair-lists (the
intended policy). A mismatch fails the test and names the exact cell.

Because they need a live rossoctl cluster + operator + Keycloak + a real LLM, they are
`@pytest.mark.integration` (out of the default unit run, `-m "not integration"`) and additionally
`pytest.skip` when no `opa` binary is found.

## Topology

- **One in-cluster AIAC stack.** A single AIAC agent (Controller, `POST /apply/service/{id}`) + Policy
  Store + **OPA Policy Writer (filesystem stub)**, mounting the **single abstract** `policy.md`. AIAC runs
  in-cluster so UC-1's `analyze_tool` can reach the tool's MCP endpoint at its cluster-internal DNS name
  (`github-tool.{ns}.svc.cluster.local`); the tests trigger over `kubectl port-forward`.
- **OPA filesystem-stub writer, not the legacy Keycloak composite writer.** The stack must run
  `aiac-pdp-policy-opa` (the filesystem stub: writes `{slug}.inbound.rego` + `{slug}.outbound.rego` to
  `REGO_OUTPUT_DIR`, default `/rego`) — this is what the K8s Phase 1 Interface Pod actually deploys.
  **Not** the superseded `aiac-pdp-policy-keycloak` composite writer, which manages Keycloak composite
  roles and emits **no Rego at all**, and is not deployed by any current manifest. The `.rego` files are
  the artifact under test; without the OPA writer there is nothing to capture.
- **Rego capture.** `kubectl cp` the writer's `/rego` to a per-rung host dir (a `rung{1,2,3}` subfolder
  under the gitignored `test/integration/rego_out/uc1/` tree, so artifacts stay in the project for
  eyeballing but are never committed; each rung clears its dir first), then run `opa eval` on the host.

## Preconditions (assumed, not performed by the tests)

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
  > `"{ns}/github-agent"`, then trigger with that client's **`id`** (the UUID).
- **Users + realm roles.** The fixture provisions them (UC-1 does not) — see
  *[Scenario](#scenario)* — via `KeycloakAdmin` into `AIAC_TEST_REALM`, **before** onboarding; idempotent;
  left in place.

## Per-rung flow

**Keycloak cleanup → onboard (rung order) → validate end state → Keycloak cleanup.**

1. **Cleanup** (before and after each rung). Unmap composites and delete the **agent's and tool's**
   provisioned realm roles + client scopes, leaving the clients registered exactly as before the first
   run; clear the writer's `/rego`. This gives every rung a clean slate and makes reruns converge. (With
   the OPA writer there are no composites — the only Keycloak mutations are the roles/scopes the onboarding
   agent provisions — but cleaning both is harmless and future-proofs against the Keycloak writer.)
2. **Onboard** in the rung's order via `POST /apply/service/{service_id}`, where `{service_id}` is the
   internal Keycloak UUID (`Service.id`), **not** the clientId — the onboard route is keyed on the UUID.
   - `POST /apply/service/{github-tool id}` → UC-1 classifies it a **Tool**, reads the MCP manifest,
     provisions scopes `github-tool.{source-read, source-write, issues-read, issues-write}`, sets
     `client.type=Tool`. **No rules are written for the tool directly.**
   - `POST /apply/service/{github-agent id}` → UC-1 classifies it an **Agent**, reads the AgentCard,
     provisions **one operator role per skill** `github-agent.{source_operations, issue_operations}`
     (mirroring the scopes) + scopes `github-agent.{source_operations, issue_operations}`, sets
     `client.type=Agent`; the Service Policy Builder maps roles→scopes via the real PRB (real LLM,
     `temperature=0`) and the Controller calls `compute_and_apply(rules, override=False)`.
3. **Validate two outcomes at the end** (no intermediate checks):
   1. **Keycloak provisioning.** The expected realm role(s) + client scopes exist with the expected
      names/descriptions (via `KeycloakAdmin` / the IdP Configuration read API).
   2. **Generated Rego decisions.** `kubectl cp` the `/rego` files to the host and `opa eval`:
      - **`opa` discovery** — `$OPA_BIN` → `shutil.which("opa")` → `pytest.skip`.
      - **Inbound** — per `subject`: `{"subject": <id>}` vs `data.authz.team1_github_agent.inbound.allow`.
      - **Outbound (per-scope two-gate AND)** — per `(subject × function_name)`, `function_name` a full
        discovered tool-scope name, via the probe `data.probe.outbound.allow` in `probe_uc1.rego`, which
        binds `input.function_name` against **both** the user (subject) gate and the agent capability
        gate by exact string equality — a request is allowed iff both reach the same scope (see
        *[The agent→tool gate](#the-agenttool-gate-capability-matched)*).
      - **Grant sets** — re-derive the `(role, scope)` grant sets from the Rego data maps and compare, as
        order-independent sets, to the `scenario_uc1.py` truth table.
      - Verdicts are **computed from** `scenario_uc1.py`, never from the Rego. A failing node names the
        exact cell.
4. **Cleanup** — restore the clients to their pre-run state.

## Onboarding order is irrelevant (rungs 2 vs 3)

The **final** policy must not depend on the order services are onboarded. This is a **requirement**: if
onboarding order changes the end state, that is a **bug** the ladder exists to catch — not an accepted
difference. (This corrects an earlier "order matters" note in this spec and the tracking issue.)

Why it holds: `compute_and_apply` is **affected-agent** oriented and **additive** (`override=False`, see
[../components/policy-computation-engine.md](../components/policy-computation-engine.md)). When the **tool**
is onboarded, its Service Policy Builder pairs the tool's scopes against the rest of the role universe,
producing `(agent-role, tool-scope)` and `(user-role, tool-scope)` rules; the PCE resolves those roles to
the **agent** and merges them onto the agent's stored `AgentPolicyModel`, rewriting
`team1_github_agent.outbound.rego`. So:

- **Rung 2 (agent → tool):** agent onboarding leaves outbound empty; **tool onboarding fills it in**.
- **Rung 3 (tool → agent):** the tool's scopes already exist, so **agent onboarding produces the full
  gate** in one pass.
- **Both converge** to the same grant sets. Rung 3 asserts grant-set equivalence with **rung 2**; a
  divergence fails and names the differing gate.

Rung 1 (agent only) is the exception by construction: with no tool onboarded there are no tool scopes in
the universe, so the outbound user gate is **empty** (all deny). Inbound is unaffected.

## Expected output

Verdicts are **computed from** the `scenario_uc1.py` pair-lists (these tables are the human-readable
rendering). They are **identical to policy-pipeline's** (only the scope-name strings differ).

`USERS`: `dev-user`→`developer`, `test-user`→`tester`, `devops-user`→`devops`.

**Inbound allow** (`data.authz.team1_github_agent.inbound.allow`; all rungs):

| Subject | Inbound |
|---|---|
| dev-user | ✅ |
| test-user | ✅ |
| devops-user | ❌ |

**Outbound allow(subject, function)** (`data.probe.outbound.allow`, per-scope two-gate AND; the agent
reaches all four tool scopes, so the user gate discriminates; suffixes shown for readability) —
**rungs 2 and 3** (with a tool onboarded):

| | github-tool.source-read | github-tool.source-write | github-tool.issues-read | github-tool.issues-write |
|---|---|---|---|---|
| dev-user | ✅ | ✅ | ✅ | ❌ |
| test-user | ❌ | ❌ | ✅ | ✅ |
| devops-user | ❌ | ❌ | ❌ | ❌ |

**Rung 1 (agent only):** the outbound table is **entirely deny** (empty user gate — no tool scopes).

Each rung leaves on disk exactly `{AGENT_SLUG}.inbound.rego` + `{AGENT_SLUG}.outbound.rego`; explicitly
**no** `github_tool.*.rego` (the tool is a pure target; "no rules written for the tool alone").
`AGENT_SLUG` is the Rego-package slug derived from the agent's clientId (`{namespace}/{name}`,
extracted from the SPIFFE URI under SPIRE) — `team1_github_agent` on the reference cluster's
`team1`/`github-agent` scenario, not a literal `github_agent` (see
[pdp-policy-writer-opa.md § Rego package structure](../components/pdp-policy-writer-opa.md#rego-package-structure)
for the slugify rule).

### Semantic similarity, not byte-identity

This ladder's Rego is **semantically similar** to `policy-pipeline`'s but **not byte-identical**, for two
baked-in reasons in UC-1 provisioning:

1. **Workload-prefixed names.** UC-1 names every scope `{workload}.{name}`, so the data maps hold
   `github-tool.source-read` / `github-agent.source_operations` where `policy-pipeline` holds bare names.
2. **Capability-matched `target_ok`.** UC-1 provisions one **operator role per skill**
   (`github-agent.source_operations` / `github-agent.issue_operations`), which the PRB maps to the tool
   scopes by domain (capability-match), so the agent→tool gate is populated over all four tool scopes.

The tests therefore assert **same file set + same decisions + equivalent grant sets**, not identical text.

### The agent→tool gate (capability-matched)

Phase-1 states outbound access is the **per-scope intersection** of the user→tool gate and the
agent→tool gate. UC-1 provisions **one operator role per skill**
(`github-agent.source_operations` / `github-agent.issue_operations`), and the PRB maps those operator
roles to the tool scopes by domain (capability-match under `generic_policy.md`), so `target_ok` is
**populated over all four tool scopes**. Because the agent reaches every tool scope, the **user gate
discriminates** — the probe binds the real per-scope AND (`subject_ok AND target_ok` on the same
`input.function_name`) and, for this scenario, its verdicts equal the user-gate slice. The AND is
genuine, not degenerate: if the agent reached only a subset of the tool's scopes, the request would be
denied for the scopes it does not reach.

## Scenario

Identical role→access facts to `policy-pipeline`, driven through real UC-1 onboarding of deployed
workloads.

| Element | Value |
|---------|-------|
| Realm | `AIAC_TEST_REALM` (must match the deployed stack's `KEYCLOAK_REALM`; default `rossoctl`) |
| Agent | `github-agent` — **discovered** per-skill operator roles `github-agent.source_operations`, `github-agent.issue_operations` (mirroring the scopes); scopes `github-agent.source_operations`, `github-agent.issue_operations` (from AgentCard skills) |
| Tool | `github-tool` (simplified) — **discovered** scopes `github-tool.{source-read, source-write, issues-read, issues-write}` (from MCP `tools/list`) |
| Users | `dev-user` (`developer`), `test-user` (`tester`), `devops-user` (`devops`) |
| `developer` | source read/write + issues read |
| `tester` | issues read/write |
| `devops` | no access (inbound deny; denied every outbound function) — conveyed by **role description only**, absent from the `policy.md` (deny-by-default) |

## Configuration (env)

| Variable | Purpose | Default |
|----------|---------|---------|
| `KUBECONFIG` | Kubeconfig for the live rossoctl/Kind cluster | — (required) |
| `AIAC_DEMO_NAMESPACE` | Namespace the demo workloads are deployed in (precondition) | `team1` |
| `KEYCLOAK_URL` | External Keycloak base URL | — (required) |
| `KEYCLOAK_ADMIN_REALM` | Realm the admin creds live in | `master` |
| `KEYCLOAK_ADMIN_USERNAME` / `KEYCLOAK_ADMIN_PASSWORD` | Keycloak admin creds (user/realm-role provisioning + cleanup) | — (required) |
| `AIAC_TEST_REALM` | Realm the tests resolve/provision against. **Must match the deployed AIAC stack's `KEYCLOAK_REALM`** — the in-cluster Controller resolves the onboarding trigger in *its own* realm, so a harness on a different realm resolves a client UUID the Controller can't find (404 → onboard 502). The demo namespace's clients are registered into it. | `rossoctl` |
| `AIAC_CONTROLLER_URL` | Base URL of the in-cluster AIAC Controller (via port-forward) for `POST /apply/service/{id}` | `http://127.0.0.1:7070` |
| `AIAC_OPA_POD` / `AIAC_OPA_SELECTOR` | OPA-writer pod (or label selector) to `kubectl cp` `.rego` from | — (resolved from labels) |
| `AIAC_OPA_REGO_PATH` | Writer output dir inside the pod | `/rego` |
| `REGO_OUTPUT_DIR` | Base dir the captured `.rego` is copied to (one `rung{1,2,3}` subfolder per rung) | `test/integration/rego_out/uc1/` (gitignored) |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | PRB LLM (pinned `temperature=0`); consumed by the in-cluster AIAC pod | — (required) |
| `OPA_BIN` | Path to the standalone `opa` binary (oracle); else `PATH`, else `pytest.skip` | — (optional) |

> Single stack — one Controller URL, one OPA pod, one policy. The two-variant env
> (`AIAC_EXPLICIT_URL`/`AIAC_ABSTRACT_URL`, `AIAC_OPA_POD_EXPLICIT`/`_ABSTRACT`) is removed with the
> two-stack topology.

## Runbook

Runnable against a live rossoctl/Kind cluster (operator + Keycloak + SPIRE) with the AIAC stack running the
**OPA filesystem-stub writer**, `github-agent` + `github-tool` **already deployed and registered** into
`AIAC_TEST_REALM`, a real LLM, and an `opa` binary on `PATH` (or `$OPA_BIN`).

```bash
# env: KUBECONFIG + KEYCLOAK_URL + admin creds + LLM_* set; realm defaults to rossoctl (match the stack's KEYCLOAK_REALM); opa on PATH or $OPA_BIN
.venv/bin/pytest test/integration/ -m integration -k uc1_onboard -v
# A failing node names the exact cell, e.g.:
#   test_outbound[test-user-github-tool.source-read] — expected deny, opa allowed
```

The suite `pytest.skip`s when no `opa` binary is found.

## Testing Decisions

- **Highest seam available, verified by a real oracle.** Real deployed workloads + real operator + real
  UC-1 onboarding + real PRB/PCE + real Keycloak + real LLM, driven through the production trigger
  (`POST /apply/service/{id}`), verified by the standalone `opa eval` binary. Assert only **external
  behavior** — the decisions the Rego makes — never internal Rego structure.
- **Rego is the artifact under test; the scenario is the oracle.** Verdicts computed from `scenario_uc1.py`.
- **Onboard + evaluate, no live traffic.** Enforcement / token-exchange / live A2A is out of scope.
- **Deployment is a precondition.** The tests do not deploy or wait for registration; they cleanup →
  onboard → validate → cleanup, so reruns are hermetic and cheap.
- **One stack, one policy, OPA filesystem stub.** Rungs 1–3 need only one AIAC stack; the OPA writer's
  `/rego` output is what makes the pipeline observable. The Keycloak composite writer emits no Rego and is
  not used.
- **Onboarding-order-independence is asserted, not assumed** (rungs 2 vs 3). A divergence is a bug.
- **Per-scope two-gate AND.** UC-1's per-skill operator roles are mapped to the tool scopes by
  capability-match, so `target_ok` is populated; the outbound probe binds the real per-scope AND
  (`subject_ok AND target_ok` on the same `input.function_name`). The agent reaches all four tool
  scopes, so the user gate discriminates.
- **Grant sets, semantic.** Equivalence is re-derived from the Rego data maps and compared as sets — the
  semantic-similarity guarantee, not byte-identity.
- **Stack's realm, leave-in-place; per-rung cleanup.** UC-1 resolves/provisions against the deployed
  stack's `KEYCLOAK_REALM` (default `rossoctl`) and **never deletes** the realm/users/roles; only the
  provisioned agent/tool roles/scopes are cleaned up per rung so onboarding runs from a clean slate.
  (Contrast `5.3 policy-pipeline`, which owns a **throwaway** realm it `delete_realm`s + recreates each
  run — that suite must never point `AIAC_TEST_REALM` at `rossoctl`, or it destroys the demo clients.)
- **LLM nondeterminism, contained.** PRB LLM pinned `temperature=0`; both cell-level and grant-set
  assertions; `@pytest.mark.integration`, out of default CI.
- **Prior art, shared not copied.** Reuses the `5.3` shape (`opa` discovery/skip, scenario-as-oracle,
  probe query) via `launcher.py`/`scenario_uc1.py`, adapted to deploy-precondition + port-forward +
  `kubectl cp`.

## Relationship to other integration tests

- **Discovery-driven sibling of `policy-pipeline`** ([policy-pipeline.md](policy-pipeline.md),
  `testing/5.3-policy-pipeline-integration-test.md`): identical scenario facts/tables, but this ladder
  *infers* the entities via real UC-1 onboarding of deployed workloads. `5.3` also already asserts the
  **cross-variant** (explicit vs abstract) grant-set equivalence in process, which covers the deferred
  rung 4's core guarantee until an in-cluster two-policy approach is designed.
- Same `@pytest.mark.integration` + `opa eval` flavor as `testing/5.1-integration-tests.md` and
  `policy-pipeline`; skips when `opa` is absent.

Tracking issues: `testing/5.4-uc1-onboarding-integration-test.md` (epic) + `5.4.1`/`5.4.2`/`5.4.3` (rungs)
+ `5.4.4` (deferred two-policy).

## Out of Scope

- **Writing the rung tests + `probe_uc1.rego` + `scenario_uc1.py` edits** — this spec *describes* them;
  they are written under the `5.4.x` issues.
- **The UC-1 agent, PRB, PCE, OPA writer, and demo `github-agent`** — specified/tested by their own
  components/issues. UC-1's discovery naming and per-skill operator-role behavior are **fixed**; these
  tests observe them.
- **Deploying / registering the workloads** — a precondition, not part of the tests.
- **Two-policy (rung 4)** — deferred; the two-stack topology is discarded and the in-cluster approach is
  TBD (`testing/5.4.4-uc1-onboard-two-policies.md`).
- **Live enforcement / A2A / token exchange / K8s-CR Policy Writer** — Phase-2+; these tests target the
  filesystem stub and evaluate rules.
- **Default-CI wiring** — `@pytest.mark.integration`; runs on demand.

## Scenario inputs

**Functional** inputs — the PRB reads the descriptions and the `policy.md` to produce the role→scope
mappings. Descriptions are **generic and keyword-free**; client `type` is set by UC-1 from the
`rossoctl.io/type` label.

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
descriptions to the tool-scope descriptions, not from the policy naming them. Deny by default.

```markdown
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
```

> The **explicit** enumerated variant and cross-variant equivalence are deferred to rung 4
> (`testing/5.4.4-uc1-onboard-two-policies.md`); the two-stack topology that served both variants is
> discarded.
