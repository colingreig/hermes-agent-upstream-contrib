#!/usr/bin/env python3
"""
Email-triage GATE — cheap, deterministic, zero-LLM.

Part 2 of the email->task pipeline. Part 1 is the Cloudflare Worker
(ignite-agents-inbound at agents.ignitemarketing.com) which receives mail
forwarded to hermes@ignitemarketing.com via Postmark inbound and files a
ClickUp task tagged `email-inbound` (NO LLM, no Hermes — survives moving
Hermes). This gate is the Hermes side: it finds those freshly-filed tasks
that have NOT yet been triaged and wakes the `email-triage` agent, which
ENRICHES THE SAME TASK (no duplicate) and decides how to act — add
`agent-ready` so the existing clickup-executor solves it, or escalate /
leave for a human.

Mirrors clickup_poll_gate.py conventions:
  * runs as a --no-agent cron (script stdout delivered directly), $0 when idle
  * reads CLICKUP_API_TOKEN from env (Doppler-injected into the gateway)
  * wakes the agent via `hermes cron run <id>`, resolving the id by NAME from
    ~/.hermes/cron/jobs.json (ids are reassigned across `hermes update`)
  * writes a snapshot the agent reads, plus a small state file for cooldown
  * NEVER raises out of main(): logs to stderr, returns 0, prints {wakeAgent}

A task is PENDING TRIAGE iff it carries `email-inbound` and NOT `triaged`.
The agent adds `triaged` when done, which removes it from this gate's view.

Token: CLICKUP_API_TOKEN.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TEAM_ID = "9017245888"
INBOUND_TAG = "email-inbound"
TRIAGED_TAG = "triaged"
AGENT_JOB_NAME = "email-triage"

SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")
SNAPSHOT_PATH = os.path.join(SCRIPTS_DIR, "email_triage_snapshot.json")
STATE_PATH = os.path.join(SCRIPTS_DIR, ".email_triage_state.json")
JOBS_PATH = os.path.expanduser("~/.hermes/cron/jobs.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")

WAKE_COOLDOWN_S = 20 * 60   # triage runs are quick; don't re-wake within 20 min
HTTP_TIMEOUT = 30


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _token():
    value = None
    try:
        from agent import lazy_secret_resolver
        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None
    if not value:
        value = os.environ.get("CLICKUP_API_TOKEN", "")
    return (value or "").strip()


def _get(url):
    req = urllib.request.Request(url, headers={"Authorization": _token()})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _tagnames(task):
    return {(t.get("name") or "").lower() for t in (task.get("tags") or [])}


def _pending(task):
    names = _tagnames(task)
    return INBOUND_TAG in names and TRIAGED_TAG not in names


def _entry(t):
    return {
        "id": t.get("id"),
        "name": (t.get("name") or "")[:160],
        "status": (t.get("status") or {}).get("status"),
        "list": ((t.get("list") or {}) or {}).get("name"),
        "list_id": ((t.get("list") or {}) or {}).get("id"),
        "url": t.get("url"),
        "date_created": t.get("date_created"),
    }


def _scan():
    """Return pending-triage task entries.

    Primary: the tag-filter endpoint (cheap — returns only email-inbound tasks).
    Fallback: paginate the team tasks and filter client-side (the tag filter is
    documented to occasionally 500 in clickup_poll_gate.py).
    """
    try:
        params = urllib.parse.urlencode(
            {"order_by": "created", "reverse": "true", "include_closed": "true"}
        ) + f"&tags%5B%5D={urllib.parse.quote(INBOUND_TAG)}"
        data = _get(f"https://api.clickup.com/api/v2/team/{TEAM_ID}/task?{params}")
        return [_entry(t) for t in (data.get("tasks") or []) if _pending(t)]
    except Exception as e:
        print(f"[triage-gate] tag filter failed ({e!r}); falling back to full scan",
              file=sys.stderr)

    pending, page = [], 0
    while True:
        params = urllib.parse.urlencode(
            {"page": page, "subtasks": "true", "include_closed": "true"}
        )
        data = _get(f"https://api.clickup.com/api/v2/team/{TEAM_ID}/task?{params}")
        tasks = data.get("tasks") or []
        pending.extend(_entry(t) for t in tasks if _pending(t))
        if data.get("last_page", True) or not tasks:
            break
        page += 1
        if page > 50:
            break
    return pending


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _agent_id():
    for j in _load_json(JOBS_PATH, {}).get("jobs", []):
        if j.get("name") == AGENT_JOB_NAME:
            return j.get("id")
    return None


def _wake(reason):
    if os.environ.get("DRY_RUN"):
        print(f"[triage-gate] DRY_RUN — would wake {AGENT_JOB_NAME} ({reason})")
        return True
    aid = _agent_id()
    if not aid:
        print(f"[triage-gate] agent job {AGENT_JOB_NAME!r} not in jobs.json; cannot wake",
              file=sys.stderr)
        return False
    try:
        res = subprocess.run([HERMES_BIN, "cron", "run", aid],
                             capture_output=True, text=True, timeout=60)
        print(f"[triage-gate] woke {aid} ({reason}): rc={res.returncode} {res.stdout.strip()}")
        return res.returncode == 0
    except Exception as e:
        print(f"[triage-gate] wake failed: {e!r}", file=sys.stderr)
        return False


def main():
    if not _token():
        print("[triage-gate] CLICKUP_API_TOKEN not set; skipping", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0

    try:
        pending = _scan()
    except Exception as e:
        print(f"[triage-gate] scan error: {e!r}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0

    now = time.time()
    state = _load_json(STATE_PATH, {})

    _save_json(SNAPSHOT_PATH, {
        "generated_at": _now_iso(),
        "count": len(pending),
        "tasks": pending,
    })

    if not pending:
        state["last_poll"] = _now_iso()
        _save_json(STATE_PATH, state)
        print(json.dumps({"wakeAgent": False}))
        return 0

    last_wake = state.get("last_wake_ts", 0)
    if now - last_wake < WAKE_COOLDOWN_S:
        state["last_poll"] = _now_iso()
        _save_json(STATE_PATH, state)
        print(f"[triage-gate] {len(pending)} task(s) pending but wake cooldown "
              f"({int((now - last_wake)/60)}m ago); holding")
        print(json.dumps({"wakeAgent": False}))
        return 0

    ids = ", ".join(f"{t['id']}({t['name'][:40]})" for t in pending[:5])
    print(f"[triage-gate] {len(pending)} task(s) pending triage: {ids}")
    woke = _wake(f"{len(pending)} pending")
    if woke and not os.environ.get("DRY_RUN"):
        state["last_wake_ts"] = now
    state["last_poll"] = _now_iso()
    _save_json(STATE_PATH, state)
    print(json.dumps({"wakeAgent": bool(woke), "pending": len(pending)}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[triage-gate] unexpected: {e!r}", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        sys.exit(0)
