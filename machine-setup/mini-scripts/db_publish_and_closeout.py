#!/usr/bin/env python3
"""
db_publish_and_closeout.py — the SINGLE idempotent DB-backed publish→validate→closeout path.

WHY THIS EXISTS (2026-06-27 rework)
-----------------------------------
The DB-backed publish lane (dynamics365group.com blog rewrites: a Neon `posts` row
write IS the publish — NO PR, the deliverable is a LIVE URL) had its "done" state
split across SIX PR-shaped seams, each of which bred a distinct strand that left a
successfully-published, live, passing blog wedged in `in progress`:
  1. db_publish_task.py writes the row but does NOT flip status (executor must).
  2. db_closeout_actor.py flips it — but its G2 gate refused over a STALE pre-publish
     `ignite-validate: FAIL` marker that nothing on the no-PR lane ever superseded
     (the validator cron is PR-only). Verified 86e1z6acu: FAIL 2026-06-22 vs publish
     2026-06-27, live page fine, stuck for hours.
  3. review_poll_gate woke hermes-pr-validate on the `needs-validation` tag, which is
     PR-only — for a DB task it finds no PR (or a leftover wrong one), validates the
     wrong thing, and emits a fresh FAIL/BLOCK that re-wedges the task.
  4. clickup_review_sla._resume_validation_blocked is PR-keyed too.

THIS SCRIPT collapses publish + live-URL re-validate + atomic status flip into ONE
re-runnable pass, and the companion patches NEUTRALIZE the PR-shaped seams for DB
tasks. It REUSES (does not reimplement) the proven helpers:
  * db_publish_task.main()      — idempotent row publish (db_apply is slug-scoped,
                                  column-whitelisted, rowcount-guarded, verify-after,
                                  reversible; safe to re-run).
  * db_closeout_actor.evaluate/_do_flip — gated, live-URL-verified, guarded status
                                  flip (G1/G2/G3 preserved via clickup.mjs).

FLOW (idempotent — safe to run repeatedly):
  1. PUBLISH/ENSURE the row via db_publish_task (derives slug from the task body's
     /blog/<slug> URL — NEVER paraphrased; validates the slug against `posts` first).
     A re-run regenerates+re-applies content; db_apply converges the row. Honors
     --dry-run (rollback, writes nothing). On --skip-publish we ONLY closeout (use
     when the row is already published and you just need to converge status).
  2. CLOSEOUT via db_closeout_actor: re-read the publish marker, DB-verify the row,
     LIVE-URL re-verify the page (HTTP 200 + published title served), then flip
     `in progress` -> `in review` through the guarded path (clearing a stale
     pre-publish FAIL with a fresh honest live-verified PASS marker). Idempotent: a
     task already in review/complete is a no-op.

Running this on an already-published + already-passing task converges it to the right
status with NO duplicate row write (db_apply re-verifies; the closeout is a no-op if
already in review).

USAGE
  db_publish_and_closeout.py --task-id 86e1z6acu [--slug <slug>] [--dry-run] [--skip-publish]
  --dry-run      : publish dry-runs (rollback) AND closeout dry-runs (reports, no flip).
  --skip-publish : skip step 1 entirely; just run the gated live-verify→flip closeout
                   (the recovery path for a row already written by a prior tick).

Exit 0 on a clean pass (published+converged, or already-converged). Non-zero with a
diagnostic JSON otherwise. Prints ONE structured result JSON.
"""
import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_publish_task
import db_closeout_actor as dbca


