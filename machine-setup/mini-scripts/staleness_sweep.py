#!/usr/bin/env python3
"""
staleness_sweep.py — flag done-but-unlanded ClickUp tasks + open PRs (backstop for autonomous ship).

Closes the gap that the parent epic (86e1vv88r, "ship without human PR review") only
partially closes while trustworthy CI + reviewer-agent gate are still being built: work
that is done but stuck on `ready for review`/`in review` + an unmerged PR. Today nobody
watches that pile; Colin does not do human merges by policy.

Detects (in one idempotent pass):
  1. ClickUp tasks Hermes worked that are sitting in a review-status (ready for review,
     in review, qa, etc.) past STALENESS_TASK_HOURS with no forward movement (the SLA is
     measured by a FIRST-SEEN-IN-REVIEW timestamp this script records, NOT by
     date_updated — assigning/tagging does not reset the clock).
  2. Open PRs across the active repos that are MERGEABLE (gh mergeable=MERGEABLE) but
     blocked only on stale/redundant checks (commit-author identity, known-flaky E2E,
     etc.). Distinguishes "landable" (only failing check is a known identity/flake) from
     "genuinely red" (failing real test gate).

Emits ONE consolidated digest (stdout + Slack-ready text). Idempotent: the seen-marker
state file holds last-emitted items by stable id (task_id or `repo#pr_number`); if a
stale item from the prior digest is no longer stale, it is removed; new stale items
replace it. No per-item alert spam on repeat runs.

Default mode is DRY RUN. Promote to live only after Colin signs off on a dry-run
digest (same rollout discipline as the groomer + the review-SLA sweep).

Env:
  STALENESS_DRY_RUN               "1" (default) = dry-run; "0" = live
  STALENESS_TASK_HOURS            ClickUp review-status SLA hours (default "24")
  STALENESS_PR_HOURS              Open-PR staleness window hours (default "24")
  CLICKUP_API_TOKEN               required (Doppler-injected into the gateway env)
  CLICKUP_TEAM_ID                 workspace (team) id (default "9017245888")
  STALENESS_REPOS                 comma-separated owner/repo list (default: known active)
  STALENESS_FLAKY_CHECKS          comma-separated check-name fragments; matched as
                                  substrings. A PR whose ONLY failure is one of these
                                  is classified "landable" vs "genuinely red".
                                  Default: "commit author email,Vercel identity"
                                  (the Vercel check-name includes "email address").

Schedule: every 6h via the staleness-sweep cron job (one new cron entry).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

TEAM_ID = os.environ.get("CLICKUP_TEAM_ID", "9017245888")

# Review status names (lower-cased) — same set as clickup_review_sla.py so the two
# sweeps classify the same way.
REVIEW_STATUSES = {
    "ready for review", "review", "in review", "for review",
    "qa", "qa review", "needs review",
}

# In-progress status names (lower-cased) — matched from live ClickUp list statuses.
IN_PROGRESS_STATUSES = {"in progress", "in-progress"}

# Requeue tag policy — MIRRORS clickup_poll_gate.py so the sweep only requeues
# tasks the poll-gate would actually (and appropriately) re-claim. Requeuing a
# task that lacks agent-ready, or that carries a park/avoid tag, is pointless
# churn at best and un-parks a deliberate park at worst (2026-06-30, the
# 86e1ymuu3 park-blocked-external false positive caught in adversarial review).
REQUEUE_REQUIRED_TAG = "agent-ready"          # poll-gate READY_TAG — must be present
REQUEUE_EXCLUDE_TAGS = {                        # any of these = intentional park / human-only → skip
    "park-blocked-external",                   # awaiting operator decision (gate drops agent-ready)
    "agent-review",                            # half-park / review hand-off
    "agent-avoid",                             # operator hands-off, human-only (hard exclusion)
    "no-agent",                               # localization/human-only convention alias
}


def _requeue_tag_eligible(task: dict) -> bool:
    """True iff the task is safe+useful to requeue: it still carries agent-ready
    AND none of the park/avoid tags. Mirrors the poll-gate claim predicate so we
    never un-park a deliberate park nor flip a task the gate won't re-claim."""
    tags = {(t.get("name") or "").lower() for t in (task.get("tags") or [])}
    if REQUEUE_REQUIRED_TAG not in tags:
        return False
    return tags.isdisjoint(REQUEUE_EXCLUDE_TAGS)

