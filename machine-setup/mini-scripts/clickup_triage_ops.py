#!/usr/bin/env python3
"""
ClickUp triage operations helper — read + enrich an EXISTING task.

Companion to create_clickup_task.py (which creates tasks + add/rm tags). This
one is for the `email-triage` agent enriching the task the Cloudflare Worker
already filed: read it, read its comments, post a triage comment, set priority,
and tag it (`triaged`, and `agent-ready` when handing off to the executor).

All ops read CLICKUP_API_TOKEN from env (Doppler-injected); never pass tokens on
the command line. Exit 0 on success, non-zero on failure (so the agent can tell).

Usage:
  clickup_triage_ops.py get <task_id>
  clickup_triage_ops.py comments <task_id>
  clickup_triage_ops.py comment <task_id> --body /path/to/comment.md
  clickup_triage_ops.py priority <task_id> <1|2|3|4>      # 1=urgent..4=low
  clickup_triage_ops.py add-tag <task_id> <tag>
  clickup_triage_ops.py rm-tag <task_id> <tag>
  clickup_triage_ops.py status <task_id> "<status name>"   # normalize to "to do"
  clickup_triage_ops.py statuses <list_id>                 # valid statuses for a list
  clickup_triage_ops.py move <task_id> <list_id>           # re-route off Ad Hoc
  clickup_triage_ops.py assign <task_id> --ids 168143285   # put intake in Colin's queue on escalation
  clickup_triage_ops.py create <list_id> "<name>" [--body f] [--status s] [--priority N]
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.clickup.com/api/v2"

# Hermes completion guardrails (G1–G3). Same chokepoint as clickup.mjs/status_guard.mjs,
# applied to this cron-path writer. Fail-open if the guard module isn't deployed yet
# (so triage never breaks), but log loudly. The validator's own writer
# (hermes_validate_ops.py) is intentionally NOT guarded — it owns `complete`.
try:
    from clickup_status_guard import (
        assert_status_allowed, assert_comment_allowed, is_advance_status, GuardError,
    )
except Exception:  # pragma: no cover - guard not deployed
    assert_status_allowed = assert_comment_allowed = is_advance_status = None
    GuardError = Exception


_TOKEN_CACHE = ""


def _token():
    # Env-first keeps the healthy scheduler path fast and avoids an unnecessary
    # 1Password lookup. During a gateway restart-race CLICKUP_API_TOKEN can be
    # transiently absent from os.environ; only then use the lazy resolver.
    # Cached in-process (never caches empty); never logs the value.
    global _TOKEN_CACHE
    if _TOKEN_CACHE:
        return _TOKEN_CACHE
    t = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if t:
        _TOKEN_CACHE = t
        return t
    value = None
    try:
        from agent import lazy_secret_resolver
        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None
    t = (value or "").strip()
    if not t:
        print(
            "CLICKUP_API_TOKEN not set in env and could not be resolved via 1Password",
            file=sys.stderr,
        )
        sys.exit(2)
    _TOKEN_CACHE = t
    return t


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": _token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}", file=sys.stderr)
        return e.code, None
    except Exception as e:
        print(f"request error: {e!r}", file=sys.stderr)
        return 0, None


def cmd_get(a):
    st, t = _req("GET", f"/task/{a.task_id}")
    if not t:
        return 1
    out = {
        "id": t.get("id"),
        "name": t.get("name"),
        "status": (t.get("status") or {}).get("status"),
        "priority": ((t.get("priority") or {}) or {}).get("priority"),
        "tags": [tg.get("name") for tg in (t.get("tags") or [])],
        "list": ((t.get("list") or {}) or {}).get("name"),
        "list_id": ((t.get("list") or {}) or {}).get("id"),
        "url": t.get("url"),
        "description": t.get("description") or t.get("text_content") or "",
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_comments(a):
    st, d = _req("GET", f"/task/{a.task_id}/comment")
    if d is None:
        return 1
    for c in d.get("comments", []):
        who = (c.get("user") or {}).get("username") or "?"
        print(f"- [{who}] {c.get('comment_text','').strip()[:500]}")
    return 0


def cmd_comment(a):
    with open(a.body) as f:
        text = f.read()
    if assert_comment_allowed is not None:
        try:
            assert_comment_allowed(text)
        except GuardError as e:
            print(f"BLOCKED {e}", file=sys.stderr)
            return 1
    st, d = _req("POST", f"/task/{a.task_id}/comment", {"comment_text": text, "notify_all": False})
    if st and 200 <= st < 300:
        print(f"comment posted id={d.get('id')}")
        return 0
    return 1


def cmd_priority(a):
    st, d = _req("PUT", f"/task/{a.task_id}", {"priority": int(a.level)})
    if st and 200 <= st < 300:
        print(f"priority set to {a.level}")
        return 0
    return 1


def cmd_add_tag(a):
    name = urllib.parse.quote(a.tag)
    st, _ = _req("POST", f"/task/{a.task_id}/tag/{name}")
    if st and 200 <= st < 300:
        print(f"tag '{a.tag}' added")
        return 0
    return 1


def cmd_rm_tag(a):
    name = urllib.parse.quote(a.tag)
    st, _ = _req("DELETE", f"/task/{a.task_id}/tag/{name}")
    if st and 200 <= st < 300:
        print(f"tag '{a.tag}' removed")
        return 0
    return 1


def cmd_status(a):
    """Set a task's status. Status name must be valid for the task's list
    (use `statuses <list_id>` to discover them). Default landing status for a
    triaged actionable task is 'to do' — NEVER leave it 'deferred'/'backlog'."""
    if assert_status_allowed is not None:
        comments = []
        if is_advance_status(a.status):  # fetch only when advancing (G2 verdict check)
            _, cd = _req("GET", f"/task/{a.task_id}/comment")
            comments = (cd or {}).get("comments", []) if isinstance(cd, dict) else []
        try:
            assert_status_allowed(a.status, comments=comments)
        except GuardError as e:
            print(f"BLOCKED {e}", file=sys.stderr)
            return 1
    st, d = _req("PUT", f"/task/{a.task_id}", {"status": a.status})
    if st and 200 <= st < 300:
        print(f"status set to '{a.status}'")
        return 0
    return 1


def cmd_statuses(a):
    """List the valid status names for a list (statuses differ per list — e.g.
    'jdm.com v4' has no 'ready for review'). Pass a list_id."""
    st, d = _req("GET", f"/list/{a.list_id}")
    if not d:
        return 1
    names = [s.get("status") for s in (d.get("statuses") or [])]
    print(json.dumps({"list_id": a.list_id, "name": d.get("name"), "statuses": names}, indent=2))
    return 0


def cmd_move(a):
    """Move a task to a new home list (re-route). Used when the Worker defaulted
    a forward to 'Ad Hoc' but the content clearly belongs to a project list.
    Uses the v2 'add task to list' endpoint; requires the Tasks-in-Multiple-Lists
    ClickApp. If it fails, the agent should note it and flag for human re-route."""
    st, _ = _req("POST", f"/list/{a.list_id}/task/{a.task_id}")
    if st and 200 <= st < 300:
        print(f"task {a.task_id} routed to list {a.list_id}")
        return 0
    return 1


def cmd_assign(a):
    """Add (and optionally remove) assignees on a task. Used to put an intake
    task into a HUMAN's queue — e.g. when triage has a genuine question it
    cannot resolve, assign the intake to Colin (168143285) so it lands on his
    board instead of dying in a comment he never sees. ClickUp wants the
    add/rem object on PUT /task: {"assignees":{"add":[id],"rem":[id]}}."""
    add = [int(x) for x in (a.ids or "").split(",") if x.strip().isdigit()]
    rem = [int(x) for x in (a.rem or "").split(",") if x.strip().isdigit()] if a.rem else []
    if not add and not rem:
        print("assign: no valid numeric ids in --ids/--rem", file=sys.stderr)
        return 2
    st, d = _req("PUT", f"/task/{a.task_id}", {"assignees": {"add": add, "rem": rem}})
    if st and 200 <= st < 300:
        now = [u.get("id") for u in ((d or {}).get("assignees") or [])]
        print(f"assignees now: {now}")
        return 0
    return 1


def cmd_create(a):
    """Create a NEW task in a list — for actioning 'create a task'/'split into
    tasks' requests. TWO-STEP: create the stub first (this call returns the new
    id immediately, so nothing is lost if a later enrich step fails), THEN enrich
    it with `comment`/`priority`/`add-tag agent-ready`. Body is optional markdown."""
    body = {"name": a.name}
    if a.body:
        with open(a.body) as f:
            body["markdown_content"] = f.read()
    if a.status:
        body["status"] = a.status
    if a.priority:
        body["priority"] = int(a.priority)
    st, d = _req("POST", f"/list/{a.list_id}/task", body)
    if st and 200 <= st < 300 and d:
        print(json.dumps({"id": d.get("id"), "url": d.get("url"), "status": (d.get("status") or {}).get("status")}, indent=2))
        return 0
    return 1


def main():
    p = argparse.ArgumentParser(description="ClickUp triage ops (read + enrich a task).")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get"); g.add_argument("task_id"); g.set_defaults(fn=cmd_get)
    c = sub.add_parser("comments"); c.add_argument("task_id"); c.set_defaults(fn=cmd_comments)
    cm = sub.add_parser("comment"); cm.add_argument("task_id"); cm.add_argument("--body", required=True); cm.set_defaults(fn=cmd_comment)
    pr = sub.add_parser("priority"); pr.add_argument("task_id"); pr.add_argument("level", choices=["1", "2", "3", "4"]); pr.set_defaults(fn=cmd_priority)
    at = sub.add_parser("add-tag"); at.add_argument("task_id"); at.add_argument("tag"); at.set_defaults(fn=cmd_add_tag)
    rt = sub.add_parser("rm-tag"); rt.add_argument("task_id"); rt.add_argument("tag"); rt.set_defaults(fn=cmd_rm_tag)
    ss = sub.add_parser("status"); ss.add_argument("task_id"); ss.add_argument("status"); ss.set_defaults(fn=cmd_status)
    sl = sub.add_parser("statuses"); sl.add_argument("list_id"); sl.set_defaults(fn=cmd_statuses)
    mv = sub.add_parser("move"); mv.add_argument("task_id"); mv.add_argument("list_id"); mv.set_defaults(fn=cmd_move)
    asg = sub.add_parser("assign"); asg.add_argument("task_id"); asg.add_argument("--ids", help="comma-separated user ids to add (Colin=168143285)"); asg.add_argument("--rem", help="comma-separated user ids to remove"); asg.set_defaults(fn=cmd_assign)
    cr = sub.add_parser("create"); cr.add_argument("list_id"); cr.add_argument("name"); cr.add_argument("--body"); cr.add_argument("--status"); cr.add_argument("--priority", choices=["1", "2", "3", "4"]); cr.set_defaults(fn=cmd_create)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
