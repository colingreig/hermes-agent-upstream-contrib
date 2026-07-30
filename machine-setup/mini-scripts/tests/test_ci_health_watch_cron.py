from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "ci-health-watch-cron.py"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"ci_health_watch_cron_ut_{_COUNTER}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def _isolate_watchdog(module, tmp_path, *, checked_at=None):
    module.FLEET_PROBE_RECEIPT = tmp_path / "fleet-receipt.json"
    module.FLEET_WATCHDOG_STATE = tmp_path / "fleet-watchdog.json"
    observed = checked_at or module._now()
    module.FLEET_PROBE_RECEIPT.write_text(
        json.dumps(
            {
                "checked_at": observed.isoformat(),
                "mode": "production",
                "status": "clean",
                "alarm": {"action": "clean"},
            }
        ),
        encoding="utf-8",
    )


def test_ci_health_always_runs_and_propagates_streams_and_exit(tmp_path):
    module = _load_module()
    module.DAILY_STATE_PATH = tmp_path / "daily.json"
    _isolate_watchdog(module, tmp_path)
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        if Path(cmd[1]).name == "ci_health_watch.py":
            return _completed(cmd, returncode=7, stdout="ci-out\n", stderr="ci-err\n")
        if Path(cmd[1]).name == "pr_staleness_alert.py":
            return _completed(cmd)
        raise AssertionError(f"unexpected command: {cmd}")

    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(module.subprocess, "run", side_effect=run):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = module.main()

    assert result == 7
    assert stdout.getvalue() == "ci-out\n"
    assert stderr.getvalue() == "ci-err\n"
    assert Path(calls[0][1]).name == "ci_health_watch.py"
    assert Path(calls[1][1]).name == "pr_staleness_alert.py"


def test_daily_scan_is_skipped_inside_rolling_24_hours(tmp_path):
    module = _load_module()
    module.DAILY_STATE_PATH = tmp_path / "daily.json"
    _isolate_watchdog(module, tmp_path, checked_at=NOW)
    module.DAILY_STATE_PATH.write_text(
        json.dumps({"last_attempt_at": (NOW - timedelta(hours=23)).isoformat()}),
        encoding="utf-8",
    )
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return _completed(cmd)

    with mock.patch.object(module, "_now", return_value=NOW):
        with mock.patch.object(module.subprocess, "run", side_effect=run):
            assert module.main() == 0

    assert len(calls) == 1
    assert Path(calls[0][1]).name == "ci_health_watch.py"


def test_daily_claim_rechecks_state_after_cross_process_lock(tmp_path):
    module = _load_module()
    state_path = tmp_path / "daily.json"
    competing_claim = {"last_attempt_at": (NOW - timedelta(minutes=1)).isoformat()}

    def acquire_after_competing_claim(_fd, _operation):
        module._atomic_json(state_path, competing_claim)

    with mock.patch.object(module.fcntl, "flock", side_effect=acquire_after_competing_claim):
        assert module._claim_daily_scan(now=NOW, state_path=state_path) is False

    assert json.loads(state_path.read_text(encoding="utf-8")) == competing_claim


def test_due_stale_result_is_sent_to_ci_health_slack_channel(tmp_path):
    module = _load_module()
    module.DAILY_STATE_PATH = tmp_path / "daily.json"
    module.HERMES_BIN = tmp_path / "hermes"
    _isolate_watchdog(module, tmp_path, checked_at=NOW)
    calls = []
    alert = "⏰ 1 PR(s) stale without a fresh verdict."

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if Path(cmd[0]) == module.HERMES_BIN:
            return _completed(cmd)
        if Path(cmd[1]).name == "ci_health_watch.py":
            return _completed(cmd)
        if Path(cmd[1]).name == "pr_staleness_alert.py":
            assert "SLACK_WEBHOOK_URL" not in kwargs["env"]
            return _completed(cmd, stdout=alert + "\n")
        raise AssertionError(f"unexpected command: {cmd}")

    with mock.patch.dict(module.os.environ, {"SLACK_WEBHOOK_URL": "https://example.invalid/hook"}):
        with mock.patch.object(module, "_now", return_value=NOW):
            with mock.patch.object(module.subprocess, "run", side_effect=run):
                assert module.main() == 0

    assert calls[-1][0] == [
        str(module.HERMES_BIN),
        "send",
        "--to",
        "slack:hermes",
        alert,
    ]
    state = json.loads(module.DAILY_STATE_PATH.read_text(encoding="utf-8"))
    assert state == {"last_attempt_at": NOW.isoformat()}
    assert module.DAILY_STATE_PATH.stat().st_mode & 0o777 == 0o600


