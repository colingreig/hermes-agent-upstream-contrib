#!/usr/bin/env python3
"""hermes-metrics.py — cost + task-completion metrics for the autonomous stack.

The North-Star metric Hermes never surfaced: cost-per-task and completion-rate,
NOT "crons green / uptime". `hermes insights` shows sessions/tokens by model only.
This joins the two sides that were never joined:

  • COST/USAGE  — from ~/.hermes/state.db `sessions`. Cron sessions store tokens
    but leave cost_status='unknown' because the default stack runs on
    subscription-FLAT providers (GLM Coding Plan, MiniMax, gpt-5-mini) that have
    no per-token price. So the honest cost signals are: (a) METERED $ for the
    rungs that DO have pricing (Anthropic/OpenAI overflow), computed with Hermes's
    OWN pricing module so numbers match its accounting, and (b) TOKEN VOLUME as
    the flat-plan consumption proxy.
  • COMPLETION  — from the ClickUp board (the durable source; the ~/.hermes/state
    claims dir is ephemeral, ~6 ids). Throughput, completion-rate, stuck count.

  • cost-per-task = window metered $ / tasks completed in window
    tokens-per-task = window tokens / tasks completed in window (the flat-plan unit)

Read-only. No writes, no core edits, no patch-treadmill surface. ClickUp 86e2530dz.

Usage:
  ~/.hermes/runtime-current/venv/bin/python ~/.hermes/scripts/hermes-metrics.py [--days N] [--list <id>] [--json]
  (Any python3 works; the hermes venv gives accurate metered cost. Needs
   CLICKUP_API_TOKEN in env for the completion section — skipped cleanly if absent.)
"""
import argparse, json, os, sqlite3, sys, time, urllib.request, urllib.error
from collections import defaultdict

HERMES   = os.path.expanduser("~/.hermes")
REPO     = os.path.join(HERMES, "runtime-current")
STATE_DB = os.path.join(HERMES, "state.db")
DEFAULT_LIST = "901714465284"  # AI Dev Assistant
AUTONOMOUS_SOURCES = ("cron", "subagent")  # cron executors + the workers they fan out


# ── cost engine: prefer Hermes's own pricing module for consistency ──────────
def load_pricer():
    try:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        from agent.usage_pricing import estimate_usage_cost, CanonicalUsage
        return estimate_usage_cost, CanonicalUsage
    except Exception:
        return None, None


def session_cost(pricer, Canon, r):
    """Return (usd_or_None, status). Prefers live pricing; falls back to stored."""
    if pricer and Canon:
        try:
            u = Canon(
                input_tokens=r["input_tokens"] or 0,
                output_tokens=r["output_tokens"] or 0,
                cache_read_tokens=r["cache_read_tokens"] or 0,
                cache_write_tokens=r["cache_write_tokens"] or 0,
                reasoning_tokens=r["reasoning_tokens"] or 0,
            )
            res = pricer(r["model"] or "", u, provider=r["billing_provider"],
                         base_url=r["billing_base_url"])
            if res.amount_usd is not None:
                return float(res.amount_usd), res.status  # includes $0 for 'included'
        except Exception:
            pass
    for k in ("actual_cost_usd", "estimated_cost_usd"):
        if r[k] is not None:
            return float(r[k]), "stored"
    return None, "unknown"


def job_name(title):
    """Cron sessions title as '<job> · <stamp>'; strip the stamp to the job."""
    if not title:
        return "(untitled)"
    return title.split(" · ")[0].strip() or "(untitled)"


def collect_cost(days):
    cutoff = time.time() - days * 86400
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = ("SELECT id, source, model, title, billing_provider, billing_base_url, "
         "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
         "reasoning_tokens, actual_cost_usd, estimated_cost_usd, cost_status "
         "FROM sessions WHERE source IN (%s) AND started_at >= ? AND archived=0"
         % ",".join("?" * len(AUTONOMOUS_SOURCES)))
    rows = con.execute(q, (*AUTONOMOUS_SOURCES, cutoff)).fetchall()
    con.close()

    pricer, Canon = load_pricer()
    by_job = defaultdict(lambda: {"sessions": 0, "in": 0, "out": 0, "cache": 0,
                                  "metered_usd": 0.0, "unpriced": 0})
    by_model = defaultdict(lambda: {"sessions": 0, "tokens": 0, "metered_usd": 0.0})
    tot = {"sessions": 0, "in": 0, "out": 0, "cache": 0, "metered_usd": 0.0,
           "unpriced": 0, "priced_engine": pricer is not None}
    for r in rows:
        usd, status = session_cost(pricer, Canon, r)
        toks_io = (r["input_tokens"] or 0) + (r["output_tokens"] or 0)
        cache = (r["cache_read_tokens"] or 0) + (r["cache_write_tokens"] or 0)
        j = by_job[job_name(r["title"]) if r["source"] == "cron" else f"(subagent)"]
        j["sessions"] += 1; j["in"] += r["input_tokens"] or 0
        j["out"] += r["output_tokens"] or 0; j["cache"] += cache
        m = by_model[r["model"] or "(none)"]
        m["sessions"] += 1; m["tokens"] += toks_io + cache
        tot["sessions"] += 1; tot["in"] += r["input_tokens"] or 0
        tot["out"] += r["output_tokens"] or 0; tot["cache"] += cache
        if usd is not None and status != "unknown":
            j["metered_usd"] += usd; m["metered_usd"] += usd; tot["metered_usd"] += usd
        else:
            j["unpriced"] += 1; tot["unpriced"] += 1
    return by_job, by_model, tot


