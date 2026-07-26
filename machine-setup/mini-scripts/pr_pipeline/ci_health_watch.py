#!/usr/bin/env python3
"""CI-health watcher — cheap, deterministic, zero-LLM. Closes the observability
gap where a red `main` check (esp. scheduled / non-gating workflows) rots unseen.

For each allowlisted repo it asks GitHub for recent workflow runs on the default
branch, takes the LATEST run per workflow, and flags any whose conclusion is
`failure`. Alerts to slack:hermes ONLY on CHANGE vs the last poll (newly-red or
recovered) so it never spams. $0 when idle (gh API only). Never raises out of main().

State: ~/.hermes/scripts/.ci_health_state.json   Alerts via `hermes send`.
Wire as a cron (no-agent). Auth: gh CLI from the environment.
"""
import json, os, subprocess, sys
from datetime import datetime, timezone

ALLOWED = os.path.expanduser("~/.hermes/allowed-repos.txt")
STATE_PATH = os.path.expanduser("~/.hermes/scripts/.ci_health_state.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
MAIN_BRANCHES = {"main", "master"}
RUNS_PER_REPO = 40          # window to find latest-per-workflow + failure streak
GH_TIMEOUT = 45

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _repos():
    try:
        with open(ALLOWED, encoding="utf-8") as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except Exception as e:
        print(f"[ci-health] cannot read {ALLOWED}: {e!r}", file=sys.stderr)
        return []

def _gh_runs(repo):
    """Latest runs for repo as a list of dicts, newest first. [] on any error."""
    try:
        r = subprocess.run(
            ["gh", "run", "list", "--repo", repo, "-L", str(RUNS_PER_REPO),
             "--json", "workflowName,conclusion,status,createdAt,event,headBranch,url"],
            capture_output=True, text=True, timeout=GH_TIMEOUT)
        if r.returncode != 0:
            print(f"[ci-health] {repo}: gh rc={r.returncode} {r.stderr.strip()[:80]}", file=sys.stderr)
            return []
        return json.loads(r.stdout or "[]")
    except Exception as e:
        print(f"[ci-health] {repo}: {e!r}", file=sys.stderr)
        return []

# Events that GATE the branch — i.e. that tell you whether main's COMMITTED code
# is sound. A failure on one of these means "don't merge onto red main." Other
# events (workflow_run / schedule / workflow_dispatch / dynamic) are post-deploy
# or monitoring runs: a scheduled prod-targeting smoke ("E2E Smoke — Report
# Outage") failing means PROD is unhealthy, NOT that main is unmergeable. Letting
# such a monitor gate merges created a permanent self-referential deadlock on
# elevatoruptime.com (push-CI green, smoke red forever → nothing could ever land).
GATING_EVENTS = {"push", "merge_group", "pull_request"}


def _red_workflows(repo, gating_only=False):
    """Return {workflow_name: {since, count, url}} for workflows whose LATEST
    completed run on a main branch failed.

    gating_only=True (the MERGE gate's view): consider only branch-gating events
    (GATING_EVENTS) so monitoring/scheduled/post-deploy smokes never block a
    merge. gating_only=False (the monitoring cron's view): consider everything so
    a rotting scheduled red still surfaces an alert."""
    runs = [x for x in _gh_runs(repo)
            if (x.get("headBranch") in MAIN_BRANCHES)
            and str(x.get("status")) == "completed"
            and (not gating_only or (x.get("event") in GATING_EVENTS))]
    by_wf = {}
    for x in runs:                       # runs are newest-first
        by_wf.setdefault(x.get("workflowName") or "?", []).append(x)
    red = {}
    for wf, xs in by_wf.items():
        if not xs or xs[0].get("conclusion") != "failure":
            continue
        # how long has it been red: count leading failures, find when streak began
        streak = 0
        since = xs[0].get("createdAt", "")[:10]
        for x in xs:
            if x.get("conclusion") == "failure":
                streak += 1
                since = x.get("createdAt", "")[:10]
            else:
                break
        red[wf] = {"since": since, "count": streak, "url": xs[0].get("url", "")}
    return red

def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state(obj):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, STATE_PATH)

def _send_slack(msg):
    if os.environ.get("DRY_RUN"):
        print(f"[ci-health] DRY_RUN slack:\n{msg}")
        return
    try:
        subprocess.run([HERMES_BIN, "send", "--to", "slack:hermes", msg],
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[ci-health] slack send failed: {e!r}", file=sys.stderr)

def main():
    repos = _repos()
    if not repos:
        print(json.dumps({"checked": 0, "red": 0})); return 0

    current = {}   # "repo::workflow" -> info
    for repo in repos:
        for wf, info in _red_workflows(repo).items():
            current[f"{repo}::{wf}"] = info

    prev = _load_state().get("red", {})
    newly_red = [k for k in current if k not in prev]
    recovered = [k for k in prev if k not in current]

    lines = []
    if newly_red:
        lines.append(f"🔴 *CI went red on `main`* ({len(newly_red)}):")
        for k in sorted(newly_red):
            repo, wf = k.split("::", 1)
            i = current[k]
            lines.append(f"  • `{repo}` / {wf} — failing since {i['since']} ({i['count']} run(s)) {i['url']}")
    if recovered:
        lines.append(f"🟢 *CI recovered* ({len(recovered)}): " +
                     ", ".join(f"`{k.split('::',1)[0]}` / {k.split('::',1)[1]}" for k in sorted(recovered)))

    if lines:
        total = len(current)
        lines.append(f"_(total still red: {total} across {len({k.split('::',1)[0] for k in current})} repo(s) — `ci_health_watch.py`)_")
        _send_slack("\n".join(lines))

    _save_state({"generated_at": _now_iso(), "red": current})
    # Summary to STDERR only (logs/debug). STDOUT stays EMPTY so the no-agent
    # cron is silent; real alerts go via hermes send (above). This keeps the
    # classic watchdog "empty stdout = silent" contract.
    print(json.dumps({"checked": len(repos), "red": len(current),
                      "newly_red": len(newly_red), "recovered": len(recovered)}),
          file=sys.stderr)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[ci-health] unexpected: {e!r}", file=sys.stderr)
        print(json.dumps({"error": True})); sys.exit(0)