def test_due_clean_result_stays_silent(tmp_path):
    module = _load_module()
    module.DAILY_STATE_PATH = tmp_path / "daily.json"
    _isolate_watchdog(module, tmp_path, checked_at=NOW)
    calls = []
    clean = "✅ Previously stale PR(s) are no longer stale."

    def run(cmd, **_kwargs):
        calls.append(cmd)
        if len(cmd) > 1 and Path(cmd[1]).name == "ci_health_watch.py":
            return _completed(cmd)
        if len(cmd) > 1 and Path(cmd[1]).name == "pr_staleness_alert.py":
            return _completed(cmd, stdout=clean + "\n")
        raise AssertionError(f"unexpected command: {cmd}")

    with mock.patch.object(module, "_now", return_value=NOW):
        with mock.patch.object(module.subprocess, "run", side_effect=run):
            assert module.main() == 0

    assert [Path(cmd[1]).name for cmd in calls] == [
        "ci_health_watch.py",
        "pr_staleness_alert.py",
    ]


def test_stale_fleet_probe_alert_is_delivery_aware_and_deduped(tmp_path):
    module = _load_module()
    module.FLEET_PROBE_RECEIPT = tmp_path / "fleet-receipt.json"
    module.FLEET_WATCHDOG_STATE = tmp_path / "fleet-watchdog.json"
    module.FLEET_PROBE_RECEIPT.write_text(
        json.dumps(
            {
                "checked_at": (NOW - timedelta(hours=1)).isoformat(),
                "mode": "production",
                "status": "clean",
                "alarm": {"action": "clean"},
            }
        ),
        encoding="utf-8",
    )
    problem = module._fleet_probe_problem(now=NOW)
    assert problem and problem[0] == "receipt-stale"

    failed = _completed(["hermes"], returncode=1, stderr="offline")
    with mock.patch.object(module, "_now", return_value=NOW):
        with mock.patch.object(module, "_send_slack", return_value=failed):
            module._route_fleet_probe_watchdog(problem)
    assert not module.FLEET_WATCHDOG_STATE.exists()

    sent = _completed(["hermes"])
    sender = mock.Mock(return_value=sent)
    with mock.patch.object(module, "_now", return_value=NOW):
        with mock.patch.object(module, "_send_slack", sender):
            module._route_fleet_probe_watchdog(problem)
            module._route_fleet_probe_watchdog(problem)
    assert sender.call_count == 1
    state = json.loads(module.FLEET_WATCHDOG_STATE.read_text(encoding="utf-8"))
    assert state["active"] is True

    later_problem = module._fleet_probe_problem(now=NOW + timedelta(minutes=5))
    assert later_problem and later_problem[0] == "receipt-stale"
    with mock.patch.object(module, "_send_slack", sender):
        module._route_fleet_probe_watchdog(later_problem)
    assert sender.call_count == 1


def test_fleet_probe_recovery_clears_only_after_confirmed_delivery(tmp_path):
    module = _load_module()
    module.FLEET_WATCHDOG_STATE = tmp_path / "fleet-watchdog.json"
    module.FLEET_WATCHDOG_STATE.write_text(
        json.dumps({"active": True, "delivered_signature": "abc"}),
        encoding="utf-8",
    )

    with mock.patch.object(module, "_now", return_value=NOW):
        with mock.patch.object(
            module, "_send_slack", return_value=_completed(["hermes"], returncode=2)
        ):
            module._route_fleet_probe_watchdog(None)
    assert json.loads(module.FLEET_WATCHDOG_STATE.read_text())["active"] is True

    with mock.patch.object(module, "_now", return_value=NOW):
        with mock.patch.object(module, "_send_slack", return_value=_completed(["hermes"])):
            module._route_fleet_probe_watchdog(None)
    assert json.loads(module.FLEET_WATCHDOG_STATE.read_text())["active"] is False


def test_unconfirmed_probe_alarm_delivery_is_not_a_healthy_heartbeat(tmp_path):
    module = _load_module()
    module.FLEET_PROBE_RECEIPT = tmp_path / "fleet-receipt.json"
    module.FLEET_PROBE_RECEIPT.write_text(
        json.dumps(
            {
                "checked_at": NOW.isoformat(),
                "mode": "production",
                "status": "alert",
                "alarm": {"action": "delivery-failed"},
            }
        ),
        encoding="utf-8",
    )
    assert module._fleet_probe_problem(now=NOW) == (
        "alarm-delivery-unconfirmed",
        "fleet outcome probe alarm delivery is unconfirmed (action=delivery-failed)",
    )