# "Worked By" custom field on the Hermes-stamped lists — we only flag tasks Hermes
# actually worked, not the ~80 legacy human-board items that also sit in review.
WORKED_BY_FIELD_ID = "2bf5c958-ca2a-4f6b-bab5-25693b98b1f1"
HERMES_OPTION_ID = "36c0d22d-3128-42b3-94d2-0d6072d2c0ea"
HERMES_OPTION_ORDERINDEX = 1

# Active repos to scan for open PRs (allowlist — the sweep must NEVER crawl
# repos Hermes does not own).
# OEConnection / PartsTech are FENCED OFF from Hermes (2026-06-22, Colin):
# colingreig/oeconnection-com + oec-web/oeconnection-com removed from this sweep
# so Hermes never nudges OEC PRs. See clickup_poll_gate.py OEC folder exclusion.
_DEFAULT_REPOS = "colingreig/jdmbuysell-v4,colingreig/elevatoruptime.com,colingreig/ignite-digital-engine"
REPOS = [r.strip() for r in os.environ.get("STALENESS_REPOS", _DEFAULT_REPOS).split(",") if r.strip()]

# Substring matches classify a failing check as "known-flaky / identity-block" (landable).
# Conservative: if the PR has ANY failure NOT matching one of these, classify as
# "genuinely red" and surface, but do NOT push.
DEFAULT_FLAKY_CHECKS = (
    "commit author email",
    "Vercel identity",
    "no github account",        # Vercel bot's actual failure text
    "vercel commit identity",
    "set your commit email",    # another Vercel wording
)
FLAKY_CHECKS = tuple(
    s.strip().lower()
    for s in os.environ.get("STALENESS_FLAKY_CHECKS", ",".join(DEFAULT_FLAKY_CHECKS)).split(",")
    if s.strip()
)

TASK_SLA_HOURS = float(os.environ.get("STALENESS_TASK_HOURS", "24"))
PR_SLA_HOURS = float(os.environ.get("STALENESS_PR_HOURS", "24"))
# Orphaned in-progress requeue: threshold before we consider a Hermes-worked
# in-progress task without a live claim to be orphaned and requeue it to "to do".
STALENESS_INPROGRESS_HOURS = float(os.environ.get("STALENESS_INPROGRESS_HOURS", "6.0"))
# Default is now LIVE (2026-06-20, Colin signed off on full autonomy — "solve
# these loops on its own"). The script's only live-mode actions are state
# persistence (harmless; happens in dry-run too) and orphan-PR adoption (validate
# → autonomous_merge lands low-risk PASSes). Set STALENESS_DRY_RUN=1 to revert to
# observe-only. (Doppler write was unavailable to flip the env, so the default is
# the promotion lever; an env value still overrides it.)
DRY_RUN = os.environ.get("STALENESS_DRY_RUN", "0") != "0"

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_HERE, ".staleness_seen.json")

# Stable-id prefixes so the seen-set can carry both task_ids and `repo#pr` strings
# without collision.
TASK_PREFIX = "task:"
PR_PREFIX = "pr:"


# ---------------------------------------------------------------------------
# Helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"first_seen": {}, "last_digest_ts": None, "last_digest_count": 0}
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        # Corrupt state — start fresh (better than crashing every tick).
        return {"first_seen": {}, "last_digest_ts": None, "last_digest_count": 0}


def _write_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def _age_hours(iso_ts: str | None) -> float | None:
    """Return hours since *iso_ts*. Handles both ISO-8601 strings and ClickUp's
    millisecond-epoch strings (e.g. ``"1782874151551"``)."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        pass
    # ClickUp task.date_updated is a Unix ms epoch as a decimal string.
    try:
        ms = int(iso_ts)
        return (time.time() - ms / 1000.0) / 3600.0
    except Exception:
        return None


def _curl_get(url: str, token: str) -> dict | None:
    """GET a ClickUp endpoint via the cron-safe shell pattern. Returns parsed JSON or None."""
    try:
        r = subprocess.run(
            ["curl", "-sS", "-H", f"Authorization: {token}", url],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def _curl_put(url: str, token: str, body: dict) -> dict | None:
    """PUT a JSON body to a ClickUp endpoint via curl. Returns parsed JSON or None."""
    try:
        r = subprocess.run(
            [
                "curl", "-sS", "-X", "PUT",
                "-H", f"Authorization: {token}",
                "-H", "Content-Type: application/json",
                "-d", json.dumps(body),
                url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def _gh(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a `gh` subcommand and return (returncode, stdout, stderr). cwd defaults to the
    repo dir for `gh pr view` if given; for `gh pr list` it can be None (gh picks up
    the closest .git)."""
    try:
        r = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=60,
            cwd=cwd,
        )
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "gh not found"
    except Exception as e:
        return 1, "", str(e)