def _run_publish(task_id, slug, dry_run):
    """Invoke db_publish_task.main() with a synthesized argv, capturing its single
    result JSON. Returns (ok, result_dict). Reuses the proven publish path verbatim."""
    argv = ["db_publish_task.py", "--task-id", task_id]
    if slug:
        argv += ["--slug", slug]
    if dry_run:
        argv += ["--dry-run"]
    saved = sys.argv
    buf = io.StringIO()
    code = None
    try:
        sys.argv = argv
        with redirect_stdout(buf):
            try:
                code = db_publish_task.main()
            except SystemExit as e:  # main() returns an int; be defensive
                code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = saved
    res = None
    for line in reversed(buf.getvalue().strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                res = json.loads(line); break
            except Exception:
                continue
    if res is None:
        res = {"ok": False, "error": "db_publish_task produced no parseable JSON",
               "raw_tail": buf.getvalue()[-400:]}
    return (code == 0 and res.get("ok") is True), res


def main():
    ap = argparse.ArgumentParser(
        description="Single idempotent DB-backed publish -> live-URL validate -> closeout.")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--slug", default=None,
                    help="Target posts.slug. Omit to derive from the task body's /blog/<slug> URL.")
    ap.add_argument("--dry-run", action="store_true",
                    help="publish rolls back; closeout reports without flipping. Writes nothing.")
    ap.add_argument("--skip-publish", action="store_true",
                    help="skip the publish step; run only the gated live-verify->flip closeout.")
    ap.add_argument("--max-age-h", type=float, default=dbca.DEFAULT_MAX_AGE_H,
                    help="publish-marker freshness window for closeout (hours).")
    args = ap.parse_args()

    result = {"ok": False, "task_id": args.task_id, "dry_run": args.dry_run,
              "skip_publish": args.skip_publish, "stage": "init"}

    # 1. PUBLISH / ENSURE the row (idempotent via db_apply). ---------------------
    if not args.skip_publish:
        result["stage"] = "publish"
        pok, pres = _run_publish(args.task_id, args.slug, args.dry_run)
        result["publish"] = pres
        if not pok:
            # FAIL-SAFE-LOUD: an unsupported/underivable target site PARKS — it must
            # NOT be retried against the wrong DB/domain and must NOT enter any retry
            # loop. Surface the park verdict (with its method comment) distinctly so
            # the caller leaves the task 'to do' and posts the SITE_CONFIG hint.
            if pres.get("park"):
                result["stage"] = "site_route"
                result["park"] = True
                result["domain"] = pres.get("domain")
                result["comment"] = pres.get("comment")
                result["supported_domains"] = pres.get("supported_domains")
                result["error"] = pres.get("reason")
                print(json.dumps(result))
                return 2  # soft park (claimable), NOT a hard/retryable failure
            result["error"] = f"publish step failed at stage={pres.get('stage')}: {pres.get('error')}"
            print(json.dumps(result))
            return 3
        result["slug"] = pres.get("slug")
        result["live_url"] = pres.get("live_url")

    # 2. CLOSEOUT: live-URL re-verify + guarded atomic flip (idempotent). --------
    # Reuse db_closeout_actor's gated sweep, restricted to THIS task. On --dry-run
    # it reports what it WOULD do (validate verdict + flip decision) without writing.
    result["stage"] = "closeout"
    sweep = dbca.sweep(dry_run=args.dry_run, only_task=args.task_id, max_age_h=args.max_age_h)
    rec = next((r for r in sweep if (r.get("task_id") == args.task_id)), None)
    if rec is None:
        # No publish marker for this task (e.g. --skip-publish on a never-published
        # task, or marker missing). That is a real gap, not success.
        result["closeout"] = {"action": "skip",
                              "detail": f"no publish marker under deliverables/{args.task_id} — "
                                        "nothing to closeout (publish first)"}
        result["error"] = "no publish marker to closeout"
        print(json.dumps(result))
        return 2
    result["closeout"] = rec
    action = rec.get("action")

    if action == "blocked":
        result["error"] = f"closeout BLOCKED: {rec.get('detail')}"
        print(json.dumps(result))
        return 4
    if action == "flip":
        if args.dry_run:
            result["ok"] = True
            result["stage"] = "done"
            result["note"] = "DRY-RUN: would publish (or row ensured) + flip to 'in review'"
            print(json.dumps(result))
            return 0
        if rec.get("flip_ok"):
            result["ok"] = True
            result["stage"] = "done"
            print(json.dumps(result))
            return 0
        result["error"] = f"flip failed: {rec.get('flip_out')}"
        print(json.dumps(result))
        return 5
    # action == 'skip'
    detail = (rec.get("detail") or "")
    if "already terminal" in detail or "already" in detail:
        # idempotent convergence: already in review/complete -> success no-op.
        result["ok"] = True
        result["stage"] = "done"
        result["note"] = f"already converged: {detail}"
        print(json.dumps(result))
        return 0
    # A genuine skip (e.g. live-url not yet serving / db verify failed) — not done.
    result["error"] = f"closeout did not flip: {detail}"
    print(json.dumps(result))
    return 6


if __name__ == "__main__":
    sys.exit(main())
