"""Tests for the durable [SILENT] delivery-skip observability trail
(ClickUp 86e2kxk4t) that backs silent_delivery_monitor.py.

Silence itself stays a legitimate, deliberately quiet outcome (see
SILENT_MARKER's docs in cron/scheduler.py) — these tests only cover the
*additive* structured record written alongside the existing INFO log line,
never a change to delivery/execution semantics.
"""
from __future__ import annotations

import json

import pytest

from cron.scheduler import _CronExecutionOutcome, _deliver_cron_outcome


def _read_records(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_silent_delivery_appends_a_structured_line(tmp_path, monkeypatch):
    log_path = tmp_path / "silent.jsonl"
    monkeypatch.setenv("HERMES_CRON_SILENT_LOG_PATH", str(log_path))
    from cron.scheduler import _record_silent_delivery

    _record_silent_delivery("job-a", now=1000.0)
    _record_silent_delivery("job-a", now=1001.0)
    _record_silent_delivery("job-b", now=1002.0)

    records = _read_records(log_path)
    assert [(r["job_id"], r["at"]) for r in records] == [
        ("job-a", 1000.0), ("job-a", 1001.0), ("job-b", 1002.0),
    ]


def test_record_silent_delivery_never_raises_on_write_failure(tmp_path, monkeypatch):
    # Point the log at a path whose parent cannot be created (a file, not a
    # directory) — the write must fail closed and silently, never raise.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("HERMES_CRON_SILENT_LOG_PATH", str(blocker / "silent.jsonl"))
    from cron.scheduler import _record_silent_delivery

    _record_silent_delivery("job-a")  # must not raise


def test_deliver_cron_outcome_records_explicit_silent_marker(tmp_path, monkeypatch):
    log_path = tmp_path / "silent.jsonl"
    monkeypatch.setenv("HERMES_CRON_SILENT_LOG_PATH", str(log_path))
    job = {"id": "executor-job"}
    outcome = _CronExecutionOutcome(True, None, "completed")

    result = _deliver_cron_outcome(job, outcome, "[SILENT]")

    assert result.delivery_error is None
    records = _read_records(log_path)
    assert [r["job_id"] for r in records] == ["executor-job"]


def test_deliver_cron_outcome_records_empty_output_as_silent(tmp_path, monkeypatch):
    """An agent that produces genuinely empty stdout is also a silent skip
    (no explicit [SILENT] marker required) — must be recorded too."""
    log_path = tmp_path / "silent.jsonl"
    monkeypatch.setenv("HERMES_CRON_SILENT_LOG_PATH", str(log_path))
    job = {"id": "executor-job"}
    outcome = _CronExecutionOutcome(True, None, "completed")

    _deliver_cron_outcome(job, outcome, "")

    records = _read_records(log_path)
    assert [r["job_id"] for r in records] == ["executor-job"]


def test_deliver_cron_outcome_does_not_record_a_real_delivery(tmp_path, monkeypatch):
    log_path = tmp_path / "silent.jsonl"
    monkeypatch.setenv("HERMES_CRON_SILENT_LOG_PATH", str(log_path))
    job = {"id": "executor-job"}
    outcome = _CronExecutionOutcome(True, None, "completed")

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("cron.scheduler._deliver_result", lambda *a, **k: None)
        _deliver_cron_outcome(job, outcome, "Daily report: 4 PRs merged.")

    assert _read_records(log_path) == []


def test_deliver_cron_outcome_does_not_record_a_failed_run(tmp_path, monkeypatch):
    """A genuine failure is a distinct signal (mark_job_run/error path) — it
    must not also masquerade as a silent delivery-skip."""
    log_path = tmp_path / "silent.jsonl"
    monkeypatch.setenv("HERMES_CRON_SILENT_LOG_PATH", str(log_path))
    job = {"id": "executor-job"}
    outcome = _CronExecutionOutcome(False, "boom", "run_failed")

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("cron.scheduler._deliver_result", lambda *a, **k: None)
        _deliver_cron_outcome(job, outcome, "")

    assert _read_records(log_path) == []