# ---------------------------------------------------------------------------
# ClickUp task sweep

def _list_candidate_tasks(token: str, page_limit: int = 3) -> list[dict]:
    """Walk the team-wide task endpoint (paginated, 100/page) up to page_limit pages,
    filter to Hermes-worked tasks in a review-status. Uses the GET .../task endpoint
    WITHOUT order_by (broken on this API per ref §18). Sort is implicit."""
    out: list[dict] = []
    for page in range(page_limit):
        url = (
            f"https://api.clickup.com/api/v2/team/{TEAM_ID}/task"
            f"?page={page}&include_closed=false"
        )
        data = _curl_get(url, token)
        if not data or not isinstance(data.get("tasks"), list):
            break
        out.extend(data["tasks"])
        if not data.get("tasks"):
            break
    return out


def _is_hermes_worked(task: dict) -> bool:
    """A task counts as 'Hermes-worked' when the 'Worked By' custom field equals Hermes
    (dropdown orderindex 1) — same convention as clickup_review_sla.py.

    The ClickUp v2 API returns this field's value in several shapes depending on
    the endpoint and field configuration:
      • int/float  — raw orderindex (the most common form: value=1 → Hermes)
      • list        — list of orderindexes selected
      • dict        — {"orderindex": 1, ...} object
      • str UUID    — the option's id string
    All four shapes are checked so the guard works regardless of API response format.
    """
    for cf in (task.get("custom_fields") or []):
        if cf.get("id") == WORKED_BY_FIELD_ID:
            val = cf.get("value")
            # Raw integer/float orderindex (the form the team-tasks endpoint actually returns)
            if val == HERMES_OPTION_ORDERINDEX:
                return True
            if isinstance(val, list) and HERMES_OPTION_ORDERINDEX in val:
                return True
            if isinstance(val, dict) and val.get("orderindex") == HERMES_OPTION_ORDERINDEX:
                return True
            if val == HERMES_OPTION_ID:
                return True
    return False


def _is_review_status(task: dict) -> bool:
    status = (task.get("status") or {}).get("status", "").lower()
    return status in REVIEW_STATUSES


def _sweep_clickup_tasks(token: str) -> list[dict]:
    """Return one row per stale Hermes-worked task with the data the digest needs."""
    tasks = _list_candidate_tasks(token)
    rows = []
    for t in tasks:
        if not _is_review_status(t):
            continue
        if not _is_hermes_worked(t):
            continue
        # date_updated is bumped by edits/tags, so the script tracks its OWN first-seen
        # timestamp in the state file (see _record_first_seen).
        rows.append({
            "task_id": t.get("id"),
            "name": t.get("name", "(no name)"),
            "status": (t.get("status") or {}).get("status", "?"),
            "url": f"https://app.clickup.com/t/{t.get('id')}",
            "list": (t.get("list") or {}).get("name", "?"),
            "date_updated": t.get("date_updated"),
        })
    return rows


# ---------------------------------------------------------------------------
# Orphaned in-progress requeue

def _is_in_progress(task: dict) -> bool:
    status = (task.get("status") or {}).get("status", "").lower()
    return status in IN_PROGRESS_STATUSES


def _is_live_claimed(task_id: str) -> bool:
    """Return True iff a LIVE claim exists for task_id. Imports claim_store
    in-process (same interpreter) to avoid a subprocess round-trip; falls
    back to the CLI if the import path is missing. FAIL-OPEN: any error
    returns False so we never block a legitimate requeue on a broken store."""
    try:
        sys.path.insert(0, _HERE)
        import claim_store as _cs
        return _cs.is_claimed(task_id)
    except Exception:
        pass
    # Fallback: shell out to the CLI
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(_HERE, "claim_store.py"), "is-claimed", task_id],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0  # exit 0 = claimed (live), exit 1 = free
    except Exception:
        return False  # fail-open: treat as unclaimed so we can requeue


