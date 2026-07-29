#!/usr/bin/env python3
"""Review-daemon GATE — cheap, deterministic, zero-LLM. Mirrors clickup_poll_gate.py.

Finds Hermes-built tasks carrying the `needs-validation` tag (ANY non-closed
status), writes review_snapshot.json, and wakes the `hermes-pr-validate` agent
only when there is work. $0 when idle. Never raises out of main().
Token: CLICKUP_API_TOKEN. Wake target resolved by NAME from ~/.hermes/cron/jobs.json.
"""
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

# This module is the implementation. The runtime-root review_poll_gate.py is
# intentionally only a compatibility shim, so implementation dependencies stay
# inside the package and can never fall back to retired flat helper modules.
from . import autonomous_merge
from . import pr_pipeline_event_driven
from . import pr_pipeline_improvements
from . import validator_verdict

TEAM_ID = "9017245888"
# The `needs-validation` TAG is the SOLE discriminator for a daemon handoff.
# Status is NOT gated: 3-status lists (e.g. AI Dev Assistant: to do / in
# progress / complete) have NO review status, so a status filter strands their
# handoffs forever (the executor can only reach `in progress` or `complete`
# there). Decision-parks use `agent-review`, not this tag, so they never
# false-match; and `include_closed=false` already drops archived/closed tasks.
# This aligns the gate with the long-documented "polls by tag, not status"
# intent (ref #47) that the old status filter silently contradicted.
# (babysitter fix 2026-06-17 — was {"ready for review","in review"} 2026-06-15)
NEEDS_TAG = "needs-validation"
# Re-pointed 2026-06-18 from the old "hermes-validate" (now a SKILL.md size
# monitor that checked nothing about PRs) to the real risk-gated PR validator.
AGENT_JOB_NAME = "hermes-pr-validate"
# DB-publish lane neutralization (2026-06-27 rework). The DB-backed publish lane
# (dynamics365group.com blogs: a Neon `posts` row write IS the publish, NO PR —
# the deliverable is a LIVE URL) writes a self-attesting publish marker at
# ~/.hermes/deliverables/<task_id>/publish_result.json. The validator this gate
# wakes (hermes-pr-validate) is PR-ONLY: for a DB task it finds no PR (or a
# leftover wrong-approach PR) and emits a spurious FAIL/BLOCK that re-wedges the
# task. The DB lane is validated+closed by db_closeout_actor (live-URL re-verify +
# guarded flip on the SAME closeout-actor cron), NOT by this PR validator. So a
# `needs-validation` task that has a DB publish marker is NOT a PR-validation
# handoff — exclude it here so the PR validator never touches the DB lane.
DELIVERABLES_DIR = os.path.expanduser("~/.hermes/deliverables")
# 86e29q8qd/86e2eu8a4 (no-measurement failure class, Audit M6). ignite-validate
# ESCALATEs a structurally-unmeasurable/external-blocked objective to Needs
# Human on the first fail and tags it `needs-human` (never `validate-failed`,
# which would re-loop it). Without this exclusion, a subsequent orphan-PR sweep
# here could still re-add `needs-validation` and hand it right back to the
# validator, undoing the escalation the same way the executor-side SLA guard
# (`clickup_review_sla.py::_resume_validation_blocked`) had to be hardened
# against — mirror that guard here.
NEEDS_HUMAN_TAG = "needs-human"
# Keep accepting the original classifier tag while historical tasks are still
# being reconciled. New validator escalations carry needs-human too, but a
# no-measurement-only task must receive the same hard fence on its first and
# every later review-gate tick.
NO_MEASUREMENT_TAG = "no-measurement"


def _is_db_publish_task(task_id):
    """True iff the task has a DB-publish marker (so it ships via a posts-row write,
    not a PR). Such tasks must NOT be routed to the PR validator."""
    if not task_id:
        return False
    return os.path.isfile(os.path.join(DELIVERABLES_DIR, task_id, "publish_result.json"))
SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
SNAPSHOT_PATH = os.path.join(SCRIPTS_DIR, "review_snapshot.json")
STATE_PATH = os.path.join(SCRIPTS_DIR, ".review_gate_state.json")
JOBS_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
WAKE_COOLDOWN_S = 20 * 60
# Orphan-sweep is expensive (gh API call per allowlisted repo); throttle to once per 2h.
ORPHAN_SCAN_INTERVAL_S = 2 * 60 * 60
HTTP_TIMEOUT = 30

def _token(): return os.environ.get("CLICKUP_API_TOKEN", "").strip()
def _now_iso(): return datetime.now(timezone.utc).isoformat()

def _tagnames(t): return {(x.get("name") or "").lower() for x in (t.get("tags") or [])}

def is_pending(task):
    # Tag-only: any non-closed task carrying needs-validation is a handoff —
    # EXCEPT DB-publish tasks (no PR; live-URL lane), which db_closeout_actor owns.
    # Routing a DB task to the PR validator emits a spurious FAIL/BLOCK that wedges
    # it (the 2026-06-27 strand). Exclude by the durable DB publish marker.
    if NEEDS_TAG not in _tagnames(task):
        return False
    if _is_db_publish_task(task.get("id")):
        return False
    if {NEEDS_HUMAN_TAG, NO_MEASUREMENT_TAG} & _tagnames(task):
        return False
    return True

def entry(t):
    return {"id": t.get("id"), "name": (t.get("name") or "")[:160],
            "status": (t.get("status") or {}).get("status"),
            "list": ((t.get("list") or {}) or {}).get("name"),
            "list_id": ((t.get("list") or {}) or {}).get("id"),
            "url": t.get("url")}

def wake_allowed(state, now):
    return (now - state.get("last_wake_ts", 0)) >= WAKE_COOLDOWN_S

def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": _token()})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _scan():
    """Tag-filter first (cheap); fall back to full paginated scan on error."""
    try:
        # subtasks=true is REQUIRED: the team/task endpoint excludes subtasks by
        # default, so Hermes PRs shipped as subtasks (e.g. islandwell 86e1z3ccm/
        # 86e1z3cck under parent 86e1z37qq) carried needs-validation but were
        # invisible to the validator — the primary path silently dropped them and
        # the subtasks=true fallback only fired on exception (never reached on
        # success). Babysitter fix 2026-06-22. Live-verified: 29→33 tasks, both
        # islandwell PR subtasks surface only with this flag.
        q = urllib.parse.urlencode({"include_closed": "false", "subtasks": "true"}) + \
            f"&tags%5B%5D={urllib.parse.quote(NEEDS_TAG)}"
        data = _get(f"https://api.clickup.com/api/v2/team/{TEAM_ID}/task?{q}")
        return [entry(t) for t in (data.get("tasks") or []) if is_pending(t)]
    except Exception as e:
        print(f"[review-gate] tag filter failed ({e!r}); full scan", file=sys.stderr)
    pending, page = [], 0
    while True:
        q = urllib.parse.urlencode({"page": page, "subtasks": "true", "include_closed": "false"})
        data = _get(f"https://api.clickup.com/api/v2/team/{TEAM_ID}/task?{q}")
        tasks = data.get("tasks") or []
        pending.extend(entry(t) for t in tasks if is_pending(t))
        if data.get("last_page", True) or not tasks: break
        page += 1
        if page > 50: break
    return pending

def _load(path, d):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return d

def _save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, indent=2)
    os.replace(tmp, path)

def _agent_id():
    for j in _load(JOBS_PATH, {}).get("jobs", []):
        if j.get("name") == AGENT_JOB_NAME: return j.get("id")
    return None

