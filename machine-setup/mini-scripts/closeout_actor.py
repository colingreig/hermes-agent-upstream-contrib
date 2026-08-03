#!/usr/bin/env python3
"""closeout_actor.py — the CLOSEOUT ACTOR that advances merged+validated tasks.

THE GAP THIS FILLS
------------------
autonomous_merge.py MERGES a PR once the validator records a fresh PASS. But after
the merge nothing advances the *task*: the `_clickup_followup()` in autonomous_merge
deliberately leaves status untouched (it only clears `needs-validation` + adds a
`merged` tag), per the 2026-06-22 policy. So merged+validated work piles up in
`in progress` forever (the "wall ~76 and growing"). This actor is the missing
advancer: a deterministic, zero-LLM sweep that flips a task from in-progress/in-review
to Hermes's TERMINAL status once BOTH (a) its linked PR is MERGED and (b) the
validator recorded a PASS verdict.

WHAT "DONE" MEANS HERE (G1 policy — NOT bypassed)
-------------------------------------------------
On the AI Dev Assistant list the statuses are: deferred / to do / in progress /
in review (type=done) / complete (type=closed). The completion guardrails (G1,
SKILL.md, clickup_status_guard.py) HARD-BLOCK Hermes from ever setting `complete`:
that transition is owned exclusively by ignite-validate / Colin. Hermes's terminal
status IS `in review` (which is literally type=done on this list). So this actor
advances merged+validated tasks to `in review` THROUGH the guarded clickup.mjs path
(`status`), which re-enforces G1/G2/G3. It NEVER passes CLICKUP_ALLOW_COMPLETE and
NEVER sets `complete` — flipping to `complete` would be bypassing status_guard.
(If a list has no `in review`, we resolve its review-class status via clickup.mjs
`review`, same auto-resolution the executor uses.)

THE SIGNALS (pinned 2026-06-26, with evidence — see brain note)
---------------------------------------------------------------
- Linked PR:  the validator verdict store `.validator_verdicts.json` is keyed by
              `owner/repo#pr` and each record carries `task_id`. That IS the
              authoritative task<->PR link (same source autonomous_merge consumes).
              We iterate the store; no comment-scraping needed.
- Merge state: `gh pr view <pr> --repo <r> --json state,mergedAt` == MERGED
              (re-uses autonomous_merge's _gh_json shim-env approach).
- Validation PASS: the verdict record's `verdict == "PASS"`. NOTE: the executor
              writes prose "Validator status: PASS" comments, NOT the machine
              `ignite-validate: PASS` marker, so comment-marker scraping is NOT a
              reliable PASS source — the STORE is. (For a *merged* PR the PASS is
              permanent fact, so we do NOT apply the 24h staleness window that
              gates *merges* — a stale-but-merged PASS still warrants closeout.)
- G2 safety:  we additionally refuse if the task's NEWEST `ignite-validate:` comment
              marker is FAIL/BLOCK (defense-in-depth; the guarded clickup.mjs path
              re-checks this too).

SAFETY
------
- Default-deny, fail-closed, idempotent. A task already in a terminal/review status
  is a silent no-op. Every skip is logged with its reason (no silent caps).
- `--dry-run` lists what WOULD flip and why; touches nothing. Ambiguity => skip.
- sweep() never raises.

ALSO DRIVEN FROM HERE
---------------------
`main()` additionally drives two sibling backstops on this same cron cadence:
`db_closeout_actor` (DB-backed publish tasks, no PR) and
`manual_platform_handoff` (OPEN CI-green PRs on repos ignite-ship classifies
PLATFORM=manual, where the executor can never deploy and a CI-green PR IS the
complete deliverable). All three sweeps are disjoint on their PR-state predicate.

CLI:
  closeout_actor.py --dry-run         # report, touch nothing (recommended first)
  closeout_actor.py                    # live: advance every qualifying task
  closeout_actor.py --repo R --pr N    # restrict to one PR (still fully gated)
  closeout_actor.py --task T           # restrict to one task id (still fully gated)
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validator_verdict
import clickup_status_guard as guard
import task_action_lock as task_lock
try:
    import report_activity_journal as activity_journal
except ImportError:  # governed bundle may not have landed yet; closeout stays fail-open
    activity_journal = None

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_BIN_DIR = os.path.expanduser("~/.hermes/bin")
PY = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11")
CLICKUP_MJS = os.path.expanduser("~/dev/ignite-skills-live/skills/clickup/clickup.mjs")
ALLOWLIST_PATH = os.path.expanduser("~/.hermes/allowed-repos.txt")
# Same offline rename-equivalence file validator_repo_guard.py uses (its RC2
# header). Expands a loaded allowlist to every alias spelling of a repo that
# has been renamed, so a caller supplying either name still matches.
REPO_ALIASES_PATH = os.path.expanduser("~/.hermes/config/repo-aliases.json")
GH_TIMEOUT = 60
NODE_TIMEOUT = 90
POST_CLICKUP_COMMENT = os.path.expanduser("~/.hermes/scripts/post_clickup_comment.py")

# Hermes's terminal status (G1: `complete` is owned by ignite-validate/Colin and
# is hard-blocked). `in review` is type=done on the executor lists.
TERMINAL_STATUS = "in review"
# Statuses we will advance FROM. A task already in review/complete/closed is a no-op.
ADVANCEABLE = {"in progress", "in-progress", "to do", "to-do"}


def _shim_env():
    env = dict(os.environ)
    env["PATH"] = HERMES_BIN_DIR + os.pathsep + env.get("PATH", "")
    return env


def _post_clickup_comment(task_id, body):
    """Post a guarded ClickUp comment body through the truncation checker."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="closeout_actor_", delete=False, encoding="utf-8")
    try:
        tmp.write(body)
        tmp.close()
        r = subprocess.run(
            [PY, POST_CLICKUP_COMMENT, task_id, tmp.name],
            capture_output=True, text=True, timeout=NODE_TIMEOUT, env=_shim_env(),
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out
    except Exception as e:
        return False, f"guarded comment error: {e!r}"
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _expand_repo_aliases(names, path=REPO_ALIASES_PATH):
    """See hermes_validate_ops._expand_repo_aliases — same tolerant behavior,
    duplicated here because this module does not import that one."""
    try:
        with open(path, encoding="utf-8") as f:
            groups = json.load(f).get("aliases") or []
    except Exception:
        return names
    expanded = set(names)
    for group in groups:
        if isinstance(group, list) and expanded.intersection(group):
            expanded.update(group)
    return expanded


def _load_allowlist(path=ALLOWLIST_PATH):
    result = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    result.add(s)
    except FileNotFoundError:
        pass
    return _expand_repo_aliases(result)


def _gh_json(repo, pr, fields):
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo, "--json", fields],
            capture_output=True, text=True, timeout=GH_TIMEOUT, env=_shim_env())
        if r.returncode != 0:
            return None, f"gh pr view rc={r.returncode}: {r.stderr.strip()[:160]}"
        return json.loads(r.stdout or "{}"), None
    except Exception as e:
        return None, f"gh pr view error: {e!r}"


