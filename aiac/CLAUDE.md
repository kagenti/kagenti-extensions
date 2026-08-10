# AIAC Codebase Guide

All paths below are relative to `kagenti-extensions/aiac/`.

## Requirements / PRD docs

`docs/specs/PRD.md` — master PRD.
`docs/specs/components/` — per-component specs.

For current file list, `ls docs/specs/` and `ls docs/specs/components/`.

## Requirements directory — link-following policy

When a document under `docs/specs/` contains a markdown link to another file, use the AskUserQuestion tool to ask before reading it — present "Yes" and "No" as clickable options. If the user picks Yes, read the file normally. If No, treat the link as a label and continue without reading it.

## Issue tracking

Issues are tracked as **GitHub issues** on the `s-and-p-team/cortex` repo,
organized in the org-level **AIAC** Project (Projects v2):
<https://github.com/orgs/s-and-p-team/projects/1>. Use `gh` to read and manage
them.

Note: `s-and-p-team/cortex` is the AIAC team's fork of the canonical upstream
`rossoctl/cortex`. Tracking issues on the fork (not upstream) is deliberate —
PRs still target upstream, but issue tracking stays on the team fork, so the
`-R s-and-p-team/cortex` scoping below is intentional.

Hierarchy: the Project groups **Feature**-typed container issues — one per
component area, nested via GitHub **native sub-issues** to form the tree — over
**Task**-typed leaf issues. Every issue carries the `aiac` label plus cumulative
`area:<path>` labels; open issues also carry a `status:<value>` label, and the
Project's built-in **Status** field mirrors that value.

```bash
# list / view (filter to the AIAC set)
gh issue list -R s-and-p-team/cortex --label aiac --state all
gh issue view <number> -R s-and-p-team/cortex
```

Filtered web list:
<https://github.com/s-and-p-team/cortex/issues?q=is%3Aissue+label%3Aaiac>

## Issue tracking — codebase inspection policy

When working on an issue would benefit from inspecting the relevant source code, use the AskUserQuestion tool to ask before doing so — present "Yes" and "No" as clickable options. If the user picks Yes, inspect the codebase normally. If No, work from the issue description and existing context only.

## Handoffs

Per-task handoff documents live under `docs/handoffs/` — one markdown file per task, numeric-prefixed (e.g. `01-update-issues.md`, `02-update-source-and-tests.md`). When asked to generate a handoff, write it here (not a scratch/temp path). Each handoff must be self-contained — background, task, exact files, and acceptance criteria — so a fresh session can execute it without the originating conversation.

## Source code

`src/aiac/` — Python package root (`__init__.py` is empty).

Key stable structure:
- `idp/` — IdP configuration service and models
- `pdp/` — PDP policy writer service and library
- `agent/` — the AIAC Agent layer (rebuilt on the SPM/APM model):
  - `agent/controller/` — FastAPI Controller (`routes.py` + Dockerfile); `/apply/*` routes dispatch to the UC sub-agents and make the single `compute_and_apply` (PCE) call
  - `agent/uc/` — use-case sub-agents: `onboarding/` (provision + policy_builder + orchestrator), `policy_update/` (build/rebuild), `role_update/`
  - `agent/policy_rules_builder/` — PRB: `build_role_rules` / `build_scope_rules` emit `list[PolicyRule]`
  - `agent/shared/` — shared helpers (`roles.py`, e.g. `flatten_role`)
  - `agent/onboarding.old/` — archived prior implementation (built on the superseded `ProposedDiff` model); not part of the active build
- `policy/` — the two-layer policy stack (all implemented):
  - `policy/model/` — `PolicyRule`, `ServicePolicyModel` (SPM), `AgentPolicyModel` (APM), `PolicyModel`
  - `policy/store/` — Policy Store service + library (SPM CRUD)
  - `policy/computation/` — Policy Computation Engine (`compute_and_apply`, SPM-based)

For current file list, `ls` or `find` under `src/aiac/`.

## Tests

`test/` — mirrors `src/aiac/` structure. For current file list, `ls` under `test/`.

**Unit test command:**

```bash
.venv/bin/pytest test/ -m "not integration"
```

The whole `test/` tree (including `test/policy/`) collects and runs green. The Policy
Computation Engine (`aiac.policy.computation.engine`) was migrated to the SPM store surface in
**Wave 3 / Handoff 05**, so the earlier PCE-chain collection failures (which required ignoring
`test/policy/computation`, `test/agent/controller/test_routes.py`, and
`test/integration/test_policy_pipeline.py`) are resolved — no `--ignore` flags are needed.

Use `ls test/` to discover current test directories.

