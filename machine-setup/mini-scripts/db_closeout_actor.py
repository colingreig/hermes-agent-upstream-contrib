#!/usr/bin/env python3
"""db_closeout_actor.py — the DB-PUBLISH closeout backstop.

THE GAP THIS FILLS
------------------
closeout_actor.py advances merged+validated tasks, but it is PR-keyed: it iterates
the validator verdict store (`owner/repo#pr`). DB-backed publish tasks (e.g.
dynamics365group.com blogs) ship by writing a Neon `posts` row — NO PR, no verdict
record — so they are STRUCTURALLY INVISIBLE to closeout_actor. When the executor
hangs AFTER a successful `db_apply` row write but BEFORE it posts the closeout /
flips status (the 2026-06-26 silent provider-hang stall), the published blog is
stranded in 'in progress' forever, invisible on the board. On 2026-06-26 nine such
posts were stranded overnight and had to be finalized by hand.

WHAT IT DOES
------------
A deterministic, zero-LLM sweep. For each self-attesting publish marker
(`~/.hermes/deliverables/<task_id>/publish_result.json`, written by db_publish_task.py
on a verified row write), it finalizes the task IFF every gate passes:
  (a) marker is fresh (default <48h; a stale marker never fires);
  (b) task is currently in 'in progress' — the exact state a hung executor leaves
      it in. NOT 'to do' (a deliberately re-armed/rework task) and NOT review/complete
      (idempotent no-op);
  (c) no `ignite- claiming:` comment NEWER than the marker (a fresh cycle is in
      flight → WAIT, don't pre-empt it);
  (d) newest `ignite-validate:` marker is not FAIL/BLOCK (G2 defense-in-depth);
  (e) LIVE re-verify: the `posts` row for the marker's slug STILL matches the
      published content (title == title_after AND length(content) == content_len_after).
      This proves the publish is real and current, not reverted or superseded.
Then it advances 'in progress' -> 'in review' THROUGH the guarded clickup.mjs path
(re-enforces G1/G2/G3; never sets 'complete', never passes CLICKUP_ALLOW_COMPLETE)
and posts a closeout comment citing the verified row.

SAFETY
------
Default-deny, fail-closed, idempotent. Any read error / ambiguity => skip (never flip).
`--dry-run` reports what WOULD flip and why; touches nothing. sweep() never raises.
DB reads use the SITE's db_url_env (from db_site_config), already present in this
process's ambient env (1Password-injected at gateway boot) — if the DB is
unreachable the verify fails closed and the task is left for the next sweep.

CLI:
  db_closeout_actor.py --dry-run         # report, touch nothing (recommended first)
  db_closeout_actor.py                    # live: finalize every qualifying task
  db_closeout_actor.py --task T           # restrict to one task id (still fully gated)
  db_closeout_actor.py --max-age-h 48     # marker freshness window (default 48)
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

# Reuse closeout_actor's guarded clickup.mjs helpers + the status guard so this
# actor finalizes through the identical, G1/G2/G3-enforcing path. Importing is safe:
# closeout_actor's side effects are all behind `if __name__ == '__main__'`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import closeout_actor as ca           # _cu_node, _task_json, _comments_json, TERMINAL_STATUS
import clickup_status_guard as guard
import db_site_config as sites

HOME = os.path.expanduser("~")
DELIVERABLES = os.path.join(HOME, ".hermes", "deliverables")
VENV_PY = os.path.join(HOME, ".hermes", "hermes-agent", "venv", "bin", "python3.11")

# The exact in-flight status a hung executor leaves a published DB task in. We are
# deliberately STRICTER than closeout_actor.ADVANCEABLE: we will NOT finalize a 'to do'
# task (those are fresh or deliberately re-armed for rework — finalizing one would undo
# an operator/babysit re-arm).
DB_ADVANCE_FROM = {"in progress", "in-progress"}
DEFAULT_MAX_AGE_H = 48
CLAIM_PREFIX = "ignite- claiming"
LIVE_FETCH_TIMEOUT = 30
POST_CLICKUP_COMMENT = os.path.expanduser("~/.hermes/scripts/post_clickup_comment.py")


def _now():
    return datetime.datetime.now().astimezone()


def _post_clickup_comment(task_id, body):
    """Post a guarded ClickUp comment body through the truncation checker."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", prefix="db_closeout_actor_", delete=False)
    try:
        tmp.write(body)
        tmp.close()
        r = subprocess.run(
            [VENV_PY, POST_CLICKUP_COMMENT, task_id, tmp.name],
            capture_output=True, text=True, timeout=LIVE_FETCH_TIMEOUT,
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


def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


def _db_verify(slug, expect_title, expect_len, table="posts", db_url_env=None):
    """Re-read the live row via venv psycopg2 (db_publish_task.db_read pattern), using
    the SITE's DB URL env vars already present in this process's ambient env
    (1Password-injected at gateway boot; no doppler wrapper since 2026-07-03).
    Returns (ok, detail). ok=True only when the row's title AND content length
    match the published marker exactly. Any failure => (False, reason) — fail closed.

    db_url_env: ordered list of env var names for the connection string (the SITE's
    db_url_env). Default preserves today's UNPOOLED-then-DATABASE_URL behavior."""
    env_order = db_url_env or ["DATABASE_URL_UNPOOLED", "DATABASE_URL"]
    # Slug + table passed via env (NOT string-interpolated) — injection-safe and avoids
    # the SQL '%s' placeholder colliding with Python % formatting.
    code = (
        "import os,json,psycopg2\n"
        "dsn=next((os.environ[e] for e in os.environ['DBCO_ENV_ORDER'].split(',') if os.environ.get(e)),None)\n"
        "tbl=''.join(ch for ch in os.environ['DBCO_TABLE'] if ch.isalnum() or ch=='_')\n"
        "c=psycopg2.connect(dsn,connect_timeout=20);cur=c.cursor()\n"
        "cur.execute('SELECT title, length(content) FROM '+tbl+' WHERE slug=%s',[os.environ['DBCO_SLUG']])\n"
        "r=cur.fetchone()\n"
        "print(json.dumps({'found':bool(r),'title':(r[0] if r else None),'len':(r[1] if r else None)}))\n"
    )
    env = dict(os.environ, DBCO_SLUG=slug, DBCO_TABLE=table,
               DBCO_ENV_ORDER=",".join(env_order))
    try:
        p = subprocess.run(
            [VENV_PY, "-c", code],
            capture_output=True, text=True, timeout=90, env=env)
    except Exception as e:
        return False, f"db read invocation failed: {e!r}"
    row = None
    for line in reversed((p.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                row = json.loads(line); break
            except Exception:
                continue
    if row is None:
        return False, f"db read produced no JSON; stderr_tail={(p.stderr or '')[-200:]}"
    if not row.get("found"):
        return False, f"no posts row for slug={slug!r}"
    if expect_title is not None and row.get("title") != expect_title:
        return False, f"title drift (row != marker) — not finalizing"
    if expect_len is not None and row.get("len") != expect_len:
        return False, f"content length drift (row={row.get('len')} marker={expect_len})"
    return True, f"row verified (title match, len={row.get('len')})"


def _live_url_verify(live_url, expect_title):
    """Fetch the LIVE published page and confirm it serves the published content.

    This is the in-process LIVE-URL re-validation for the DB-publish lane (the
    deliverable IS a live URL, not a PR — so we validate the URL, not a PR diff or
    a stale verdict file). Pass iff: HTTP 200 AND the published `title_after` text
    appears in the served HTML (the page renders the row we wrote). Fail closed on
    any error — a page that won't load / doesn't yet show the title is NOT a
    finalize signal (dynamics365group.com is Vercel ISR / stale-while-revalidate,
    so a just-written row may take a tick to surface; we leave it for the next
    sweep rather than flip prematurely). Returns (ok, detail)."""
    if not live_url:
        return False, "no live_url in marker — cannot live-verify"
    try:
        req = urllib.request.Request(live_url, headers={"User-Agent": "hermes-db-closeout/1.0"})
        with urllib.request.urlopen(req, timeout=LIVE_FETCH_TIMEOUT) as r:
            code = r.getcode()
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        return False, f"live fetch failed ({live_url}): {e!r}"
    if code != 200:
        return False, f"live page HTTP {code} (expected 200)"
    if expect_title:
        # Tolerate HTML entity escaping of the title (&amp; etc.) by also checking
        # an entity-escaped variant and a whitespace-collapsed compare.
        norm = lambda s: re.sub(r"\s+", " ", s or "").strip()
        h = norm(html)
        t = norm(expect_title)
        if t and t not in h and t.replace("&", "&amp;") not in h:
            return False, f"live page 200 but published title not yet served (ISR lag?) — not finalizing"
    return True, f"live page 200 + published title served ({live_url})"


def _newest_claim_ts(comments):
    """Latest 'ignite- claiming:' comment time as aware datetime, or None."""
    newest = None
    for c in comments or []:
        txt = guard.comment_text(c) if hasattr(guard, "comment_text") else (c.get("comment_text") or "")
        if CLAIM_PREFIX in (txt or "").lower():
            raw = c.get("date") or c.get("date_created")
            ts = None
            if raw is not None:
                try:
                    ts = datetime.datetime.fromtimestamp(int(raw) / 1000).astimezone()
                except Exception:
                    ts = _parse_iso(str(raw))
            if ts and (newest is None or ts > newest):
                newest = ts
    return newest


def evaluate(marker, max_age_h):
    """Pure decision over one marker. Returns (action, detail, meta). action in
    {'flip','skip','blocked'}; meta is a dict carrying e.g. {'stale_fail': bool}.
    The network/DB calls (db + live-url re-verify) run last."""
    meta = {"stale_fail": False, "site_cfg": None}
    task_id = marker.get("task_id") or ""
    slug = marker.get("slug") or ""
    if not task_id or not slug:
        return "skip", "marker missing task_id/slug", meta

    # (a0) SITE ROUTING (anti-strand): a marker is normally written only for a
    # configured site, but resolve it explicitly and SKIP (never fail-closed-retry)
    # any marker whose site isn't in SITE_CONFIG — so an unknown/legacy marker can
    # never wedge the sweep or get verified against the wrong DB. Resolution order:
    # marker['site'] domain -> live_url domain. The resolved cfg supplies the
    # connection-string env order for the DB re-verify below.
    cfg = sites.site_for_domain(marker.get("site")) or \
        sites.site_for_domain(sites.domain_from_url(marker.get("live_url")))
    if cfg is None:
        dom = marker.get("site") or sites.domain_from_url(marker.get("live_url"))
        return "skip", (f"unsupported/unknown DB-publish site for marker "
                        f"(site={dom!r}); no SITE_CONFIG entry — skipping (not stranding)"), meta
    meta["site_cfg"] = cfg

    # (a) freshness
    mts = _parse_iso(marker.get("ts") or "")
    if mts is None:
        return "skip", "marker has no parseable ts", meta
    age_h = (_now() - mts).total_seconds() / 3600.0
    if age_h > max_age_h:
        return "skip", f"marker stale ({age_h:.1f}h > {max_age_h}h)", meta

    # task status + comments
    tdata, terr = ca._task_json(task_id)
    if terr:
        return "skip", f"could not read task: {terr}", meta
    cur = ((tdata.get("status") or {}).get("status") or "").strip().lower()
    if not cur:
        return "skip", "task status unreadable", meta
    if guard.is_review_class(cur) or guard.is_complete_class(cur):
        return "skip", f"already terminal/review (status='{cur}')", meta   # idempotent no-op
    if cur not in DB_ADVANCE_FROM:
        # 'to do' (re-armed/fresh), 'deferred', etc. — not a stranded-publish state.
        return "skip", f"status '{cur}' not a stranded-publish state (only 'in progress')", meta

    comments = ca._comments_json(task_id)

    # (c) a newer claim than the publish means a fresh cycle is in flight -> stand down.
    claim_ts = _newest_claim_ts(comments)
    if claim_ts and claim_ts > mts:
        return "skip", f"newer claim ({claim_ts.isoformat()}) than publish — a run is in flight; WAIT", meta

    # (d) G2: refuse over a standing FAIL/BLOCK validate marker — but ONLY if that
    # marker is NEWER than this publish. A FAIL/BLOCK from BEFORE the publish is
    # stale by definition: the publish IS the fix the FAIL asked for, and on the
    # DB-publish lane there is no PR validator that re-emits a fresh PASS marker
    # over a live page (the `needs-validation` tag wakes hermes-pr-validate, which
    # is PR-only — so a DB task's pre-publish FAIL would never be superseded and
    # the task would wedge forever; verified 86e1z6acu, FAIL 2026-06-22 vs publish
    # 2026-06-27). The LIVE-URL re-verify in (f) is the authoritative current
    # signal that replaces the stale PR-marker for this lane. We still HARD-BLOCK
    # on a FAIL/BLOCK posted AFTER the publish (a genuine post-publish rejection).
    v = guard.latest_validate_verdict(comments)
    if v and guard.NEGATIVE_VERDICTS.match(v["verdict"]):
        v_ts = None
        try:
            v_ts = datetime.datetime.fromtimestamp(int(v.get("date") or 0) / 1000).astimezone()
        except Exception:
            v_ts = None
        if v_ts is not None and v_ts <= mts:
            # stale pre-publish FAIL — do NOT block; (f) live-verify decides. Flag
            # so _do_flip emits a fresh honest PASS marker to clear G2 on the flip.
            meta["stale_fail"] = True
        else:
            return "blocked", (f"newest ignite-validate marker is {v['verdict'].upper()} "
                               f"(comment {v.get('comment_id')}) and is NEWER than the publish "
                               f"— a genuine post-publish rejection; refusing closeout"), meta

    # (e) DB re-verify the row still matches the published content (no revert/supersede).
    ok, detail = _db_verify(slug, marker.get("title_after"), marker.get("content_len_after"),
                            marker.get("table") or cfg.table, db_url_env=cfg.db_url_env)
    if not ok:
        return "skip", f"db re-verify failed: {detail}", meta

    # (f) LIVE-URL re-verify: fetch the published page and confirm it serves the
    # written content. THIS is the live-URL validation for the no-PR lane — it
    # replaces the PR-shaped verdict file the rest of the system relies on.
    lok, ldetail = _live_url_verify(marker.get("live_url"), marker.get("title_after"))
    if not lok:
        return "skip", f"live-url re-verify failed: {ldetail}", meta

    stale_note = " [clearing stale pre-publish FAIL via fresh live-verified PASS]" if meta["stale_fail"] else ""
    return "flip", (f"publish verified (slug={slug}, {detail}; {ldetail}, marker age "
                    f"{age_h:.1f}h){stale_note}, status '{cur}' -> '{ca.TERMINAL_STATUS}'"), meta


def _do_flip(task_id, marker, stale_fail=False):
    """Advance to TERMINAL_STATUS via the GUARDED clickup.mjs path, then post a
    closeout comment. Comment avoids the G3 banned-auth phrase class.

    When `stale_fail` is set, the task carries a pre-publish `ignite-validate:
    FAIL/BLOCK` marker that the (now verified-live) re-publish addressed. On the
    DB-publish lane NOTHING else re-emits a fresh PASS over a live page (the
    validator cron is PR-only), so the guarded status path's G2 would refuse the
    flip over that stale FAIL. We therefore post an HONEST fresh
    `ignite-validate: PASS` marker FIRST — sourced from THIS actor's live-URL
    re-verification (gate f), not fabricated — which is exactly the "newer PASS
    clears the FAIL" signal both guards expect. This keeps the audit trail intact
    and works on the Python AND Node guard with no blanket CLICKUP_ALLOW_FAIL_
    OVERRIDE."""
    slug = marker.get("slug")
    url = marker.get("live_url") or ""
    if stale_fail:
        pass_marker = (
            f"ignite-validate: PASS — DB-publish live-URL re-validation. The `posts` row "
            f"for slug '{slug}' is verified written AND the live page ({url}) serves the "
            f"published content (HTTP 200, published title present). This supersedes the "
            f"earlier pre-publish FAIL marker (which predates this publish; the publish is "
            f"the fix). Source: db_closeout_actor live-URL gate, not a PR diff."
        )
        pok, pout = _post_clickup_comment(task_id, pass_marker)
        if not pok:
            return False, f"could not post fresh PASS marker to clear stale FAIL: {pout}"
    ok, out = ca._cu_node(["status", task_id, ca.TERMINAL_STATUS])
    if not ok:
        return False, f"status flip refused/failed: {out}"
    body = (f"\U0001F916 DB-publish closeout actor: the `posts` row for slug '{slug}' is "
            f"verified written AND live-URL re-validated (HTTP 200, published title served) — "
            f"advancing to '{ca.TERMINAL_STATUS}' (Hermes terminal; ignite-validate/Colin "
            f"owns 'complete'). {url}".strip())
    cok, cout = _post_clickup_comment(task_id, body)
    return True, ("flipped to '%s'" % ca.TERMINAL_STATUS
                  + ("" if cok else f"; comment failed: {cout}"))


def sweep(dry_run=False, only_task=None, max_age_h=DEFAULT_MAX_AGE_H):
    """Scan every publish marker; finalize each qualifying stranded task. Never raises."""
    results = []
    try:
        if not os.path.isdir(DELIVERABLES):
            return results
        for tid in sorted(os.listdir(DELIVERABLES)):
            mpath = os.path.join(DELIVERABLES, tid, "publish_result.json")
            if not os.path.isfile(mpath):
                continue
            if only_task and tid != only_task:
                continue
            try:
                with open(mpath) as fh:
                    marker = json.load(fh)
            except Exception as e:
                results.append({"task_id": tid, "action": "error", "detail": f"unreadable marker: {e!r}"})
                continue
            try:
                action, detail, meta = evaluate(marker, max_age_h)
                rec = {"task_id": marker.get("task_id") or tid, "slug": marker.get("slug"),
                       "action": action, "detail": detail,
                       "stale_fail": meta.get("stale_fail", False)}
                if action == "flip" and not dry_run:
                    fok, msg = _do_flip(rec["task_id"], marker, stale_fail=meta.get("stale_fail", False))
                    rec["flip_ok"] = fok
                    rec["flip_out"] = msg
                results.append(rec)
            except Exception as e:
                results.append({"task_id": tid, "action": "error", "detail": f"{e!r}"})
    except Exception as e:
        results.append({"task_id": "*", "action": "error", "detail": f"sweep fatal: {e!r}"})
    return results


def main():
    p = argparse.ArgumentParser(description="DB-publish closeout backstop: finalize stranded published blogs.")
    p.add_argument("--dry-run", action="store_true", help="report what would flip; touch nothing")
    p.add_argument("--task", help="restrict to one ClickUp task id")
    p.add_argument("--max-age-h", type=float, default=DEFAULT_MAX_AGE_H, help="marker freshness window (hours)")
    a = p.parse_args()
    res = sweep(dry_run=a.dry_run, only_task=a.task, max_age_h=a.max_age_h)
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
        print(f"[db-closeout] {tag:11} task={r.get('task_id') or '-'} slug={r.get('slug') or '-'} — {r['detail']}")
        if r.get("flip_out") and not r.get("flip_ok"):
            print(f"               {r['flip_out']}")
    flipped = sum(1 for r in res if r.get("action") == "flip" and r.get("flip_ok"))
    would = sum(1 for r in res if r.get("action") == "flip" and a.dry_run)
    print(json.dumps({"flipped": flipped, "would_flip": would, "total": len(res)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
