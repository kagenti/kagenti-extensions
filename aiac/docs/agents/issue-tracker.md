# Issue tracker: GitHub (AIAC convention)

Issues live as GitHub issues on **`s-and-p-team/cortex`** (the `origin` fork),
filtered by the `aiac` label. Use the `gh` CLI for all operations, scoped with
`-R s-and-p-team/cortex`.

This file exists so the engineering skills (`to-issues`, `triage`, `to-prd`,
`qa`) have a single place to read the convention from.

## Conventions

- **Create an issue**: `gh issue create -R s-and-p-team/cortex --title "..." --body "..." --label aiac`.
  Use a heredoc for multi-line bodies. Always include the `aiac` label plus the
  relevant cumulative `area:<path>` label(s) for the component being touched.
  `area:<path>` labels don't exist yet — create them lazily with
  `gh label create area:<path> -R s-and-p-team/cortex` the first time a
  component area comes up.
- **Read an issue**: `gh issue view <number> -R s-and-p-team/cortex --comments`.
- **List issues**: `gh issue list -R s-and-p-team/cortex --label aiac --state all`,
  narrowing with additional `--label area:<path>` filters as needed.
- **Comment on an issue**: `gh issue comment <number> -R s-and-p-team/cortex --body "..."`.
- **Apply / remove labels**: `gh issue edit <number> -R s-and-p-team/cortex --add-label "..."` / `--remove-label "..."`.
- **Close**: `gh issue close <number> -R s-and-p-team/cortex --comment "..."`.

## Known account limitation

The `gh` account in use **cannot** `deleteIssue` or `createPullRequest` on this
fork. Don't attempt issue deletion via `gh`; for PRs, push the branch and open
the PR manually (or via the web UI) instead of `gh pr create`.

## Hierarchy

Issues are filtered by `aiac` + cumulative `area:<path>` labels. **Native
sub-issues are configured** on the fork — use them for container/leaf structure:
a `Feature:`-prefixed umbrella issue with `Task:`-prefixed children linked as
native sub-issues. (`Feature`/`Task` are a **title-prefix** convention — native
GitHub *issue types* are not set on the fork, so `issueType` reads `null`.) Set
a child's parent with `gh issue edit <child> -R s-and-p-team/cortex --parent <umbrella>`.

## Project board

There **is** an org-level Project board: **AIAC** (project number `1` on the
`s-and-p-team` owner; id `PVT_kwDOEInZ0c4BfRuR`). (The separate `rossoctl` AIAC
Project #11 still does not apply here.) When you file or update an `aiac` issue,
add it to the board and set its Status field to match the triage label:

- **Add to the board**: `gh issue edit <number> -R s-and-p-team/cortex --add-project "AIAC"`
  (requires the `project` OAuth scope — `gh auth refresh -s project` if missing).
- **Set the Status field**: via the web UI, or `gh project item-edit
  --project-id PVT_kwDOEInZ0c4BfRuR --id <item-id> --field-id <status-field-id>
  --single-select-option-id <option-id>`. Discover the item and field ids with
  `gh project item-list 1 --owner s-and-p-team --format json --limit 300` and
  `gh project field-list 1 --owner s-and-p-team --format json`.

The Status field options are `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `blocked`, `deferred`, `resolved`, `wontfix`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `s-and-p-team/cortex` with the `aiac` label and the
appropriate `area:<path>` label(s).

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R s-and-p-team/cortex --comments`.

## When a skill mentions triage roles

The `triage` skill's five canonical roles (`needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`) map to **both** a `status:<role>`
issue label **and** the matching option on the AIAC board's Status field (see
Project board above); the board adds `blocked` / `deferred` / `resolved` beyond
the five. Apply the `status:<role>` label with `gh issue edit` and set the board
Status field to the same value. There is no `docs/agents/triage-labels.md` to map
against — this section is the mapping.

Filtered web list:
<https://github.com/s-and-p-team/cortex/issues?q=is%3Aissue+label%3Aaiac>