**Integration tests** (`-m integration`) need live config — Keycloak + admin creds + an LLM
endpoint (`opa` on PATH for the policy-pipeline suite). Those variables live in
`test/integration/.env` (gitignored): `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `KEYCLOAK_URL`,
`KEYCLOAK_ADMIN_USERNAME`, `KEYCLOAK_ADMIN_PASSWORD`. Source it before running:

```bash
set -a; . test/integration/.env; set +a
.venv/bin/pytest test/integration/ -m integration
```

**Smoke test** (requires live service at `AIAC_PDP_CONFIG_URL`, default `http://127.0.0.1:7071`):

```bash
.venv/bin/python test/idp/configuration/show_keycloak_data.py
```

Exercises all `Configuration` methods — run `ls test/idp/configuration/` to see current coverage.

## Python environment

Virtual environment: `kagenti-extensions/aiac/.venv`

Activate: `source kagenti-extensions/aiac/.venv/bin/activate`
Run directly: `kagenti-extensions/aiac/.venv/bin/python` / `kagenti-extensions/aiac/.venv/bin/pytest`

Always use this venv for any Python execution, test runs, or dependency checks.

## Kubernetes & builds

Config: `k8s/`, `pyproject.toml`, `pyrightconfig.json`

Docker images:

| Image | Dockerfile location |
|-------|-------------------|
| `aiac-agent` | `src/aiac/agent/controller/Dockerfile` (build context `src/`) |
| `aiac-pdp-config` | `src/aiac/idp/service/configuration/keycloak/Dockerfile` |
| `aiac-pdp-policy-opa` | `src/aiac/pdp/service/policy/opa/Dockerfile` |
| `aiac-policy-store` | `src/aiac/policy/store/service/Dockerfile` |
| `aiac-rag-ingest` | `rag-ingest/` (separate directory) |

### Non-root container / volume-ownership pattern

All AIAC service images run as **non-root UID 10001**. Each service Dockerfile
adds the user before `CMD`, matching the `authbridge/sparc-service` pattern:

```dockerfile
# Drop privileges.
RUN useradd --no-create-home --uid 10001 aiac
USER 10001
```

A Dockerfile `chown` of a directory is **masked once a volume is mounted over
it** (the mounted volume, not the image layer, is what the container sees), and
the kubelet leaves emptyDir/PVC volumes root-owned by default. So any service
that writes to a mounted volume also needs pod-level `securityContext` in its
k8s manifest so the kubelet chowns the volume to the non-root user:

```yaml
spec:
  securityContext:
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001   # makes the mounted volume group-writable by UID 10001
```

Services requiring this today:
- **Policy store** — PVC at `/data` (SQLite backend), `policy-store-statefulset.yaml`
- **PDP policy OPA writer** — emptyDir at `/rego` (`REGO_OUTPUT_DIR`), `pdp-interface-deployment.yaml`

Services that mount no volumes still need the Dockerfile `USER` directive; the
pod-level `fsGroup`/volume-chown block above is only required for those that
write to a mounted volume.

### Pod-security hardening baseline

Beyond non-root, every workload in `k8s/` and the demo manifests carries a
hardened `securityContext`. Pod level (omitted on the demo `github-agent`, whose
injected AuthBridge sidecar runs as UID 1337 — hardening there is set per
container instead so a pod-level `runAsUser` can't clobber the sidecar):

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001        # 1001 for the demo github-agent
    seccompProfile:
      type: RuntimeDefault
```

Container level, on each app container:

```yaml
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
```

With `readOnlyRootFilesystem: true` the container gets a writable `/tmp`
`emptyDir` (and its real data mount — `/data`, `/rego`) so runtime temp writes
have somewhere to land. The demo `github-agent` **omits** `readOnlyRootFilesystem`
because its runtime (`uv` / `litellm` / `crewai`) writes caches under `HOME=/app`.
All core workloads also carry both readiness **and** liveness probes (`httpGet
/health` where the service exposes one; `tcpSocket` for the Controller and the
demo workloads, which don't) and CPU/memory requests + limits.

## External references

- [Kagenti Developer Guide](https://github.com/kagenti/kagenti/blob/main/docs/dev-guide.md) — upstream Kagenti dev guide: per-persona workflows (agent, tool, extensions developers, MCP gateway operators), Git/PR process, pre-commit hooks, feature flags, local Kagenti UI v2 development (React frontend + FastAPI backend, building/deploying images to Kubernetes), and HyperShift-based testing on ephemeral OpenShift clusters (cluster lifecycle, cost management, troubleshooting).

## Agent skills

### Issue tracker

GitHub issues on `s-and-p-team/cortex`, filtered by the `aiac` label, tracked in the org-level AIAC Project. See `docs/agents/issue-tracker.md`.

### Triage labels

Issue status is tracked with `status:<value>` labels; the board's built-in **Status** field carries the same values. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context, scoped to `aiac/` (`CONTEXT.md` + `docs/adr/` at the `aiac/` root). See `docs/agents/domain.md`.
