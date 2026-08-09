# Domain Docs

How the engineering skills should consume this repo's domain documentation
when exploring the codebase. This is scoped to `aiac/` — treated as its own
single context, separate from other components in the `cortex` monorepo (e.g.
`authbridge/`).

## Before exploring, read these

- **`CONTEXT.md`** at the `aiac/` root (this directory).
- **`docs/adr/`** (under `aiac/`) — read ADRs that touch the area you're about
  to work in.

If any of these files don't exist, **proceed silently**. Don't flag their
absence; don't suggest creating them upfront. The producer skill
(`/grill-with-docs`) creates them lazily when terms or decisions actually get
resolved.

## File structure

```
aiac/
├── CONTEXT.md
├── docs/
│   ├── adr/
│   │   ├── 0001-....md
│   │   └── 0002-....md
│   ├── specs/          ← PRD.md + components/ (see CLAUDE.md)
│   └── agents/         ← this file and its siblings
└── src/aiac/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor
proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`.
Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either
you're inventing language the project doesn't use (reconsider) or there's a
real gap (note it for `/grill-with-docs`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than
silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_

## Relationship to `docs/specs/`

`aiac/CLAUDE.md` already documents `docs/specs/PRD.md` and
`docs/specs/components/` as the requirements source, with a link-following
policy for cross-references. `CONTEXT.md` and `docs/adr/` are a different
layer — domain vocabulary and past architectural decisions, not requirements —
and don't replace the `docs/specs/` link-following policy.
