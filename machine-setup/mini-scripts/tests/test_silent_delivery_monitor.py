#!/usr/bin/env python3
"""Contract tests for silent_delivery_monitor.py (ClickUp 86e2kxk4t)."""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "silent_delivery_monitor.py"
SCRIPT_DIR = SOURCE.parent
LAUNCHD_PLIST = SCRIPT_DIR / "launchd" / "com.colingreig.hermes.silent-delivery-monitor.plist"

SPEC = importlib.util.spec_from_file_location("silent_delivery_monitor_under_test", SOURCE)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


def _write_log(path: Path, records) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + ("\n" if records else ""))


def _inject_records(job_id: str, count: int, *, at: float):
    return [{"job_id": job_id, "at": at} for _ in range(count)]


class TestEvaluateSilentRate:
    NOW = 1_800_000_000.0

    def test_healthy_below_both_thresholds(self):
        records = _inject_records("job-a", 2, at=self.NOW - 60)
        result = monitor.evaluate_silent_rate(records, now=self.NOW)
        assert result["triggered"] is False
        assert result["breached_jobs"] == []
        assert result["total_breached"] is False

    def test_per_job_threshold_breach_is_isolated_to_that_job(self):
        records = (
            _inject_records("stuck-job", 4, at=self.NOW - 60)
            + _inject_records("quiet-job", 1, at=self.NOW - 60)
        )
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, per_job_threshold=4, total_threshold=100,
        )
        assert result["triggered"] is True
        assert result["breached_jobs"] == ["stuck-job"]
        assert result["total_breached"] is False
        assert result["per_job_counts"] == {"quiet-job": 1, "stuck-job": 4}

    def test_total_threshold_breach_across_many_jobs(self):
        records = []
        for i in range(8):
            records += _inject_records(f"job-{i}", 1, at=self.NOW - 60)
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, per_job_threshold=100, total_threshold=8,
        )
        assert result["triggered"] is True
        assert result["breached_jobs"] == []
        assert result["total_breached"] is True
        assert result["total_count"] == 8

    def test_records_outside_the_rolling_window_are_excluded(self):
        records = _inject_records("job-a", 10, at=self.NOW - 3600 * 3)
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, window_min=60, per_job_threshold=1, total_threshold=1,
        )
        assert result["triggered"] is False
        assert result["total_count"] == 0

    def test_boundary_timestamps_are_inclusive(self):
        window_min = 60
        records = [
            {"job_id": "at-start", "at": self.NOW - window_min * 60},
            {"job_id": "at-now", "at": self.NOW},
        ]
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, window_min=window_min, per_job_threshold=100, total_threshold=1,
        )
        assert result["total_count"] == 2


def test_read_records_skips_malformed_lines_without_failing(tmp_path):
    path = tmp_path / "silent.jsonl"
    path.write_text(
        '{"job_id": "a", "at": 1.0}\n'
        "not json\n"
        '{"job_id": "b"}\n'  # missing "at" — also skipped
        '{"job_id": "c", "at": 2.0}\n'
    )
    records = monitor.read_records(str(path))
    assert [(r["job_id"], r["at"]) for r in records] == [("a", 1.0), ("c", 2.0)]


def test_read_records_missing_file_returns_empty():
    assert monitor.read_records("/nonexistent/path/silent.jsonl") == []


def test_cli_forced_silent_drill_fires_a_real_alert_within_the_window(tmp_path):
    """Acceptance drill: inject repeated [SILENT] endings for one job and
    confirm the CLI fires a real (DRY_RUN-visible) alert, not just a log
    line, well inside the configured window."""
    log_path = tmp_path / "silent.jsonl"
    now = time.time()
    _write_log(log_path, _inject_records("clickup-executor", 5, at=now - 300))

    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    env = os.environ.copy()
    env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})

    result = subprocess.run(
        [
            sys.executable, str(SOURCE), "--alert",
            "--log-file", str(log_path), "--now", str(now),
            "--per-job-threshold", "4",
        ],
        capture_output=True, text=True, cwd=SCRIPT_DIR, env=env, check=False,
    )

    assert result.returncode == 1
    assert "job 'clickup-executor' breached per-job threshold" in result.stdout
    assert "DRY_RUN slack" in result.stdout
    assert "clickup-executor" in result.stdout
    assert "[silent-delivery-monitor] alerted" in result.stdout


