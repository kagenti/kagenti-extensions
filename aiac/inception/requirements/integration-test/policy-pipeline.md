# Integration Test: policy-pipeline — `policy_pipeline.py`

> **One spec among several.** This document specifies a **single** integration test.
> Integration-test specs live **one spec per test** under `inception/requirements/integration-test/`
> (a sibling of `components/`), and the master PRD's *Integration test specifications* section
> ([../PRD.md](../PRD.md)) is the index of them. This is the **policy-pipeline** integration test —
> the full identity→policy pipeline — not the definition of integration testing in general, and not
> the only integration-test PRD.

## Location
`aiac/test/integration/policy_pipeline.py`

## Description

A standalone launcher script that drives the **whole identity→policy pipeline** —
**Keycloak → PRB → PCE → OPA Policy Writer** — end-to-end and leaves the generated Rego on disk for a
human to eyeball. It is **not** a pytest test, **not** part of CI, and **not** marked
`@pytest.mark.integration` — it is run by hand when an operator wants to see the actual `.rego` output
produced by the real pipeline for a known scenario.

This is the same *flavor* as the PDP Policy Writer launcher
([pdp-policy-writer.md](pdp-policy-writer.md), issue `testing/5.2-pdp-writer-integration-test.md`) but
**broader**: where `5.2` hand-builds a `PolicyModel` in Python and POSTs it to the OPA stub —
deliberately bypassing Keycloak, the PRB, and the PCE — this test provisions a **live Keycloak** realm,
calls the real **Policy Rules Builder (PRB)** to map roles→scopes with a real LLM, then calls the real
**Policy Computation Engine (PCE)** to build the `PolicyModel` and drive the **OPA Policy Writer** to
emit Rego. Nothing is mocked; the only shortcut is that the OPA target is the filesystem stub
([../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md) §1.14) rather than the
Kubernetes-CR implementation, so the output is `.rego` files instead of a patched
`AuthorizationPolicy` CR — identical to `5.2`.

### What it does

1. **Set service URLs in env before importing the aiac libraries.** Export `AIAC_PDP_CONFIG_URL`,
   `AIAC_POLICY_STORE_URL`, `AIAC_PDP_POLICY_URL`, and `AIAC_REALM` *before* importing the aiac
   libraries — the libraries read env at import time. This is the pattern
   `test/pdp/policy/generate_rego.py` already follows.
2. **Spawn the three services as `uvicorn` subprocesses** (no Docker) and poll each `GET /health`
   until ready, with a bounded timeout:
   - IdP Configuration Service — `aiac.idp.service.configuration.keycloak.main:app` on `7071`.
   - Policy Store — its ASGI app on `7074`, with `AGENTPOLICY_DB_PATH` pointed at a fresh temp dir.
   - OPA Policy Writer — `aiac.pdp.service.policy.opa.main:app` on `7072`, with `REGO_OUTPUT_DIR` and
     the Policy Store DB path in its env.