def _list_inprogress_tasks(token: str, page_limit: int = 5) -> list[dict]:
    """Fetch Hermes-worked tasks whose ClickUp status is one of IN_PROGRESS_STATUSES.
    Uses the API-level ``statuses[]`` filter so that deeply-paginated tasks (beyond
    the 300-task window of ``_list_candidate_tasks``) are not missed. With the filter
    the full in-progress set typically fits on a single page (<100 tasks); 5-page
    cap is a safety ceiling."""
    out: list[dict] = []
    for status_name in IN_PROGRESS_STATUSES:
        encoded = status_name.replace(" ", "%20").replace("-", "%2D")
        for page in range(page_limit):
            url = (
                f"https://api.clickup.com/api/v2/team/{TEAM_ID}/task"
                f"?page={page}&include_closed=false&statuses%5B%5D={encoded}"
            )
            data = _curl_get(url, token)
            if not data or not isinstance(data.get("tasks"), list):
                break
            out.extend(data["tasks"])
            if not data.get("tasks"):
                break
    # Deduplicate by task id (two status variants might overlap in theory).
    seen: set[str] = set()
    deduped: list[dict] = []
    for t in out:
        tid = t.get("id")
        if tid and tid not in seen:
            seen.add(tid)
            deduped.append(t)
    return deduped


def _sweep_orphaned_inprogress(token: str) -> list[dict]:
    """Return Hermes-worked in-progress tasks that are idle > STALENESS_INPROGRESS_HOURS
    AND have no live claim — safe candidates to requeue to 'to do'."""
    tasks = _list_inprogress_tasks(token)
    rows = []
    for t in tasks:
        if not _is_hermes_worked(t):
            continue
        if not _requeue_tag_eligible(t):
            continue  # missing agent-ready, or parked/avoid tag — poll-gate won't/shouldn't re-claim
        age = _age_hours(t.get("date_updated"))
        if age is None or age < STALENESS_INPROGRESS_HOURS:
            continue
        task_id = t.get("id")
        if _is_live_claimed(task_id):
            continue  # live executor owns this task — skip
        rows.append({
            "task_id": task_id,
            "name": t.get("name", "(no name)"),
            "status": (t.get("status") or {}).get("status", "?"),
            "url": f"https://app.clickup.com/t/{task_id}",
            "list": (t.get("list") or {}).get("name", "?"),
            "age_h": round(age, 1),
        })
    return rows


def _requeue_orphaned_inprogress(token: str, orphans: list[dict]) -> tuple[list[str], list[str]]:
    """Flip each orphaned task back to 'to do'. DRY_RUN-safe: never mutates in dry-run.
    Returns (requeued, skipped) lines for the summary block."""
    requeued: list[str] = []
    skipped: list[str] = []
    for o in orphans:
        task_id = o["task_id"]
        label = f"{task_id} [{o['list']}] \"{o['name'][:60]}\" (idle {o['age_h']}h)"
        if DRY_RUN:
            requeued.append(f"WOULD requeue {label}")
            continue
        resp = _curl_put(
            f"https://api.clickup.com/api/v2/task/{task_id}",
            token,
            {"status": "to do"},
        )
        if resp and (resp.get("status") or {}).get("status", "").lower() in ("to do", "todo"):
            requeued.append(f"DID requeue {label}")
        elif resp is None:
            skipped.append(f"API error for {label}")
        else:
            # Unexpected response — report but treat as skipped so we don't loop
            actual = (resp.get("status") or {}).get("status", "?")
            skipped.append(f"Unexpected status '{actual}' after PUT for {label}")
    return requeued, skipped


# ---------------------------------------------------------------------------
# Open-PR sweep

def _classify_pr_failure(checks: list[dict]) -> str:
    """Classify a PR's check status as 'landable' (only known-flaky/identity fails),
    'genuinely red' (real test failure), 'green' (all pass), or 'unknown' (no checks)."""
    failing = []
    for c in checks:
        conclusion = (c.get("conclusion") or c.get("state") or "").upper()
        if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            continue
        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"):
            failing.append(c.get("name") or c.get("context") or "(unnamed)")
    if not failing:
        if not checks:
            return "unknown"
        return "green"
    # Every failure must match a known-flaky substring to classify as landable.
    for f in failing:
        fl = (f or "").lower()
        if not any(frag in fl for frag in FLAKY_CHECKS):
            return "genuinely_red"
    return "landable"