def _cu_node(args, soft=False):
    """Run clickup.mjs <args>. Returns (ok, out). Never raises."""
    try:
        r = subprocess.run([CLICKUP_MJS, *args], capture_output=True, text=True,
                           timeout=NODE_TIMEOUT, env=_shim_env())
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode == 0, out[:600]
    except Exception as e:
        return False, f"clickup.mjs error: {e!r}"


def _task_json(task_id):
    """Fetch the task (status + comments) via clickup.mjs --json. Returns
    (data, err). data has at least {'status': {'status':..}}."""
    try:
        r = subprocess.run([CLICKUP_MJS, "task", task_id, "--json"],
                           capture_output=True, text=True, timeout=NODE_TIMEOUT,
                           env=_shim_env())
        if r.returncode != 0:
            return None, f"task fetch rc={r.returncode}: {(r.stderr or '').strip()[:160]}"
        out = (r.stdout or "").strip()
        if not out:
            return None, "empty task json (harness quirk?)"
        return json.loads(out), None
    except Exception as e:
        return None, f"task fetch error: {e!r}"


def _comments_json(task_id):
    """Fetch task comments via clickup.mjs comments --json. Returns list (may be
    empty); errors yield []."""
    try:
        r = subprocess.run([CLICKUP_MJS, "comments", task_id, "--json"],
                           capture_output=True, text=True, timeout=NODE_TIMEOUT,
                           env=_shim_env())
        if r.returncode != 0:
            return []
        out = (r.stdout or "").strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            return data.get("comments") or []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _task_json_or_raise(task_id):
    data, error = _task_json(task_id)
    if data is None:
        raise RuntimeError(error or "ClickUp task read failed")
    return data


