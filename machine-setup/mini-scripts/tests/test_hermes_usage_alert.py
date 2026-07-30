from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "hermes_usage_alert.py"
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    dependency_root = SCRIPTS / "pr_pipeline"
    sys.path.insert(0, str(dependency_root))
    try:
        spec = importlib.util.spec_from_file_location(f"hermes_usage_alert_ut_{_COUNTER}", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(dependency_root))
    return module


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["hermes"], returncode, stdout, stderr)


def _prepare(module, tmp_path, *, status="error"):
    module.STATE_PATH = str(tmp_path / "state.json")
    module.RECEIPT_PATH = str(tmp_path / "receipt.json")
    module.JOBS_PATH = str(tmp_path / "jobs.json")
    module.LOGS = []
    Path(module.JOBS_PATH).write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "fixture",
                        "last_status": status,
                        "last_error": "synthetic",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_slack_failure_does_not_advance_offsets_or_dedup(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    with mock.patch.object(
        module, "_send_slack", return_value=_completed(1, stderr="offline")
    ):
        assert module.main() == 2
    assert not Path(module.STATE_PATH).exists()
    assert json.loads(Path(module.RECEIPT_PATH).read_text())["delivery"] == "failed"


def test_confirmed_slack_delivery_persists_dedup_state(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path)
    sender = mock.Mock(return_value=_completed())
    with mock.patch.object(module, "_send_slack", sender):
        assert module.main() == 0
    assert sender.call_count == 1
    assert sender.call_args.args[0].startswith("🚨")
    state = json.loads(Path(module.STATE_PATH).read_text(encoding="utf-8"))
    assert state["cron_job_statuses"]["job-1"] == "error"
    assert state["cron_job_error_last_alert"]["job-1"]
    assert json.loads(Path(module.RECEIPT_PATH).read_text())["delivery"] == "confirmed"


def test_clean_scan_is_silent_but_records_observation(tmp_path):
    module = _load_module()
    _prepare(module, tmp_path, status="ok")
    with mock.patch.object(module, "_send_slack") as sender:
        assert module.main() == 0
    sender.assert_not_called()
    state = json.loads(Path(module.STATE_PATH).read_text(encoding="utf-8"))
    assert state["cron_job_statuses"]["job-1"] == "ok"
    assert json.loads(Path(module.RECEIPT_PATH).read_text()) == {
        "checked_at": mock.ANY,
        "delivery": "confirmed",
        "schema_version": 1,
        "status": "clean",
    }