def test_healthy_fleet_never_alerts(tmp_path):
    log_path = tmp_path / "silent.jsonl"
    now = time.time()
    _write_log(log_path, _inject_records("job-a", 1, at=now - 60))

    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    env = os.environ.copy()
    env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})

    result = subprocess.run(
        [sys.executable, str(SOURCE), "--alert", "--log-file", str(log_path), "--now", str(now)],
        capture_output=True, text=True, cwd=SCRIPT_DIR, env=env, check=False,
    )

    assert result.returncode == 0
    assert "healthy" in result.stdout
    assert "DRY_RUN slack" not in result.stdout


def test_alert_dedupes_on_repeat_checks_of_the_same_signature(tmp_path):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        state_file = base / "state.json"
        log_path = base / "silent.jsonl"
        now = time.time()
        _write_log(log_path, _inject_records("stuck-job", 5, at=now - 60))
        argv = [
            str(SOURCE), "--alert", "--log-file", str(log_path),
            "--now", str(now), "--per-job-threshold", "4",
        ]
        slack = mock.Mock(return_value=True)
        with mock.patch.object(monitor, "STATE_PATH", str(state_file)):
            with mock.patch.object(monitor, "_send_slack", slack):
                with mock.patch.object(sys, "argv", argv):
                    for _ in range(2):
                        with pytest.raises(SystemExit) as raised:
                            monitor.main()
                        assert raised.value.code == 1
        state = json.loads(state_file.read_text())

    assert slack.call_count == 1
    assert state["last_alert_signature"]["breached_jobs"] == [["stuck-job", 5]]


def test_recovery_clears_dedup_state_and_a_future_recurrence_realerts(tmp_path):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        state_file = base / "state.json"
        log_path = base / "silent.jsonl"
        now = time.time()
        _write_log(log_path, _inject_records("stuck-job", 5, at=now - 60))
        slack = mock.Mock(return_value=True)

        with mock.patch.object(monitor, "STATE_PATH", str(state_file)):
            with mock.patch.object(monitor, "_send_slack", slack):
                argv = [
                    str(SOURCE), "--alert", "--log-file", str(log_path),
                    "--now", str(now), "--per-job-threshold", "4",
                ]
                with mock.patch.object(sys, "argv", argv):
                    with pytest.raises(SystemExit):
                        monitor.main()

                # Recover: rewrite the log with nothing in the window.
                _write_log(log_path, [])
                with mock.patch.object(sys, "argv", argv):
                    with pytest.raises(SystemExit) as raised:
                        monitor.main()
                assert raised.value.code == 0
                state = json.loads(state_file.read_text())
                assert state["last_alert_signature"] is None

                # Recurrence: same breach reappears — must alert again.
                _write_log(log_path, _inject_records("stuck-job", 5, at=now - 60))
                with mock.patch.object(sys, "argv", argv):
                    with pytest.raises(SystemExit):
                        monitor.main()

    assert slack.call_count == 2


def test_launchagent_contract_targets_live_monitor_and_a_sub_hour_interval():
    payload = plistlib.loads(LAUNCHD_PLIST.read_bytes())

    assert payload["Label"] == "com.colingreig.hermes.silent-delivery-monitor"
    assert payload["ProgramArguments"] == [
        "/usr/bin/python3",
        "/Users/colingreig/.hermes/scripts/silent_delivery_monitor.py",
        "--alert",
    ]
    assert 0 < payload["StartInterval"] <= 3600
    assert payload["RunAtLoad"] is True
    env = payload["EnvironmentVariables"]
    assert env["HOME"] == "/Users/colingreig"
    assert env["HERMES_HOME"] == "/Users/colingreig/.hermes"
    assert env["CRON_SILENT_ALERT_SLACK"] == "slack:D0BA2PM9CFM"
    serialized = LAUNCHD_PLIST.read_text(encoding="utf-8")
    assert "CLICKUP_API_TOKEN" not in serialized
    assert "op://" not in serialized


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
