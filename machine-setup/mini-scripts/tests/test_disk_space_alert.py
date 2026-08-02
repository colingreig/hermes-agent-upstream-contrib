from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "disk_space_alert.py"
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    dependency_root = SCRIPTS / "pr_pipeline"
    sys.path.insert(0, str(dependency_root))
    try:
        spec = importlib.util.spec_from_file_location(f"disk_space_alert_ut_{_COUNTER}", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(dependency_root))
    return module


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["hermes"], returncode, stdout, stderr)


class _Usage:
    def __init__(self, free_gb, total_gb=500):
        self.free = free_gb * 1024 ** 3
        self.total = total_gb * 1024 ** 3


def _prepare(module, tmp_path):
    module.STATE_PATH = str(tmp_path / "state.json")
    module.RECEIPT_PATH = str(tmp_path / "receipt.json")
    module.MIN_FREE_GB = 5.0
    module.LOW_DISK_COOLDOWN_S = 3600
    module.CHECK_ERROR_COOLDOWN_S = 21600
    # Pressure-trigger plumbing: point at tmp_path (never the real mini paths) and
    # default to "just triggered" so unrelated low-disk tests don't shell out to
    # sweep scripts that don't exist on the test box. Tests that exercise the
    # trigger itself reset/clear this receipt explicitly.
    module.TRIGGER_RECEIPT_PATH = str(tmp_path / "trigger.json")
    module.TRIGGER_COOLDOWN_S = 21600
    module.SWEEP_TIMEOUT_S = 5
    module.WORKTREE_SWEEP_PATH = str(tmp_path / "worktree_backstop_sweep.py")
    module.KANBAN_SWEEP_PATH = str(tmp_path / "kanban_workspace_sweep.py")
    Path(module.TRIGGER_RECEIPT_PATH).write_text(
        json.dumps({"last_trigger_ts": time.time()}), encoding="utf-8"
    )


def test_healthy_disk_is_silent_and_no_slack_send(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(50)):
        with mock.patch.object(module, "_send_slack") as send:
            rc = module.main()
    assert rc == 0
    assert not send.called
    receipt = json.loads(Path(module.RECEIPT_PATH).read_text(encoding="utf-8"))
    assert receipt["status"] == "ok"


