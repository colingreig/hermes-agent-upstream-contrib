#!/usr/bin/env python3
"""stalled_task_reconciler.py — the RECONCILER that un-strands ClickUp tasks stuck
in 'in progress' with no live claim behind them.

THE GAP THIS FILLS (confirmed 2026-07-03, see brain operations/learnings for the
full diagnosis; do not re-derive it here)
------------------------------------------------------------------------------
~17-18 tasks sit at status='in progress' with ZERO live claim
(`claim_store.py list` == []). They stalled at the back of the pipeline and
nothing pulls them back out, because:
  - The nightly AGENT-based `clickup-reconciler` skill/cron tries to flip
    PR-merged tasks to 'in review', but that flip goes through the SAME guarded
    `clickup.mjs status` path as everything else, so a standing FAIL/BLOCK
    `ignite-validate:` marker in the comment thread trips **G2**
    (clickup_status_guard.py / status_guard.mjs) and the LLM-driven reconciler
    has no mechanical retry — it just gives up silently for that task.
  - `hermes-pr-validate` (the validator) only ever evaluates tasks in status
    'in review' — a task stuck 'in progress' is invisible to it, so a stale,
    low-tier (pre-Claude-high-tier) BLOCK from ~2026-06-23 can never be
    re-judged even after the validator's high tier moved on.
  - Separately, the executor's mid-task "discovered a genuine dependency
    block" unclaim path (see clickup-queue-poller SKILL.md patch, 2026-07-03)
    correctly releases the claim but historically left status at 'in progress'
    instead of reverting to 'to do' (task 86e251a0z / CMMS-1 is the verified
    example) — a second, distinct leak into the same stuck population.

`closeout_actor.py` is the existing DETERMINISTIC sibling that solved the
adjacent "merged + validator PASS but status never advanced" gap. This script
is modeled directly on it (same helpers, same guardrails, same --dry-run
discipline) but solves the PRIOR stage: getting a stranded task BACK INTO the
pipeline at all, either by re-queuing it for validation (PR exists — let the
CURRENT validator tier re-judge it; we do NOT judge PR quality ourselves) or by
handing it back to the front of the queue cleanly (no PR at all — nothing to
validate).

THE TWO RECONCILE RULES
------------------------
For every task with status=='in progress' AND no LIVE claim
(`claim_store.py is-claimed` == free) AND not OEConnection-fenced:

  1. Has an OPEN or MERGED PR for this task (checked via the validator-verdict
     store's task_id index PLUS a live `gh pr list` branch-name scan on the
     resolved repo — see find_prs_for_task()) -> flip 'in progress' -> 'in
     review' and post an audit comment. This re-enters the task into the
     existing review-poll-gate -> hermes-pr-validate -> closeout_actor
     pipeline; the validator re-PASSes or re-FAILs it on the CURRENT tier.
     NOTE: this flip almost always needs to go over a standing FAIL/BLOCK
     marker (that marker is *why* the validator never got to re-see it) —
     CLICKUP_ALLOW_FAIL_OVERRIDE=1 is used deliberately for exactly this
     class, exactly as clickup_status_guard.py's own G2 message prescribes
     ("re-pick ... and let the validator re-run"). This is forcing
     RE-EVALUATION, not asserting the work is correct — the audit comment
     says so and never fabricates authorization (G3-safe).
  2. NO PR found at all (no verdict-store entry, no matching `agent/<taskId>`
     branch on the resolved repo) -> revert 'in progress' -> 'to do' (ensuring
     `agent-ready` stays attached) and post an audit comment. Covers both a
     claim that died before any artifact existed AND a correct-unclaim whose
     status revert never happened.
  3. A PR was found but every match is CLOSED and unmerged -> SKIP (mirrors
     the existing `clickup-reconciler` skill's own policy: Colin may have
     deliberately killed that approach; do not auto-requeue, flag it instead).

SAFETY
------
- Default-deny, fail-closed, idempotent. Every skip is logged with a reason.
- `--dry-run` prints planned actions; touches nothing.
- ClickUp project blocklist entries (shared via clickup_sync.py) are a hard skip —
  mirrors clickup_poll_gate.py / clickup_sync.py default-allow blocklist semantics.
- LIVE claims are NEVER touched (claim_store.py is the single source of truth).
- Anti-flap: a task this script already acted on within ANTI_FLAP_HOURS is
  skipped on subsequent runs (state file), so it can never flip-flop.
- A same-task OpenCode log younger than FRESH_LOG_MINUTES is treated as
  "possibly still in flight" and skipped this tick (mirrors the existing
  clickup-reconciler skill's own "<15 min old -> LEAVE IT" guard).
- sweep() never raises.

CLI:
  stalled_task_reconciler.py --dry-run     # report, touch nothing (run this first)
  stalled_task_reconciler.py               # live: reconcile every qualifying task
  stalled_task_reconciler.py --task T      # restrict to one ClickUp task id
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
import clickup_sync                  # noqa: E402
import closeout_actor as ca          # noqa: E402  (reuse its gh/clickup.mjs primitives)
import validator_verdict             # noqa: E402
import clickup_status_guard as guard  # noqa: E402
import resolve_repo                  # noqa: E402
import task_action_lock as task_lock  # noqa: E402

QUEUE_SNAPSHOT_PATH = os.path.expanduser("~/.hermes/scripts/queue_snapshot.json")
STATE_PATH = os.path.expanduser("~/.hermes/scripts/.stalled_reconciler_state.json")
OPENCODE_LOGS_DIR = os.path.expanduser("~/.hermes/logs/opencode")
CLAIM_STORE = os.path.expanduser("~/.hermes/scripts/claim_store.py")
PY = ca.PY

# Anti-flap: never re-act on a task this script already touched within this window.
ANTI_FLAP_HOURS = 6
# A same-task OpenCode log fresher than this may mean a run is still in flight
# even though no claim file is present (claim released right before this tick).
FRESH_LOG_MINUTES = 15

TARGET_STATUS = "in progress"
REVIEW_STATUS = "in review"
TODO_STATUS = "to do"

# ---- ClickUp project blocklist shared with clickup_sync.py.
# Default-allow model: anything not in ~/.claude/skills/ignite-state/references/blocklist.json is allowed.

AGENT_BRANCH_RE_TMPL = r"^agent/{}(?:[-_].*)?$"


def _now() -> float:
    return time.time()


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _is_oec_fenced(task_json):
    blocked, reason = clickup_sync.is_clickup_project_blocked(task_json)
    if blocked:
        return True, reason or "clickup project blocklist"
    return False, ""


def _is_claim_live(task_id):
    """True iff claim_store reports a LIVE claim. Fails closed to 'treat as
    live' on any error so we never act over a claim we couldn't verify."""
    try:
        r = subprocess.run([PY, CLAIM_STORE, "is-claimed", task_id],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            return True   # "claimed"
        if r.returncode == 1:
            return False  # "free"
        return True       # unexpected -> fail closed (never act over ambiguity)
    except Exception:
        return True  # fail closed


def _recent_opencode_log(task_id):
    """True iff a {task_id}-*.jsonl log exists with mtime within
    FRESH_LOG_MINUTES — a possible in-flight run even with no claim file."""
    try:
        cutoff = _now() - FRESH_LOG_MINUTES * 60
        for name in os.listdir(OPENCODE_LOGS_DIR):
            if name.startswith(f"{task_id}-") and name.endswith(".jsonl"):
                path = os.path.join(OPENCODE_LOGS_DIR, name)
                if os.path.getmtime(path) >= cutoff:
                    return True
        return False
    except Exception:
        return False  # dir missing/unreadable -> nothing to guard against


def find_prs_for_task(task_id, repo_hint, verdict_store):
    """Returns (state, matches) where state in
    {'MERGED','OPEN','CLOSED_ONLY','NONE'} and matches is a list of
    {'repo','pr','state','mergedAt','via'} dicts (best-effort, never raises).

    Combines two signals:
      (a) the validator-verdict store's task_id index (no network — the SAME
          store closeout_actor.py sweeps, just indexed the other direction:
          PR -> task there, task -> PR here);
      (b) a live `gh pr list --repo <resolved> --state all` branch-name scan
          (agent/<taskId> with an optional suffix) on the repo resolved from
          the task's ClickUp list name, catching PRs that never got a verdict
          at all (never reached 'in review', so hermes-pr-validate never ran).
    """
    matches = []
    seen = set()

    # (a) verdict store — no network.
    for key, v in (verdict_store or {}).items():
        if v.get("task_id") != task_id or "#" not in key:
            continue
        repo, prs = key.rsplit("#", 1)
        try:
            pr = int(prs)
        except ValueError:
            continue
        info, err = ca._gh_json(repo, pr, "state,mergedAt")
        state = (info or {}).get("state", "").upper() if not err else "UNKNOWN"
        seen.add((repo, pr))
        matches.append({"repo": repo, "pr": pr, "state": state,
                        "mergedAt": (info or {}).get("mergedAt"), "via": "verdict-store"})

    # (b) live branch-name scan on the resolved repo (covers never-validated PRs).
    if repo_hint:
        try:
            r = subprocess.run(
                ["gh", "pr", "list", "--repo", repo_hint, "--state", "all",
                 "--json", "number,headRefName,state,mergedAt", "--limit", "100"],
                capture_output=True, text=True, timeout=ca.GH_TIMEOUT, env=ca._shim_env())
            if r.returncode == 0:
                pat = re.compile(AGENT_BRANCH_RE_TMPL.format(re.escape(task_id)))
                for pr_data in json.loads(r.stdout or "[]"):
                    head = pr_data.get("headRefName") or ""
                    if not pat.match(head):
                        continue
                    num = pr_data.get("number")
                    if num is None or (repo_hint, num) in seen:
                        continue
                    seen.add((repo_hint, num))
                    matches.append({"repo": repo_hint, "pr": num,
                                    "state": (pr_data.get("state") or "").upper(),
                                    "mergedAt": pr_data.get("mergedAt"), "via": "branch-scan"})
        except Exception:
            pass  # best-effort; verdict-store signal still stands

    if any(m["state"] == "MERGED" or m.get("mergedAt") for m in matches):
        return "MERGED", matches
    if any(m["state"] == "OPEN" for m in matches):
        return "OPEN", matches
    if matches:
        return "CLOSED_ONLY", matches
    return "NONE", matches


def _resolve_repo_for_task(list_name):
    if not list_name:
        return None
    try:
        code, res = resolve_repo.resolve(list_name)
        if code == 0:
            return res.get("resolved")
    except Exception:
        pass
    return None


def _flip_to_review(task_id, repo, pr, state):
    """Advance over a standing FAIL/BLOCK marker on purpose — CLICKUP_ALLOW_FAIL_OVERRIDE
    is the sanctioned door for exactly this ('re-pick ... let the validator re-run'),
    used here to force RE-EVALUATION, never to assert the work already passed."""
    env = ca._shim_env()
    env["CLICKUP_ALLOW_FAIL_OVERRIDE"] = "1"
    try:
        r = subprocess.run([ca.CLICKUP_MJS, "status", task_id, REVIEW_STATUS],
                           capture_output=True, text=True, timeout=ca.NODE_TIMEOUT, env=env)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        ok = r.returncode == 0
    except Exception as e:
        ok, out = False, f"clickup.mjs error: {e!r}"
    if not ok:
        return False, f"status flip refused/failed: {out[:400]}"
    pr_url = f"https://github.com/{repo}/pull/{pr}" if repo and pr else "(no PR url)"
    body = ("\U0001F501 ignite-reconciler: re-queuing for validation — stranded "
            "'in progress' with a non-live claim and an existing PR "
            f"({repo}#{pr}, state={state}). {pr_url}\n"
            "Re-entering the review pipeline so the validator re-evaluates on the "
            "current tier; this does NOT assert the work is correct — a standing "
            "FAIL/BLOCK marker is being deliberately overridden so it gets re-seen, "
            "not bypassed. Validator will re-PASS or re-FAIL as warranted.")
    cok, cout = ca._cu_node(["comment", task_id, body])
    return True, ("flipped to 'in review'" + ("" if cok else f"; comment failed: {cout}"))


def _revert_to_todo(task_id):
    ok, out = ca._cu_node(["status", task_id, TODO_STATUS])
    if not ok:
        return False, f"status revert refused/failed: {out[:400]}"
    body = ("\U0001F501 ignite-reconciler: reverting 'in progress' → 'to do' — "
            "no open/merged PR found for this task and its claim is not live "
            "(a claim that died before any artifact existed, or a correct unclaim "
            "whose status revert never happened). Re-queuing for a clean re-claim.")
    cok, cout = ca._cu_node(["comment", task_id, body])
    # Best-effort: keep agent-ready attached so the poll gate can re-claim it.
    ca._cu_node(["tag", task_id, "agent-ready"])
    return True, ("reverted to 'to do'" + ("" if cok else f"; comment failed: {cout}"))


def evaluate(task_id, verdict_store, state_file):
    """Pure-ish decision (one gh/clickup network round-trip for the task detail
    + comments; more for the PR search). Returns a result dict. Never raises."""
    tdata, terr = ca._task_json(task_id)
    if terr or not tdata:
        return {"task_id": task_id, "action": "skip", "reason": f"task fetch failed: {terr}"}

    cur = ((tdata.get("status") or {}).get("status") or "").strip().lower()
    if cur != TARGET_STATUS:
        return {"task_id": task_id, "action": "skip",
                "reason": f"status is '{cur}', not '{TARGET_STATUS}' (changed since scan)"}

    fenced, freason = _is_oec_fenced(tdata)
    if fenced:
        return {"task_id": task_id, "action": "skip", "reason": f"OEC-fenced: {freason}"}

    if _is_claim_live(task_id):
        return {"task_id": task_id, "action": "skip", "reason": "live claim held — never touch"}

    if _recent_opencode_log(task_id):
        return {"task_id": task_id, "action": "skip",
                "reason": f"OpenCode log <{FRESH_LOG_MINUTES}min old — may still be in flight"}

    last = state_file.get(task_id)
    if last and (_now() - last.get("ts", 0)) < ANTI_FLAP_HOURS * 3600:
        return {"task_id": task_id, "action": "skip",
                "reason": f"anti-flap: already acted {(_now()-last['ts'])/3600:.1f}h ago "
                          f"({last.get('action')})"}

    list_name = (tdata.get("list") or {}).get("name") or ""
    repo = _resolve_repo_for_task(list_name)
    pr_state, matches = find_prs_for_task(task_id, repo, verdict_store)

    if pr_state in ("MERGED", "OPEN"):
        best = next((m for m in matches if m["state"] == pr_state), matches[0])
        return {"task_id": task_id, "action": "flip_to_review", "reason":
                 f"{pr_state} PR found ({best['repo']}#{best['pr']}) — re-queue for validation",
                 "repo": best["repo"], "pr": best["pr"], "pr_state": pr_state}
    if pr_state == "CLOSED_ONLY":
        return {"task_id": task_id, "action": "skip",
                "reason": f"only CLOSED (unmerged) PR(s) found ({matches}); Colin may have "
                          "killed the approach — not auto-requeuing (mirrors clickup-reconciler policy)"}
    return {"task_id": task_id, "action": "revert_to_todo",
            "reason": f"no PR found for repo={repo or '?'} — stranded claim or correct-unclaim leak"}


def sweep(dry_run=False, only_task=None):
    results = []
    try:
        snapshot = _load_json(QUEUE_SNAPSHOT_PATH, {"tasks": []})
        candidates = [t.get("id") for t in snapshot.get("tasks", [])
                      if (t.get("status") or "").strip().lower() == TARGET_STATUS]
        if only_task:
            candidates = [t for t in candidates if t == only_task] or [only_task]
        verdict_store = validator_verdict.load_verdicts()
        state_file = _load_json(STATE_PATH, {})

        for task_id in candidates:
            try:
                with task_lock.task_lock(task_id) as locked:
                    if not locked:
                        res = {"task_id": task_id, "action": "skip",
                               "reason": "task lock busy — another reconciler/closeout is handling this task"}
                    else:
                        res = evaluate(task_id, verdict_store, state_file)
            except Exception as e:
                res = {"task_id": task_id, "action": "error", "reason": f"{e!r}"}

            if res["action"] in ("flip_to_review", "revert_to_todo") and not dry_run:
                if res["action"] == "flip_to_review":
                    ok, msg = _flip_to_review(task_id, res.get("repo"), res.get("pr"),
                                              res.get("pr_state"))
                else:
                    ok, msg = _revert_to_todo(task_id)
                res["did_ok"] = ok
                res["did_msg"] = msg
                if ok:
                    state_file[task_id] = {"ts": _now(), "action": res["action"]}
            results.append(res)

        if not dry_run:
            _save_state(state_file)
    except Exception as e:
        results.append({"task_id": "*", "action": "error", "reason": f"sweep fatal: {e!r}"})
    return results


def main():
    p = argparse.ArgumentParser(description="Stalled-task reconciler: un-strand "
                                             "'in progress' tasks with no live claim.")
    p.add_argument("--dry-run", action="store_true", help="report only; touch nothing")
    p.add_argument("--task", help="restrict to one ClickUp task id")
    a = p.parse_args()
    res = sweep(dry_run=a.dry_run, only_task=a.task)

    tally = {"flip_to_review": 0, "revert_to_todo": 0, "skip": 0, "error": 0}
    for r in res:
        act = r["action"]
        if act == "flip_to_review":
            tag = "WOULD-REVIEW" if a.dry_run else ("REVIEWED" if r.get("did_ok") else "REVIEW-FAILED")
        elif act == "revert_to_todo":
            tag = "WOULD-TODO" if a.dry_run else ("REVERTED" if r.get("did_ok") else "REVERT-FAILED")
        elif act == "error":
            tag = "ERROR"
        else:
            tag = "SKIP"
        tally[act] = tally.get(act, 0) + 1
        print(f"[reconciler] {tag:14} {r['task_id']} — {r.get('reason','')}")
        if r.get("did_msg") and not r.get("did_ok"):
            print(f"              {r['did_msg']}")

    print(json.dumps({
        "flip_to_review": tally.get("flip_to_review", 0),
        "revert_to_todo": tally.get("revert_to_todo", 0),
        "skip": tally.get("skip", 0),
        "error": tally.get("error", 0),
        "total": len(res),
        "dry_run": a.dry_run,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
