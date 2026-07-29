#!/usr/bin/env python3
"""
daily_spend_alert.py — daily cron wiring for SessionDB.check_daily_spend_alerts()
(86e260vnu). Companion to spend_meter.py (which covers OpenCode/opencode_exec.py
spend from log files) — this one covers Hermes gateway/orchestrator session spend
recorded directly in state.db via SessionDB.

Why this exists: the validator flagged get_daily_provider_spend()/
check_daily_spend_alerts() (added to hermes_state.py under PR #1) as dormant —
called from nothing outside their own test file. This script is that missing
caller, scheduled via a dedicated launchd job (com.colingreig.hermes.daily-spend-alert)
rather than folded into the LLM-driven hermes-self-report skill, so the alert
doesn't depend on an LLM prompt being followed correctly on any given day.

Usage:
    python3 daily_spend_alert.py                  # today, real threshold
    python3 daily_spend_alert.py --threshold 0.01  # forced-low-threshold test
    python3 daily_spend_alert.py --date 2026-07-05 --dry-run

Env:
    HERMES_PROVIDER_DAILY_ALERT_USD   default $10/day/provider (same env var
                                       spend_meter.py uses, kept consistent)
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from datetime import date

HERMES_AGENT_CHECKOUT = os.path.expanduser("~/.hermes/runtime-current")
HERMES_BIN = os.path.expanduser("~/.hermes/bin/hermes") if os.path.isfile(
    os.path.expanduser("~/.hermes/bin/hermes")
) else "hermes"


def _load_session_db_class():
    # hermes_state.py imports sibling packages (e.g. `agent`) by absolute
    # import, so the checkout root must be on sys.path, not just the one file
    # loaded via spec_from_file_location.
    if HERMES_AGENT_CHECKOUT not in sys.path:
        sys.path.insert(0, HERMES_AGENT_CHECKOUT)
    spec = importlib.util.spec_from_file_location(
        "hermes_state", os.path.join(HERMES_AGENT_CHECKOUT, "hermes_state.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SessionDB


def _send_slack(msg: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"[daily_spend_alert] DRY_RUN slack:\n{msg}")
        return True
    try:
        r = subprocess.run([HERMES_BIN, "send", "--to", "slack:hermes", msg],
                            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            sys.stderr.write(f"[daily_spend_alert] slack send failed: {r.stderr}\n")
        return r.returncode == 0
    except Exception as e:
        sys.stderr.write(f"[daily_spend_alert] slack send failed: {e!r}\n")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--threshold", type=float,
                     default=float(os.environ.get("HERMES_PROVIDER_DAILY_ALERT_USD", "10.0")))
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending to Slack")
    args = ap.parse_args()

    day = args.date or date.today().strftime("%Y-%m-%d")

    try:
        SessionDB = _load_session_db_class()
        db = SessionDB(read_only=True)
        alerts = db.check_daily_spend_alerts(day, threshold_usd=args.threshold)
    except Exception as e:
        sys.stderr.write(f"[daily_spend_alert] FATAL: could not compute spend alerts: {e!r}\n")
        return 1

    if not alerts:
        print(f"[daily_spend_alert] {day} OK — no provider over ${args.threshold:.2f}/day "
              "(gateway/orchestrator sessions only; see spend_meter.py for OpenCode spend)")
        return 0

    lines = [f":warning: Hermes daily spend alert ({day}, cap=${args.threshold:.2f}/provider, "
             "gateway/orchestrator sessions):"]
    for a in alerts:
        lines.append(f"  - `{a['provider']}`: ${a['spend_usd']:.2f}")
    msg = "\n".join(lines)
    print(msg)
    ok = _send_slack(msg, args.dry_run)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
