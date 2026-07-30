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


def test_ci_health_always_runs_and_propagates_streams_and_exit(tmp_path):
    module = _load_module()
    module.DAILY_STATE_PATH = tmp_path / "daily.json"
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
