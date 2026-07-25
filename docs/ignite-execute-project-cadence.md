# ignite-execute project cadence — coarse prioritization

**Status:** authoritative ops runbook for prioritizing one project over another
under `ignite-execute`.
**Audience:** anyone deciding how much executor throughput a project should
get, or standing up a dedicated Hermes cron for a project.

## There is no cross-project priority score

`ignite-execute` has no numeric priority score that ranks tasks or projects
against each other across the fleet. Prioritization today is **intentionally
coarse**: how *often* a project's executor loop fires (cadence), and
optionally how many loops run for it concurrently. There is no weighted
scoring model, and this doc does not propose adding one — it documents the
mechanism that already exists and how to use it deliberately.

## The mechanism: `--list` + a dedicated cron per priority project

`ignite-execute` accepts `--list <listId|client:key>` to scope a single pass
to one project's board directly, instead of letting the resolver's default
list/lists win. That flag is the per-project targeting primitive.

The throughput knob built on top of it is a **dedicated Hermes cron per
priority project**, each on its own schedule:

```
hermes cron create "<interval>" "/ignite-execute --list <id>"
```

A project that should get more attention gets its own cron entry at a
tighter interval; a project that's quiet gets a looser one (or relies on the
default board-wide loop). This is cadence-based prioritization: more
frequent passes over a project's board is how it gets more of the executor's
time, not a priority field on individual tasks.

## Recommended default: 3-tier cadence (tunable)

These are starting points, not fixed policy — tune per project based on
board volume and how time-sensitive its work is:

| Tier   | Suggested cadence     | When to use it                                   |
|--------|------------------------|---------------------------------------------------|
| High   | ~20–25 min             | Active incident work, a project mid-migration, or anything Colin is actively steering |
| Normal | hourly                 | Default for a project with a steady trickle of `agent-ready` work |
| Low    | every 3–4h, or daily    | Low-volume or maintenance-mode projects |

Per `/ignite-execute`'s own cadence guidance, don't loop tighter than
~20–25 minutes for a Claude session — every fire re-reads the skill and
re-pulls the board, so a tighter interval mostly burns tokens re-establishing
context rather than doing more work.

## Per-project concurrency: policy, not new enforcement code

Concurrency across projects is a **policy statement**, not something this
mechanism enforces in code: run at most **one active `ignite-execute` loop
per repo** at a time. This matches `ignite-execute`'s existing shared-cycle-
worktree model — one cycle worktree per pass, one shared batch per repo — so
two concurrent loops against the same repo would collide on that worktree
rather than parallelize safely.

If a project genuinely needs more throughput than one loop's cadence can
provide, tighten that project's cron interval (within the ~20–25 min floor
above) rather than running a second concurrent loop against the same repo.

## Scope

This doc covers the mechanism and defaults only. It does **not** stand up
any real cron entries for specific projects — that's a separate, per-project
decision made when a project actually needs a dedicated cadence.

## See also

- `/ignite-execute`'s own `--list` and `--lane` flag docs (skill body) for
  the full targeting/lane-filter semantics.
- `ignite-state`'s resolver (`references/resolver.md`) for how the default
  (non-`--list`) board resolution works.
