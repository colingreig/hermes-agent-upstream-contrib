from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
