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
    content = "\n".join(json.dumps(r) for r in records) + ("\n" if records else "")
    path.write_text(content, encoding="utf-8")


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

    def test_healthy_ci_health_watch_alone_never_breaches(self):
        """86e2mg7jb acceptance: a healthy ci-health-watch (job e835c614cfb2,
        a */5min no_agent tick) ending [SILENT] every run for an hour is
        9-12 endings — above the default per-job (4) and fleet-wide (8)
        thresholds — yet must not trigger anything on its own."""
        records = _inject_records("e835c614cfb2", 12, at=self.NOW - 60)
        result = monitor.evaluate_silent_rate(records, now=self.NOW)
        assert result["triggered"] is False
        assert result["breached_jobs"] == []
        assert result["total_breached"] is False
        assert result["total_count"] == 0
        assert result["excluded_total_count"] == 12
        assert result["excluded_per_job_counts"] == {"e835c614cfb2": 12}

    def test_all_by_design_silent_jobs_together_never_breach_fleet_wide(self):
        """Every known by-design-silent job going quiet heavily at once must
        still not drive the fleet-wide "possible total fleet outage" count —
        this is the exact chronic-false-alarm shape from the ClickUp report
        (multiple by-design-silent jobs all ticking within the same hour)."""
        records = []
        for job_id in monitor.BY_DESIGN_SILENT_JOB_IDS:
            records += _inject_records(job_id, 10, at=self.NOW - 60)
        result = monitor.evaluate_silent_rate(records, now=self.NOW)
        assert result["triggered"] is False
        assert result["breached_jobs"] == []
        assert result["total_breached"] is False
        assert result["total_count"] == 0
        assert result["excluded_total_count"] == 10 * len(monitor.BY_DESIGN_SILENT_JOB_IDS)

    def test_synthetic_executor_breach_still_triggers_amid_by_design_noise(self):
        """A real per-job breach on a non-excluded (agent) job must still
        trigger even while by-design-silent jobs are simultaneously noisy —
        the fix must not weaken genuine outage detection."""
        records = (
            _inject_records("e835c614cfb2", 12, at=self.NOW - 60)
            + _inject_records("bcf275768661", 10, at=self.NOW - 60)
            + _inject_records("clickup-executor", 5, at=self.NOW - 60)
        )
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, per_job_threshold=4, total_threshold=100,
        )
        assert result["triggered"] is True
        assert result["breached_jobs"] == ["clickup-executor"]
        assert result["total_breached"] is False
        assert result["total_count"] == 5
        assert result["per_job_counts"] == {"clickup-executor": 5}

    def test_synthetic_fleet_wide_breach_from_real_jobs_still_triggers(self):
        """A genuine cross-job silence spike among non-excluded jobs must
        still breach TOTAL_THRESHOLD even though by-design-silent jobs are
        excluded from the same counter."""
        records = _inject_records("e835c614cfb2", 12, at=self.NOW - 60)
        for i in range(8):
            records += _inject_records(f"real-job-{i}", 1, at=self.NOW - 60)
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, per_job_threshold=100, total_threshold=8,
        )
        assert result["triggered"] is True
        assert result["total_breached"] is True
        assert result["total_count"] == 8
        assert result["excluded_total_count"] == 12

    def test_excluded_job_ids_param_is_overridable(self):
        """The exclusion set is a parameter, not hardwired only into the
        module constant — callers (or a future config-driven caller) can
        substitute their own table."""
        records = _inject_records("custom-quiet-job", 12, at=self.NOW - 60)
        result = monitor.evaluate_silent_rate(
            records, now=self.NOW, excluded_job_ids={"custom-quiet-job": "custom"},
        )
        assert result["triggered"] is False
        assert result["excluded_total_count"] == 12


def test_fleet_config_drift_warnings_empty_when_no_fleet_config_reachable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_FLEET_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    monkeypatch.setattr(monitor, "_fleet_config_candidates", lambda: [str(tmp_path / "nonexistent.json")])
    assert monitor._fleet_config_drift_warnings() == []


