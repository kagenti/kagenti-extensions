# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those
roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker          | Meaning                                  |
| --------------------------- | ------------------------------ | ----------------------------------------- |
| `needs-triage`              | `aiac-status:needs-triage`     | Maintainer needs to evaluate this issue   |
| `needs-info`                | `aiac-status:needs-info`       | Waiting on reporter for more information  |
| `ready-for-agent`           | `aiac-status:ready-for-agent`  | Fully specified, ready for an AFK agent   |
| `ready-for-human`           | `aiac-status:ready-for-human`  | Requires human implementation             |
| `wontfix`                   | `aiac-status:wontfix`          | Will not be actioned                      |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), apply
the corresponding `aiac-status:<value>` label from the right-hand column via
`gh issue edit <number> -R s-and-p-team/cortex --add-label "aiac-status:..." --remove-label "aiac-status:<old-value>"`.

## Two separate things named "status"

Don't confuse these:

- **`aiac-status:<value>` label** — the source of truth this table maps to.
  Set it directly with `gh issue edit --add-label` / `--remove-label`.
- **`AIAC Status` Project field** — a separate field on the org-level AIAC
  Project (Projects v2) board. It's a display/board-column field, not a label,
  and `gh issue edit` does not touch it. If a skill needs to keep the board in
  sync, that requires `gh project item-edit` against the AIAC Status field —
  don't assume setting the label alone moves the card.

Every open issue also carries the `aiac` label plus cumulative `area:<path>`
labels (see `docs/agents/issue-tracker.md`) — those are unrelated to triage
state and should not be touched by triage operations.
