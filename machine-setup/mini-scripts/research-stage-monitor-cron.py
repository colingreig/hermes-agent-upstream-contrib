#!/usr/bin/env python3
"""research-stage-monitor-cron.py — thin cron wrapper around
`research_stage_monitor.py --quiet-when-healthy`.

Why this exists: mini `no_agent` cron jobs cannot pass script arguments —
`argv` is hardcoded to `[interpreter, path]` in `cron/scheduler.py` (~line
2029), with no way to append flags. `research_stage_monitor.py` only
suppresses stdout on a healthy result when called with
`--quiet-when-healthy` (see its own docstring: those cron jobs deliver a
message on ANY non-empty stdout, so without that flag every tick would post
regardless of health). This wrapper bakes the flag in so the cron job can
invoke a bare, argument-free script and still get quiet-on-healthy behavior.

It does nothing else: it resolves the monitor as a sibling file next to
this wrapper (both are deployed side by side to `~/.hermes/scripts/` on the
mini and live side by side in `machine-setup/mini-scripts/` in this repo —
never hardcode an absolute path here), runs it with `--quiet-when-healthy`,
and propagates its stdout, stderr, and exit code verbatim. All actual
monitor logic (ledger evaluation, exit-code mapping) lives in
`research_stage_monitor.py`; see that file's docstring for the full
0/2/3/4 exit-code contract.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

MONITOR_NAME = "research_stage_monitor.py"


def main() -> int:
    monitor_path = Path(__file__).resolve().with_name(MONITOR_NAME)
    if not monitor_path.is_file():
        print(
            f"[research-stage-monitor-cron] missing sibling script: {monitor_path} "
            "not found — cannot run the research-stage monitor."
        )
        return 1

    result = subprocess.run(
        [sys.executable, str(monitor_path), "--quiet-when-healthy"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