3. **Provision Keycloak** (idempotent — delete-if-exists the realm first, then create):
   - via **`python-keycloak` `KeycloakAdmin`** (test fixture): create realm `AIAC_TEST_REALM`; create
     users `dev-user` and `test-user`; create realm roles `developer` and `tester`; assign roles to
     users; create the `github-agent` and `github-tool` clients with the descriptions in
     *[Scenario inputs](#scenario-inputs-prb-functional-inputs)* so the IdP library's `type` inference
     (`_build_service`) tags them **Agent** / **Tool**.
   - via the **aiac IdP `Configuration` library** (the real product surface the PCE reads back): create
     the client roles (`source-helper`, `issues-helper`) and scopes (`source-access`, `issues-access`,
     `source-read`, `source-write`, `issues-read`, `issues-write`) with the descriptions in
     *[Scenario inputs](#scenario-inputs-prb-functional-inputs)*, and map roles→services and
     scopes→services so `get_services_by_role` / `get_services_by_scope` and `get_service().roles` /
     `.scopes` resolve correctly.
4. **Proto-UC1 orchestration** — run the three PRB mappings against a pinned LLM (`temperature=0`) and
   concatenate the results into one `list[PolicyRule]`:
   - **(a)** `build_scope_rules(user_roles, agent_scope)` per agent scope → user→agent-scope rules.
   - **(b)** `build_scope_rules(user_roles, tool_scope)` per tool scope → user→tool-scope rules.
   - **(c)** `build_role_rules(agent_role, tool_scopes)` per agent role → agent-role→tool-scope rules.

   Concatenate into a single `list[PolicyRule]` and call
   `aiac.policy.computation.engine.compute_and_apply(rules, override=False)` against a **fresh** Policy
   Store. The PCE resolves the IdP relationships, builds the `github-agent` model (with `agent_roles` /
   `agent_scopes`; mapping (b) routed into `outbound_subject_rules`; and **no** `github-tool` model),
   writes it to the store, and pushes it to the OPA stub.
5. **Terminate the three subprocesses in `finally`.**
6. **Print** `REGO_OUTPUT_DIR` and the two `.rego` filenames.

**Write-only.** The script performs no read-back and makes **no assertions**. The realm is left in
place; the `.rego` files are left on disk for eyeballing. There is no pass/fail exit contract beyond
the script running to completion.

## Expected output

Exactly **two** files in `REGO_OUTPUT_DIR`:

- `github_agent.inbound.rego` — package `authz.github_agent.inbound`; the **user→agent** gate.
  `subject_roles` = `{dev-user: [developer], test-user: [tester]}`; `agent_scopes` populated.
- `github_agent.outbound.rego` — package `authz.github_agent.outbound`; `allow if { subject_ok;
  target_ok }`. Its **`subject_ok`** is the new **user→tool** gate (mapping (b), grouped from
  `outbound_subject_rules` into `outbound_subject_role_scopes`, matched against
  `target_scopes[input.target]`); its **`target_ok`** is the **agent→tool** gate (mapping (c), over
  `agent_roles` × `agent_role_scopes`). `agent_roles` and `target_scopes` are populated.

Explicitly **no** `github_tool.*.rego` — the pipeline emits no tool model. Eyeball both files against
the **ID-only** package shapes in
[../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md)
(§ *Rego package structure*), the same source of truth `5.2` uses.

## Scenario

A single agent + tool + two users, fixed so the generated Rego is reproducible and reviewable by
inspection. This is the same canonical `github-agent` worked example as `5.2`, driven end to end
through the real pipeline rather than a hand-built `PolicyModel`.

| Element | Value |
|---------|-------|
| Realm | `AIAC_TEST_REALM` (default `aiac-e2e`) |
| Agent | `github-agent` (client roles `source-helper`, `issues-helper`; scopes `source-access`, `issues-access`) |
| Tool | `github-tool` (scopes `source-read`, `source-write`, `issues-read`, `issues-write`) |
| Users | `dev-user` (role `developer`), `test-user` (role `tester`) |
| `developer` | source read/write + issues read |
| `tester` | issues read/write |

Role → access (confirmed with the user; the single source of truth the descriptions and both
`policy.md` versions below must agree with):

- `developer` — source read/write, issues read.
- `tester` — issues read/write.

## Configuration (env)

| Variable | Purpose | Default |
|----------|---------|---------|
| `KEYCLOAK_URL` | External Keycloak base URL | — (required) |
| `KEYCLOAK_ADMIN_REALM` | Realm the admin creds live in | `master` |
| `KEYCLOAK_ADMIN_USERNAME` / `KEYCLOAK_ADMIN_PASSWORD` | Keycloak admin creds | — (required) |
| `AIAC_TEST_REALM` | Realm the launcher provisions | `aiac-e2e` |
| `AIAC_REALM` | Realm the PCE reads back (= `AIAC_TEST_REALM`) | `aiac-e2e` |
| `AIAC_PDP_CONFIG_URL` | IdP Configuration Service base URL (set before import) | `http://127.0.0.1:7071` |
| `AIAC_POLICY_STORE_URL` | Policy Store base URL (set before import) | `http://127.0.0.1:7074` |
| `AIAC_PDP_POLICY_URL` | OPA Policy Writer base URL (set before import) | `http://127.0.0.1:7072` |
| `REGO_OUTPUT_DIR` | Dir the OPA stub subprocess writes `.rego` to; printed at end | operator-chosen local dir |
| `AGENTPOLICY_DB_PATH` | Policy Store DB path for the subprocess (fresh temp dir) | temp |
| `AIAC_POLICY_FILE` | PRB whole-file policy — path to the `policy.md` variant to feed the PRB | `/etc/aiac/policy.md` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | PRB LLM (pinned `temperature=0`) | — (required) |

> When the launcher is written, confirm the Policy Store's ASGI import path and its DB-path env-var
> name against the Policy Store component spec / issue — `AGENTPOLICY_DB_PATH` is the placeholder used
> here; use the real one. `AIAC_POLICY_FILE` selects which `policy.md` variant (see
> *[Scenario inputs](#scenario-inputs-prb-functional-inputs)*) the PRB reads.

## Runbook

Runnable only once the pipeline fixes (handoffs 01 + 02, P1–P5) have landed.

```bash
# env: KEYCLOAK_URL + admin creds + LLM_* set; realm defaults to aiac-e2e
.venv/bin/python test/integration/policy_pipeline.py
# then inspect the printed REGO_OUTPUT_DIR, e.g.:
#   github_agent.inbound.rego    (user->agent gate; subject_roles dev-user/test-user)
#   github_agent.outbound.rego   (user->tool AND agent->tool gates)
#   (no github_tool.*.rego)
```

Eyeball against the adjusted package shapes in
[../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md); optionally inspect the
Policy Store DB and the provisioned Keycloak realm.

## Testing Decisions

- **Highest seam available.** Real libraries + real services + real Keycloak + real LLM. The launcher
  drives the pipeline through its real surfaces — the IdP `Configuration` library, the PRB entry points
  (`build_scope_rules` / `build_role_rules`), and the PCE's `compute_and_apply` — and observes the real
  filesystem output. The only shortcut is the OPA filesystem stub (same as `5.2`). It asserts nothing;
  it produces artefacts a human reviews.
- **Self-contained subprocess lifecycle.** The launcher spawns IdP (7071), Policy Store (7074), and OPA
  (7072) as `uvicorn` subprocesses, polls each `GET /health` before use, and tears them all down in
  `finally`. Keycloak and the LLM are **external** (reached via env).
- **Write-only, human-verified.** LLM nondeterminism is tolerated precisely because there are no
  assertions — the reviewer eyeballs the two `.rego` files against
  [../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md). The value is the
  concrete, real-pipeline `.rego` output for a known scenario.
- **Prior art.** `test/pdp/policy/generate_rego.py` (the `5.2` launcher) established the shape this test
  reuses: `uvicorn` subprocess spawn, `GET /health` poll, env-before-import ordering, `finally`
  teardown, and print-the-dir. The live-Keycloak pytest suite (`testing/5.1-integration-tests.md`) is
  the marker-gated counterpart for the read-side services.

## Relationship to other integration tests

This is **one** integration-test spec among several indexed by the master PRD
([../PRD.md](../PRD.md), § *Integration test specifications*).

- Distinct from the **live-Keycloak pytest integration tests** (`testing/5.1-integration-tests.md`) — a
  different flavor: `@pytest.mark.integration`, run in/near CI against a live Keycloak/NATS, asserting
  on typed responses.
- **Broader than** the OPA-stub-only **PDP Policy Writer** launcher
  ([pdp-policy-writer.md](pdp-policy-writer.md), `testing/5.2-pdp-writer-integration-test.md`): `5.2`
  hand-builds a `PolicyModel` and exercises only OPA; this test adds Keycloak provisioning + PRB + PCE
  in front of the **same** OPA stub, so both eyeball their output against the same package shapes.

Tracking issue for this test: `testing/5.3-policy-pipeline-integration-test.md`.

## Out of Scope

- **Writing `policy_pipeline.py` or any P1–P5 pipeline code** — this spec *describes* the launcher; the
  launcher itself is written in a later session against the fixed pipeline (tracked by
  `testing/5.3-policy-pipeline-integration-test.md` and the prerequisite issues).
- **The Rego generator, the canonical policy model, the PRB, and the PCE implementations** — specified
  and unit-tested by their own components ([../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md),
  [../components/policy-model.md](../components/policy-model.md),
  [../components/policy-computation-engine.md](../components/policy-computation-engine.md), and the PRB
  component spec), not here.
- **The Kubernetes-CR Policy Writer (1.13)** — this test targets the filesystem **stub** (1.14) only.
- **Automated pass/fail** — no assertions, no CI wiring, no `@pytest.mark.integration`.

## Further Notes

- The scenario is deliberately fixed. The role→access facts in the *Scenario* table, the entity/role/
  scope descriptions, and **both** `policy.md` versions in *Scenario inputs* must be kept mutually
  consistent — they are a single source of truth. If the role→access facts change, update all three
  together so the eyeballed output stays reviewable.
- Two `policy.md` variants are shipped on purpose (see *Scenario inputs*): an **explicit** one and an
  **abstract** one. `AIAC_POLICY_FILE` selects which the PRB reads, so a reviewer can compare the PRB's
  output on explicit vs. abstract policy text against the same expected Rego.

## Blocked-by

The pipeline can only produce correct output once handoffs 01 (P1, P3) and 02 (P2, P4, P5) land; those
are **resolved**, so this test is ready to be written. Component prerequisites:

- PRB — `agent/3.20-policy-rules-builder.md`
- PCE — `policy/pce/8.10-policy-computation-engine.md`
- Policy model — `policy/model/8.1-policy-model.md`
- OPA filesystem stub — `pdp-policy-writer/1.14-pdp-policy-writer-opa-stub.md`
- Rego package generator — `pdp-policy-writer/1.10-rego-package-generator.md`
- pdp-policy library — `library/pdp/8.9-pdp-policy-library-rename.md`
- Policy Store library / service — `policy/store/8.7-policy-store-library.md` /
  `policy/store/8.5-policy-store-service.md`

## Scenario inputs (PRB functional inputs)

These are **functional** inputs — the LLM reads the entity/role/scope descriptions and the `policy.md`
to produce the role→scope mappings, so they are part of the fixed scenario, not decoration. Confirmed
with the user; keep them in sync with the *Scenario* table (see *Further Notes*).

### Entity descriptions

The `github-agent` and `github-tool` client descriptions deliberately contain the words **"Agent"** /
**"Tool"** so the IdP library's `type` inference (`_build_service`) tags them correctly (needed by the
type-based suppression that omits the tool model).

**`github-agent`** — client (Agent):
> GitHub Agent — an autonomous agent that acts on a user's GitHub source repositories and issue tracker
> on the user's behalf. It performs source-code work (inspecting repository file contents and committing
> changes) and issue-management work (reading issue threads and creating or updating issues). Its
> source-code responsibility is represented by the `source-helper` client role and gated at the agent
> boundary by the `source-access` scope; its issue-management responsibility is represented by the
> `issues-helper` client role and gated by the `issues-access` scope. The agent does not call GitHub
> directly — it delegates each concrete operation to the `github-tool`, so its own scopes describe
> capabilities it may exercise while the tool's scopes describe the operations those capabilities
> resolve to.

**`github-tool`** — client (Tool):
> GitHub Tool — a capability provider that exposes fine-grained, least-privilege operations against
> GitHub source repositories and the issue tracker. It offers four distinct operations, each represented
> by its own scope: read source (`source-read`) and write source (`source-write`) for repository file
> contents, and read issues (`issues-read`) and write issues (`issues-write`) for the issue tracker. The
> tool performs the actual GitHub calls; every caller (such as the `github-agent` acting for a user)
> must present the specific scope for each operation it invokes.

**`developer`** — realm role (user):
> Developer — an engineering user who works on the codebase. A developer needs full read and write
> access to source repository contents (to inspect and change code) and read access to the issue tracker
> (to see reported work), but does not modify issues. Resolves to source read, source write, and issues
> read.

**`tester`** — realm role (user):
> Tester — a quality-assurance user who works through the issue tracker. A tester needs full read and
> write access to issues (to file, triage, and update defect and test reports) but does not touch source
> repository contents. Resolves to issues read and issues write.

### Role & scope descriptions

**Client roles (agent):**

- `source-helper` — The github-agent's client role for source-code operations. Groups the agent's
  ability to read and write repository source content; gated at the agent boundary by `source-access`,
  and downstream resolves to the tool's `source-read` / `source-write`.
- `issues-helper` — The github-agent's client role for issue operations. Groups the agent's ability to
  read and write issues; gated at the agent boundary by `issues-access`, and downstream resolves to the
  tool's `issues-read` / `issues-write`.

**Agent scopes:**

- `source-access` — Agent-boundary scope granting use of the github-agent's source capability (the
  `source-helper` role). A user holding it may invoke the agent's source-code functions.
- `issues-access` — Agent-boundary scope granting use of the github-agent's issues capability (the
  `issues-helper` role). A user holding it may invoke the agent's issue functions.

