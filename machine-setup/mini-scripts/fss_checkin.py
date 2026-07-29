#!/usr/bin/env python3
"""
fss_checkin.py — one-time post-quota-reset check-in on the FSS deploy task.

Scheduled 2026-06-14 to fire ~14:00 PT, after MiniMax's session/quota limit
resets (~3h), to report whether the autonomous loop picked up and shipped
ClickUp task 86e1vrh4t ([FSS] Fix PR #2) once quota was back.

Zero-LLM by design (we're trying to CUT Hermes token burn, not add to it): it
gathers deterministic facts and posts a digest to Slack via the cron's stdout
delivery. Every section is best-effort and must never crash the run.

Reports:
  1. MiniMax quota recovery — any 429 / pool-exhaustion in agent.log in the last hour?
  2. FSS task 86e1vrh4t — ClickUp status, still agent-ready?, last comment snippet.
  3. PR #2 (colingreig/fieldservicesoftware.io) — merged? open? merge SHA.
  4. Prod check — does fieldservicesoftware.io emit JobPosting ONLY on the salary page?
  5. Token burn since the schedule baseline (sessions started >= 2026-06-14 17:39 UTC).
  6. New usage-limit Slack alerts since baseline.

Schedule: cron "0 21 * * *" (21:00 UTC = 14:00 PDT) --repeat 1, --no-agent,
deliver: slack:UN4CQ1EGG.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
AGENT_LOG = os.path.join(HOME, ".hermes/logs/agent.log")
SC = os.path.join(HOME, ".hermes/skills/clickup-queue-poller/scripts")
TASK_ID = "86e1vrh4t"
REPO = "colingreig/fieldservicesoftware.io"
BASELINE_UTC = "20260614_173900"  # sessions at/after this are "since we scheduled"
SALARY_URL = "https://fieldservicesoftware.io/field-service-manager-salary/"
NONSALARY_URL = "https://fieldservicesoftware.io/field-service-kpis/"


def _run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)


def _curl_clickup(path):
    tok = None
    try:
        from agent import lazy_secret_resolver
        tok = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        tok = None
    if not tok:
        tok = os.environ.get("CLICKUP_API_TOKEN", "")
    rc, out, _ = _run(["curl", "-s", "-H", f"Authorization: {tok}",
                       f"https://api.clickup.com/api/v2{path}"])
    try:
        return json.loads(out)
    except Exception:
        return {}


def section_quota():
    try:
        cutoff = time.time() - 3600
        hits = 0
        with open(AGENT_LOG, errors="replace") as f:
            for line in f:
                if re.search(r"exhausted \(status=429\)|no available entries \(all exhausted|RateLimitError", line):
                    # crude recency: trust last ~4000 lines window via tail instead
                    hits += 1
        # recency check via tail of last 400 lines
        rc, out, _ = _run(["tail", "-n", "400", AGENT_LOG])
        recent = len(re.findall(r"exhausted \(status=429\)|no available entries \(all exhausted|RateLimitError", out))
        if recent == 0:
            return "✅ *MiniMax quota:* recovered — no 429 / pool-exhaustion in the last ~400 log lines."
        return f"⚠️ *MiniMax quota:* still hitting limits — {recent} exhaustion/429 signals in the recent log tail."
    except Exception as e:
        return f"• MiniMax quota: (check failed: {e})"


def section_task():
    t = _curl_clickup(f"/task/{TASK_ID}")
    if not t:
        return "• FSS task: (could not fetch from ClickUp)"
    status = (t.get("status") or {}).get("status", "?")
    tags = [x.get("name") for x in (t.get("tags") or [])]
    still_ready = "agent-ready" in tags
    # last comment
    c = _curl_clickup(f"/task/{TASK_ID}/comment")
    comments = c.get("comments", []) if isinstance(c, dict) else []
    last = (comments[0].get("comment_text") if comments else "") or ""
    last = last.replace("\n", " ")[:200]
    flag = "🟢" if status.lower() in ("complete", "closed") else ("🟡" if status.lower() in ("ready for review", "in progress") else "⚪")
    return (f"{flag} *FSS task {TASK_ID}:* status = *{status}* · agent-ready still on: {still_ready}\n"
            f"    last comment: _{last or '(none)'}_")


def section_pr():
    rc, out, _ = _run(["gh", "pr", "view", "2", "--repo", REPO,
                       "--json", "state,mergedAt,mergeCommit"])
    try:
        d = json.loads(out)
    except Exception:
        return "• PR #2: (could not fetch)"
    state = d.get("state", "?")
    merged = d.get("mergedAt")
    sha = (d.get("mergeCommit") or {}).get("oid", "")[:9] if d.get("mergeCommit") else ""
    if state == "MERGED":
        return f"🟢 *PR #2:* MERGED at {merged} (sha {sha})."
    return f"🟡 *PR #2:* {state} (not merged)."


def section_prod():
    sv = os.path.join(SC, "ship_verify.py")
    if not os.path.exists(sv):
        return "• Prod check: (ship_verify.py missing)"
    rc, out, err = _run(["python3", sv, "--marker", "JobPosting",
                         "--require-on", SALARY_URL, "--forbid-on", NONSALARY_URL], timeout=120)
    if rc == 0:
        return "🟢 *Prod:* JobPosting emits ONLY on the salary page (fix is live & correct)."
    if rc == 1:
        tail = (out + err).strip().splitlines()
        detail = " ".join(l.strip() for l in tail if "PASS" in l or "FAIL" in l)[:200]
        return f"🟡 *Prod:* JobPosting gating NOT yet correct on live site (likely not deployed). {detail}"
    return "⚪ *Prod:* indeterminate (couldn't fetch the live pages)."


def section_tokens():
    pat = re.compile(r'\[(cron_\w+_(\d{8}_\d{6}))\].*API call #\d+:.* in=(\d+) out=(\d+)')
    runs = {}
    try:
        with open(AGENT_LOG, errors="replace") as f:
            for line in f:
                if "2026-06-14" not in line:
                    continue
                m = pat.search(line)
                if not m:
                    continue
                sid, ts, ins, outs = m.groups()
                if ts < BASELINE_UTC:
                    continue
                r = runs.setdefault(sid, [0, 0, 0])
                r[0] += 1; r[1] += int(ins); r[2] += int(outs)
    except Exception as e:
        return f"• Token burn: (failed: {e})"
    if not runs:
        return "• *Token burn since check-in scheduled:* 0 — no executor runs fired since ~10:39 PT."
    tin = sum(r[1] for r in runs.values()); tout = sum(r[2] for r in runs.values())
    return (f"• *Token burn since ~10:39 PT:* {tin/1e6:.2f}M in / {tout/1e3:.0f}K out "
            f"across {len(runs)} run(s).")


def section_alerts():
    try:
        s = json.load(open(os.path.join(HOME, ".hermes/scripts/.usage_alert_state.json")))
        la = s.get("last_alert_ts", 0)
        # baseline epoch for 2026-06-14 17:39 UTC
        base = datetime(2026, 6, 14, 17, 39, tzinfo=timezone.utc).timestamp()
        if la and la >= base:
            when = datetime.fromtimestamp(la).strftime("%H:%M")
            return f"🔔 A new usage-limit alert fired at {when} since scheduling."
        return "• No new usage-limit alerts since scheduling."
    except Exception:
        return "• Usage alerts: (state unavailable)"


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
    parts = [
        f"📋 *FSS deploy task check-in* — {now}",
        "(scheduled after MiniMax quota reset to see if the autonomous loop shipped 86e1vrh4t)",
        "",
        section_quota(),
        section_task(),
        section_pr(),
        section_prod(),
        section_tokens(),
        section_alerts(),
        "",
        "_If PR #2 is still open / prod not gated: the executor refactor (slim skill, "
        "max_turns→30, drop Gemini fallback, prompt caching) likely needs to land before "
        "a run can complete cleanly. See brain note 'Hermes Autonomous Ship Loop'._",
    ]
    print("\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
