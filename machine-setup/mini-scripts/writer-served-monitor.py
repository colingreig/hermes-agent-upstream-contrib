#!/usr/bin/env python3
"""
writer-served-monitor.py — WRITER-LIVENESS (2026-06-25)

Reads ~/.hermes/logs/writer-served.jsonl and surfaces silent Codex→GLM degrades:
an armed run (HERMES_WRITER_CODEX=1) where served_model != openai/gpt-5.4.

Mirrors ci_health_watch.py alert conventions exactly:
  HERMES_BIN send --to slack:hermes <msg>
  DRY_RUN env var honored
  Alert only on STATE TRANSITIONS (healthy→degraded, degraded→healthy) via
  ~/.hermes/state/writer-served-monitor.json atomic-json dedup.

Usage:
  writer-served-monitor.py              # print human summary to stdout, exit 1 if degraded
  writer-served-monitor.py --json       # emit JSON summary to stdout
  writer-served-monitor.py --alert      # same + send Slack alert on state change
  writer-served-monitor.py --window 100 # consider last N records (default: 50)
  DRY_RUN=1 writer-served-monitor.py --alert   # test without posting to Slack

Exit codes: 0 = healthy (or no data), 1 = currently degraded
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

LEDGER_PATH  = os.path.expanduser("~/.hermes/logs/writer-served.jsonl")
STATE_PATH   = os.path.expanduser("~/.hermes/state/writer-served-monitor.json")
HERMES_BIN   = os.path.expanduser("~/.local/bin/hermes")
EXPECTED_PRIMARY = "openai/gpt-5.4"
DEFAULT_WINDOW   = 50   # last N records (most recent)


# ── helpers ─────────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _send_slack(msg):
    if os.environ.get("DRY_RUN"):
        print(f"[writer-served-monitor] DRY_RUN slack:\n{msg}")
        return
    try:
        subprocess.run(
            [HERMES_BIN, "send", "--to", "slack:hermes", msg],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        print(f"[writer-served-monitor] slack send failed: {e!r}", file=sys.stderr)


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(obj):
    tmp = STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _read_ledger(window):
    """Read and parse the JSONL ledger. Returns the last `window` valid records."""
    if not os.path.isfile(LEDGER_PATH):
        return []
    records = []
    try:
        with open(LEDGER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # tolerate corrupt/partial trailing lines
    except Exception as e:
        print(f"[writer-served-monitor] ledger read error: {e!r}", file=sys.stderr)
        return []
    return records[-window:]


# ── analysis ────────────────────────────────────────────────────────────────

def _analyze(records):
    """Build a summary dict from the windowed records."""
    total = len(records)
    if total == 0:
        return {"no_data": True, "total": 0}

    armed_runs     = [r for r in records if r.get("armed")]
    degraded_runs  = [r for r in records if r.get("degraded")]
    codex_served   = [r for r in armed_runs if r.get("served_model") == EXPECTED_PRIMARY]
    glm_served     = [r for r in degraded_runs if (r.get("served_model") or "").startswith("zai-coding/")]
    other_degraded = [r for r in degraded_runs if not (r.get("served_model") or "").startswith("zai-coding/")]

    # "currently degraded" = most recent ARMED run was a degrade
    last_armed = None
    for r in reversed(records):
        if r.get("armed"):
            last_armed = r
            break

    currently_degraded = bool(last_armed and last_armed.get("degraded"))

    # streak: how many consecutive trailing armed runs were degraded
    degrade_streak = 0
    for r in reversed(records):
        if not r.get("armed"):
            continue
        if r.get("degraded"):
            degrade_streak += 1
        else:
            break

    last_rec = records[-1]

    return {
        "no_data":            False,
        "total_records":      total,
        "armed_runs":         len(armed_runs),
        "codex_served":       len(codex_served),
        "glm_served":         len(glm_served),
        "other_degraded":     len(other_degraded),
        "degraded_runs":      len(degraded_runs),
        "degrade_streak":     degrade_streak,
        "currently_degraded": currently_degraded,
        "last_ts":            last_rec.get("ts"),
        "last_served_model":  last_rec.get("served_model"),
        "last_armed":         last_rec.get("armed"),
        "last_task_id":       last_rec.get("task_id"),
        "last_degraded_model": (last_armed.get("served_model") if currently_degraded and last_armed else None),
        "expected_primary":   EXPECTED_PRIMARY,
    }


# ── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Writer-served liveness monitor")
    ap.add_argument("--json",   action="store_true", help="Emit JSON summary")
    ap.add_argument("--alert",  action="store_true", help="Send Slack alert on state transition")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help=f"Number of most-recent records to consider (default: {DEFAULT_WINDOW})")
    args = ap.parse_args()

    records = _read_ledger(args.window)
    summary = _analyze(records)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        if summary.get("no_data"):
            print("[writer-served-monitor] No data — ledger is empty or missing.")
        else:
            status = "DEGRADED" if summary["currently_degraded"] else "HEALTHY"
            print(f"[writer-served-monitor] STATUS: {status}")
            print(f"  Window: last {args.window} records ({summary['total_records']} found)")
            print(f"  Armed runs: {summary['armed_runs']}  "
                  f"Codex-served: {summary['codex_served']}  "
                  f"GLM-served: {summary['glm_served']}  "
                  f"Other-degraded: {summary['other_degraded']}")
            if summary["currently_degraded"]:
                print(f"  !! Degrade streak: {summary['degrade_streak']} armed run(s)")
                print(f"  !! Last served: {summary['last_degraded_model']} (expected {EXPECTED_PRIMARY})")
            print(f"  Last run: {summary['last_ts']}  model={summary['last_served_model']}  task={summary['last_task_id']}")

    if args.alert:
        prev_state  = _load_state()
        was_degraded = prev_state.get("degraded", None)  # None = first run (no prior state)
        is_degraded  = summary.get("currently_degraded", False)
        no_data      = summary.get("no_data", True)

        transition = None
        if not no_data:
            if was_degraded is None:
                # First run — record state, alert only if currently degraded
                if is_degraded:
                    transition = "degraded"
            elif not was_degraded and is_degraded:
                transition = "degraded"   # healthy → degraded
            elif was_degraded and not is_degraded:
                transition = "recovered"  # degraded → healthy

        if transition == "degraded":
            served  = summary.get("last_degraded_model", "unknown")
            streak  = summary.get("degrade_streak", 0)
            n_arm   = summary.get("armed_runs", 0)
            n_codex = summary.get("codex_served", 0)
            msg = (
                f":rotating_light: *Codex writer silently degraded to `{served}`* — "
                f"armed but not serving `{EXPECTED_PRIMARY}`\n"
                f"  Degrade streak: {streak} consecutive armed run(s)\n"
                f"  Window ({args.window} records): {n_arm} armed, {n_codex} Codex-served, "
                f"{summary.get('degraded_runs',0)} degraded\n"
                f"  _(writer-served-monitor.py)_"
            )
            _send_slack(msg)

        elif transition == "recovered":
            n_arm   = summary.get("armed_runs", 0)
            n_codex = summary.get("codex_served", 0)
            msg = (
                f":white_check_mark: *Codex writer recovered* — now serving `{EXPECTED_PRIMARY}` again\n"
                f"  Window ({args.window} records): {n_arm} armed, {n_codex} Codex-served\n"
                f"  _(writer-served-monitor.py)_"
            )
            _send_slack(msg)

        if not no_data:
            _save_state({
                "generated_at": _now_iso(),
                "degraded":     is_degraded,
                "last_served":  summary.get("last_served_model"),
                "last_ts":      summary.get("last_ts"),
                "transition":   transition,
            })

    # Exit non-zero when currently degraded so a caller/timer can react
    if summary.get("currently_degraded"):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[writer-served-monitor] unexpected: {e!r}", file=sys.stderr)
        print(json.dumps({"error": True, "detail": str(e)}))
        sys.exit(0)  # fail-open: don't crash a cron on a monitor bug
