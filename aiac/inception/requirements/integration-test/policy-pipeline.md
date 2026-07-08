# Integration Test: policy-pipeline — `policy_pipeline.py`

> **One spec among several.** This document specifies a **single** integration test.
> Integration-test specs live **one spec per test** under `inception/requirements/integration-test/`
> (a sibling of `components/`), and the master PRD's *Integration test specifications* section
> ([../PRD.md](../PRD.md)) is the index of them. This is the **policy-pipeline** integration test —
> the full identity→policy pipeline — not the definition of integration testing in general, and not
> the only integration-test PRD.

## Location
`aiac/test/integration/policy_pipeline.py`, plus two shared modules it imports:
`aiac/test/integration/scenario.py` — the canonical `github-agent` scenario as pure data (one of the
role→access fact sources the *Further Notes* mandate — the pair-lists, alongside the *Scenario* table
and both `policy.md` variants) — and `aiac/test/integration/launcher.py` — the shared
`uvicorn` subprocess-lifecycle helpers. The `5.2` launcher `test/pdp/policy/generate_rego.py` was
refactored onto both so the two launchers cannot drift.

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
     *[Scenario inputs](#scenario-inputs-prb-functional-inputs)* and with the `client.type`
     client attribute set to the plain string `"Agent"` / `"Tool"` respectively, so `Service` type
     resolution tags them from the attribute (not from description prose). Set the type via the product
     surface `Configuration.set_service_type(service, type)` (`POST /services/{id}/type`) or by writing
     the `client.type` attribute directly at client create. The attribute value is a plain string,
     **not** a list — a list fails the `in ("Agent","Tool")` check, resolves the type to `None`, and
     yields empty pipeline output.
   - via the **aiac IdP `Configuration` library** (the real product surface the PCE reads back): create
     the client roles (`source-helper`, `issues-helper`) and scopes (`source-access`, `issues-access`,
     `source-read`, `source-write`, `issues-read`, `issues-write`) with the descriptions in
     *[Scenario inputs](#scenario-inputs-prb-functional-inputs)*, and map roles→services and
     scopes→services so `get_services_by_role` / `get_services_by_scope` and `get_service().roles` /
     `.scopes` resolve correctly.
4. **Read-back type guard** — after provisioning, call `Configuration.get_service` for both clients and
   assert each resolved `.type` (`github-agent` ⇒ `Agent`, `github-tool` ⇒ `Tool`) **before** spawning
   the pipeline; abort with a clear message otherwise. This is a provisioning sanity check on the
   `client.type` attribute, **not** a Rego-output assertion — the test stays write-only.
5. **Proto-UC1 orchestration** — run the three PRB mappings against a pinned LLM (`temperature=0`) and
   concatenate the results into one `list[PolicyRule]`:
   - **(a)** `build_scope_rules(user_roles, agent_scope)` per agent scope → user→agent-scope rules.
   - **(b)** `build_scope_rules(user_roles, tool_scope)` per tool scope → user→tool-scope rules.
   - **(c)** `build_role_rules(agent_role, tool_scopes)` per agent role → agent-role→tool-scope rules.

   Concatenate into a single `list[PolicyRule]` and call
   `aiac.policy.computation.engine.compute_and_apply(rules, override=False)` against a **fresh** Policy
   Store. The PCE resolves the IdP relationships, builds the `github-agent` model (with `agent_roles` /
   `agent_scopes`; mapping (b) routed into `outbound_subject_rules`; and **no** `github-tool` model),
   writes it to the store, and pushes it to the OPA stub.
6. **Terminate the three subprocesses in `finally`.**
7. **Print** `REGO_OUTPUT_DIR` and the two `.rego` filenames.

**Write-only.** Apart from the step-4 provisioning type guard (a `Configuration.get_service` `.type`
check on the freshly provisioned attribute), the script reads nothing back and makes **no assertions**
on the pipeline output. The realm is left in place; the `.rego` files are left on disk for eyeballing.
There is no pass/fail exit contract on the generated Rego beyond the script running to completion.

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

Role → access (confirmed with the user; the fixed facts that both `policy.md` versions below and the
`scenario.py` pair-lists must agree with — the generic descriptions are not part of this triad):

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
  filesystem output. The only shortcut is the OPA filesystem stub (same as `5.2`). It makes no
  assertions on the pipeline output; it produces artefacts a human reviews.
- **Attribute-based client typing + read-back guard.** Clients are typed by the `client.type`
  attribute (plain string `"Agent"` / `"Tool"`), provisioned by the launcher — not by description
  keywords. Because that attribute drives whether the PCE emits an agent model (and suppresses the tool
  model), the launcher reads each service back via `Configuration.get_service` and asserts its `.type`
  before running the pipeline, aborting on mismatch. This is a **provisioning** sanity check, not a
  Rego-output assertion — the write-only ethos is preserved.
- **Self-contained subprocess lifecycle.** The launcher spawns IdP (7071), Policy Store (7074), and OPA
  (7072) as `uvicorn` subprocesses, polls each `GET /health` before use, and tears them all down in
  `finally`. Keycloak and the LLM are **external** (reached via env).
- **Write-only, human-verified.** LLM nondeterminism is tolerated precisely because there are no
  assertions on the output — the reviewer eyeballs the two `.rego` files against
  [../components/pdp-policy-writer-opa.md](../components/pdp-policy-writer-opa.md). The value is the
  concrete, real-pipeline `.rego` output for a known scenario.
- **Prior art, shared not copied.** `test/pdp/policy/generate_rego.py` (the `5.2` launcher) established
  the shape this test reuses — `uvicorn` subprocess spawn, `GET /health` poll, env-before-import
  ordering, `finally` teardown, and print-the-dir. Rather than duplicate it, that machinery lives in
  the shared `test/integration/launcher.py`, and the fixed scenario lives in
  `test/integration/scenario.py`; `generate_rego.py` was refactored onto both (its `.rego` output
  verified byte-identical to before the refactor). The live-Keycloak pytest suite
  (`testing/5.1-integration-tests.md`) is the marker-gated counterpart for the read-side services.

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

- The scenario is deliberately fixed. The role→access facts are owned by **three** artefacts that must
  agree: the *Scenario* table, **both** `policy.md` versions in *Scenario inputs*, and the
  `scenario.py` pair-lists (`INBOUND_PAIRS` / `OUTBOUND_SUBJECT_PAIRS` / `OUTBOUND_PAIRS`). The
  entity/role/scope **descriptions no longer encode those facts** — they are generic and functional and
  drop out of the fact triad; they must stay generic and simply not contradict the facts. If the
  role→access facts change, update the *Scenario* table, both `policy.md` variants, and the pair-lists
  together so the eyeballed output stays reviewable.
- Two `policy.md` variants are shipped on purpose (see *Scenario inputs*): an **explicit** one and an
  **abstract** one. `AIAC_POLICY_FILE` selects which the PRB reads, so a reviewer can compare the PRB's
  output on explicit vs. abstract policy text against the same expected Rego. The abstract variant now
  carries an agent-capability line (the `source-helper` / `issues-helper` bullet) so mapping (c)
  survives deny-by-default and both variants reproduce the same Rego.
- Descriptions are ≤255 characters and written **verbatim** into Keycloak; there is no shortened /
  verbatim split. (Keycloak caps role and client descriptions at 255 chars, and the generic descriptions
  are authored to stay within that cap.)

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

The descriptions are **generic and keyword-free** — they describe what each entity/role/scope *does*,
carry no policy grant ("Resolves to…") and no owning-client naming, and stay within Keycloak's 255-char
cap so they are written verbatim (no shortened renderings). Client `type` is **not** inferred from
description prose: the launcher provisions each client's `client.type` attribute (the type UC1
discovers from the agent card / `kagenti.io/type` label) as a plain string `"Agent"` / `"Tool"`, so
`Service` type resolution ([../../src/aiac/idp/configuration/models.py:79-87](../../src/aiac/idp/configuration/models.py#L79-L87))
tags each client from the attribute without touching the TEMP description-keyword fallback.

**`github-agent`** — client (Agent):
> Autonomous Agent acting on a user's behalf against source repositories and an issue tracker. It
> inspects and changes repository source contents and reads, creates, and updates issues and their
> threads.

**`github-tool`** — client (Tool):
> Capability provider Tool for source repositories and an issue tracker. It performs read and write
> operations on repository source contents and on issues and their comment threads.

**`developer`** — realm role (user):
> Developer — an engineering user who develops the source codebase (writing and maintaining code) and
> fixes code defects reported in the issue tracker; works primarily in source and consults issues for
> defect reports.

**`tester`** — realm role (user):
> Tester — a quality-assurance user who verifies software quality and tracks defects through the issue
> tracker: filing, triaging, and updating issue reports; works in the issue tracker, not in source.

### Role & scope descriptions

**Client roles (agent):**

- `source-helper` — Client role for source-code operations, covering reading and writing repository
  source content.
- `issues-helper` — Client role for issue-tracker operations, covering reading and writing issues and
  their threads.

**Agent scopes:**

- `source-access` — Scope granting use of a source-code capability — invoking source-code functions such
  as reading and changing repository contents.
- `issues-access` — Scope granting use of an issue-management capability — invoking issue functions such
  as reading and updating issues.

**Tool scopes:**

- `source-read` — Read source repository contents: file listings and file bodies. Read-only.
- `source-write` — Create, modify, or delete source repository contents; commit file changes.
- `issues-read` — Read issues and their comment threads. Read-only.
- `issues-write` — Create and update issues: open, edit, comment, and close.

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
role→access facts as Version 1. The third bullet is an abstract agent-capability line so mapping (c)
(agent-role→tool-scope) survives the PRB's deny-by-default-on-silence rule and both variants reproduce
the same Rego.

```markdown
Grant access on a least-privilege basis: allow only what this policy states; deny by default.

- Developers may read and modify source, and read issues.
- Testers may read and modify issues.
- The source-helper role covers reading and modifying source; the issues-helper role covers reading and modifying issues.
```
