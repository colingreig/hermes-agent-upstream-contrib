#!/usr/bin/env python3
"""Bare cron entrypoint for CI health plus the daily PR-staleness scan.

Mini ``no_agent`` cron jobs invoke scripts without argv.  This wrapper keeps
``pr_pipeline/ci_health_watch.py`` unchanged and always runs it with no
arguments, forwarding its stdout, stderr, and exit code.  At most once per
rolling 24-hour window it also runs the existing
``pr_pipeline/pr_staleness_alert.py`` entrypoint.

The staleness child retains ownership of repository scanning, staleness
selection, message construction, and its own coarse alert dedupe.  Its legacy
webhook delivery is disabled in the child so any emitted non-clean result has
one delivery path: the same ``hermes send --to slack:hermes`` target used by
the CI health watcher.

The outer cadence gate records the attempt atomically before starting the
staleness child.  Its state is deliberately distinct from
``pr_staleness_last.json``, which remains the staleness script's alert-dedupe
state.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CI_HEALTH_PATH = Path(__file__).resolve().parent / "pr_pipeline" / "ci_health_watch.py"
PR_STALENESS_PATH = Path(__file__).resolve().parent / "pr_pipeline" / "pr_staleness_alert.py"
DAILY_STATE_PATH = Path.home() / ".hermes/state/ci-health-pr-staleness-last-run.json"
FLEET_PROBE_RECEIPT = Path.home() / ".hermes/state/fleet-outcome-probe.json"
FLEET_WATCHDOG_STATE = Path.home() / ".hermes/state/ci-health-fleet-probe-watchdog.json"
HERMES_BIN = Path.home() / ".local/bin/hermes"
SLACK_TARGET = "slack:hermes"
DAILY_INTERVAL = timedelta(hours=24)
FLEET_PROBE_MAX_AGE = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _claim_daily_scan(*, now: datetime | None = None, state_path: Path | None = None) -> bool:
    current = (now or _now()).astimezone(timezone.utc)
    path = state_path or DAILY_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous = _parse_time(_load_state(path).get("last_attempt_at"))
        if previous is not None and current - previous < DAILY_INTERVAL:
            return False
        _atomic_json(path, {"last_attempt_at": current.isoformat()})
        return True


def _run_ci_health() -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(CI_HEALTH_PATH)],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result


def _send_slack(message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HERMES_BIN), "send", "--to", SLACK_TARGET, message],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_daily_staleness() -> None:
    if not PR_STALENESS_PATH.is_file():
        print(
            f"[ci-health-watch-cron] missing PR-staleness script: {PR_STALENESS_PATH}",
            file=sys.stderr,
        )
        return

    child_env = os.environ.copy()
    child_env.pop("SLACK_WEBHOOK_URL", None)
    result = subprocess.run(
        [sys.executable, str(PR_STALENESS_PATH)],
        capture_output=True,
        text=True,
        env=child_env,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(
            f"[ci-health-watch-cron] PR-staleness scan exited {result.returncode}",
            file=sys.stderr,
        )
        return

    message = result.stdout.strip()
    # The staleness authority emits a green resolution when a previously
    # stale set becomes clean.  The folded check is alert-only, so keep that
    # clean transition silent on this channel.
    if not message or message.lstrip().startswith("✅"):
        return
    try:
        delivery = _send_slack(message)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[ci-health-watch-cron] Slack delivery failed: {exc}", file=sys.stderr)
        return
    if delivery.returncode != 0:
        detail = delivery.stderr.strip()
        suffix = f": {detail}" if detail else ""
        print(
            f"[ci-health-watch-cron] Slack delivery exited {delivery.returncode}{suffix}",
            file=sys.stderr,
        )


def _fleet_probe_problem(*, now: datetime | None = None) -> str | None:
    current = (now or _now()).astimezone(timezone.utc)
    try:
        payload = json.loads(FLEET_PROBE_RECEIPT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"fleet outcome probe receipt unreadable at {FLEET_PROBE_RECEIPT}: {exc}"
    checked_at = _parse_time(payload.get("checked_at") if isinstance(payload, dict) else None)
    if checked_at is None:
        return f"fleet outcome probe receipt has no valid checked_at: {FLEET_PROBE_RECEIPT}"
    age = current - checked_at
    if age < -timedelta(minutes=1):
        return (
            f"fleet outcome probe heartbeat is timestamped "
            f"{int(-age.total_seconds())}s in the future"
        )
    if age > FLEET_PROBE_MAX_AGE:
        return (
            f"fleet outcome probe heartbeat is stale ({int(age.total_seconds())}s; "
            f"limit {int(FLEET_PROBE_MAX_AGE.total_seconds())}s)"
        )
    return None


def _route_fleet_probe_watchdog(problem: str | None) -> None:
    state = _load_state(FLEET_WATCHDOG_STATE)
    previous = str(state.get("delivered_signature") or "")
    if problem is None:
        if not state.get("active") or not previous:
            return
        message = (
            "✅ Hermes fleet outcome probe heartbeat recovered\n"
            f"Previous signature: {previous[:12]}"
        )
        try:
            delivery = _send_slack(message)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[ci-health-watch-cron] fleet watchdog recovery delivery failed: {exc}", file=sys.stderr)
            return
        if delivery.returncode != 0:
            print(
                "[ci-health-watch-cron] fleet watchdog recovery delivery exited "
                f"{delivery.returncode}: {delivery.stderr.strip()}",
                file=sys.stderr,
            )
            return
        state.update({"active": False, "recovered_at": _now().isoformat()})
        _atomic_json(FLEET_WATCHDOG_STATE, state)
        return

    signature = hashlib.sha256(problem.encode("utf-8")).hexdigest()
    if state.get("active") and previous == signature:
        return
    message = (
        "🚨 Hermes fleet outcome probe stopped checking in\n"
        f"{problem}\n"
        "Next: inspect the fleet-outcome-probe LaunchAgent and its launchd error log."
    )
    try:
        delivery = _send_slack(message)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[ci-health-watch-cron] fleet watchdog delivery failed: {exc}", file=sys.stderr)
        return
    if delivery.returncode != 0:
        print(
            f"[ci-health-watch-cron] fleet watchdog delivery exited {delivery.returncode}: "
            f"{delivery.stderr.strip()}",
            file=sys.stderr,
        )
        return
    state.update(
        {
            "active": True,
            "delivered_signature": signature,
            "last_alert_at": _now().isoformat(),
        }
    )
    _atomic_json(FLEET_WATCHDOG_STATE, state)


def main() -> int:
    ci_result = _run_ci_health()
    try:
        if _claim_daily_scan():
            _run_daily_staleness()
    except OSError as exc:
        print(f"[ci-health-watch-cron] daily gate failed: {exc}", file=sys.stderr)
    try:
        _route_fleet_probe_watchdog(_fleet_probe_problem())
    except OSError as exc:
        print(f"[ci-health-watch-cron] fleet watchdog state failed: {exc}", file=sys.stderr)
    return ci_result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
