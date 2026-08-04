# Issue tracker: GitHub (AIAC convention)

Issues live as GitHub issues on **`s-and-p-team/cortex`**, organized in the
org-level **AIAC** Project (Projects v2):
<https://github.com/orgs/s-and-p-team/projects/1>. Use the `gh` CLI for all
operations, always scoped with `-R s-and-p-team/cortex` (this repo's `origin`
remote points here, but explicit `-R` avoids ambiguity since `upstream` also
exists).

This is the same convention already documented in `CLAUDE.md` under "Issue
tracking" — this file exists so the engineering skills (`to-issues`, `triage`,
`to-prd`, `qa`) have a single place to read it from.

## Conventions

- **Create an issue**: `gh issue create -R s-and-p-team/cortex --title "..." --body "..." --label aiac`.
  Use a heredoc for multi-line bodies. Always include the `aiac` label plus the
  relevant cumulative `area:<path>` label(s) for the component being touched.
- **Read an issue**: `gh issue view <number> -R s-and-p-team/cortex --comments`.
- **List issues**: `gh issue list -R s-and-p-team/cortex --label aiac --state all`,
  narrowing with additional `--label area:<path>` or `--label aiac-status:<value>`
  filters as needed.
- **Comment on an issue**: `gh issue comment <number> -R s-and-p-team/cortex --body "..."`.
- **Apply / remove labels**: `gh issue edit <number> -R s-and-p-team/cortex --add-label "..."` / `--remove-label "..."`.
- **Close**: `gh issue close <number> -R s-and-p-team/cortex --comment "..."`.

## Hierarchy

The Project groups **Feature**-typed container issues (one per component area,
nested via GitHub **native sub-issues**) over **Task**-typed leaf issues. Every
issue carries the `aiac` label plus cumulative `area:<path>` labels. See
`docs/agents/triage-labels.md` for how triage state is represented.

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `s-and-p-team/cortex` with the `aiac` label and the
appropriate `area:<path>` label(s).

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R s-and-p-team/cortex --comments`.

Filtered web list:
<https://github.com/s-and-p-team/cortex/issues?q=is%3Aissue+label%3Aaiac>