def evaluate(repo, pr, verdict, allowlist):
    """Pure-ish decision. Returns (action, detail, task_id).
    action in {'flip','skip','blocked'}. Network is read-only here."""
    task_id = verdict.get("task_id") or ""

    # (b) validation PASS — from the authoritative store (NOT comment scraping).
    if verdict.get("verdict") != "PASS":
        return "skip", f"verdict is {verdict.get('verdict')!r}, not PASS", task_id
    if repo not in allowlist:
        return "skip", f"repo not on allowlist ({ALLOWLIST_PATH})", task_id
    if not task_id:
        return "skip", "verdict has no task_id — cannot map PR to a ClickUp task", task_id

    # (a) PR must be MERGED.
    info, err = _gh_json(repo, pr, "state,mergedAt")
    if err:
        return "skip", err, task_id
    state = (info.get("state") or "").upper()
    if state != "MERGED":
        return "skip", f"PR state is {state or 'UNKNOWN'} (not MERGED)", task_id

    # Idempotency + current status.
    tdata, terr = _task_json(task_id)
    if terr:
        return "skip", f"could not read task status: {terr}", task_id
    cur = ((tdata.get("status") or {}).get("status") or "").strip().lower()
    if not cur:
        return "skip", "task status unreadable", task_id
    if guard.is_review_class(cur) or guard.is_complete_class(cur):
        # already in review/complete/closed -> nothing to do (idempotent no-op)
        return "skip", f"already terminal/review (status='{cur}')", task_id
    if cur not in ADVANCEABLE:
        return "skip", f"status '{cur}' is not an advanceable in-flight status", task_id

    # G2 defense-in-depth: refuse if the newest ignite-validate marker is FAIL/BLOCK.
    comments = _comments_json(task_id)
    v = guard.latest_validate_verdict(comments)
    if v and guard.NEGATIVE_VERDICTS.match(v["verdict"]):
        return "blocked", (f"newest ignite-validate marker is {v['verdict'].upper()} "
                           f"(comment {v['comment_id']}); refusing closeout"), task_id

    return "flip", (f"PR MERGED ({info.get('mergedAt','?')}), verdict=PASS "
                    f"tier={verdict.get('tier')}, status '{cur}' -> '{TERMINAL_STATUS}'"), task_id


def _do_flip(task_id, repo, pr):
    """Advance the task to TERMINAL_STATUS via the GUARDED clickup.mjs path, then
    post a closeout comment citing the merged PR. Returns (ok, msg).

    The guarded path re-enforces G1 (no complete), G2 (no advance over a standing
    FAIL marker), G3 (no fabricated auth in the comment). We do NOT set any
    CLICKUP_ALLOW_* override. The closeout comment is worded to avoid the G3
    banned-auth phrase class (no 'per Colin' / 'approved by Colin')."""
    ok, out = _cu_node(["status", task_id, TERMINAL_STATUS])
    if not ok:
        return False, f"status flip refused/failed: {out}"
    if activity_journal is None:
        journal_result = {"status": "UNKNOWN", "confirmed": False,
                          "error": "report_activity_journal unavailable"}
    else:
        journal_result = activity_journal.confirm_transition(
            kind="review_handoff",
            task_id=task_id,
            source="closeout-actor",
            expected_status=TERMINAL_STATUS,
            run_id=os.environ.get("HERMES_EXECUTOR_RUN_ID") or None,
            execution_id=os.environ.get("HERMES_EXECUTION_ID") or None,
            fetch_task=_task_json_or_raise,
        )
    if not journal_result.get("confirmed"):
        return False, (
            "status writer returned success but ClickUp read-after-write could not "
            f"verify '{TERMINAL_STATUS}': {journal_result.get('error', 'unknown')}"
        )
    pr_url = f"https://github.com/{repo}/pull/{pr}"
    body = (
        f"Outcome: {repo}#{pr} is merged and the task moved to '{TERMINAL_STATUS}'.\n"
        "Why: That keeps the validated work visible for handoff and review.\n"
        f"Evidence: validator PASS recorded; merged PR: {pr_url}\n"
        "Next: watch for any fresh BLOCKs on follow-up review."
    )
    cok, cout = _post_clickup_comment(task_id, body)
    journal_note = ""
    if journal_result.get("status") != "ok":
        journal_note = f"; report activity health UNKNOWN: {journal_result.get('error', 'append failed')}"
    return True, (f"flipped to '{TERMINAL_STATUS}'"
                  + ("" if cok else f"; comment failed: {cout}") + journal_note)