**Tool scopes:**

- `source-read` — Tool operation: read source repository contents (file listings and file bodies).
  Read-only.
- `source-write` — Tool operation: create, modify, or delete source repository contents (commits /
  file writes).
- `issues-read` — Tool operation: read issues and their comments/threads. Read-only.
- `issues-write` — Tool operation: create and update issues (open, edit, comment, close).

### `policy.md` — Version 1 (explicit)

Each granted `(role, scope)` pair is spelled out; the three sections map 1:1 to PRB mappings (a)/(b)/(c)
and to the expected Rego gates.

```markdown
# Access Control Policy — github-agent / github-tool

Grant access on a least-privilege basis. Only grant a (role, scope) pair when this
policy supports it; deny by default.

## Users → agent capabilities (inbound; user may call the agent)
- developer may use source-access and issues-access.
- tester may use issues-access.

## Users → tool operations (outbound subject; user may reach the tool)
- developer may perform source-read, source-write, and issues-read.
- tester may perform issues-read and issues-write.

## Agent roles → tool operations (outbound target; agent may reach the tool)
- source-helper may perform source-read and source-write.
- issues-helper may perform issues-read and issues-write.
```

### `policy.md` — Version 2 (abstract)

Relies on the PRB / LLM to expand "read and modify source" into the concrete scopes. Encodes the same
role→access facts as Version 1.

```markdown
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
```
