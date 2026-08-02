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
import re
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

# Raised 5->20 (86e2k3ryc follow-up): the mini hit ENOSPC while sitting on a
# 5GB threshold for 30+ hours of advisory-only alerts — 20GB gives the
# pressure-sweep trigger below (and a human) real runway to act before the
# disk is actually full.
MIN_FREE_GB = float(os.environ.get("HERMES_DISK_ALERT_MIN_FREE_GB", "20"))
# While genuinely low, re-alert on this cadence rather than going silent after
# the first ping (same rationale as hermes_usage_alert.py's RC1 fix).
LOW_DISK_COOLDOWN_S = int(os.environ.get("HERMES_DISK_ALERT_COOLDOWN_MIN", "60")) * 60
# The "I can't even check" alert gets its own, longer cooldown so a persistently
# broken stat doesn't spam every tick.
CHECK_ERROR_COOLDOWN_S = int(os.environ.get("HERMES_DISK_ALERT_ERROR_COOLDOWN_MIN", "360")) * 60

# Pressure-triggered remediation (86e2k3ryc follow-up): advisory Slack alerts
# alone don't reclaim anything. Once free space drops to/under MIN_FREE_GB,
# actually invoke the real sweeps instead of just narrating the problem.
WORKTREE_SWEEP_PATH = os.environ.get("HERMES_DISK_ALERT_WORKTREE_SWEEP_PATH") or os.path.join(
    HOME, ".hermes/scripts/worktree_backstop_sweep.py"
)
KANBAN_SWEEP_PATH = os.environ.get("HERMES_DISK_ALERT_KANBAN_SWEEP_PATH") or os.path.join(
    HOME, ".hermes/scripts/kanban_workspace_sweep.py"
)
# Age gate the worktree sweep uses once its own --min-free-gb pressure check
# trips (it always will here, since we only call it when we're already low).
WORKTREE_SWEEP_PRESSURE_DAYS = int(os.environ.get("HERMES_DISK_ALERT_WORKTREE_PRESSURE_DAYS", "2"))
# Receipt used purely to cool down repeated triggering — independent of, and
# much longer than, LOW_DISK_COOLDOWN_S (which only governs the Slack ping).
TRIGGER_RECEIPT_PATH = os.environ.get("HERMES_DISK_ALERT_TRIGGER_RECEIPT_PATH") or os.path.join(
    HOME, ".hermes/state/disk-pressure-trigger.json"
)
TRIGGER_COOLDOWN_S = int(os.environ.get("HERMES_DISK_ALERT_TRIGGER_COOLDOWN_MIN", "360")) * 60
# Sweeps do real du/git work over potentially many worktrees — generous but bounded.
SWEEP_TIMEOUT_S = int(os.environ.get("HERMES_DISK_ALERT_SWEEP_TIMEOUT_S", "1800"))

_REMOVED_BYTES_RE = re.compile(r"removed_bytes=(\d+)")


def _fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n:.0f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"


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


def _load_trigger_receipt() -> dict:
    try:
        with open(TRIGGER_RECEIPT_PATH, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _run_sweep(cmd: list[str], label: str) -> dict:
    """Run one remediation sweep. Never raises: a failed/timed-out/crashed sweep is
    captured as a result dict, never allowed to break the alert itself."""
    result: dict = {"label": label, "ok": False, "removed_bytes": None, "summary": None, "error": None}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SWEEP_TIMEOUT_S)
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        match = _REMOVED_BYTES_RE.search(output)
        if match:
            result["removed_bytes"] = int(match.group(1))
        finish_lines = [ln.strip() for ln in output.splitlines() if "sweep-finish" in ln]
        if finish_lines:
            result["summary"] = finish_lines[-1][:300]
        result["ok"] = proc.returncode == 0
        if proc.returncode != 0:
            result["error"] = f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        result["error"] = f"timeout after {SWEEP_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        result["error"] = str(exc)
    return result


