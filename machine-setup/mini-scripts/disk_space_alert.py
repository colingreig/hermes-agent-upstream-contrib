#!/usr/bin/env python3
"""disk_space_alert.py — notify Colin on Slack when the Mac mini's disk gets low
(ClickUp 86e2k3ryc: worktree/kanban/release disk lifecycle).

WHY THIS EXISTS: the mini hit a 100%-disk incident from unbounded growth of
per-task worktrees (~101GB/135 items), kanban scratch workspaces (~26GB/30
items), and un-pruned release directories (~11GB/5 items) — see
``worktree_backstop_sweep.py``, ``kanban_workspace_sweep.py``, and
``scripts/mini-release-cut.sh``'s ``--prune`` for the retention side of the
fix. Retention alone is a leading, not a trailing, indicator: it only helps if
its age thresholds are tight enough for whatever growth actually happens. This
script is the trailing safety net — a cheap, zero-LLM, direct disk-free check
that fires on Slack the moment free space is genuinely low, independent of
whether any retention job ran, ran correctly, or ran in time.

Design mirrors ``hermes_usage_alert.py``: zero-LLM, cheap, cron-friendly,
stays silent when healthy, alerts via ``hermes send --to slack:hermes``, and
persists a JSON state file (cooldown + last-known-good) plus a receipt file so
an external health check can distinguish "ran and healthy" from "didn't run"
(see the ``hermes-silent-monitor-failure-pattern`` / ``degraded-flag-
unsatisfiable-alarm`` lessons — a monitor that can't fail loudly is worse than
none). Re-alerts on a bounded cadence while still low (never goes silent after
the first alert), and separately alerts (once, own cooldown) if the free-space
check itself cannot be performed — an unreadable filesystem stat is itself a
signal, never silently treated as "healthy."
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

import slack_msg_builder as smb

HOME = os.path.expanduser("~")
# The mini's Hermes tree (worktrees/kanban/releases) and the user's home share
# one filesystem, so checking the Hermes home's device is representative and
# avoids depending on any one subdirectory existing.
CHECK_PATH = os.environ.get("HERMES_DISK_ALERT_PATH") or os.path.join(HOME, ".hermes")
if not os.path.isdir(CHECK_PATH):
    CHECK_PATH = HOME

STATE_PATH = os.environ.get("HERMES_DISK_ALERT_STATE_PATH") or os.path.join(
    HOME, ".hermes/scripts/.disk_alert_state.json"
)
RECEIPT_PATH = os.environ.get("HERMES_DISK_ALERT_RECEIPT_PATH") or os.path.join(
    HOME, ".hermes/state/hermes-disk-space-alert.json"
)
HERMES_BIN = os.path.join(HOME, ".local/bin/hermes")
SLACK_TARGET = "slack:hermes"

MIN_FREE_GB = float(os.environ.get("HERMES_DISK_ALERT_MIN_FREE_GB", "5"))
# While genuinely low, re-alert on this cadence rather than going silent after
# the first ping (same rationale as hermes_usage_alert.py's RC1 fix).
LOW_DISK_COOLDOWN_S = int(os.environ.get("HERMES_DISK_ALERT_COOLDOWN_MIN", "60")) * 60
# The "I can't even check" alert gets its own, longer cooldown so a persistently
# broken stat doesn't spam every tick.
CHECK_ERROR_COOLDOWN_S = int(os.environ.get("HERMES_DISK_ALERT_ERROR_COOLDOWN_MIN", "360")) * 60


def _fmt_gb(n: float) -> str:
    return f"{n:.1f}GB"


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
            return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _save_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _save_state(state: dict) -> None:
    _save_json(STATE_PATH, state)


def _write_receipt(*, status: str, delivery: str, free_gb, now: float) -> None:
    _save_json(
        RECEIPT_PATH,
        {
            "status": status,          # "ok" | "low" | "check_error"
            "delivery": delivery,      # "confirmed" | "failed" | "n/a"
            "free_gb": free_gb,
            "min_free_gb": MIN_FREE_GB,
            "check_path": CHECK_PATH,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        },
    )


def _send_slack(message: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [HERMES_BIN, "send", "--to", SLACK_TARGET, message],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _build_message(free_gb: float, total_gb: float) -> str:
    return smb.build_alert_message(
        "🛑",
        f"Mac mini disk space low: {_fmt_gb(free_gb)} free (threshold {_fmt_gb(MIN_FREE_GB)}).",
        facts=[
            f"total capacity: {_fmt_gb(total_gb)}",
            f"check path: {CHECK_PATH}",
        ],
        next_step=(
            "Run worktree_backstop_sweep.py / kanban_workspace_sweep.py --dry-run to see "
            "reclaimable space, or mini-release-cut.sh --prune for old releases."
        ),
        footer=f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
        max_words=70,
    )


def _build_error_message(exc: Exception) -> str:
    return smb.build_alert_message(
        "⚠️",
        "Disk-space monitor couldn't read free space on the mini.",
        facts=[f"check path: {CHECK_PATH}", f"error: {exc}"],
        next_step="Check the filesystem / mount is healthy — this alert is a fail-loud, not a fail-silent, condition.",
        footer=f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
        max_words=60,
    )


def main() -> int:
    if os.environ.get("HERMES_DISK_ALERT_DISABLE") == "1":
        return 0

    state = _load_state()
    now = time.time()

    try:
        usage = shutil.disk_usage(CHECK_PATH)
    except OSError as exc:
        last_error_alert = float(state.get("last_error_alert_ts", 0) or 0)
        if (now - last_error_alert) >= CHECK_ERROR_COOLDOWN_S:
            msg = _build_error_message(exc)
            print(msg, file=sys.stderr)
            try:
                delivery = _send_slack(msg)
            except (OSError, subprocess.SubprocessError) as send_exc:
                print(f"[disk_space_alert] Slack delivery failed: {send_exc}", file=sys.stderr)
                _write_receipt(status="check_error", delivery="failed", free_gb=None, now=now)
                return 2
            if delivery.returncode != 0:
                detail = (delivery.stderr or delivery.stdout or "unknown failure").strip()
                print(f"[disk_space_alert] Slack delivery exited {delivery.returncode}: {detail}", file=sys.stderr)
                _write_receipt(status="check_error", delivery="failed", free_gb=None, now=now)
                return 2
            state["last_error_alert_ts"] = now
            _save_state(state)
            _write_receipt(status="check_error", delivery="confirmed", free_gb=None, now=now)
        else:
            _write_receipt(status="check_error", delivery="n/a", free_gb=None, now=now)
        # A failed disk-space check is always a non-zero exit — this monitor
        # must be able to fail loudly, never silently report clean.
        return 1

    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)

    if free_gb > MIN_FREE_GB:
        _save_state(state)
        _write_receipt(status="ok", delivery="n/a", free_gb=round(free_gb, 2), now=now)
        return 0  # silent — healthy

    last_alert = float(state.get("last_low_alert_ts", 0) or 0)
    if (now - last_alert) < LOW_DISK_COOLDOWN_S:
        # Still low, but within the re-alert cooldown — stay quiet this tick.
        _write_receipt(status="low", delivery="deduped", free_gb=round(free_gb, 2), now=now)
        return 0

    msg = _build_message(free_gb, total_gb)
    print(msg)
    try:
        delivery = _send_slack(msg)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[disk_space_alert] Slack delivery failed: {exc}", file=sys.stderr)
        _write_receipt(status="low", delivery="failed", free_gb=round(free_gb, 2), now=now)
        return 2
    if delivery.returncode != 0:
        detail = (delivery.stderr or delivery.stdout or "unknown failure").strip()
        print(f"[disk_space_alert] Slack delivery exited {delivery.returncode}: {detail}", file=sys.stderr)
        _write_receipt(status="low", delivery="failed", free_gb=round(free_gb, 2), now=now)
        return 2

    state["last_low_alert_ts"] = now
    _save_state(state)
    _write_receipt(status="low", delivery="confirmed", free_gb=round(free_gb, 2), now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