def test_fleet_config_drift_warnings_flags_name_and_missing_id_drift(tmp_path, monkeypatch):
    fixture = tmp_path / "jobs.json"
    fixture.write_text(json.dumps({
        "jobs": [
            {"id": "e835c614cfb2", "name": "renamed-ci-health-watch"},
            # review-poll-gate, spend-meter, reap-stranded-claims,
            # clickup-workspace-refresh deliberately absent below.
        ]
    }))
    monkeypatch.setattr(monitor, "_fleet_config_candidates", lambda: [str(fixture)])
    warnings = monitor._fleet_config_drift_warnings()
    assert any("e835c614cfb2" in w and "drifted" in w for w in warnings)
    assert any("8d3b1d53470d" in w and "not found" in w for w in warnings)


def test_fleet_config_drift_warnings_clean_when_ids_and_names_match(tmp_path, monkeypatch):
    fixture = tmp_path / "jobs.json"
    fixture.write_text(json.dumps({
        "jobs": [
            {"id": job_id, "name": name}
            for job_id, name in monitor.BY_DESIGN_SILENT_JOB_IDS.items()
        ]
    }))
    monkeypatch.setattr(monitor, "_fleet_config_candidates", lambda: [str(fixture)])
    assert monitor._fleet_config_drift_warnings() == []


def test_by_design_silent_job_ids_resolve_correctly_against_real_fleet_config():
    """Guards the exclusion table against the actual repo-tracked fleet
    config so a rename/retirement in machine-setup/fleet-config/jobs.json is
    caught by CI, not just by the best-effort runtime warning."""
    fleet_config_path = (
        Path(__file__).resolve().parents[2] / "fleet-config" / "jobs.json"
    )
    if not fleet_config_path.is_file():
        pytest.skip(f"fleet config not present at {fleet_config_path}")
    payload = json.loads(fleet_config_path.read_text(encoding="utf-8"))
    by_id = {str(job.get("id")): str(job.get("name") or "") for job in payload.get("jobs", [])}
    for job_id, expected_name in monitor.BY_DESIGN_SILENT_JOB_IDS.items():
        assert by_id.get(job_id) == expected_name, (
            f"BY_DESIGN_SILENT_JOB_IDS[{job_id!r}] = {expected_name!r} no longer "
            f"matches fleet-config/jobs.json ({by_id.get(job_id)!r}) — update the "
            "exclusion table in silent_delivery_monitor.py"
        )


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


def test_cli_healthy_by_design_silent_traffic_stays_green_but_real_breach_still_exits_1(tmp_path):
    """86e2mg7jb end-to-end acceptance: heavy by-design-silent traffic
    (ci-health-watch + clickup-workspace-refresh) alone must exit 0, and a
    synthetic executor breach layered on top of that same noisy fleet must
    still exit 1 — the exclusion must not weaken real-outage detection."""
    now = time.time()
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    env = os.environ.copy()
    env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})

    healthy_log = tmp_path / "healthy.jsonl"
    _write_log(
        healthy_log,
        _inject_records("e835c614cfb2", 12, at=now - 300)
        + _inject_records("bcf275768661", 10, at=now - 300),
    )
    healthy_result = subprocess.run(
        [sys.executable, str(SOURCE), "--alert", "--log-file", str(healthy_log), "--now", str(now)],
        capture_output=True, text=True, cwd=SCRIPT_DIR, env=env, check=False,
    )
    assert healthy_result.returncode == 0
    assert "healthy" in healthy_result.stdout
    assert "DRY_RUN slack" not in healthy_result.stdout

    breach_log = tmp_path / "breach.jsonl"
    _write_log(
        breach_log,
        _inject_records("e835c614cfb2", 12, at=now - 300)
        + _inject_records("bcf275768661", 10, at=now - 300)
        + _inject_records("clickup-executor", 5, at=now - 300),
    )
    breach_result = subprocess.run(
        [
            sys.executable, str(SOURCE), "--alert",
            "--log-file", str(breach_log), "--now", str(now),
            "--per-job-threshold", "4",
        ],
        capture_output=True, text=True, cwd=SCRIPT_DIR, env=env, check=False,
    )
    assert breach_result.returncode == 1
    assert "job 'clickup-executor' breached per-job threshold" in breach_result.stdout
    assert "DRY_RUN slack" in breach_result.stdout


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
