#!/usr/bin/env python3
"""
ClickUp poll GATE — cheap, deterministic, zero-LLM.

Runs every 15 min as a --no-agent cron. Polls ClickUp for `agent-ready`
tasks and wakes the Claude executor (`hermes cron run <EXECUTOR_ID>`) only
when there is real work. Empty queue -> exit silently, $0 spent.

v2 (2026-06-12) adds:
  * CONTINUATION wakes — a task left `in progress` with the agent-ready tag
    still on (prior run hit its iteration limit) is re-woken so the executor
    resumes it, instead of being stuck forever. Cooldown 60 min per task,
    max 4 continuation wakes per task per day.
  * QUEUE SNAPSHOT — every poll writes queue_snapshot.json next to this
    script. The executor skill reads it (if fresh) instead of re-scanning
    all ~13 pages of the workspace, saving ~13 of its iteration budget.
  * WAKE COOLDOWN + ERROR BACKOFF — never wake more than once per 20 min
    (executor runs take ~10 min; the scheduler also has an in-flight guard).
    After a run errors, the FIRST retry wake is allowed at the normal 20-min
    cooldown (and increments the error counter); from the second consecutive
    error on, the hold doubles: 30 min, 1 h, 2 h, capped at 4 h. The counter
    resets as soon as a run records last_status=ok. This stops the
    crash-rewake loop seen on 2026-06-12 (8 consecutive quota-crash wakes).

v3 (2026-06-13) fixes the REAL "snapshot empty right after tag add" bug:
  * SUBTASKS ARE NOW SCANNED (`subtasks=true`). The prior scan passed
    `subtasks=false`, so any `agent-ready` task that was a CHILD of another
    task (e.g. a Phase-3 deliverable filed under its initiative parent
    86e1uxqm2) was silently dropped and NEVER appeared in the snapshot —
    not after an hour, not ever. This was misdiagnosed as "ClickUp search-
    index lag ~1h" (see ClickUp 86e1vwq1q and the SKILL.md "gate-miss race"
    notes). It was not lag: empirically, `subtasks=true` returns the tasks
    instantly while `subtasks=false` never does. Colin's task-filing recipe
    routinely sets `parent`, so agent-ready tasks are commonly subtasks —
    the gate MUST include them.
  * DIRECT-GET RECOVERY on an empty scan (secondary safety net, requested
    in 86e1vwq1q). We remember the ids seen `agent-ready` in the last
    non-empty scan; if a later scan comes back empty, we re-verify those
    ids with a direct `GET /task/{id}` (always fresh, unlike list endpoints)
    and recover any that are still live + agent-ready. This guards against a
    genuine transient listing dropout. Capped at RECOVERY_MAX ids/poll, so
    it adds at most a handful of calls and stays well under the rate ceiling.

Why this exists: previously the executor (an LLM agent) ran every 15 min
just to discover an empty queue -> 96 Claude runs/day + the agent's
inline curl/`python -c` ClickUp calls tripped the cron approval gate
(approvals.cron_mode=deny) and failed. This script reads the token from
env internally and runs as the cron's own process, so neither gate applies.

Token: CLICKUP_API_TOKEN (Doppler-injected into the gateway env).
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import re
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clickup_sync

_SCRIPT_START = time.time()  # 2026-06-30 babysit: deadline guard for _wake's self-heal retry
                              # (see below) — the outer cron wrapper hard-kills this script
                              # after 120s; nothing inside may risk exceeding that with no margin.

TEAM_ID = "9017245888"
EXECUTOR_ID = "62714b869845"  # cron job: skill=clickup-queue-poller (Claude). Gate-triggered.
# N>=2 concurrency (2026-06-24). A SECOND executor job; woken alongside the
# primary ONLY when there are >=2 unclaimed first-claim candidates AND
# HERMES_EXECUTOR_CONCURRENCY>=2. The two executors diverge via the atomic
# claim store (claim_next.py / claim_store.py) — exactly-once per task is a code
# guarantee, so a spurious second wake at worst yields a no-work tick. Rollback:
# set HERMES_EXECUTOR_CONCURRENCY=1 (or pause job baa3251e033d).
EXECUTOR_ID_2 = "baa3251e033d"  # cron job: clickup-executor-2 (same skill)


_CONCURRENCY_FILE = os.path.expanduser("~/.hermes/state/executor_concurrency")


def _executor_concurrency():
    """How many executors the gate may wake. Reads a state FILE first (so it can
    be toggled live with no gateway restart — the gate is a fresh subprocess each
    15-min tick), then the HERMES_EXECUTOR_CONCURRENCY env var, else 1.
    Enable N=2:  echo 2 > ~/.hermes/state/executor_concurrency
    Rollback:    echo 1 > ~/.hermes/state/executor_concurrency  (or rm it)."""
    try:
        if os.path.exists(_CONCURRENCY_FILE):
            with open(_CONCURRENCY_FILE, encoding="utf-8") as f:
                return max(1, int(f.read().strip()))
    except (OSError, ValueError):
        pass
    try:
        return max(1, int(os.environ.get("HERMES_EXECUTOR_CONCURRENCY", "1")))
    except (TypeError, ValueError):
        return 1

READY_TAG = "agent-ready"
# (2026-06-19, ClickUp 86e1ynuw1 + brain note
# `2026-06-19-hermes-loop-class-structurally-unsatisfiable-verify-gate-...`)
# When the executor's DETECTION heuristic classifies a verify failure as
# un-retryable (success criterion gated on external state the agent can't
# change in code + same diagnosis ≥2 cycles + fix is operator/upstream),
# the executor stamps this tag on the task as part of its SELF-PARK action.
# The gate treats agent-ready + this tag as the load-bearing signal that
# the gate should drop agent-ready and NOT re-wake — this is the backstop
# in case the executor ever fails to remove agent-ready itself. Fully
# recoverable: operator removes the tag (and re-adds agent-ready) to
# resume work after acting.
PARK_BLOCKED_EXTERNAL_TAG = "park-blocked-external"
# Tasks parked awaiting an OPERATOR DECISION carry this tag (managed by
# clickup_review_sla.py). They must NEVER be selected as a continuation — the
# only thing that unblocks them is a human reply, which the SLA sweep handles by
# swapping agent-review → agent-ready. Without this guard a task left with BOTH
# agent-review and agent-ready (a half-park) would be re-woken every cycle — the
# operator-blocked loop seen on 86e1yxn5e (2026-06-19).
AGENT_REVIEW_TAG = "agent-review"
# Operator "hands off" tag (2026-06-19, Colin). A task carrying agent-avoid must
# NEVER be touched by Hermes — not woken, not claimed, not continued, not
# self-parked — regardless of any other tag (including agent-ready) or status.
# It is a hard, top-priority exclusion in _classify(): the task simply drops out
# of every wake/snapshot bucket as if it were not in the queue. Fully reversible:
# the operator removes agent-avoid to let Hermes resume normal selection.
AVOID_TAG = "agent-avoid"

_HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(_HERE, "queue_snapshot.json")
STATE_PATH = os.path.join(_HERE, ".poll_gate_state.json")
# Deterministic executor target (2026-06-16): the gate records WHICH task it woke
# the executor for, so the executor works THAT task instead of self-selecting.
TARGET_PATH = os.path.join(_HERE, ".gate_target.json")
JOBS_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")

# ---- D1: Atomic claim primitive (2026-06-23, ClickUp 86e20h5ma) ----
# Per-task lockfile directory. The gate acquires an exclusive flock on
# ~/.hermes/state/claims/<taskId>.lock BEFORE writing TARGET_PATH or waking
# an executor on that task. A second caller that races the same task id
# cannot acquire the lock while the first holds it → exactly-once claim.
# TTL: if the lock file is older than CLAIM_LOCK_TTL_S the lock is stale
# (executor died without releasing) and is reclaimable. The gate holds the
# lock open for the duration of the wake + TARGET_PATH write, then closes it
# (auto-release). N=2 is NOT enabled here; the primitive is built so it is
# safe to enable later without changing this file.
CLAIMS_DIR = os.path.expanduser("~/.hermes/state/claims")
CLAIM_LOCK_TTL_S = 90 * 60  # 90 min; longer than any executor run

# ---- Localization / i18n / translation hard exclusion (2026-06-20, 86e1z1fy0) ----
# The positive signal is AVOID_TAG (above) — any board groomer can apply it to
# mark a task human-only. AVOID_TAG is the durable signal; the title-keyword
# match below is DEFENSE-IN-DEPTH so a single manual mis-tag (operator forgets
# agent-avoid, agent-ready gets re-added) cannot silently re-arm autonomous
# pickup of a localization task.
#
# Origin: Hermes autonomously claimed an OEC localization task (86e1yxn5e) on
# 2026-06-19. The work was bounded and harmless, but localization tasks need
# human judgment (stub-vs-real content, destructive --force over existing
# translations, live fetches that require headed Chrome — oeconnection.com
# WAF-blocks headless, Colin branch-diff gate before merge). They must NEVER
# be agent-claimed. See ClickUp 86e1z1fy0 for the full rationale.
#
# Convention for board groomers / ignite-prep: tag localization tasks with
# `agent-avoid`. Title-keyword + deny-list fallback only exists so a stray
# mis-tag does NOT result in a silent re-claim.
LOCALIZATION_TITLE_PATTERN = re.compile(
    r"""
    \b(locale|locales|i18n|translation|translations|hreflang)\b   # word-boundary
    |
    \[oec\].*?(translation|locale|i18n|hreflang)                   # OEC-prefixed
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Negative patterns — tasks whose PRIMARY topic is talking ABOUT the
# localization exclusion are NOT themselves localization tasks. These are
# meta-tasks (e.g. "[Hermes] Task-selector must exclude localization" or
# "Phase 0 · Foundation (clone OEC architecture, minus i18n)") and should
# be claimable by Hermes. Without this exemption the gate would skip its
# own definition and never iterate.
LOCALIZATION_META_PATTERN = re.compile(
    r"""
    (\[hermes\]|\[meta\]|                          # Hermes/meta-task tags
     \bexclude[sd]?\b|\bexclusion\b|              # "exclude localization"
     \bnever\s+claim\b|\bnever\s+agent\b|         # "never agent-claim"
     \bselector\b|\bhard\s+exclusion\b|           # "task selector"
     (?:^|[\.\s])minus\s+i18n\b|                 # "minus i18n" preceded by start/./space
     \babout\s+the\s+(?:exclusion|gating|gate)\b  # meta-discussion
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
# Curated list of localization task ids that have been formally de-armed.
# Loaded at runtime from this file if present (operator can edit without
# code change); the defaults below are the known OEC localization offenders
# (verified 2026-06-20: 86e1yxn5e / 86e1yq89q / 86e1z13vv).
_LOCALIZATION_DENY_IDS_DEFAULT = {
    "86e1yxn5e",  # [OEC] Locale build-out — hreflang linkage
    "86e1yq89q",  # [OEC] Content/site refresh from WP export — translations
    "86e1z13vv",  # [OEC] Locale content follow-ups
}
LOCALIZATION_DENY_IDS_PATH = os.path.join(_HERE, "localization_deny_ids.json")
LOCALIZATION_DENY_TAG = "no-agent"  # positive signal convention — alias for AVOID_TAG

# ---- ClickUp project blocklist (shared with clickup_sync.py + future publish)
# Default-allow model: anything not in ~/.claude/skills/ignite-state/references/blocklist.json is allowed.

WAKE_COOLDOWN_S = 20 * 60          # never wake more often than this
ERROR_BACKOFF_BASE_S = 30 * 60     # first backoff step after an errored run
ERROR_BACKOFF_CAP_S = 4 * 3600
CONTINUATION_COOLDOWN_S = 60 * 60  # per stuck task
CONTINUATION_MAX_PER_DAY = 4       # per stuck task
# Waste-cut 2026-06-23 (Colin: "like 2 tasks done today, so slow"). A continuation
# woken CONTINUATION_STRIKE_CAP times with the task's date_updated FROZEN (no comment,
# no status flip, no PR — i.e. claimed-then-abandoned) stops being re-woken: ~9 such
# tasks were each burning the 4-wakes/day cap (~36 wakes/day) re-pinning the executor
# onto dead work. This is NON-MUTATING — the task stays agent-ready and visible to the
# operator; the gate just won't pin the executor to it. Any real progress (date_updated
# advancing) resets the strike count, so a task on a live validator/rework cycle is
# never skipped. 8 ≈ two days at 4/day.
CONTINUATION_STRIKE_CAP = 8
RECOVERY_MAX = 10                  # max direct GETs per empty-scan recovery

# A task is "claimed" once the executor moves it out of an open status.
# `in progress` + agent-ready tag is special-cased as a CONTINUATION below.
#
# NEW (ClickUp-map-driven classification, API-call-reduction pass): status
# classification used to hardcode an English status-NAME allow/deny list here
# (CLAIMED_STATUS_NAMES / CLAIMED_STATUS_TYPES), which silently mis-classified
# any board using different vocabulary (e.g. a custom "needs human" or
# "assigned" status not in the list fell through to the permissive default
# and was treated as unclaimed). _classify() now derives the terminal/active
# signal from clickup_sync.status_type_for_task() (topology-aware, falls back
# to the list's own status map when a task's inline status JSON omits
# "type") plus a single name check for the literal "in progress" status
# (ClickUp's standard active-work name — type alone can't distinguish
# "actively being worked" from other custom-type stages like "in review" or
# "needs human", so this one name comparison is unavoidable, not a
# fragile enumeration). See _classify() for the derivation.


def _token():
    return os.environ.get("CLICKUP_API_TOKEN", "").strip()


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": _token()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _has_tag(task, tag):
    """True iff task has a tag whose name matches `tag` (case-insensitive)."""
    target = tag.lower()
    tags = task.get("tags") or []
    return any((t.get("name") or "").lower() == target for t in tags)


def _has_ready_tag(task):
    return _has_tag(task, READY_TAG)


def _load_localization_deny_ids():
    """Load operator-curated deny-list ids from JSON file, falling back to
    the in-code defaults if the file is absent or malformed. The file lives
    next to this script (`~/.hermes/scripts/localization_deny_ids.json`) and
    is a simple JSON list of task ids. Operator edits it without code change.

    Format: `{"ids": ["86e1...", "86e1..."]}`. Empty/missing file → defaults.
    """
    try:
        with open(LOCALIZATION_DENY_IDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        ids = set(data.get("ids") or [])
        return ids if ids else _LOCALIZATION_DENY_IDS_DEFAULT
    except (FileNotFoundError, ValueError, TypeError):
        return _LOCALIZATION_DENY_IDS_DEFAULT


def _is_localization_task(task):
    """Defense-in-depth classifier: return (True, reason) if this task looks
    like a localization / i18n / translation / hreflang task, (False, None)
    otherwise.

    Three signals are checked (any one is enough to exclude):
      1. ID in operator-curated deny-list (handles edge cases the keyword
         regex misses — e.g. a future "i18n audit" task named ambiguously).
      2. Title-keyword regex (locale|locales|i18n|translation|translations|
         hreflang word-boundary OR `[OEC]`-prefixed with a localization word).
      3. The positive `no-agent` tag convention (LOCALIZATION_DENY_TAG) is
         the durable board-groomer signal. NOTE: the general AVOID_TAG is
         NOT included here — it has its own FIRST check in _classify() that
         handles non-localization fences (operator hands-off for other
         reasons). Including it here would log "localization skip" for any
         operator-fenced task, which is misleading.

    The reason string is short and human-readable; logged to stderr by the
    caller so the skip is observable (not a silent drop).
    """
    task_id = task.get("id") or ""
    if task_id in _load_localization_deny_ids():
        return True, "deny-list"
    if _has_tag(task, LOCALIZATION_DENY_TAG):
        return True, "tag:no-agent"
    name = task.get("name") or ""
    if LOCALIZATION_TITLE_PATTERN.search(name):
        # Negative match: if the title is META (talks ABOUT the exclusion
        # rather than performing localization), the task is NOT itself a
        # localization task — let the gate claim it.
        if LOCALIZATION_META_PATTERN.search(name):
            return False, None
        # Identify which keyword matched for the log line.
        m = LOCALIZATION_TITLE_PATTERN.search(name)
        kw = m.group(0) if m else "?"
        return True, f"title-keyword:{kw!r}"
    return False, None


def _is_oec_excluded_task(task):
    """Return (True, reason) if this task belongs to a blocked ClickUp project."""
    return clickup_sync.is_clickup_project_blocked(task)


def _status_parts(task):
    """Return (name, type) for a task's status.  is resolved via
    clickup_sync.status_type_for_task() (falls back to the list's own status
    topology in ~/.hermes/state/clickup-map.json when the task's inline
    status JSON omits "type", which the raw team/task pull sometimes does)
    rather than reading task['status']['type'] directly."""
    status = task.get("status") or {}
    return (
        (status.get("status") or "").strip().lower(),
        clickup_sync.status_type_for_task(task),
    )


def _classify(task):
    """Return 'unclaimed', 'continuation', 'parked', or None.

    NEW 2026-06-19 (ClickUp 86e1ynuw1): a 3rd return value 'parked' is added
    for tasks the executor has stamped with PARK_BLOCKED_EXTERNAL_TAG while
    agent-ready is still on. The gate treats this as a load-bearing signal
    that the executor has decided this continuation will never pass its
    verify gate and the right action is "drop agent-ready, do NOT wake."
    The caller (main()) handles the self-park side effect (tag delete +
    log); this function is pure classification.

    HARD EXCLUSION (2026-06-19): an agent-avoid task is invisible to the gate
    no matter what else it carries. This check is FIRST so it dominates
    agent-ready, the park tags, and the continuation path — Hermes must never
    touch a task the operator has fenced off. main() does NO side effect on
    these (no tag mutation): leave an operator-fenced task exactly as found.

    NEW 2026-06-20 (ClickUp 86e1z1fy0): localization / i18n / translation
    tasks are excluded at the SAME level as agent-avoid — they're a category
    the operator has decided Hermes must never claim, regardless of tags.
    Defense-in-depth: deny-list + title-keyword + tag signal. main() takes
    NO side effect on these either (no tag mutation); the task drops out of
    every bucket as if it were not in the queue. The skip is logged to
    stderr with the reason so it's visible in cron logs (not a silent drop).
    """
    is_oec, oec_reason = _is_oec_excluded_task(task)
    if is_oec:
        print(
            f"[gate] OEC/PartsTech project skip: {task.get('id','?')}({(task.get('name') or '')[:60]!r}) reason={oec_reason}",
            file=sys.stderr,
        )
        return None
    is_loc, loc_reason = _is_localization_task(task)
    if is_loc:
        print(
            f"[gate] localization skip: {task.get('id','?')}({(task.get('name') or '')[:60]!r}) reason={loc_reason}",
            file=sys.stderr,
        )
        return None
    if _has_tag(task, AVOID_TAG):
        return None
    if not _has_ready_tag(task):
        return None
    name, stype = _status_parts(task)
    if stype in {"closed", "done"}:
        return None  # finished, or resolved into a review/done checkpoint
    # STRUCTURALLY-UNSATISFIABLE detection: park tag + agent-ready + in
    # progress = the executor already classified this as un-retryable. The
    # gate's job here is just to surface the signal — the actual tag
    # deletion + log line happens in main().
    if name == "in progress" and _has_tag(task, PARK_BLOCKED_EXTERNAL_TAG):
        return "parked"
    if _has_tag(task, AGENT_REVIEW_TAG):
        # Operator-decision park: only a human reply unblocks it (the SLA sweep
        # swaps agent-review → agent-ready when that happens). Never a
        # continuation — guards against a half-park (agent-review + agent-ready)
        # being re-woken every cycle. Zero side effect: just don't select it.
        return None
    if name == "in progress":
        # Claimed but never finished (prior run died mid-task) — resumable.
        return "continuation"
    if stype == "custom":
        # Any OTHER custom-type intermediate status (in review / needs human /
        # blocked / testing / scoping / schedule for deployment / assigned /
        # ready-for-review / ...) is someone else's turn, not agent-claimable.
        # Deny-by-default on type="custom" generalizes across boards with
        # different status vocab instead of requiring every board's non-active
        # status name to be individually enumerated (the old CLAIMED_STATUS_NAMES
        # list silently missed board-specific names like "needs human").
        return None
    return "unclaimed"  # type open/unstarted/etc. — not yet started, available


def _entry(t, kind):
    return {
        "id": t.get("id"),
        "name": (t.get("name") or "")[:120],
        "status": (t.get("status") or {}).get("status"),
        "priority": ((t.get("priority") or {}) or {}).get("priority"),
        "list": ((t.get("list") or {}) or {}).get("name"),
        "url": t.get("url"),
        "kind": kind,
        # Snapshot is advisory only — the wake decision reads tags/status off the
        # LIVE task during _scan_queue, never off this file. These two fields are
        # surfaced purely so downstream consumers (executor briefs, babysit scan)
        # don't read None and mistake the always-thin snapshot for "corruption".
        "tags": [(tg.get("name") or "") for tg in (t.get("tags") or [])],
        "date_updated": t.get("date_updated"),
        # date_created (ms-epoch string) so the executor's STEP ZERO can break
        # priority ties OLDEST-FIRST (Colin 2026-06-23). Sort: (priority, date_created asc).
        "date_created": t.get("date_created"),
    }


def _scan_queue():
    """Classify the team-wide queue from the shared local task index instead
    of a live paginated /team/{id}/task walk every 15 min (API-call-reduction
    pass). NOTE 2026-07-12: a live re-test of the tags[] filtered team
    endpoint (`/team/{id}/task?tags[]=agent-ready`) found it now returns 200
    with correctly-filtered results (previously documented elsewhere as
    500ing — that appears to be FIXED on ClickUp's side as of this check).
    This rewrite still reads the local index rather than switching to a live
    tag-filtered call because the local index is the API-call-reduction goal
    of this pass (zero live calls on a warm cache vs one filtered call every
    poll); revisit if a future pass wants server-side filtering instead.

    clickup_sync.load_team_task_index() delta-syncs each active list's cache
    (subtasts=true + include_closed=true baked into its own sync calls — see
    clickup_sync._full_sync_list / _delta_sync_list) and reads the combined
    local JSON the rest of the time, so agent-ready subtasks of an initiative
    parent are still included (the v3 2026-06-13 fix this docstring used to
    describe is preserved inside clickup_sync, not lost by this rewire).
    Per-list sync failures fall back to that list's last-good cache inside
    load_team_task_index() itself, so a transient ClickUp error degrades to
    stale-but-present data rather than an empty scan.

    NEW 2026-06-19 (ClickUp 86e1ynuw1): a 3rd bucket 'parked' is returned for
    tasks the executor stamped with PARK_BLOCKED_EXTERNAL_TAG. main() handles
    the self-park side effect on these — drop agent-ready tag + log — and
    they are EXCLUDED from wake/unclaimed/continuation bookkeeping so the
    gate's normal wake logic never sees them.
    """
    unclaimed, continuations, parked = [], [], []
    index = clickup_sync.load_team_task_index()
    if index.get("errors"):
        print(
            f"[gate] task-index sync warnings: {len(index['errors'])} "
            f"list(s) fell back to cached data: {index['errors']}",
            file=sys.stderr,
        )
    for t in index.get("tasks") or []:
        kind = _classify(t)
        if kind is None:
            continue
        if kind == "unclaimed":
            unclaimed.append(_entry(t, kind))
        elif kind == "continuation":
            continuations.append(_entry(t, kind))
        elif kind == "parked":
            parked.append(_entry(t, kind))
    return unclaimed, continuations, parked


def _fetch_task(task_id):
    """Direct GET of a single task — the only ClickUp read that is always
    fresh (list/team endpoints can transiently omit a task). Returns the task
    dict, or None on any error (never raise: recovery is best-effort)."""
    try:
        return _get(f"https://api.clickup.com/api/v2/task/{task_id}")
    except Exception as e:
        print(f"[gate] recovery GET {task_id} failed: {e!r}", file=sys.stderr)
        return None


def _recover(last_seen_ids):
    """Empty-scan recovery: re-verify recently-seen agent-ready ids by direct
    GET. Returns (unclaimed, continuations, parked, confirmed_dead_ids).
    Best-effort, capped at RECOVERY_MAX ids. `confirmed_dead_ids` are ids
    whose GET SUCCEEDED and that are no longer live (so they can be safely
    pruned from memory) — a GET that *failed* is not reported dead, so a
    network blip never wipes the recovery set.

    NEW 2026-06-19 (ClickUp 86e1ynuw1): parked tasks are also surfaced here
    so the empty-scan recovery path can drop their agent-ready tag — the
    executor-stamped park signal is durable across recovery rounds.
    """
    unclaimed, continuations, parked, dead = [], [], [], []
    for task_id in list(last_seen_ids)[:RECOVERY_MAX]:
        t = _fetch_task(task_id)
        if t is None:
            continue  # GET failed — keep the id, do not confirm dead
        kind = _classify(t)
        if kind is None:
            dead.append(task_id)
            continue
        if kind == "unclaimed":
            unclaimed.append(_entry(t, kind))
        elif kind == "continuation":
            continuations.append(_entry(t, kind))
        elif kind == "parked":
            parked.append(_entry(t, kind))
    return unclaimed, continuations, parked, dead


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _snapshot_sig(unclaimed, continuations):
    """A cheap, comparable signature of the wakeable queue: each task's
    (id, status, date_updated). Used to suppress a wake when NOTHING changed
    since the last wake (no-delta churn) — those wakes only re-discover an
    unchanged snapshot inside an (expensive) LLM run. New unclaimed work, a
    status flip, or any date_updated bump all change the signature -> wake."""
    items = sorted(
        (t.get("id"), t.get("status"), t.get("date_updated"))
        for t in (unclaimed + continuations)
    )
    return repr(items)


def _executor_job():
    data = _load_json(JOBS_PATH, {})
    for j in data.get("jobs", []):
        if j.get("id") == EXECUTOR_ID:
            return j
    return {}


def _wake_allowed(state, now):
    """Apply wake cooldown + error backoff. Returns (allowed, reason)."""
    last_wake = state.get("last_wake_ts", 0)
    if now - last_wake < WAKE_COOLDOWN_S:
        return False, f"wake cooldown ({int((now - last_wake) / 60)}m ago)"

    job = _executor_job()
    if job.get("last_status") == "error":
        n = state.get("consecutive_error_wakes", 0)
        backoff = min(ERROR_BACKOFF_BASE_S * (2 ** max(n - 1, 0)), ERROR_BACKOFF_CAP_S)
        if n > 0 and now - last_wake < backoff:
            return False, (
                f"error backoff ({n} consecutive errored wakes, "
                f"next wake allowed {int((backoff - (now - last_wake)) / 60)}m from now)"
            )
    return True, ""


def _pick_continuation(continuations, state, now):
    """First continuation task whose per-task cooldown + daily cap allow a wake.

    Also SKIPS stale continuations (woken >= CONTINUATION_STRIKE_CAP times with the
    task's date_updated FROZEN — claimed-then-abandoned tasks that re-burn wakes with
    no forward progress). Stale skips are NON-MUTATING: the task stays agent-ready and
    visible; the gate just stops pinning the executor to it. `total_wakes` is cumulative
    (NOT daily-reset) and resets to 0 the moment date_updated advances, so a task on a
    live validator/rework cycle is never skipped. Returns (task, rec, stale_ids)."""
    today = date.today().isoformat()
    per_task = state.setdefault("continuation_wakes", {})
    stale_ids = []
    pick, pick_rec = None, None
    for t in continuations:
        rec = per_task.get(t["id"], {})
        cur_upd = t.get("date_updated")
        # Real progress since our last wake of it -> reset the strike count.
        if rec.get("last_updated") is not None and cur_upd != rec.get("last_updated"):
            rec["total_wakes"] = 0
        is_validating = "needs-validation" in (t.get("tags") or [])
        if rec.get("total_wakes", 0) >= CONTINUATION_STRIKE_CAP and not is_validating:
            stale_ids.append(t["id"])
            per_task[t["id"]] = rec
            continue
        if pick is None:
            if rec.get("date") != today:
                rec = {**rec, "date": today, "count": 0, "last_ts": 0}
            if rec.get("count", 0) >= CONTINUATION_MAX_PER_DAY:
                per_task[t["id"]] = rec
                continue
            if now - rec.get("last_ts", 0) < CONTINUATION_COOLDOWN_S:
                per_task[t["id"]] = rec
                continue
            pick, pick_rec = t, rec
        per_task[t["id"]] = rec
    return pick, pick_rec, stale_ids


def _hermes_bin():
    for cand in (
        shutil.which("hermes"),
        os.path.expanduser("~/.hermes/bin/hermes"),
        os.path.expanduser("~/.local/bin/hermes"),
    ):
        if cand and os.path.exists(cand):
            return cand
    return None


def _claim_lock_path(task_id):
    os.makedirs(CLAIMS_DIR, exist_ok=True)
    return os.path.join(CLAIMS_DIR, f"{task_id}.lock")


def _try_acquire_claim(task_id):
    """Acquire an exclusive fcntl lock for task_id.

    Returns an open file object on success (caller must close to release),
    or None if the lock is already held (live claim exists).

    TTL reclaim: if the lockfile mtime is older than CLAIM_LOCK_TTL_S we
    assume the prior holder died and we forcibly re-open (still needs to
    win the flock, which it will since the stale holder's fd is closed).
    The TTL check is advisory only — correctness relies on flock semantics,
    not mtime; mtime just prevents spurious "already held" refusals on
    long-running executors that are still alive.
    """
    path = _claim_lock_path(task_id)
    try:
        lf = open(path, "a", encoding="utf-8")  # create if absent; append keeps mtime stable
        # LOCK_EX | LOCK_NB: exclusive, non-blocking. Raises BlockingIOError
        # if any other fd (same or different process) holds LOCK_EX on this
        # inode, making concurrent claims on the same task id impossible.
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Lock is currently held; check TTL.
            try:
                age = time.time() - os.stat(path).st_mtime
            except OSError:
                age = 0
            lf.close()
            if age > CLAIM_LOCK_TTL_S:
                # Stale lock: holder must have died; re-open and retry once.
                print(
                    f"[gate] claim lock for {task_id} stale ({int(age/60)}m) — reclaiming",
                    file=sys.stderr,
                )
                try:
                    lf2 = open(path, "a", encoding="utf-8")
                    fcntl.flock(lf2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # Update mtime so this holder's TTL window starts fresh.
                    os.utime(path, None)
                    return lf2
                except (BlockingIOError, OSError):
                    pass
            return None  # live claim, refuse
        # Won the lock: update mtime so TTL window starts now.
        os.utime(path, None)
        return lf
    except OSError as e:
        print(f"[gate] claim lock open failed for {task_id}: {e!r}", file=sys.stderr)
        return None


def _release_claim(lf):
    """Release a previously acquired claim lock (close the file descriptor).
    flock is released automatically on close. Safe to call with None."""
    if lf is not None:
        try:
            lf.close()
        except OSError:
            pass


def _delete_tag(task_id, tag):
    """Best-effort DELETE of a tag from a task. The tag is auto-created if
    absent on POST; DELETE of a missing tag returns 404 which is also OK
    (idempotent — re-running self-park is safe). Never raises."""
    try:
        req = urllib.request.Request(
            f"https://api.clickup.com/api/v2/task/{task_id}/tag/{tag}",
            method="DELETE",
            headers={"Authorization": _token()},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[gate] DELETE /tag/{tag} on {task_id}: rc={resp.status}")
    except Exception as e:
        # 404 = tag was never on the task (already clean). Anything else is
        # non-fatal: the gate's job is best-effort enforcement of the
        # executor's decision; a transient ClickUp error doesn't change the
        # fact that this task MUST NOT be re-woken on the next cycle.
        print(f"[gate] DELETE /tag/{tag} on {task_id} failed: {e!r}",
              file=sys.stderr)


WORKED_BY_FIELD_ID = "2bf5c958-ca2a-4f6b-bab5-25693b98b1f1"  # "Worked By" dropdown field
# The Hermes option's UUID within that dropdown is workspace-specific and is
# intentionally NOT hardcoded here; supply it via env. If unset, the stamp is
# skipped (logged, not fatal) rather than guessing an id and mis-tagging a
# task with the wrong option.
WORKED_BY_HERMES_OPTION_ENV = "CLICKUP_WORKED_BY_HERMES_OPTION_ID"


def _stamp_worked_by_hermes(task_id):
    """Best-effort POST to set the "Worked By" custom field to the Hermes
    option, so the review-SLA staleness sweep's `_worked_by_hermes()` check
    (which only ever READS this field) can actually see that Hermes worked
    this task. Until now nothing in the fleet ever wrote it, so that safety
    net could never fire for Hermes-claimed work.

    Mirrors `_delete_tag`'s best-effort contract: log and continue on any
    failure, never raise, never block the wake/claim it's called from."""
    option_id = os.environ.get(WORKED_BY_HERMES_OPTION_ENV, "").strip()
    if not option_id:
        print(
            f"[gate] worked-by stamp skipped for {task_id}: "
            f"{WORKED_BY_HERMES_OPTION_ENV} not set in env",
            file=sys.stderr,
        )
        return
    try:
        body = json.dumps({"value": option_id}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.clickup.com/api/v2/task/{task_id}/field/{WORKED_BY_FIELD_ID}",
            data=body,
            method="POST",
            headers={
                "Authorization": _token(),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"[gate] POST /field/Worked-By on {task_id}: rc={resp.status}")
    except Exception as e:
        # Best-effort: a failed stamp must never block the wake it rides
        # along with — the staleness-sweep gap this closes is a safety net,
        # not the primary claim mechanism.
        print(f"[gate] worked-by stamp failed for {task_id}: {e!r}", file=sys.stderr)


def _self_park(parked_tasks):
    """Handle parked tasks surfaced by _classify(): drop agent-ready so the
    next cron wake cannot re-wake the executor on them. This is the gate's
    load-bearing backstop for the structurally-unsatisfiable-verify-gate
    fix (2026-06-19, ClickUp 86e1ynuw1 + brain note).

    Behavior:
      * agent-ready tag is removed (DELETE is idempotent: 404 = OK).
      * The park tag is LEFT ON — it is the executor's durable "I'm parked
        on external state, do not re-claim" signal that survives even if
        the operator re-adds agent-ready. Removing it would erase the
        context the operator needs to decide what to do.
      * Status is NOT touched from the gate. The executor is responsible
        for flipping status to 'deferred' (or list's blocked-equivalent)
        as part of its SELF-PARK action. The gate's job is narrow:
        prevent the wake, period.
      * The previous `last_seen_ready_ids` (used by empty-scan recovery)
        has these ids REMOVED so the recovery path doesn't try to
        re-confirm them on the next empty scan.

    DRY_RUN: when DRY_RUN is set in the env, the side effect is logged but
    not executed — the smoke test uses this to confirm classification +
    side-effect target list without actually mutating ClickUp state.
    """
    if not parked_tasks:
        return
    summaries = ", ".join(
        "{}({})".format(t["id"], (t.get("name") or "")[:40]) for t in parked_tasks
    )
    print(
        f"[gate] self-park: {len(parked_tasks)} structurally-unsatisfiable "
        f"task(s) — dropping agent-ready to prevent re-wake: {summaries}"
    )
    for t in parked_tasks:
        tid = t["id"]
        if os.environ.get("DRY_RUN"):
            print(f"[gate] DRY_RUN — would DELETE /tag/agent-ready on {tid}")
        else:
            _delete_tag(tid, READY_TAG)


def _clear_stale_fire_claim(executor_id):
    """SELF-HEAL (2026-06-30, babysit — recurring bug, see brain 'Hermes executor
    fire_claim stuck pattern' 06-29 + 06-30 recurrence). The scheduler's fire_claim
    mutex is meant to be released when the claiming process actually starts the job.
    If that process dies first (crash, kill, gateway restart mid-dispatch), the
    mutex is held forever by a dead PID and EVERY future wake — gate-triggered or
    native-scheduled — gets rejected with "Already being fired by the scheduler",
    even though nothing is actually running. Previously this required a human to
    notice and hand-clear jobs.json. Detect + clear it here instead, so Hermes
    recovers on its own without a caretaker tick catching it.

    Returns True if a stale claim was found and cleared (safe to retry the wake)."""
    d = _load_json(JOBS_PATH, {})
    jobs = d if isinstance(d, list) else d.get("jobs", list(d.values()) if isinstance(d, dict) else d)
    changed = False
    for j in jobs:
        if j.get("id") != executor_id:
            continue
        fc = j.get("fire_claim")
        if not fc or not isinstance(fc, dict):
            continue
        by = fc.get("by") or ""
        pid_s = by.rsplit(":", 1)[-1] if ":" in by else by
        try:
            pid = int(pid_s)
        except (TypeError, ValueError):
            continue
        try:
            os.kill(pid, 0)  # windows-footgun: ok — liveness probe only, POSIX-only script (uses fcntl elsewhere)
            continue  # PID alive: genuinely in-flight, not stale. Leave it.
        except ProcessLookupError:
            pass  # dead — fall through and clear
        except (PermissionError, OSError):
            continue  # can't confirm dead (e.g. PID reused by another user) — don't touch
        print(f"[gate] self-heal: fire_claim on {executor_id} held by dead pid {pid} "
              f"(claimed {fc.get('at')}) — clearing stale mutex", file=sys.stderr)
        j["fire_claim"] = None
        changed = True
    if changed:
        _save_json(JOBS_PATH, d)
    return changed


def _wake(reason, executor_id=EXECUTOR_ID):
    if os.environ.get("DRY_RUN"):
        print(f"[gate] DRY_RUN set — would wake executor {executor_id} ({reason})")
        return True
    hb = _hermes_bin()
    if not hb:
        print("[gate] hermes binary not found — cannot wake executor", file=sys.stderr)
        return False
    # SELF-HEAL: clear any dead-PID fire_claim wedge left by a previously-killed wake
    # before firing. Safe — _clear_stale_fire_claim only clears a fire_claim held by a
    # CONFIRMED-dead PID; an alive/uncertain holder is left untouched.
    _clear_stale_fire_claim(executor_id)
    try:
        # DETACHED WAKE (2026-07-01, babysit — ROOT-CAUSE fix for the frozen board /
        # "healthy but doing nothing"). `hermes cron run <id>` runs the ENTIRE agent turn
        # SYNCHRONOUSLY IN-PROCESS (cronjob_tools._execute_job_now -> cron.scheduler.run_one_job;
        # no hand-off to the gateway daemon, no backgrounding). A real glm-4.7/z.ai turn needs
        # ~15-20s of CLI/plugin/MCP bootstrap + 10-30s per LLM call, so the OLD
        # subprocess.run(timeout=25) SIGKILLed EVERY gate-triggered wake mid-bootstrap — 0 API
        # calls, 0 work — and left dead-PID fire_claim wedges (the recurring 06-29/06-30 bug the
        # sync self-heal retry tried and failed to fix, since the retry was under the SAME 25s
        # cap). The board froze because every wake died before it could claim/work a task; the
        # scheduled 5AM run and long-lived manual fires worked only because they aren't wrapped
        # in that timeout. Fix: fire-and-forget in a NEW SESSION (start_new_session=True) so the
        # executor runs to completion independent of this gate script's 120s outer budget and
        # survives the gate's process group exiting. Concurrency is bounded by WAKE_COOLDOWN_S +
        # last_wake_ts, by the fire_claim mutex (a still-alive executor makes the next
        # `hermes cron run` a harmless "already being fired" no-op), and by claim_store (no
        # double-claim of a task) — so a detached fire is safe.
        wake_log = os.path.expanduser("~/.hermes/logs/gate_wake_executor.log")
        logf = open(wake_log, "a", encoding="utf-8")  # inherited by the child; parent exit closes its own handle
        proc = subprocess.Popen(
            [hb, "cron", "run", executor_id],
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"[gate] woke executor {executor_id} ({reason}): detached pid={proc.pid} "
              f"(runs to completion outside the gate's budget; output -> {wake_log})")
        return True
    except Exception as e:
        print(f"[gate] failed to wake executor {executor_id}: {e!r}", file=sys.stderr)
        return False


def main():
    if not _token():
        print("[gate] CLICKUP_API_TOKEN not set in env — cannot poll", file=sys.stderr)
        return 0
    try:
        unclaimed, continuations, parked = _scan_queue()
    except Exception as e:  # never crash the cron; just log
        print(f"[gate] poll error: {e!r}", file=sys.stderr)
        return 0

    now = time.time()
    state = _load_json(STATE_PATH, {})

    # Empty scan -> attempt direct-GET recovery of recently-seen ready ids.
    recovered = False
    if not unclaimed and not continuations:
        prev_seen = state.get("last_seen_ready_ids", [])
        if prev_seen:
            r_unclaimed, r_cont, r_parked, dead = _recover(prev_seen)
            if r_unclaimed or r_cont or r_parked:
                unclaimed, continuations = r_unclaimed, r_cont
                parked.extend(r_parked)
                recovered = True
                print(
                    f"[gate] empty scan: recovered {len(r_unclaimed)} unclaimed + "
                    f"{len(r_cont)} continuation + {len(r_parked)} parked via "
                    "direct GET (index dropout)"
                )
            # Prune only ids a successful GET confirmed dead; keep the rest.
            # Parked ids are NOT dead — they are intentionally parked and
            # must stay out of the recovery set so we don't try to re-fetch
            # them on the next empty scan.
            parked_ids = {t["id"] for t in parked}
            state["last_seen_ready_ids"] = [
                i for i in prev_seen if i not in dead and i not in parked_ids
            ]
    else:
        # Authoritative fresh view — remember it for the next empty-scan recovery.
        state["last_seen_ready_ids"] = [t["id"] for t in (unclaimed + continuations)]

    # STRUCTURALLY-UNSATISFIABLE self-park (NEW 2026-06-19, ClickUp 86e1ynuw1).
    # Drop agent-ready on any task the executor stamped with the park tag.
    # This runs BEFORE the snapshot save + wake bookkeeping so the snapshot
    # the executor reads does NOT include parked tasks (the executor would
    # otherwise pick them up again before the tag delete propagates).
    _self_park(parked)

    # Snapshot for the executor skill — lets it skip the full workspace scan.
    # Note: parked tasks are EXCLUDED from the snapshot. They are not agent-
    # ready work anymore (we just deleted the tag above), so the executor
    # should not see them.
    _save_json(SNAPSHOT_PATH, {
        "generated_at": now,
        "generated_at_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "recovered_via_direct_get": recovered,
        # Advisory count so consumers stop reading None and inferring "corruption".
        "agent_ready_count": len(unclaimed),
        "tasks": unclaimed + continuations,
    })

    if not unclaimed and not continuations:
        _save_json(STATE_PATH, state)
        if parked:
            print(
                f"[gate] no agent-ready work after self-park — executor stays "
                f"asleep ($0). {len(parked)} task(s) parked (structurally "
                "unsatisfiable; waiting on operator)."
            )
        else:
            print("[gate] no agent-ready work — executor stays asleep ($0)")
        return 0

    allowed, hold_reason = _wake_allowed(state, now)
    if not allowed:
        _save_json(STATE_PATH, state)
        print(
            f"[gate] work present ({len(unclaimed)} unclaimed, "
            f"{len(continuations)} continuation) but holding: {hold_reason}"
        )
        return 0

    # NO-DELTA SUPPRESSION (waste-cut 2026-06-23). Cooldown has expired, but if the
    # wakeable queue is byte-for-byte unchanged since the last wake (same tasks, same
    # statuses, nothing updated) AND the last run didn't error, waking again only
    # re-discovers an unchanged snapshot inside an LLM run. Hold instead — for $0.
    # The separate daily 05:00 `clickup-executor` cron is unaffected (it bypasses the
    # gate), so this cannot starve work; the moment ANYTHING changes, sig differs -> wake.
    #
    # D2 FIX (2026-06-23, ClickUp 86e20h5ma): liveness / time floor — never suppress
    # longer than one additional WAKE_COOLDOWN_S past the last wake. Without this, a
    # tick where the executor exits without posting a comment (no-actionable-work path)
    # produces an identical sig → infinite suppression until the daily 05:00 cron
    # (up to 24 h stall). The floor forces a probe once the cooldown expires again so
    # the gate can detect any work that became stale-signatured only due to no-op executor
    # runs.  The floor does NOT fire when the job last errored (error backoff owns that).
    sig = _snapshot_sig(unclaimed, continuations)
    last_wake_ts = state.get("last_wake_ts", 0)
    suppression_floor_passed = (now - last_wake_ts) >= (2 * WAKE_COOLDOWN_S)
    if (
        sig == state.get("last_wake_sig")
        and _executor_job().get("last_status") != "error"
        and not suppression_floor_passed
    ):
        _save_json(STATE_PATH, state)
        print(
            f"[gate] work present ({len(unclaimed)} unclaimed, "
            f"{len(continuations)} continuation) but holding: no snapshot delta "
            "since last wake (nothing claimed/updated) — executor stays asleep ($0)"
        )
        return 0
    if sig == state.get("last_wake_sig") and suppression_floor_passed:
        print(
            f"[gate] suppression floor passed ({int((now - last_wake_ts)/60)}m since last wake) "
            "— forcing probe despite no snapshot delta"
        )

    woke = False
    claim_lf = None  # D1: atomic claim lock file handle
    # Prefer resuming an ELIGIBLE continuation over starting new unclaimed work —
    # finish started work before claiming more. Continuations are throttled by
    # _pick_continuation (1h/task cooldown + 4/day cap), so this can NOT starve
    # unclaimed: when no continuation is off-cooldown we fall through to unclaimed.
    # FIX 2026-06-16 (Claude babysit): the previous `if unclaimed: ... else:
    # continuation` order starved continuations whenever ANY unclaimed agent-ready
    # task existed — a stuck in-progress task (e.g. 86e1wx7kx) was never resumed
    # while sibling to-do tasks kept the unclaimed list non-empty.
    task, rec, stale_ids = _pick_continuation(continuations, state, now) if continuations else (None, None, [])
    if stale_ids:
        print(
            f"[gate] {len(stale_ids)} stale continuation(s) NOT re-woken "
            f"(>= {CONTINUATION_STRIKE_CAP} no-progress wakes, date_updated frozen — "
            f"claimed-then-abandoned; still agent-ready + visible, needs operator): "
            f"{', '.join(stale_ids)}"
        )
    # WAKE-FREEZE FIX (2026-06-26, caretaker): a continuation whose claim lock is
    # held must NOT dead-end this tick. The bug it fixes: when the picked
    # continuation's claim was refused, the gate took the `claim_lf is None`
    # branch, left woke=False, and returned — so last_wake_ts (the cooldown /
    # liveness stamp, gated on `woke` at the D3 block below) never advanced and the
    # SAME refused candidate was re-evaluated every 15-min tick for ~12h (observed
    # 2026-06-25 22:46 -> 2026-06-26 10:38, lock 86e1zttq4.lock; consec_error=0,
    # status ok, no cooldown). Fix: if the continuation claim is held, fall through
    # to the unclaimed path instead of giving up, so the gate still makes progress.
    if task is not None:
        print(f"[gate] continuation candidate: {task['id']}({task['name'][:50]})")
        # D1: Acquire atomic claim lock before waking executor on this task.
        # Refuses to wake if another executor already holds a live claim.
        claim_lf = _try_acquire_claim(task["id"])
        if claim_lf is None:
            print(
                f"[gate] D1: claim lock for {task['id']} already held (live claim) — "
                "falling through to unclaimed work (wake-freeze fix 2026-06-26)",
                file=sys.stderr,
            )
            task = None  # let the `elif unclaimed` path below run this tick
        else:
            woke = _wake(f"continuation of {task['id']}")
            if woke:
                # WORKED-BY STAMP (86e29q8pg): the ClickUp task_id is known here
                # (unlike the unclaimed path below, where the executor self-
                # selects) — this is the tracked choke point for marking the
                # task as Hermes-worked. Best-effort; never blocks the wake.
                if not os.environ.get("DRY_RUN"):
                    _stamp_worked_by_hermes(task["id"])
                rec["count"] = rec.get("count", 0) + 1
                rec["last_ts"] = now
                # Cumulative no-progress strike bookkeeping (reset on progress in _pick).
                rec["total_wakes"] = rec.get("total_wakes", 0) + 1
                rec["last_updated"] = task.get("date_updated")
                state.setdefault("continuation_wakes", {})[task["id"]] = rec
                # Deterministic pin: tell the executor EXACTLY which task to work.
                if not os.environ.get("DRY_RUN"):
                    _save_json(TARGET_PATH, {"task_id": task["id"], "reason": "continuation",
                                             "name": task["name"], "ts": now})
    # `not woke` lets a continuation whose claim was REFUSED (task set to None
    # above) still attempt the unclaimed path this tick instead of dead-ending —
    # wake-freeze fix 2026-06-26. A successful continuation wake has woke=True and
    # skips this block (we never double-wake for one tick).
    if not woke and unclaimed:
        ids = ", ".join(f"{t['id']}({t['name'][:50]})" for t in unclaimed)
        print(f"[gate] {len(unclaimed)} unclaimed agent-ready task(s): {ids}")
        # D1: For unclaimed work, no single task is pinned (executor picks),
        # so we acquire a claim on a sentinel "unclaimed" key. This prevents
        # two concurrent gate ticks from both waking an executor on the same
        # unspecified-target claim.  When N=2 ships, this sentinel will be
        # replaced by per-task lock acquisition inside the executor itself;
        # for now it preserves correct single-executor behavior.
        claim_lf = _try_acquire_claim("__unclaimed__")
        if claim_lf is None:
            print(
                "[gate] D1: unclaimed claim lock already held — skipping wake",
                file=sys.stderr,
            )
        else:
            woke = _wake("unclaimed work")
            # No single pin for unclaimed — executor picks per its own rule.
            if woke and not os.environ.get("DRY_RUN"):
                _save_json(TARGET_PATH, {"task_id": None, "reason": "unclaimed", "ts": now})
            # N>=2: wake a SECOND executor when there is genuinely parallelisable
            # first-claim work (>=2 unclaimed candidates) and concurrency is on.
            # The two diverge via the atomic claim store; a spurious 2nd wake
            # at worst produces a no-work tick. Only on the unclaimed path —
            # never for a single pinned continuation.
            if woke and _executor_concurrency() >= 2 and len(unclaimed) >= 2:
                woke2 = _wake("unclaimed work (executor-2, N>=2)", EXECUTOR_ID_2)
                print(f"[gate] N>=2: second executor wake -> {woke2}")
    elif task is None and not unclaimed:
        # No continuation eligible AND no unclaimed work — nothing to wake.
        # Still advance last_wake_ts: this tick reached the decision point with
        # continuation(s) present but none wakeable, so the cooldown clock must
        # move (wake-freeze fix 2026-06-26) — otherwise a persistently-refused
        # continuation re-evaluates every tick forever.
        if not os.environ.get("DRY_RUN"):
            state["last_wake_ts"] = now
        _save_json(STATE_PATH, state)
        print(
            f"[gate] {len(continuations)} stuck in-progress task(s) but all on "
            "continuation cooldown/daily cap — executor stays asleep"
        )
        return 0
    elif woke and unclaimed:
        # N>=2 STARVATION FIX (2026-07-01, babysit root-cause reconfiguration).
        # Before this branch, the N>=2 second-executor wake lived ONLY inside the
        # `not woke and unclaimed` branch above — i.e. it could fire ONLY on a
        # tick where executor-1 did NOT take a continuation. But "prefer resuming
        # an eligible continuation" (comment above) means that whenever ANY
        # continuation is off-cooldown — nearly always true with a healthy
        # backlog (observed: ~20 in-progress continuation tasks vs 13 unclaimed
        # on 2026-07-01) — executor-1 takes the continuation path, sets
        # woke=True, and this whole unclaimed/N>=2 block was skipped every
        # single tick. Verified impact: clickup-executor-2 (baa3251e033d),
        # created 2026-06-24 specifically to be a second concurrent worker (see
        # CLAUDE.md model-stack note), had completed only 4 runs in 7 days vs
        # clickup-executor's 580 over the same window — structurally starved,
        # not merely under-provisioned. Fix: when executor-1 wakes on a pinned
        # continuation, independently offer executor-2 any unclaimed first-claim
        # work THIS SAME TICK. A single unclaimed candidate is enough here
        # (unlike the `not woke` branch's len>=2 requirement) because executor-1
        # in this branch is pinned to its continuation and never touches the
        # unclaimed pool.
        if _executor_concurrency() >= 2:
            claim_lf2 = _try_acquire_claim("__unclaimed__")
            if claim_lf2 is None:
                print(
                    "[gate] D1: unclaimed claim lock already held — skipping "
                    "executor-2 wake (parallel to continuation)",
                    file=sys.stderr,
                )
            else:
                woke2 = _wake(
                    "unclaimed work (executor-2, N>=2, parallel to continuation)",
                    EXECUTOR_ID_2,
                )
                print(
                    f"[gate] N>=2: second executor wake (parallel to continuation) -> {woke2}"
                )

    # WAKE-FREEZE FIX (2026-06-26, caretaker) — liveness stamp decoupled from wake
    # success. last_wake_ts also drives the cooldown floor in _wake_allowed; if a
    # tick reached the wake-decision point with work present but could NOT wake
    # (claim refused on BOTH continuation and the unclaimed sentinel), advance the
    # cooldown stamp anyway so the gate does not re-evaluate the identical refused
    # candidate every 15-min tick. The no-delta `last_wake_sig` (D3 invariant)
    # stays gated on a REAL wake below and is NOT touched here.
    if not woke and not os.environ.get("DRY_RUN"):
        state["last_wake_ts"] = now
        print("[gate] no wake this tick (claim held) — advanced cooldown stamp "
              "to prevent refused-candidate re-eval loop (wake-freeze fix)")

    # D3 FIX (2026-06-23, ClickUp 86e20h5ma): only advance last_wake_ts and
    # last_wake_sig when the wake SUCCEEDED (rc==0). A failed wake (hermes not
    # found, rc!=0) leaves the queue unchanged, so advancing the sig would
    # make the next tick see "no delta" and suppress — mistaking a failed wake
    # for a successful one and stalling work indefinitely.
    if woke and not os.environ.get("DRY_RUN"):
        state["last_wake_ts"] = now
        state["last_wake_sig"] = sig  # baseline for no-delta suppression next tick
        job = _executor_job()
        if job.get("last_status") == "error":
            state["consecutive_error_wakes"] = state.get("consecutive_error_wakes", 0) + 1
        else:
            state["consecutive_error_wakes"] = 0
    # D1: Release claim lock AFTER state is committed (TARGET_PATH written above,
    # state saved below). The executor's window to read TARGET_PATH begins after
    # we release; releasing early would allow a race to overwrite TARGET_PATH.
    _release_claim(claim_lf)
    if not os.environ.get("DRY_RUN"):
        _save_json(STATE_PATH, state)  # persist last_seen + wake bookkeeping
    return 0


if __name__ == "__main__":
    sys.exit(main())