def sweep(dry_run=False, only_repo=None, only_pr=None, only_task=None):
    """Sweep the verdict store; advance every merged+validated task. Returns a
    list of result dicts. Never raises."""
    results = []
    try:
        store = validator_verdict.load_verdicts()
        allowlist = _load_allowlist()
        for key, verdict in store.items():
            try:
                if "#" not in key:
                    continue
                repo, prs = key.rsplit("#", 1)
                pr = int(prs)
                if only_repo and repo != only_repo:
                    continue
                if only_pr and pr != only_pr:
                    continue
                if only_task and verdict.get("task_id") != only_task:
                    continue
                if verdict.get("verdict") != "PASS":
                    continue  # cheap pre-filter
                task_id = verdict.get("task_id") or ""
                with task_lock.task_lock(task_id) as locked:
                    if not locked:
                        results.append({"repo": repo, "pr": pr, "task_id": task_id,
                                        "action": "skip",
                                        "detail": "task lock busy — another reconciler/closeout is handling this task"})
                        continue
                    action, detail, task_id = evaluate(repo, pr, verdict, allowlist)
                    rec = {"repo": repo, "pr": pr, "task_id": task_id,
                           "action": action, "detail": detail}
                    if action == "flip" and not dry_run:
                        ok, msg = _do_flip(task_id, repo, pr)
                        rec["flip_ok"] = ok
                        rec["flip_out"] = msg
                    results.append(rec)
            except Exception as e:
                results.append({"repo": key, "pr": None, "action": "error",
                                "detail": f"{e!r}"})
    except Exception as e:
        results.append({"repo": "*", "pr": None, "action": "error",
                        "detail": f"sweep fatal: {e!r}"})
    return results