def _wake(reason):
    if os.environ.get("DRY_RUN"):
        print(f"[review-gate] DRY_RUN — would wake {AGENT_JOB_NAME} ({reason})"); return True
    aid = _agent_id()
    if not aid:
        print(f"[review-gate] {AGENT_JOB_NAME!r} not in jobs.json", file=sys.stderr); return False
    try:
        r = subprocess.run([HERMES_BIN, "cron", "run", aid], capture_output=True, text=True, timeout=60)
        print(f"[review-gate] woke {aid} ({reason}): rc={r.returncode} {r.stdout.strip()}")
        return r.returncode == 0
    except Exception as e:
        print(f"[review-gate] wake failed: {e!r}", file=sys.stderr); return False

def _merge_sweep():
    """Deterministic merge of every PR the gates permit (fresh PASS + non-shadow
    + tier-autonomy + allowlist + live-head match + checks green). This is the
    ACTOR that closes the autonomy loop — the validator only writes verdicts; the
    gate (this cron, every 15 min) is what actually lands them. Best-effort: a
    failure here never breaks the poll gate. $0 / no-op when nothing is mergeable."""
    try:
        res = autonomous_merge.sweep()
        merged = [r for r in res if r.get("action") == "merge" and r.get("merge_ok")]
        if merged:
            ids = ", ".join(f"{r['repo']}#{r['pr']}" for r in merged)
            print(f"[review-gate] autonomous_merge landed {len(merged)} PR(s): {ids}")
        return len(merged)
    except Exception as e:
        print(f"[review-gate] merge sweep error (non-fatal): {e!r}", file=sys.stderr)
        return 0


def _human_merge_sweep():
    """Escalate green + mergeable PASS PRs whose risk tier is NOT auto-merge
    eligible. These would otherwise sit forever: the merge actor skips them
    (tier not enabled) and the validator keeps failing them on 'is it live',
    looping execute↔review with no one told a HUMAN must merge (the 'auto-merge
    expectation loop', brain 2026-07-02). sweep_human_merge() emits a one-time
    Slack line + tags the task needs-human. Best-effort, never fatal. Returns
    the count escalated this tick."""
    try:
        human = autonomous_merge.sweep_human_merge()
        n = len([h for h in human if h.get("action") == "needs-human-merge"])
        if n:
            print(f"[review-gate] {n} PR(s) need a HUMAN merge (green+mergeable, tier not auto-eligible)")
        return n
    except Exception as e:
        print(f"[review-gate] human-merge sweep error (non-fatal): {e!r}", file=sys.stderr)
        return 0


def _revalidation_sweep():
    """Detect BLOCK verdicts where the live PR head has moved since the block
    was written (fix pushed but re-validation never triggered) and re-add
    needs-validation to the ClickUp task so the validator picks it up next tick.

    This closes the gap class: 'validator BLOCKed, author pushed a fix, nothing
    noticed.' Without this, a BLOCK sits in the store forever even if the PR head
    changed — the PASS pre-filter in sweep() skips BLOCKs, so the actor is blind
    to head changes on blocked PRs. (Root cause confirmed 2026-06-29 for
    ignite-digital-engine#43: BLOCK for head aadba3a7, live head 354f1719.)

    Best-effort — never fatal. Returns number of tasks re-tagged."""
    try:
        # ~/.hermes/hermes-agent/venv no longer exists post 2026-07-19 deploy
        # topology change (releases/vX.Y.Z-<sha>/ + runtime-current symlink).
        PY = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11")
        VAL_OPS = os.path.join(SCRIPTS_DIR, "hermes_validate_ops.py")
        gaps = autonomous_merge.sweep_gaps()
        retagged = 0
        for g in gaps:
            if g.get("action") != "needs-revalidation":
                continue
            task_id = g.get("task_id") or ""
            repo, pr = g.get("repo"), g.get("pr")
            print(f"[review-gate] revalidation-needed {repo}#{pr} — {g.get('detail')}")
            if task_id:
                if _task_is_human_fenced(task_id):
                    print(
                        f"[review-gate] preserving human fence on {task_id}; "
                        "not re-tagging needs-validation"
                    )
                    continue
                try:
                    r = subprocess.run(
                        [PY, VAL_OPS, "add-tag", task_id, "needs-validation"],
                        capture_output=True, text=True, timeout=30)
                    if r.returncode == 0:
                        print(f"[review-gate] re-tagged {task_id} with needs-validation")
                        retagged += 1
                    else:
                        print(f"[review-gate] re-tag {task_id} failed: {r.stderr.strip()[:120]}",
                              file=sys.stderr)
                except Exception as e:
                    print(f"[review-gate] re-tag {task_id} error: {e!r}", file=sys.stderr)
            else:
                print(f"[review-gate] no task_id for {repo}#{pr} — cannot auto-re-tag; "
                      "manual re-tag needed", file=sys.stderr)
        return retagged
    except Exception as e:
        print(f"[review-gate] revalidation sweep error (non-fatal): {e!r}", file=sys.stderr)
        return 0


