"""Delivery-aware incident dedup for the canonical mini backup script."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "offbox_restic_backup.py"

op_sdk_stub = types.ModuleType("op_sdk_resolve")
op_sdk_stub.resolve_all_fields = lambda *_args, **_kwargs: {}
sys.modules["op_sdk_resolve"] = op_sdk_stub

spec = importlib.util.spec_from_file_location("offbox_restic_backup_test", SCRIPT)
backup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backup
spec.loader.exec_module(backup)


def test_unsent_failure_remains_retryable(monkeypatch, tmp_path):
    state_path = tmp_path / "offbox-backup-monitor.json"
    monkeypatch.setattr(backup, "BACKUP_STATE_PATH", str(state_path))
    outcomes = iter([False, True])
    sent = []

    def send(message):
        sent.append(message)
        return next(outcomes)

    monkeypatch.setattr(backup, "send_failure_alert", send)

    assert not backup.maybe_send_failure_alert("mini", RuntimeError("R2 unreachable"))
    assert not state_path.exists()

    assert backup.maybe_send_failure_alert("mini", RuntimeError("R2 unreachable"))
    assert len(sent) == 2
    assert json.loads(state_path.read_text())["last_alert_signature"]

    assert backup.maybe_send_failure_alert("mini", RuntimeError("R2 unreachable"))
    assert len(sent) == 2


def test_unsent_recovery_preserves_failure_for_retry(monkeypatch, tmp_path):
    state_path = tmp_path / "offbox-backup-monitor.json"
    state_path.write_text(json.dumps({"last_alert_signature": "mini:RuntimeError:R2 unreachable"}))
    monkeypatch.setattr(backup, "BACKUP_STATE_PATH", str(state_path))
    outcomes = iter([False, True])
    sent = []

    def send(message):
        sent.append(message)
        return next(outcomes)

    monkeypatch.setattr(backup, "send_failure_alert", send)

    assert not backup.maybe_send_recovery_alert("mini")
    assert json.loads(state_path.read_text())["last_alert_signature"]

    assert backup.maybe_send_recovery_alert("mini")
    assert len(sent) == 2
    assert json.loads(state_path.read_text())["last_alert_signature"] is None


def test_real_transport_boundary_reports_confirmed_delivery(monkeypatch, tmp_path):
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/bin/sh\n")
    hermes.chmod(0o755)
    monkeypatch.setattr(backup, "HERMES_BIN", str(hermes))
    monkeypatch.delenv("HERMES_BACKUP_NO_ALERT", raising=False)
    monkeypatch.setattr(backup, "resolve_slack_bot_token", lambda: "xoxb-test")
    calls = []

    def successful_run(cmd, *, env=None, check=False):
        calls.append((cmd, env, check))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup, "run", successful_run)

    assert backup.send_failure_alert("transport probe")
    assert len(calls) == 1
    assert calls[0][0] == [
        str(hermes),
        "send",
        "--to",
        backup.SLACK_TARGET,
        "transport probe",
    ]
    assert calls[0][1]["SLACK_BOT_TOKEN"] == "xoxb-test"
    assert calls[0][2] is True


def test_real_transport_boundary_reports_unsent(monkeypatch, tmp_path):
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/bin/sh\n")
    hermes.chmod(0o755)
    monkeypatch.setattr(backup, "HERMES_BIN", str(hermes))
    monkeypatch.delenv("HERMES_BACKUP_NO_ALERT", raising=False)
    monkeypatch.setattr(backup, "resolve_slack_bot_token", lambda: None)
    monkeypatch.setattr(
        backup,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(backup.BackupError("send failed")),
    )

    assert not backup.send_failure_alert("transport probe")
