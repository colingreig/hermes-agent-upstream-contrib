# ClickUp queue discovery — local-index migration (2026-07-12)

## What changed

`clickup_poll_gate.py` (`_scan_queue()`) previously did its own live,
paginated `/team/{id}/task` walk every 15 minutes (up to ~50 pages, team-wide,
`subtasks=true&include_closed=false`), then filtered client-side in
`_classify()`. It has been converted to call
`clickup_sync.load_team_task_index()` instead — a combined, local-first task
index built from per-list JSON caches. On a warm cache this is a pure local
read with zero live ClickUp API calls; a cache only re-syncs (delta, not
full) once it's stale (`CLICKUP_TASK_INDEX_STALE_AFTER_S`, default 30 min).

`_classify()` was also converted off two hardcoded English-name/type sets
(`CLAIMED_STATUS_NAMES`, `CLAIMED_STATUS_TYPES`) and now derives the
terminal/active signal from `clickup_sync.status_type_for_task()` (which
falls back to the list's own status topology in `clickup-map.json` when a
task's inline status JSON omits `type`). Only one literal name check remains
("in progress", for continuation detection — ClickUp's type enum can't by
itself distinguish "actively being worked" from other custom-type stages
like "in review" or "needs human"). This is a superset of the old behavior
and fixes a latent bug: board-specific custom statuses not in the old
hardcoded list (e.g. "needs human", "scoping", "schedule for deployment")
used to fall through to "unclaimed" by default; they're now correctly
excluded. Verified against every real status/type combo present in the live
`clickup-map.json` with no regressions (see verification note below).

`clickup_review_sla.py`'s four scan phases (`_scan_review_tasks`,
`_scan_agent_review_tasks` — used by both `_post_decision_threads` and
`_resume_agent_review` — and `_scan_validation_blocked_tasks`) were already
converted to the local index in a prior pass, and each already has a
try/except that falls back to the old live paginated/tag-filtered call on
any exception. Re-verified 2026-07-12: all three underlying scan functions
correctly have this fallback. No changes were needed there.

## Why

API call volume reduction. The poll gate ran every 15 min, each tick walking
the entire team's task list live; on a warm local cache it now makes zero
live calls per tick.

## Local index path

Per-list JSON caches: `~/.hermes/state/clickup-tasks/<list_id>.json`
(see `clickup_sync.py::cache_path`). Combined view via
`clickup_sync.load_team_task_index()`. Topology cache (list → statuses,
used for the type fallback and the OEC/PartsTech project blocklist):
`~/.hermes/state/clickup-map.json`.

## tags[] filtered endpoint test (2026-07-12)

Prior investigation/code comments documented
`/team/{id}/task?tags[]=<tag>` as returning HTTP 500 ("tag filter endpoint
500s; filter client-side" — the reason `_scan_queue()` originally pulled the
whole team and filtered client-side instead of asking ClickUp to filter).

Re-tested live (token sourced from the running `clickup-queue-poller`
executor cron process's own environ via `ps eww`, not via a fresh `op read`,
since `op` is currently known-hanging on this machine):

- `tags[]=agent-ready` → **HTTP 200**, 100 tasks returned, page 1 also 200.
  Spot-checked: all 100 returned tasks genuinely carry the `agent-ready` tag
  (not a silent no-op filter).
- `tags[]=agent-review` → HTTP 200, 2 tasks.
- `tags[]=validation-blocked` → HTTP 200, 3 tasks.
- `include_closed=true` variant → HTTP 200.

**Conclusion: the tags[] filtered endpoint now works** — this appears to be
fixed on ClickUp's side since the earlier investigation. The local-index
approach was implemented anyway per the API-call-reduction goal (a warm
cache costs zero live calls vs. one filtered call per poll), but a future
pass could consider server-side tag filtering as a cheaper fallback path
when the local cache needs a live refresh, instead of a full team pull.

## Backups

Pre-edit backups of both files are next to the originals:
`clickup_poll_gate.py.bak-20260712-165930`,
`clickup_review_sla.py.bak-20260712-165930`.