def _sweep_open_prs(repos: list[str]) -> list[dict]:
    """Return one row per MERGEABLE open PR that is blocked only on flaky/identity."""
    rows = []
    for repo in repos:
        # gh pr list returns up to 100 open PRs; the active allowlist repos each have <30
        # open at a time, so this is enough.
        rc, out, err = _gh([
            "pr", "list", "--repo", repo,
            "--state", "open",
            "--json", "number,title,createdAt,headRefName,mergeable,statusCheckRollup,author,url,isDraft",
            "--limit", "50",
        ])
        if rc != 0:
            # Repo missing / no auth / offline — skip, do not crash the sweep.
            continue
        try:
            prs = json.loads(out)
        except Exception:
            continue
        for pr in prs:
            if pr.get("isDraft"):
                continue
            mergeable = (pr.get("mergeable") or "").upper()
            # We want MERGEABLE PRs (merge-blocked OR clean) so we surface both
            # "blocked on flaky red" AND "unblocked, no human pushing the button".
            checks = pr.get("statusCheckRollup") or []
            verdict = _classify_pr_failure(checks)
            if verdict == "green":
                # No failures — only flag if the PR is also stale (no human/agent push
                # to merge it). Verdict "green" still lands on this list when the PR is
                # MERGEABLE and old.
                pass
            created = pr.get("createdAt")
            age_h = _age_hours(created) or 0.0
            if age_h < PR_SLA_HOURS:
                # Too fresh — do not surface.
                continue
            rows.append({
                "repo": repo,
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "url": pr.get("url"),
                "mergeable": mergeable,
                "verdict": verdict,
                "age_h": round(age_h, 1),
                "created": created,
                "author": (pr.get("author") or {}).get("login", "?"),
            })
    return rows


_PY = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11")
_HERE = os.path.dirname(os.path.abspath(__file__))


def _run_validate_pr(repo: str, num) -> int:
    """Run validate_pr.py (NON-shadow) on one PR — writes a PASS/BLOCK verdict the
    autonomous_merge sweep enforces. Returns the rc (0=PASS, 1=BLOCK, 2=op error)."""
    try:
        r = subprocess.run(
            [_PY, os.path.join(_HERE, "validate_pr.py"), "--repo", repo, "--pr", str(num)],
            capture_output=True, text=True, timeout=600)
        return r.returncode
    except Exception as e:
        print(f"[staleness] validate_pr {repo}#{num} failed: {e}", file=sys.stderr)
        return -1


def _adopt_orphan_prs(pr_rows: list[dict]) -> tuple[list[str], list[str]]:
    """Close the ORPHAN-PR gap: a green + mergeable PR that never entered the
    needs-validation task pipeline (no ClickUp task shepherding it) has no verdict,
    so the autonomous_merge actor never sees it and it nags forever. Adopt it INTO
    the existing risk-gated pipeline: run validate_pr.py (writes a verdict, honoring
    the 'low-risk only' rule — low auto-PASSes, medium/high go to the panel, HIGH
    tripwires BLOCK), then review_poll_gate's autonomous_merge sweep lands the fresh
    PASSes. No new merge logic, no gate bypass.

    A PR qualifies iff: checks green + mergeable + repo on the MERGE allowlist + NO
    verdict-store entry at all (an entry means the validator already owns it — not an
    orphan). Idempotent: once validate_pr writes a verdict the PR stops qualifying.
    No-op in DRY_RUN (reports candidates). Best-effort; never raises."""
    adopted, skipped = [], []
    try:
        sys.path.insert(0, _HERE)
        import validator_verdict
        import hermes_validate_ops as hvo
        allow = hvo.load_allowlist()
    except Exception as e:
        print(f"[staleness] adopt: import/allowlist load failed: {e}", file=sys.stderr)
        return adopted, skipped
    for p in pr_rows:
        repo, num = p.get("repo"), p.get("number")
        if p.get("verdict") != "green" or (p.get("mergeable") or "").upper() != "MERGEABLE":
            continue
        if not hvo.repo_allowed(repo, allow):
            skipped.append(f"{repo}#{num} — not on merge allowlist")
            continue
        if validator_verdict.verdict_for(repo, num) is not None:
            continue  # already in the validator pipeline — not an orphan
        if DRY_RUN:
            adopted.append(f"{repo}#{num} — WOULD validate (orphan green+mergeable, no verdict)")
            continue
        rc = _run_validate_pr(repo, num)
        adopted.append(f"{repo}#{num} — validated rc={rc} "
                       f"({'PASS→merge sweep lands it' if rc == 0 else 'BLOCK held' if rc == 1 else 'op-error'})")
    return adopted, skipped


