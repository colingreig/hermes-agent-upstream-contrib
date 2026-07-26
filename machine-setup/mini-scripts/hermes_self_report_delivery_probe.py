#!/usr/bin/env python3
"""
hermes_self_report_delivery_probe.py — deterministic, no-LLM outcome probe for
the hermes-self-report cron.

RC4 postmortem: the hermes-self-report cron job's `last_status` in jobs.json
only reflects whether the LLM turn completed without raising — NOT whether an
email (or its `hermes send` fallback) actually reached Colin. It stayed "ok"
through the entire 2026-07-23 -> 07-26 delivery outage. `postmark_send_report.py`
now writes a delivery receipt to
~/.hermes/logs/hermes-self-report-last-send.json every run (channel, status,
short error text — never the token); this script reads that receipt and turns
"nothing was delivered" (or "the report never even ran") into a real non-zero
exit code / red cron job, independent of what the agent believes happened.

Intended to run as its own small `no_agent` / `script` cron job (see
machine-setup/mini-scripts/README.md's "Files" section for the convention),
on a schedule slightly offset from hermes-self-report's own `0 */6 * * *` so
the receipt has time to land. This file does NOT modify jobs.json itself —
see the README/task notes for the exact job-entry delta to add.

Exit codes:
  0  receipt is fresh and reports a successful delivery (postmark or fallback)
  1  receipt reports a failed delivery (both channels failed)
  2  receipt is missing or stale (older than --max-age-minutes) — the report
     never ran, crashed before reaching Step 3, or the tick was skipped
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

RECEIPT_PATH_DEFAULT = os.path.expanduser("~/.hermes/logs/hermes-self-report-last-send.json")
# hermes-self-report runs `0 */6 * * *` (every 6h); default max age gives a
# generous buffer for a slow tick without letting a genuinely stuck/skipped
# cron go unnoticed for a full extra cycle.
MAX_AGE_MINUTES_DEFAULT = 6 * 60 + 30


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--receipt-path", default=RECEIPT_PATH_DEFAULT)
    p.add_argument("--max-age-minutes", type=int, default=MAX_AGE_MINUTES_DEFAULT)
    args = p.parse_args()

    if not os.path.exists(args.receipt_path):
        print(json.dumps({
            "status": "missing",
            "detail": f"no delivery receipt at {args.receipt_path} — "
                      f"hermes-self-report has never completed Step 3 on this host",
        }))
        return 2

    try:
        with open(args.receipt_path) as f:
            rec = json.load(f)
    except Exception as e:
        print(json.dumps({"status": "unreadable", "detail": f"{args.receipt_path}: {e}"}))
        return 2

    ts_raw = rec.get("ts")
    try:
        ts = datetime.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        print(json.dumps({"status": "bad-timestamp", "detail": f"unparseable ts: {ts_raw!r}"}))
        return 2

    now = datetime.datetime.now(datetime.timezone.utc)
    age_min = (now - ts).total_seconds() / 60.0
    if age_min > args.max_age_minutes:
        print(json.dumps({
            "status": "stale",
            "age_minutes": round(age_min, 1),
            "max_age_minutes": args.max_age_minutes,
            "detail": f"last delivery receipt is {age_min:.0f}m old (limit {args.max_age_minutes}m) "
                      f"— hermes-self-report may have stopped running or stopped reaching Step 3",
        }))
        return 2

    channel = rec.get("channel")
    rec_status = rec.get("status")
    if rec_status != "sent" or channel in (None, "none"):
        print(json.dumps({
            "status": "delivery-failed",
            "age_minutes": round(age_min, 1),
            "channel": channel,
            "detail": rec.get("detail"),
        }))
        return 1

    print(json.dumps({
        "status": "ok",
        "age_minutes": round(age_min, 1),
        "channel": channel,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
