# AIAC Codebase Guide

All paths below are relative to `kagenti-extensions/aiac/`.

## Requirements / PRD docs

`inception/requirements/PRD.md` — master PRD.
`inception/requirements/components/` — per-component specs.

For current file list, `ls inception/requirements/` and `ls inception/requirements/components/`.

## Requirements directory — link-following policy

When a document under `inception/requirements/` contains a markdown link to another file, use the AskUserQuestion tool to ask before reading it — present "Yes" and "No" as clickable options. If the user picks Yes, read the file normally. If No, treat the link as a label and continue without reading it.

## Issue tracking

Issues are tracked as local markdown files under `inception/issues/`, not on GitHub.
Never use `gh` commands to create, update, or list issues — always read/write the local files directly.

`inception/issues/implementation-plan.md` — overall implementation plan.
For current issue list, `ls` the subdirectories under `inception/issues/`.

## Issue tracking — codebase inspection policy

When working on an issue would benefit from inspecting the relevant source code, use the AskUserQuestion tool to ask before doing so — present "Yes" and "No" as clickable options. If the user picks Yes, inspect the codebase normally. If No, work from the issue description and existing context only.

## Handoffs

Per-task handoff documents live under `inception/handoffs/` — one markdown file per task, numeric-prefixed (e.g. `01-update-issues.md`, `02-update-source-and-tests.md`). When asked to generate a handoff, write it here (not a scratch/temp path). Each handoff must be self-contained — background, task, exact files, and acceptance criteria — so a fresh session can execute it without the originating conversation.

## Source code

`src/aiac/` — Python package root (`__init__.py` is empty).

Key stable structure:
- `idp/` — IdP configuration service and models
- `pdp/` — PDP policy writer service and library
- `agent/` — reset pending a fresh rebuild; currently only `__init__.py` plus the archived `onboarding.old/`. The prior controller/orchestrator/shared implementation was removed as stale (built on the superseded `ProposedDiff` model).
  - `agent/onboarding.old/policy/` — archived prior implementation (was FROZEN); not part of the active build

Pending namespaces (to be added per PRD): `policy/model/`, `policy/store/`, `policy/computation/`.

For current file list, `ls` or `find` under `src/aiac/`.

## Tests

`test/` — mirrors `src/aiac/` structure. For current file list, `ls` under `test/`.

**Unit test command** (`test/policy/` excluded — frozen imports cause collection errors):

```bash
.venv/bin/pytest test/ --ignore=test/policy/ -m "not integration"
```

Use `ls test/` to discover current test directories.

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
| `aiac-pdp-config` | `src/aiac/idp/service/configuration/keycloak/Dockerfile` |
| `aiac-pdp-policy-opa` | `src/aiac/pdp/service/policy/opa/Dockerfile` |
| `aiac-policy-store` | `src/aiac/policy/store/service/Dockerfile` |
| `aiac-rag-ingest` | `rag-ingest/` (separate directory) |

> `aiac-agent` (was `src/aiac/agent/controller/Dockerfile`) is temporarily removed — the agent layer was reset and will be rebuilt; the image and its Dockerfile will return with it.