def _task_is_human_fenced(task_id):
    """Return whether a task must never be sent back to PR validation.

    This guard deliberately runs *before* the mutating add-tag call. The
    revalidation gap input contains only a task id, not ClickUp tag state, so
    filtering later in ``is_pending`` is too late: it has already revived a
    fenced task. On a ClickUp read failure we fail closed; missing one retry is
    recoverable on the next 15-minute sweep, while reviving a human-only task
    recreates the executor/validator loop this gate is meant to stop.
    """
    try:
        data = _get(f"https://api.clickup.com/api/v2/task/{task_id}")
        tags = _tagnames(data)
        return bool({NEEDS_HUMAN_TAG, NO_MEASUREMENT_TAG} & tags)
    except Exception as exc:
        print(
            f"[review-gate] cannot inspect fencing tags for {task_id}: {exc!r}; "
            "skipping re-tag this tick",
            file=sys.stderr,
        )
        return True


def _extract_clickup_task_id(body, head_ref=""):
    """Extract a ClickUp task ID from a PR body, falling back to the branch name.

    Hermes commonly writes the task id in one of these forms:
      - 'clickup.com/t/86e1yjkmp'            (linked format)
      - 'ClickUp task [86e1yjkmp](...)'       (markdown link)
      - 'ClickUp task 86e1yjkmp'              (plain text)
      - 'agent/86e1yjkmp'                     (executor branch name)

    Returns the task ID string (lowercase alphanumeric) or '' if none found."""
    if body:
        m = re.search(r"clickup\.com/t/([a-z0-9]+)", body, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"[Cc]lick[Uu]p task \[?([a-z0-9]+)\]?", body, re.IGNORECASE)
        if m:
            return m.group(1)
    if head_ref:
        m = re.search(r"(?:^|/)agent/([a-z0-9]+)", head_ref, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _task_has_needs_validation_tag(task_id):
    """True iff the ClickUp task already carries the needs-validation tag. Fails
    OPEN (returns False) if the fetch errors — callers then attempt the re-tag
    which is idempotent (double-tag is a no-op in ClickUp)."""
    try:
        data = _get(f"https://api.clickup.com/api/v2/task/{task_id}")
        tags = {(x.get("name") or "").lower() for x in (data.get("tags") or [])}
        return NEEDS_TAG in tags
    except Exception:
        return False  # fail-open: if we can't verify, attempt the tag anyway


def _orphan_pr_sweep():
# Event-driven validator wake (PR pipeline improvement #1)
    # Trigger hermes-pr-validate immediately when PRs are detected
    # needing immediate validation (>6h old, no fresh verdict)
    # instead of waiting for the next hourly tick.
    try:
        allowlist = autonomous_merge._load_allowlist()
        woken, prs = pr_pipeline_event_driven.wake_validator_if_needed(allowlist)
        if woken:
            print(f"[review-gate] event-driven wake triggered for {woken} PR(s)")
    except Exception as e:
        print(f"[review-gate] event-driven wake error (non-fatal): {e!r}", file=sys.stderr)
    """Detect open PRs on allowlisted repos that have NO verdict in the store at
    all (never validated — neither PASS nor BLOCK) and add `needs-validation` to
    their linked ClickUp task so the validator picks them up on the next tick.

    Closes the gap class: 'executor created a PR, committed, but the ClickUp
    needs-validation handoff tag was never set.' This is distinct from
    _revalidation_sweep() which handles BLOCK verdicts where the head moved — here
    there is simply *no verdict entry* for the PR at all.

    To avoid hammering the GitHub API on every 15-min cron tick, this sweep only
    runs once per ORPHAN_SCAN_INTERVAL_S (default 2h). Best-effort — never fatal.
    Returns number of tasks re-tagged."""
    try:
        # Throttle: read state before loading heavy deps
        state = _load(STATE_PATH, {})
        last_scan = state.get("last_orphan_scan_ts", 0)
        if (time.time() - last_scan) < ORPHAN_SCAN_INTERVAL_S:
            return 0  # not time yet — skip silently

        # ~/.hermes/hermes-agent/venv no longer exists post 2026-07-19 deploy
        # topology change (releases/vX.Y.Z-<sha>/ + runtime-current symlink).
        PY = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11")
        VAL_OPS = os.path.join(SCRIPTS_DIR, "hermes_validate_ops.py")

        allowlist = autonomous_merge._load_allowlist()
        store = validator_verdict.load_verdicts()
        stored_keys = set(store.keys())  # e.g. {"owner/repo#72", ...}

        tagged = 0
        scanned_repos = 0

        for repo in sorted(allowlist):
            try:
                r = subprocess.run(
                    ["gh", "pr", "list", "--repo", repo, "--state", "open",
                     "--json", "number,body,headRefName", "--limit", "50"],
                    capture_output=True, text=True, timeout=60)
                if r.returncode != 0:
                    continue
                prs = json.loads(r.stdout or "[]")
                scanned_repos += 1
                for pr_data in prs:
                    pr_num = pr_data.get("number")
                    if pr_num is None:
                        continue
                    key = f"{repo}#{pr_num}"
                    if key in stored_keys:
                        continue  # has a verdict (PASS or BLOCK) — not an orphan
                    # No verdict at all: potential validation orphan.
                    body = pr_data.get("body") or ""
                    head_ref = pr_data.get("headRefName") or ""
                    task_id = _extract_clickup_task_id(body, head_ref)
                    if not task_id:
                        print(f"[review-gate] orphan-pr {repo}#{pr_num} — no verdict "
                              f"in store and no ClickUp task ID found in body; "
                              f"cannot auto-tag (manual intervention needed)")
                        continue
                    if _task_has_needs_validation_tag(task_id):
                        continue  # already tagged — validator will pick it up
                    print(f"[review-gate] orphan-pr {repo}#{pr_num} — no verdict in "
                          f"store, task {task_id} lacks needs-validation; re-tagging")
                    try:
                        r2 = subprocess.run(
                            [PY, VAL_OPS, "add-tag", task_id, NEEDS_TAG],
                            capture_output=True, text=True, timeout=30)
                        if r2.returncode == 0:
                            print(f"[review-gate] orphan-tagged {task_id} "
                                  f"({repo}#{pr_num}) with needs-validation")
                            tagged += 1
                        else:
                            print(f"[review-gate] orphan-tag {task_id} failed: "
                                  f"{r2.stderr.strip()[:120]}", file=sys.stderr)
                    except Exception as e:
                        print(f"[review-gate] orphan-tag {task_id} error: {e!r}",
                              file=sys.stderr)
            except Exception as e:
                print(f"[review-gate] orphan scan {repo} error (non-fatal): {e!r}",
                      file=sys.stderr)

        # Record the scan time (so the throttle suppresses until next window)
        state = _load(STATE_PATH, {})
        state["last_orphan_scan_ts"] = time.time()
        _save(STATE_PATH, state)
        print(f"[review-gate] orphan-pr-sweep complete: "
              f"{tagged} orphan(s) re-tagged, {scanned_repos} repos scanned")
        return tagged
    except Exception as e:
        print(f"[review-gate] orphan pr sweep error (non-fatal): {e!r}", file=sys.stderr)
        return 0


def main():
    if not _token():
        print("[review-gate] CLICKUP_API_TOKEN not set", file=sys.stderr)
        print(json.dumps({"wakeAgent": False})); return 0

    # Load allowlist for PR pipeline improvements
    try:
        allowlist = autonomous_merge._load_allowlist()
    except Exception as e:
        print(f"[review-gate] Failed to load allowlist for PR pipeline: {e}", file=sys.stderr)
        allowlist = []

    # PR pipeline: route conflicting PRs to executor
    try:
        routed = pr_pipeline_improvements.route_conflicting_prs_to_executor(allowlist, dry_run=False)
        if routed > 0:
            print(f"[review-gate] Routed {routed} conflicting PR(s) to executor")
    except Exception as e:
        print(f"[review-gate] PR conflict routing error (non-fatal): {e!r}", file=sys.stderr)

    # PR pipeline: staleness alert — moved out to its own cron job
    # (pr_staleness_alert.py / "pr-staleness-alert", deliver=slack:UN4CQ1EGG,
    # 2026-07-22). This job's `deliver` is "local", so a check_staleness_and_alert()
    # call here was computing + printing the >6h-no-verdict alert but it never
    # reached Slack — nobody ever saw it. Running it here AND in the dedicated
    # job also raced on the shared .pr_pipeline_state.json dedup key and could
    # intermittently suppress the real (Slack-delivered) alert. Do not re-add
    # this call; see pr_staleness_alert.py for the live mechanism.

    # Land anything already cleared to merge BEFORE scanning for new work — the
    # validator wrote those verdicts on a prior tick; this is where they ship.
    _merge_sweep()
    # Re-tag any BLOCK verdicts whose PR head has moved (fix pushed, no re-validate).
    # Re-tagging adds needs-validation to the ClickUp task so the scan below picks
    # it up and wakes the validator. Best-effort, non-fatal.
    _revalidation_sweep()
    # Escalate green+mergeable PASS PRs that can't auto-merge (tier not eligible) so
    # they get a HUMAN merge instead of looping execute↔review forever. Once per head.
    _human_merge_sweep()
    # Detect open PRs with NO verdict at all (validation orphans — executor built the
    # PR but the needs-validation handoff tag was never set). Throttled to once per 2h.
    _orphan_pr_sweep()
    try:
        pending = _scan()
    except Exception as e:
        print(f"[review-gate] scan error: {e!r}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False})); return 0
    now = time.time(); state = _load(STATE_PATH, {})
    _save(SNAPSHOT_PATH, {"generated_at": _now_iso(), "count": len(pending), "tasks": pending})
    if not pending:
        state["last_poll"] = _now_iso(); _save(STATE_PATH, state)
        print(json.dumps({"wakeAgent": False})); return 0
    if not wake_allowed(state, now):
        state["last_poll"] = _now_iso(); _save(STATE_PATH, state)
        print(f"[review-gate] {len(pending)} pending but cooldown; holding")
        print(json.dumps({"wakeAgent": False})); return 0
    ids = ", ".join(f"{t['id']}({t['name'][:40]})" for t in pending[:5])
    print(f"[review-gate] {len(pending)} task(s) need validation: {ids}")
    woke = _wake(f"{len(pending)} pending")
    if woke and not os.environ.get("DRY_RUN"): state["last_wake_ts"] = now
    state["last_poll"] = _now_iso(); _save(STATE_PATH, state)
    print(json.dumps({"wakeAgent": bool(woke), "pending": len(pending)})); return 0


def safe_main():
    """Run the gate without letting an operational failure break cron delivery."""
    try:
        return main()
    except Exception as e:
        print(f"[review-gate] unexpected: {e!r}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0


if __name__ == "__main__":
    sys.exit(safe_main())
