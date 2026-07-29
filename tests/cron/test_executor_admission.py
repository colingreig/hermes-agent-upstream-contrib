"""Behavior contracts for the singleton autonomous-executor admission lease."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import sqlite3

import pytest


def _store(monkeypatch, tmp_path):
    import cron.executor_admission as admission

    database = tmp_path / "state" / "executor-admission.db"
    monkeypatch.setattr(admission, "_database_path", lambda: database)
    return admission, database


def test_lease_contains_required_identity_and_fences_all_executor_jobs(
    monkeypatch, tmp_path
):
    admission, _database = _store(monkeypatch, tmp_path)

    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="86e2gmgc6",
        owner_run_id="run-primary",
        ledger_execution_id="ledger-primary",
    )

    assert lease is not None
    assert set(lease.as_dict()) == {
        "task_id",
        "job_id",
        "owner_run_id",
        "fencing_token",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
        "ledger_execution_id",
    }
    assert lease.task_id == "86e2gmgc6"
    assert admission.acquire_executor_lease(
        job_id="baa3251e033d",
        task_id="another-task",
        owner_run_id="run-secondary",
        ledger_execution_id="ledger-secondary",
    ) is None

    refreshed = admission.heartbeat_executor_lease(lease)
    assert refreshed.fencing_token == lease.fencing_token
    admission.finalize_executor_lease(refreshed, status="completed")
    admission.release_executor_lease(refreshed)

    successor = admission.acquire_executor_lease(
        job_id="baa3251e033d",
        task_id="another-task",
        owner_run_id="run-secondary",
        ledger_execution_id="ledger-secondary",
    )
    assert successor is not None
    assert successor.fencing_token > lease.fencing_token


def test_every_mutation_rejects_a_stale_fencing_token(monkeypatch, tmp_path):
    admission, _database = _store(monkeypatch, tmp_path)
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="ledger",
    )
    assert lease is not None
    stale = replace(lease, fencing_token=lease.fencing_token + 1)

    with pytest.raises(admission.ExecutorAdmissionError, match="heartbeat rejected"):
        admission.heartbeat_executor_lease(stale)
    with pytest.raises(admission.ExecutorAdmissionError, match="finalization rejected"):
        admission.finalize_executor_lease(stale, status="failed")
    with pytest.raises(admission.ExecutorAdmissionError, match="release rejected"):
        admission.release_executor_lease(stale)

    assert admission.executor_drain_status()["lease"]["fencing_token"] == (
        lease.fencing_token
    )


def test_expiry_never_reclaims_an_uncertain_owner(monkeypatch, tmp_path):
    admission, _database = _store(monkeypatch, tmp_path)
    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="ledger",
        lease_seconds=1,
    )
    assert lease is not None
    clock[0] += timedelta(seconds=2)

    assert admission.acquire_executor_lease(
        job_id="baa3251e033d",
        task_id="other",
        owner_run_id="other-owner",
        ledger_execution_id="other-ledger",
    ) is None
    with pytest.raises(admission.ExecutorAdmissionError, match="heartbeat rejected"):
        admission.heartbeat_executor_lease(lease)

    status = admission.executor_drain_status()
    assert status["safe_to_cutover"] is False
    assert status["lease"]["expired"] is True
    assert status["mutated"] is False


def test_drain_status_never_reaps_or_kills(monkeypatch, tmp_path):
    admission, _database = _store(monkeypatch, tmp_path)
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="ledger",
    )
    assert lease is not None

    first = admission.executor_drain_status()
    second = admission.executor_drain_status()
    assert first == second
    assert first["safe_to_cutover"] is False
    assert first["state"] == "active"

    admission.finalize_executor_lease(lease, status="completed")
    admission.release_executor_lease(lease)
    assert admission.executor_drain_status()["safe_to_cutover"] is True


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(admission.ExecutorAdmissionError):
        admission.acquire_executor_lease(
            job_id="62714b869845",
            task_id="task",
            owner_run_id="owner",
            ledger_execution_id="ledger",
        )
    assert database.read_bytes() == b"not a sqlite database"


def test_gate_wake_is_consumed_by_the_admitted_run(monkeypatch, tmp_path):
    admission, _database = _store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "cron.jobs.request_job_run_now", lambda job_id: job_id == "62714b869845"
    )

    assert admission.request_executor_wake(
        job_id="62714b869845",
        task_id="86e2gmgc6",
        reason="validator repair",
    )
    pending = admission.executor_drain_status()["pending_wakes"]
    assert pending[0]["task_id"] == "86e2gmgc6"

    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id=None,
        owner_run_id="run",
        ledger_execution_id="ledger",
    )
    assert lease is not None
    assert lease.task_id == "86e2gmgc6"
    assert admission.executor_drain_status()["pending_wakes"] == []


def test_locked_store_fails_closed(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    idle = admission.executor_drain_status()
    assert idle["safe_to_cutover"] is True

    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(
            admission.ExecutorAdmissionError,
            match="database unavailable|acquisition failed",
        ):
            admission.acquire_executor_lease(
                job_id="62714b869845",
                task_id="task",
                owner_run_id="owner",
                ledger_execution_id="ledger",
            )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_shared_run_body_denies_executor_before_agent_dispatch(monkeypatch):
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_args, **_kwargs: {
            "id": "ledger",
            "owner_token": "ledger-token",
        },
    )
    monkeypatch.setattr(
        scheduler, "acquire_executor_lease", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        scheduler,
        "claim_dispatch",
        lambda *_args, **_kwargs: pytest.fail(
            "dispatch claim must not run after executor admission denial"
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: pytest.fail(
            "agent body must not run after executor admission denial"
        ),
    )
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded["reason"] = outcome.reason
        return False

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "skill": "clickup-queue-poller",
        }
    ) is False
    assert recorded["reason"] == "executor_admission_denied"