def main():
    p = argparse.ArgumentParser(description="Closeout actor: advance merged+validated tasks.")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would flip; touch nothing")
    p.add_argument("--repo", help="restrict to one owner/repo")
    p.add_argument("--pr", type=int, help="restrict to one PR number")
    p.add_argument("--task", help="restrict to one ClickUp task id")
    a = p.parse_args()
    res = sweep(dry_run=a.dry_run, only_repo=a.repo, only_pr=a.pr, only_task=a.task)
    flipped = [r for r in res if r.get("action") == "flip" and r.get("flip_ok")]
    would = [r for r in res if r.get("action") == "flip" and a.dry_run]
    # Print every non-trivial line; skips of clearly-irrelevant verdicts are noisy,
    # so only print skips that reached a task (had a task_id) OR were blocked/error.
    for r in res:
        act = r["action"]
        if act == "flip":
            tag = "WOULD-FLIP" if a.dry_run else ("FLIPPED" if r.get("flip_ok") else "FLIP-FAILED")
        elif act == "blocked":
            tag = "BLOCKED"
        elif act == "error":
            tag = "ERROR"
        else:
            tag = "SKIP"
        # suppress the high-volume "not PASS / not merged / repo not allowed" skips
        # for verdicts that never had a task in flight, to keep cron output quiet;
        # always show flips, blocks, errors, and skips that touched a task.
        if act == "skip" and not (r.get("task_id") and
                                  ("already terminal" in r["detail"]
                                   or "advanceable" in r["detail"]
                                   or "not MERGED" in r["detail"]
                                   or "could not read" in r["detail"])):
            continue
        print(f"[closeout] {tag:11} {r['repo']}#{r['pr']} "
              f"task={r.get('task_id') or '-'} — {r['detail']}")
        if r.get("flip_out") and not r.get("flip_ok"):
            print(f"            {r['flip_out']}")
    print(json.dumps({"flipped": len(flipped), "would_flip": len(would),
                      "total": len(res)}))

    # DB-publish backstop (2026-06-26): the PR sweep above is blind to DB-backed
    # publish tasks (no PR). Drive the DB-publish closeout actor on the SAME cadence
    # so a hung executor can't strand a successfully-published blog in 'in progress'.
    # Lazy import to avoid an import cycle (db_closeout_actor imports this module).
    try:
        import db_closeout_actor as dbca
        dres = dbca.sweep(dry_run=a.dry_run, only_task=a.task)
        for r in dres:
            if r.get("action") == "flip" and r.get("flip_ok") and not a.dry_run:
                if activity_journal is None:
                    confirmation = {"confirmed": False,
                                    "error": "report_activity_journal unavailable"}
                else:
                    confirmation = activity_journal.confirm_transition(
                        kind="review_handoff",
                        task_id=r.get("task_id") or "",
                        source="db-publish-closeout",
                        expected_status=TERMINAL_STATUS,
                        run_id=os.environ.get("HERMES_EXECUTOR_RUN_ID") or None,
                        execution_id=os.environ.get("HERMES_EXECUTION_ID") or None,
                        fetch_task=_task_json_or_raise,
                    )
                r["activity_journal"] = confirmation
                if not confirmation.get("confirmed"):
                    r["flip_ok"] = False
                    r["flip_out"] = (
                        "status writer returned success but ClickUp read-after-write "
                        f"confirmation failed: {confirmation.get('error', 'unknown')}"
                    )
            act = r["action"]
            tag = ({"flip": ("WOULD-FLIP" if a.dry_run else ("FLIPPED" if r.get("flip_ok") else "FLIP-FAILED")),
                    "blocked": "BLOCKED", "error": "ERROR"}).get(act, "SKIP")
            # keep cron output quiet: only surface flips/blocks/errors and touched skips
            if act == "skip" and "already terminal" not in r["detail"] and "in flight" not in r["detail"]:
                continue
            print(f"[db-closeout] {tag:11} task={r.get('task_id') or '-'} "
                  f"slug={r.get('slug') or '-'} — {r['detail']}")
        dflip = sum(1 for r in dres if r.get("action") == "flip" and r.get("flip_ok"))
        dwould = sum(1 for r in dres if r.get("action") == "flip" and a.dry_run)
        print(json.dumps({"db_flipped": dflip, "db_would_flip": dwould, "db_total": len(dres)}))
    except Exception as e:
        print(f"[db-closeout] ERROR sweep failed to run: {e!r}")

    # Manual-platform handoff backstop (2026-08-03): this actor only advances
    # MERGED+validated work. On a repo ignite-ship classifies PLATFORM=manual
    # there is no pipeline the executor can drive, so a finished task can sit at
    # an OPEN CI-green PR forever ("ignite- BLOCKED HANDOFF", tasks 86e2kmud4 /
    # 86e2kxh59). The policy is that a CI-green PR IS the complete deliverable on
    # those repos, so drive that sweep on the SAME cadence. The two sweeps are
    # disjoint by construction: this one requires MERGED, that one requires OPEN.
    try:
        import manual_platform_handoff as mph
        mres = mph.sweep(dry_run=a.dry_run, only_repo=a.repo, only_task=a.task)
        mph.print_results(mres, dry_run=a.dry_run)
        mhanded = sum(1 for r in mres
                      if r.get("action") == "handoff" and r.get("handoff_ok"))
        mwould = sum(1 for r in mres if r.get("action") == "handoff" and a.dry_run)
        print(json.dumps({"manual_handoff": mhanded,
                          "manual_would_hand_off": mwould,
                          "manual_total": len(mres)}))
    except Exception as e:
        print(f"[manual-handoff] ERROR sweep failed to run: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