# ── completion side: ClickUp board is the durable source ─────────────────────
def collect_completion(list_id, days):
    token = None
    try:
        from agent import lazy_secret_resolver
        token = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        token = None
    if not token:
        token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        return None
    cutoff_ms = int((time.time() - days * 86400) * 1000)
    tasks, page = [], 0
    while True:
        url = (f"https://api.clickup.com/api/v2/list/{list_id}/task"
               f"?subtasks=true&include_closed=true&page={page}")
        req = urllib.request.Request(url, headers={"Authorization": token})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, TimeoutError) as e:
            return {"error": str(e)}
        batch = data.get("tasks", [])
        tasks.extend(batch)
        if data.get("last_page", True) or not batch:
            break
        page += 1
    by_status = defaultdict(int)
    done_in_window = 0
    stuck = 0
    now_ms = time.time() * 1000
    DONE = {"complete", "done", "closed"}
    for t in tasks:
        st = (t.get("status", {}) or {}).get("status", "").lower()
        by_status[st] += 1
        dd = t.get("date_done") or t.get("date_closed")
        if st in DONE and dd and int(dd) >= cutoff_ms:
            done_in_window += 1
        if st in ("in progress", "in review", "review") and t.get("date_updated"):
            if now_ms - int(t["date_updated"]) > 24 * 3600 * 1000:
                stuck += 1
    total = sum(by_status.values())
    done_total = sum(v for k, v in by_status.items() if k in DONE)
    # completion-rate = terminal-complete / (everything that isn't fresh backlog).
    denom = total - by_status.get("to do", 0) - by_status.get("backlog", 0)
    rate = (done_total / denom) if denom else None
    return {"total": total, "by_status": dict(by_status), "done_total": done_total,
            "done_in_window": done_in_window, "stuck": stuck,
            "completion_rate": rate, "denom": denom}


def fmt_usd(x):
    return f"${x:,.2f}" if x else "$0.00"


def fmt_tok(n):
    for unit in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}"
        n /= 1000
    return f"{n:.1f}T"


def main():
    ap = argparse.ArgumentParser(description="Hermes cost + completion metrics (read-only).")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--list", default=DEFAULT_LIST, help="ClickUp list id (default: AI Dev Assistant)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    by_job, by_model, tot = collect_cost(args.days)
    comp = collect_completion(args.list, args.days)

    throughput = comp.get("done_in_window") if isinstance(comp, dict) else None
    cost_per_task = (tot["metered_usd"] / throughput) if throughput else None
    tot_tokens = tot["in"] + tot["out"] + tot["cache"]
    tokens_per_task = (tot_tokens / throughput) if throughput else None

    if args.json:
        print(json.dumps({
            "window_days": args.days, "cost": {"by_job": by_job, "by_model": by_model, "totals": tot},
            "completion": comp, "cost_per_task_usd": cost_per_task,
            "tokens_per_task": tokens_per_task,
        }, indent=2, default=float))
        return

    W = 64
    print(f"\n╔{'═'*W}╗")
    print(f"║  HERMES AUTONOMOUS METRICS — last {args.days}d{' '*(W-27-len(str(args.days)))}║")
    print(f"╚{'═'*W}╝")

    print(f"\n── COST / USAGE (state.db, sources={'+'.join(AUTONOMOUS_SOURCES)}) ──")
    if not tot["priced_engine"]:
        print("  ! Hermes pricing module not importable — cost is from stored DB values only.")
    print(f"  {'job':<26}{'sess':>5}{'tok(in/out/cache)':>22}{'metered$':>10}")
    for job, d in sorted(by_job.items(), key=lambda kv: -(kv[1]['in']+kv[1]['out']+kv[1]['cache'])):
        io = f"{fmt_tok(d['in'])}/{fmt_tok(d['out'])}/{fmt_tok(d['cache'])}"
        print(f"  {job[:26]:<26}{d['sessions']:>5}{io:>22}{fmt_usd(d['metered_usd']):>10}")
    print(f"  {'─'*62}")
    print(f"  {'TOTAL':<26}{tot['sessions']:>5}"
          f"{fmt_tok(tot['in'])+'/'+fmt_tok(tot['out'])+'/'+fmt_tok(tot['cache']):>22}"
          f"{fmt_usd(tot['metered_usd']):>10}")
    print(f"  metered $ = overflow rungs w/ real pricing; {tot['unpriced']}/{tot['sessions']} "
          f"sessions are flat-plan/unpriced (GLM/MiniMax/etc = $0 by design).")

    print(f"\n── TASK COMPLETION (ClickUp list {args.list}) ──")
    if comp is None:
        print("  ! CLICKUP_API_TOKEN not set — completion section skipped.")
    elif comp.get("error"):
        print(f"  ! ClickUp fetch failed: {comp['error']}")
    else:
        print(f"  board: {comp['total']} tasks — " +
              ", ".join(f"{k}:{v}" for k, v in sorted(comp['by_status'].items())))
        rate = comp["completion_rate"]
        print(f"  completion-rate: {rate*100:.0f}%  ({comp['done_total']} complete / {comp['denom']} non-backlog)"
              if rate is not None else "  completion-rate: n/a")
        print(f"  throughput (completed in {args.days}d): {comp['done_in_window']}")
        print(f"  stuck (in-progress/review >24h): {comp['stuck']}")

    print(f"\n── COST PER TASK (window) ──")
    if throughput:
        print(f"  completed this window: {throughput}")
        print(f"  metered $/task:  {fmt_usd(cost_per_task)}   (near-$0 = flat-plan work; watch for overflow spikes)")
        print(f"  tokens/task:     {fmt_tok(tokens_per_task)}   (the meaningful unit on flat plans)")
    else:
        print("  n/a — no tasks completed in window (or ClickUp unavailable).")
    print()


if __name__ == "__main__":
    main()
