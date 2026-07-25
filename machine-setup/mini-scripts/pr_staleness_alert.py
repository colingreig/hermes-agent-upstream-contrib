#!/usr/bin/env python3
"""pr_staleness_alert.py — notify Colin on Slack when an open PR on an
allowlisted repo sits for more than the no-verdict SLA (6h) without a fresh
validator verdict.

This is a thin, Slack-delivered cron wrapper around
pr_pipeline_improvements.check_staleness_and_alert() — the check itself
already existed, but it used to run only inside review_poll_gate.py, a job
configured with `deliver: "local"`. The check computed the right answer and
printed it, but that stdout never reached Slack, so the alert silently never
fired from a human's point of view. This script follows the exact
hermes_usage_alert.py pattern instead: a small, cheap, print-only script
whose stdout is forwarded to Slack by the cron job's own `deliver` config
(deliver: "slack:UN4CQ1EGG", same channel as hermes-usage-alert and
spend-meter) — no webhook POST from inside the script itself. Silent when
there is nothing to report; state-deduped via .pr_pipeline_state.json so a
given PR is not re-alerted on every 15-minute tick.

The staleness check itself (STALE_WINDOW = 6h) lives in
pr_wake_and_sweep.py / pr_pipeline_improvements.py — this file owns nothing
but the cron-delivery wiring.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autonomous_merge
import pr_pipeline_improvements as ppi


def main() -> int:
    try:
        allowlist = sorted(autonomous_merge._load_allowlist())
    except Exception as exc:
        print(f"[pr-staleness-alert] failed to load allowlist: {exc}", file=sys.stderr)
        return 1
    if not allowlist:
        print("[pr-staleness-alert] allowlist is empty — nothing to check", file=sys.stderr)
        return 0
    ppi.check_staleness_and_alert(allowlist, dry_run=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
