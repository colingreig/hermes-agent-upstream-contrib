# Label registry

`labels.json` is the single source of truth for every ClickUp tag and every
GitHub label used on this workspace's tasks and PRs. Both surfaces render the
same registry, so the same string means the same thing whether you're looking
at a ClickUp tag or a GitHub label.

## The one rule

**If it's not in `labels.json`, it doesn't exist.** Adding a label is a PR to
this file. Never type a new tag into the ClickUp UI or `gh pr edit --add-label`
a name that isn't in the registry. If you need a label that doesn't exist yet,
add it to `labels.json` first (in the `area` namespace if it's subject matter;
anywhere else needs a real reason, since those namespaces are closed).

## Why this exists

Before this registry, the workspace had accumulated 566 distinct ClickUp tags
across 4 spaces — roughly 490 of them effectively unused. The sprawl included:

- Typos: `agant-avoid` instead of `agent-avoid`.
- Dash-prefixed undo artifacts: `-agent-ready` (a tool's attempt to "remove" a
  tag that just created a new one).
- Epoch-suffixed one-offs: `blocked-operator-1785301312`,
  `attempt-cap-exceeded-1785301312` — a new tag minted every time, never reused.
- Compound literal tags: a single tag literally named
  `prepped lane:code model:sonnet agent-ready` (four concepts mashed into one
  string with spaces).

None of that is enforceable or auditable. This registry, plus the two scripts
below, is the fix.

## Cardinality and what's NOT a tag

Each namespace has a cardinality: `exactly-one`, `zero-or-one`, or
`zero-or-more`. `lane`, `model`, `risk`, `flow`, and `src` are **closed** —
their value sets only change via a PR to `labels.json`. `area` is the one
**open** namespace, for subject-matter growth.

Anything that carries an **id or a timestamp** does not belong in a tag at
all — put it in a comment or a custom field instead:

- `superseded-by-<task-id>` → comment linking to the superseding task.
- `duplicate-of-<task-id>` → comment linking to the original task.
- `blocked-operator-<epoch>` → comment or custom field noting who/what and when.
- `attempt-cap-exceeded-<epoch>` → comment noting the attempt count and timestamp.

If you're about to mint a tag with a variable piece in it (an ID, a number, a
date), stop — that's a comment or custom field, not a label.

## Scripts (interfaces — implementations live alongside this file)

### `python3 tools/labels/migrate_clickup_labels.py`

- `--audit` — report every live ClickUp tag not in the registry, grouped by
  disposition (typo/near-match, undo artifact, epoch-suffixed one-off,
  compound literal, unused, other).
- `--dry-run` (default) — show what would change without writing anything.
- `--apply` — actually apply the migration (retag/remove) to ClickUp.
- `--space <id>` — scope the run to one ClickUp space instead of the whole
  workspace.
- `--yes` — skip the interactive confirmation prompt (for cron/non-interactive
  use).

### `python3 tools/labels/sync_github_labels.py`

- `--repo <owner/name>` — target repo.
- `--dry-run` — show what would change without writing anything.
- `--apply` — create/update/color GitHub labels to match `labels.json` exactly.

## When the fleet moves to GitHub Issues

The same registry renders GitHub labels today (via `sync_github_labels.py`),
so when tasks move off ClickUp onto GitHub Issues, the new tracker starts from
this clean vocabulary instead of re-growing 566 tags from scratch. No new
migration is needed — point the sync script at the target repo and go.