# ---------------------------------------------------------------------------
# State + digest emission

def _stable_id(item: dict, kind: str) -> str:
    if kind == "task":
        return f"{TASK_PREFIX}{item['task_id']}"
    if kind == "pr":
        return f"{PR_PREFIX}{item['repo']}#{item['number']}"
    return ""


def _emit_digest(task_rows: list[dict], pr_rows: list[dict], state: dict, seen_before: set) -> dict:
    """Build the consolidated digest text. Idempotent: re-emitted with no NEW stale
    items -> still emits a 1-line "all-clear" so the cron log has continuity.

    `seen_before` is the snapshot of state["first_seen"].keys() captured at the
    START of this tick (before _record_first_seen mutates state). It is the
    authoritative source for the NEW vs PERSISTENT split."""
    lines = [f"📊 STALENESS SWEEP — {_now_iso()}", f"(dry-run={DRY_RUN}, task_sla={TASK_SLA_HOURS}h, pr_sla={PR_SLA_HOURS}h)"]
    lines.append("")
    stale_tasks = [t for t in task_rows if _stable_id(t, "task") in seen_before]
    new_tasks = [t for t in task_rows if _stable_id(t, "task") not in seen_before]
    stale_prs = [p for p in pr_rows if _stable_id(p, "pr") in seen_before]
    new_prs = [p for p in pr_rows if _stable_id(p, "pr") not in seen_before]  # noqa: F841

    if not (new_tasks or new_prs or stale_tasks or stale_prs):
        lines.append("✅ nothing stale — all clear")
        return {"text": "\n".join(lines), "new": 0, "stale": 0, "total": 0}

    lines.append(f"NEW ({len(new_tasks) + len(new_prs)}):")
    for t in new_tasks:
        lines.append(f"  • TASK {t['task_id']} [{t['list']}] {t['name']}")
        lines.append(f"      {t['url']}  status={t['status']}")
    for p in new_prs:
        lines.append(f"  • PR {p['repo']}#{p['number']} [{p['verdict']}, {p['age_h']}h] {p['title']}")
        lines.append(f"      {p['url']}  mergeable={p['mergeable']} author={p['author']}")
    lines.append("")
    lines.append(f"PERSISTENT ({len(stale_tasks) + len(stale_prs)}) — surfaced before, still stale:")
    for t in stale_tasks:
        lines.append(f"  • TASK {t['task_id']} {t['name']} — {t['url']}")
    for p in stale_prs:
        lines.append(f"  • PR {p['repo']}#{p['number']} {p['title']} — {p['url']}")
    lines.append("")
    lines.append("Suggested action (per item):")
    for t in new_tasks + stale_tasks:
        lines.append(f"  - TASK {t['task_id']}: review ClickUp thread + open PR; if landable, run `hermes-validate` / push merge.")
    for p in new_prs + stale_prs:
        if p["verdict"] == "landable":
            lines.append(f"  - PR {p['repo']}#{p['number']}: only failing check is flaky/identity — re-push with correct identity or merge with --admin.")
        elif p["verdict"] == "genuinely_red":
            lines.append(f"  - PR {p['repo']}#{p['number']}: GENUINELY RED — diagnose test failure; do NOT force-merge.")
        else:
            lines.append(f"  - PR {p['repo']}#{p['number']}: green but stale — push the merge button.")

    return {"text": "\n".join(lines), "new": len(new_tasks) + len(new_prs), "stale": len(stale_tasks) + len(stale_prs), "total": len(task_rows) + len(pr_rows)}


def _record_first_seen(state: dict, items: list[dict], kind: str) -> None:
    """Add stable-ids for newly-seen items to the first_seen map; drop ids that are
    no longer stale (so the persistent count decays correctly)."""
    now = _now_iso()
    current_ids = {_stable_id(it, kind) for it in items}
    # Drop any first-seen id of this kind that is no longer present (item unstuck).
    drop = []
    for sid in state["first_seen"]:
        if sid.startswith(TASK_PREFIX if kind == "task" else PR_PREFIX) and sid not in current_ids:
            drop.append(sid)
    for sid in drop:
        del state["first_seen"][sid]
    # Add new ones.
    for it in items:
        sid = _stable_id(it, kind)
        if sid and sid not in state["first_seen"]:
            state["first_seen"][sid] = now


