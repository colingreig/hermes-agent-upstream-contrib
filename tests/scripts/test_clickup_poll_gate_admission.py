"""The ClickUp poll gate may request work but must never launch an executor."""

from __future__ import annotations

import cron.executor_admission as admission
import scripts.clickup_poll_gate as gate


def test_wake_routes_through_authoritative_admission(monkeypatch, capsys):
    captured = {}

    def request_executor_wake(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(admission, "request_executor_wake", request_executor_wake)

    assert gate._wake(
        "continuation",
        gate.EXECUTOR_ID,
        task_id="86e2gmgc6",
    ) is True
    assert captured == {
        "job_id": gate.EXECUTOR_ID,
        "task_id": "86e2gmgc6",
        "reason": "continuation",
    }
    assert "gateway ticker owns launch" in capsys.readouterr().out


def test_wake_fails_closed_when_admission_is_uncertain(monkeypatch, capsys):
    def reject(**_kwargs):
        raise admission.ExecutorAdmissionError("locked")

    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(admission, "request_executor_wake", reject)

    assert gate._wake("unclaimed", gate.EXECUTOR_ID) is False
    assert "failed closed" in capsys.readouterr().err