def _trigger_pressure_sweeps(now: float) -> dict | None:
    """Under disk pressure, run the real worktree/kanban sweeps instead of only
    narrating the problem. Cooldown-gated via TRIGGER_RECEIPT_PATH (independent of,
    and much longer than, the Slack-alert dedupe cooldown) so this doesn't re-run
    heavy du/git work on every 5-minute tick. Returns None if skipped due to
    cooldown; never raises."""
    receipt = _load_trigger_receipt()
    last_trigger = float(receipt.get("last_trigger_ts", 0) or 0)
    if (now - last_trigger) < TRIGGER_COOLDOWN_S:
        return None

    results = [
        _run_sweep(
            [
                sys.executable, WORKTREE_SWEEP_PATH,
                "--min-free-gb", str(MIN_FREE_GB),
                "--pressure-days", str(WORKTREE_SWEEP_PRESSURE_DAYS),
            ],
            "worktree",
        ),
        _run_sweep([sys.executable, KANBAN_SWEEP_PATH], "kanban"),
    ]

    parsed = [r["removed_bytes"] for r in results if r.get("removed_bytes") is not None]
    total_removed_bytes = sum(parsed) if parsed else None

    payload = {
        "last_trigger_ts": now,
        "triggered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "results": results,
        "total_removed_bytes": total_removed_bytes,
    }
    try:
        _save_json(TRIGGER_RECEIPT_PATH, payload)
    except Exception as exc:  # noqa: BLE001 - a receipt-write failure must not break the alert
        print(f"[disk_space_alert] trigger receipt write failed: {exc}", file=sys.stderr)
    return payload


def _build_message(free_gb: float, total_gb: float, trigger_result: dict | None = None) -> str:
    pressure_facts: list[str] = []
    if trigger_result is not None:
        results = trigger_result.get("results") or []
        ok_labels = [r["label"] for r in results if r.get("ok")]
        failed_labels = [r["label"] for r in results if not r.get("ok")]
        total = trigger_result.get("total_removed_bytes")
        if ok_labels:
            reclaimed_str = _fmt_bytes(total) if total is not None else "unknown"
            pressure_facts.append(
                f"pressure sweep triggered ({', '.join(ok_labels)}): reclaimed {reclaimed_str}"
            )
            if total == 0:
                pressure_facts.append(
                    "pressure sweep reclaimed 0 bytes — reclaim path is broken"
                )
        if failed_labels:
            pressure_facts.append(f"pressure sweep FAILED to run: {', '.join(failed_labels)}")

    facts = pressure_facts + [
        f"total capacity: {_fmt_gb(total_gb)}",
        f"check path: {CHECK_PATH}",
    ]

    return smb.build_alert_message(
        "🛑",
        f"Mac mini disk space low: {_fmt_gb(free_gb)} free (threshold {_fmt_gb(MIN_FREE_GB)}).",
        facts=facts,
        next_step=(
            "Run worktree_backstop_sweep.py / kanban_workspace_sweep.py --dry-run to see "
            "reclaimable space, or mini-release-cut.sh --prune for old releases."
        ),
        footer=f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
        max_words=100,
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

    # Pressure remediation runs on its own (long) cooldown, independent of the
    # Slack-alert dedupe below — so real reclaim work happens roughly every
    # TRIGGER_COOLDOWN_S even while the advisory ping itself stays deduped.
    # A trigger failure must never break the alert path itself.
    trigger_result = None
    try:
        trigger_result = _trigger_pressure_sweeps(now)
    except Exception as exc:  # noqa: BLE001 - see docstring on _trigger_pressure_sweeps
        print(f"[disk_space_alert] pressure-sweep trigger errored: {exc}", file=sys.stderr)
        trigger_result = None

    last_alert = float(state.get("last_low_alert_ts", 0) or 0)
    if (now - last_alert) < LOW_DISK_COOLDOWN_S:
        # Still low, but within the re-alert cooldown — stay quiet this tick.
        _write_receipt(status="low", delivery="deduped", free_gb=round(free_gb, 2), now=now)
        return 0

    msg = _build_message(free_gb, total_gb, trigger_result)
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
