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

Issues are tracked flat on the fork, filtered by `aiac` + cumulative
`area:<path>` labels. If `Feature`/`Task` issue types and native sub-issues are
configured on the fork, use them for container/leaf structure; otherwise keep
issues flat under the `aiac` label. There is no org-level Project board on the
fork (the `rossoctl` AIAC Project #11 does not apply here).

## When a skill says "publish to the issue tracker"

Create a GitHub issue on `s-and-p-team/cortex` with the `aiac` label and the
appropriate `area:<path>` label(s).

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> -R s-and-p-team/cortex --comments`.

## When a skill mentions triage roles

The `triage` skill's five canonical roles (`needs-triage`, `needs-info`,
`ready-for-agent`, `ready-for-human`, `wontfix`) have no dedicated label mapping
here. Use plain issue open/closed state and the `aiac` label; there is no
`docs/agents/triage-labels.md` to map against.

Filtered web list:
<https://github.com/s-and-p-team/cortex/issues?q=is%3Aissue+label%3Aaiac>