def test_low_disk_sends_slack_alert(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
            rc = module.main()
    assert rc == 0
    assert send.called
    sent_message = send.call_args[0][0]
    assert "disk space low" in sent_message.lower()
    receipt = json.loads(Path(module.RECEIPT_PATH).read_text(encoding="utf-8"))
    assert receipt["status"] == "low"
    assert receipt["delivery"] == "confirmed"


def test_low_disk_dedupes_within_cooldown(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
            module.main()
            rc = module.main()
    assert rc == 0
    assert send.call_count == 1  # second call deduped by cooldown


def test_low_disk_realerts_past_cooldown(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    module.LOW_DISK_COOLDOWN_S = 0  # expire immediately
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
            module.main()
            module.main()
    assert send.call_count == 2


def test_slack_delivery_failure_is_non_silent(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_send_slack", return_value=_completed(1, "", "boom")):
            rc = module.main()
    assert rc == 2
    receipt = json.loads(Path(module.RECEIPT_PATH).read_text(encoding="utf-8"))
    assert receipt["delivery"] == "failed"


def test_disk_usage_error_is_non_silent_and_alerts(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", side_effect=OSError("no such mount")):
        with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
            rc = module.main()
    assert rc == 1
    assert send.called
    receipt = json.loads(Path(module.RECEIPT_PATH).read_text(encoding="utf-8"))
    assert receipt["status"] == "check_error"


def test_disable_env_var_short_circuits(tmp_path, monkeypatch):
    module = _load_module()
    _prepare(module, tmp_path)
    monkeypatch.setenv("HERMES_DISK_ALERT_DISABLE", "1")
    with mock.patch.object(module, "_send_slack") as send:
        rc = module.main()
    assert rc == 0
    assert not send.called


def test_default_min_free_gb_is_20(monkeypatch):
    monkeypatch.delenv("HERMES_DISK_ALERT_MIN_FREE_GB", raising=False)
    module = _load_module()
    assert module.MIN_FREE_GB == 20.0


def test_run_sweep_parses_removed_bytes_from_output(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    completed = _completed(0, stdout="[t] sweep-finish root=/x removed=2 removed_bytes=12345 dry_run=False\n")
    with mock.patch.object(module.subprocess, "run", return_value=completed):
        result = module._run_sweep(["python3", "fake.py"], "worktree")
    assert result["ok"] is True
    assert result["removed_bytes"] == 12345
    assert "sweep-finish" in result["summary"]


def test_run_sweep_handles_timeout_without_raising(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(
        module.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd=["python3", "fake.py"], timeout=5),
    ):
        result = module._run_sweep(["python3", "fake.py"], "worktree")
    assert result["ok"] is False
    assert "timeout" in result["error"]


def test_run_sweep_handles_missing_script_without_raising(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    # No mocking: WORKTREE_SWEEP_PATH doesn't exist on the test box, so this
    # exercises the real FileNotFoundError path through subprocess.run.
    result = module._run_sweep([sys.executable, module.WORKTREE_SWEEP_PATH], "worktree")
    assert result["ok"] is False
    assert result["error"]


def test_trigger_pressure_sweeps_invokes_both_and_writes_receipt(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    Path(module.TRIGGER_RECEIPT_PATH).unlink()  # no prior trigger -> cooldown doesn't block
    worktree_result = {"label": "worktree", "ok": True, "removed_bytes": 3_000_000_000, "summary": "ok", "error": None}
    kanban_result = {"label": "kanban", "ok": True, "removed_bytes": 1_000_000_000, "summary": "ok", "error": None}
    with mock.patch.object(module, "_run_sweep", side_effect=[worktree_result, kanban_result]) as run_sweep:
        result = module._trigger_pressure_sweeps(time.time())
    assert run_sweep.call_count == 2
    worktree_cmd = run_sweep.call_args_list[0][0][0]
    assert module.WORKTREE_SWEEP_PATH in worktree_cmd
    assert "--min-free-gb" in worktree_cmd and "--pressure-days" in worktree_cmd
    kanban_cmd = run_sweep.call_args_list[1][0][0]
    assert module.KANBAN_SWEEP_PATH in kanban_cmd
    assert result["total_removed_bytes"] == 4_000_000_000
    receipt = json.loads(Path(module.TRIGGER_RECEIPT_PATH).read_text(encoding="utf-8"))
    assert receipt["total_removed_bytes"] == 4_000_000_000
    assert receipt["last_trigger_ts"] > 0


def test_trigger_pressure_sweeps_cooldown_skips_second_call(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    Path(module.TRIGGER_RECEIPT_PATH).unlink()
    ok_result = {"label": "x", "ok": True, "removed_bytes": 0, "summary": "ok", "error": None}
    with mock.patch.object(module, "_run_sweep", return_value=ok_result) as run_sweep:
        first = module._trigger_pressure_sweeps(time.time())
        second = module._trigger_pressure_sweeps(time.time())
    assert first is not None
    assert second is None
    assert run_sweep.call_count == 2  # only from the first trigger (worktree + kanban)


def test_low_disk_message_reports_trigger_and_reclaim(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    Path(module.TRIGGER_RECEIPT_PATH).unlink()
    ok_result = {"label": "worktree", "ok": True, "removed_bytes": 5_368_709_120, "summary": "ok", "error": None}
    kanban_result = {"label": "kanban", "ok": True, "removed_bytes": 0, "summary": "ok", "error": None}
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_run_sweep", side_effect=[ok_result, kanban_result]):
            with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
                rc = module.main()
    assert rc == 0
    assert send.called
    sent_message = send.call_args[0][0]
    assert "pressure sweep triggered" in sent_message
    assert "5.0GB" in sent_message


def test_low_disk_message_zero_reclaim_is_explicit_alarm(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    Path(module.TRIGGER_RECEIPT_PATH).unlink()
    zero_worktree = {"label": "worktree", "ok": True, "removed_bytes": 0, "summary": "ok", "error": None}
    zero_kanban = {"label": "kanban", "ok": True, "removed_bytes": 0, "summary": "ok", "error": None}
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_run_sweep", side_effect=[zero_worktree, zero_kanban]):
            with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
                rc = module.main()
    assert rc == 0
    sent_message = send.call_args[0][0]
    assert "pressure sweep reclaimed 0 bytes" in sent_message
    assert "reclaim path is broken" in sent_message


def test_low_disk_message_reports_sweep_failure(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    Path(module.TRIGGER_RECEIPT_PATH).unlink()
    failed = {"label": "worktree", "ok": False, "removed_bytes": None, "summary": None, "error": "boom"}
    kanban_ok = {"label": "kanban", "ok": True, "removed_bytes": 0, "summary": "ok", "error": None}
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_run_sweep", side_effect=[failed, kanban_ok]):
            with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
                rc = module.main()
    assert rc == 0
    sent_message = send.call_args[0][0]
    assert "FAILED to run" in sent_message
    assert "worktree" in sent_message


def test_trigger_failure_does_not_break_alert_delivery(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_trigger_pressure_sweeps", side_effect=RuntimeError("boom")):
            with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
                rc = module.main()
    assert rc == 0
    assert send.called
    sent_message = send.call_args[0][0]
    assert "disk space low" in sent_message.lower()


def test_pressure_trigger_skipped_within_cooldown_still_alerts(tmp_path):
    # _prepare() already seeds a "just triggered" receipt, so this tick should
    # skip triggering (cooldown) but the advisory Slack alert must still fire.
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(module.shutil, "disk_usage", return_value=_Usage(3)):
        with mock.patch.object(module, "_run_sweep") as run_sweep:
            with mock.patch.object(module, "_send_slack", return_value=_completed(0)) as send:
                rc = module.main()
    assert rc == 0
    assert not run_sweep.called
    assert send.called
    sent_message = send.call_args[0][0]
    assert "pressure sweep" not in sent_message