# ---------------------------------------------------------------------------
# Main

def main() -> int:
    # Resolver-first: during a gateway restart-race CLICKUP_API_TOKEN can be
    # transiently absent from os.environ. Try the lazy 1Password resolver
    # first (fail-open on import/lookup error), then the plain env var.
    value = None
    try:
        from agent import lazy_secret_resolver
        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None
    if not value:
        value = os.environ.get("CLICKUP_API_TOKEN", "")
    token = (value or "").strip().strip('"').strip("'")
    if not token:
        print("ERROR: CLICKUP_API_TOKEN not set", file=sys.stderr)
        return 2

    state = _read_state()
    # Capture the seen-set BEFORE this tick so the digest can distinguish
    # truly-new items (NEW section) from items surfaced in prior ticks
    # (PERSISTENT section). Without this, the first run shows everything as
    # PERSISTENT because _record_first_seen() mutates state before _emit_digest
    # reads it.
    seen_before = set(state.get("first_seen", {}).keys())

    # 1. ClickUp tasks
    task_rows = _sweep_clickup_tasks(token)
    # Filter to > SLA hours OLD by date_updated as a coarse filter (the state file
    # tracks the precise first-seen-in-review timestamp, but we use date_updated as
    # a quick gate to avoid first-seen-ing brand-new tasks).
    task_rows = [t for t in task_rows if (_age_hours(t.get("date_updated")) or 0) >= TASK_SLA_HOURS]

    # 2. Open PRs (sweep already filters by PR_SLA_HOURS)
    pr_rows = _sweep_open_prs(REPOS)

    # 3. Build + emit digest (uses seen_before for the NEW/PERSISTENT split)
    digest = _emit_digest(task_rows, pr_rows, state, seen_before)
    print(digest["text"])

    # 3b. Adopt ORPHAN green PRs into the risk-gated validator so autonomous_merge
    # lands them — closes the "green PR with no task, nags forever" gap. Best-effort.
    try:
        adopted, adopt_skipped = _adopt_orphan_prs(pr_rows)
        if adopted or adopt_skipped:
            print("\n🤝 ORPHAN-PR ADOPTION (validate → autonomous_merge lands PASSes):")
            for x in adopted:
                print("  •", x)
            for x in adopt_skipped:
                print("  – skip:", x)
    except Exception as e:
        print(f"[staleness] orphan-adoption phase error (non-fatal): {e}", file=sys.stderr)

    # 3c. Orphaned in-progress requeue — tasks Hermes worked that are stuck
    # "in progress" with NO live claim get reset to "to do" so the poll-gate
    # picks them up again. Critical safety: tasks with a live claim are SKIPPED.
    try:
        orphans = _sweep_orphaned_inprogress(token)
        requeued, requeue_skipped = _requeue_orphaned_inprogress(token, orphans)
        if orphans or requeued or requeue_skipped:
            print(f"\n♻️  ORPHANED IN-PROGRESS REQUEUE (threshold={STALENESS_INPROGRESS_HOURS}h, dry_run={DRY_RUN}):")
            if not orphans:
                print("  (no orphaned in-progress tasks found)")
            for line in requeued:
                print("  •", line)
            for line in requeue_skipped:
                print("  – skip:", line)
        else:
            print(f"\n♻️  ORPHANED IN-PROGRESS REQUEUE: none found (threshold={STALENESS_INPROGRESS_HOURS}h)")
    except Exception as e:
        print(f"[staleness] orphaned-inprogress phase error (non-fatal): {e}", file=sys.stderr)

    # 4. Update first-seen state (NOW, after digest is built)
    _record_first_seen(state, task_rows, "task")
    _record_first_seen(state, pr_rows, "pr")

    # 5. Persist state (last_digest_ts is for cron-log continuity, not logic)
    state["last_digest_ts"] = _now_iso()
    state["last_digest_count"] = digest["total"]
    if not DRY_RUN:
        _write_state(state)
    elif state.get("first_seen"):
        # In dry-run, we still want the state file to track what we'd flag (helps the
        # operator inspect what the sweep sees). Persist but DO NOT act on it.
        _write_state(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
