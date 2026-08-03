"""Contracts for profile/resource LLM admission in the shared cron path."""

from __future__ import annotations

import json
import os
import sqlite3

import pytest


def _job(job_id: str, profile: str, resources: list[str]) -> dict:
    return {
        "id": job_id,
        "name": job_id,
        "no_agent": False,
        "admission_profile": profile,
        "mutable_resources": resources,
    }


def _store(monkeypatch, tmp_path):
    import cron.executor_admission as admission

    monkeypatch.setattr(admission, "_database_path", lambda: tmp_path / "admission.db")
    return admission


def _stores(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return admission, executions


def test_disjoint_profiles_run_but_resource_and_same_task_conflicts_reject(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)
    executor = admission.acquire_job_admission_lease(
        job=_job("executor", "root/executor", ["clickup/task/{task_id}"]),
        task_id="task-a", owner_run_id="one", ledger_execution_id="one",
    )
    assert executor is not None
    validator = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["repo-validation/{task_id}"]),
        task_id="task-b", owner_run_id="two", ledger_execution_id="two",
    )
    assert validator is not None
    assert admission.acquire_job_admission_lease(
        job=_job("lifecycle", "root/lifecycle", ["clickup/task/{task_id}"]),
        task_id="task-a", owner_run_id="three", ledger_execution_id="three",
    ) is None
    assert admission.acquire_job_admission_lease(
        job=_job("validator-2", "root/validator", ["other"]),
        task_id="task-c", owner_run_id="four", ledger_execution_id="four",
    ) is None


def test_missing_metadata_fails_closed_and_no_agent_bypasses(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)

    with pytest.raises(admission.ExecutorAdmissionError, match="admission_profile"):
        admission.acquire_job_admission_lease(
            job={"id": "bad", "no_agent": False}, owner_run_id="one", ledger_execution_id="one"
        )
    assert admission.acquire_job_admission_lease(
        job={"id": "script", "no_agent": True}, owner_run_id="two", ledger_execution_id="two"
    ) is None


def test_shared_scheduler_path_applies_generic_admission(monkeypatch):
    import cron.scheduler as scheduler

    job = _job("executor", "root/executor", ["clickup/task/{task_id}"])
    job.update({"execution_id": "ledger", "execution_owner_token": "owner", "admission_task_id": "task"})
    lease = object()
    monkeypatch.setattr(scheduler, "acquire_executor_lease", lambda **kwargs: lease)
    monkeypatch.setattr(scheduler, "finalize_executor_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "release_executor_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_args: False)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda *_args, **_kwargs: object())
    assert scheduler.run_one_job(job) is False


def test_scheduled_template_is_canonical_and_expired_heartbeat_is_fenced(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)
    from datetime import timedelta
    import pytest

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    lease = admission.acquire_job_admission_lease(
        job=_job("scheduled", "root/lifecycle", ["clickup/lifecycle/{task_id}"]),
        owner_run_id="one", ledger_execution_id="one", lease_seconds=1,
    )
    assert lease is not None
    assert lease.mutable_resources == ("clickup/lifecycle/*",)
    clock[0] += timedelta(seconds=2)
    with pytest.raises(admission.ExecutorAdmissionError, match="heartbeat rejected"):
        admission.heartbeat_job_admission_lease(lease)


def test_scheduled_wildcard_conflicts_with_concrete_in_both_orders(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)
    wildcard = _job("sweep", "root/lifecycle", ["clickup/task/{task_id}"])
    concrete = _job("executor", "root/executor", ["clickup/task/{task_id}"])
    assert admission.acquire_job_admission_lease(job=wildcard, owner_run_id="one", ledger_execution_id="one")
    assert admission.acquire_job_admission_lease(job=concrete, task_id="t", owner_run_id="two", ledger_execution_id="two") is None
    admission = _store(monkeypatch, tmp_path / "other")
    assert admission.acquire_job_admission_lease(job=concrete, task_id="t", owner_run_id="three", ledger_execution_id="three")
    assert admission.acquire_job_admission_lease(job=wildcard, owner_run_id="four", ledger_execution_id="four") is None


def test_canonical_clickup_task_resource_allows_different_tasks_but_rejects_same(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)
    executor = _job("executor", "root/executor", ["clickup/task/{task_id}"])
    validator = _job("validator", "root/validator", ["clickup/task/{task_id}"])
    lifecycle = _job("lifecycle", "root/lifecycle", ["clickup/task/{task_id}"])
    assert admission.acquire_job_admission_lease(job=executor, task_id="a", owner_run_id="one", ledger_execution_id="one")
    assert admission.acquire_job_admission_lease(job=validator, task_id="b", owner_run_id="two", ledger_execution_id="two")
    assert admission.acquire_job_admission_lease(job=lifecycle, task_id="a", owner_run_id="three", ledger_execution_id="three") is None


def test_generic_recovery_requires_exact_dead_owner_proof(monkeypatch, tmp_path):
    admission = _store(monkeypatch, tmp_path)
    from datetime import timedelta
    import pytest

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    lease = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]), task_id="t",
        owner_run_id="owner", ledger_execution_id="ledger", lease_seconds=1,
    )
    clock[0] += timedelta(seconds=2)
    assert admission.recover_generic_admission_lease_before_execution_reap({"execution_id": "ledger", "job_id": "validator"}) is False
    proof = {"execution_id": "ledger", "job_id": "validator", "disposition": "stale", "owner_liveness": "dead", "proposed_terminal_status": "interrupted", "proposed_terminal_reason": "owner_dead"}
    assert admission.recover_generic_admission_lease_before_execution_reap(proof) is True


def test_quick_restart_waits_then_naturally_recovers_and_acquires(monkeypatch, tmp_path):
    admission, executions = _stores(monkeypatch, tmp_path)
    from datetime import timedelta

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    record = executions.create_execution("validator", source="builtin", lease_seconds=1)
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET pid=?, process_started_at=? WHERE id=?",
            (os.getpid(), -1, record["id"]),
        )
    first = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="first",
        owner_run_id="first-owner",
        ledger_execution_id=record["id"],
        lease_seconds=1,
    )
    assert first is not None

    assert admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="second",
        owner_run_id="second-owner",
        ledger_execution_id="second-ledger",
    ) is None

    recovery_calls = []
    recover = executions.recover_interrupted_executions

    def recover_once():
        recovery_calls.append(True)
        return recover()

    monkeypatch.setattr(executions, "recover_interrupted_executions", recover_once)
    clock[0] += timedelta(seconds=2)
    successor = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="second",
        owner_run_id="second-owner",
        ledger_execution_id="second-ledger",
    )

    assert successor is not None
    assert successor.fencing_token > first.fencing_token
    assert len(recovery_calls) == 1
    assert executions.latest_execution("validator")["status"] == "interrupted"
    with sqlite3.connect(admission._database_path()) as conn:
        states = conn.execute(
            "SELECT ledger_execution_id,state FROM admission_leases ORDER BY fencing_token"
        ).fetchall()
        receipt = conn.execute(
            "SELECT ledger_execution_id,reviewed_by FROM admission_recovery_receipts"
        ).fetchone()
    assert states == [(record["id"], "recovered"), ("second-ledger", "active")]
    assert receipt == (record["id"], "cron-startup-reaper")


def test_recovery_retry_reresolves_a_newer_pending_wake(monkeypatch, tmp_path):
    admission, executions = _stores(monkeypatch, tmp_path)
    from datetime import timedelta

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    record = executions.create_execution("validator", source="builtin", lease_seconds=1)
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET pid=?, process_started_at=? WHERE id=?",
            (os.getpid(), -1, record["id"]),
        )
    first = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="first",
        owner_run_id="first-owner",
        ledger_execution_id=record["id"],
        lease_seconds=1,
    )
    assert first is not None
    with sqlite3.connect(admission._database_path()) as conn:
        conn.execute(
            "INSERT INTO pending_wakes(job_id,task_id,reason,requested_at) "
            "VALUES (?,?,?,?)",
            ("validator", "old-task", "old", admission._iso(clock[0])),
        )

    recover = executions.recover_interrupted_executions

    def recover_then_refresh_wake():
        changed = recover()
        with sqlite3.connect(admission._database_path()) as conn:
            conn.execute(
                "UPDATE pending_wakes SET task_id=?,reason=?,requested_at=? "
                "WHERE job_id=?",
                ("new-task", "new", admission._iso(clock[0]), "validator"),
            )
        return changed

    monkeypatch.setattr(
        executions, "recover_interrupted_executions", recover_then_refresh_wake
    )
    clock[0] += timedelta(seconds=2)
    successor = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        owner_run_id="second-owner",
        ledger_execution_id="second-ledger",
    )

    assert successor is not None
    assert successor.task_id == "new-task"
    assert successor.mutable_resources == ("validation/new-task",)
    with sqlite3.connect(admission._database_path()) as conn:
        conn.row_factory = sqlite3.Row
        active = conn.execute(
            "SELECT task_id,mutable_resources_json FROM admission_leases "
            "WHERE state='active'"
        ).fetchone()
        pending = conn.execute(
            "SELECT task_id FROM pending_wakes WHERE job_id='validator'"
        ).fetchone()
    assert active["task_id"] == "new-task"
    assert json.loads(active["mutable_resources_json"]) == ["validation/new-task"]
    assert pending is None


def test_recovery_retry_recomputes_successor_lease_times(monkeypatch, tmp_path):
    admission, executions = _stores(monkeypatch, tmp_path)
    from datetime import timedelta

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    record = executions.create_execution("validator", source="builtin", lease_seconds=1)
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET pid=?, process_started_at=? WHERE id=?",
            (os.getpid(), -1, record["id"]),
        )
    first = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="first",
        owner_run_id="first-owner",
        ledger_execution_id=record["id"],
        lease_seconds=1,
    )
    assert first is not None
    recover = executions.recover_interrupted_executions

    def recover_with_elapsed_time():
        changed = recover()
        clock[0] += timedelta(seconds=30)
        return changed

    monkeypatch.setattr(
        executions, "recover_interrupted_executions", recover_with_elapsed_time
    )
    clock[0] += timedelta(seconds=2)
    successor = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="second",
        owner_run_id="second-owner",
        ledger_execution_id="second-ledger",
        lease_seconds=5,
    )

    assert successor is not None
    assert successor.acquired_at == admission._iso(clock[0])
    assert successor.heartbeat_at == admission._iso(clock[0])
    assert successor.expires_at == admission._iso(clock[0] + timedelta(seconds=5))


@pytest.mark.parametrize("owner_liveness", ["live", "unknown"])
def test_expired_live_or_unknown_owner_recovery_is_bounded_and_closed(
    monkeypatch, tmp_path, owner_liveness
):
    admission, executions = _stores(monkeypatch, tmp_path)
    from datetime import timedelta

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    monkeypatch.setattr(
        executions, "_owner_liveness", lambda *_args: owner_liveness
    )
    record = executions.create_execution("validator", source="builtin", lease_seconds=1)
    lease = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="first",
        owner_run_id="first-owner",
        ledger_execution_id=record["id"],
        lease_seconds=1,
    )
    assert lease is not None
    recovery_calls = []
    recover = executions.recover_interrupted_executions

    def recover_once():
        recovery_calls.append(True)
        return recover()

    monkeypatch.setattr(executions, "recover_interrupted_executions", recover_once)
    clock[0] += timedelta(seconds=2)

    with pytest.raises(admission.ExecutorAdmissionError, match="owner is uncertain"):
        admission.acquire_job_admission_lease(
            job=_job("validator", "root/validator", ["validation/{task_id}"]),
            task_id="second",
            owner_run_id="second-owner",
            ledger_execution_id="second-ledger",
        )

    assert len(recovery_calls) == 1
    assert executions.latest_execution("validator")["status"] == "claimed"
    with sqlite3.connect(admission._database_path()) as conn:
        row = conn.execute(
            "SELECT state,ledger_execution_id FROM admission_leases"
        ).fetchone()
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM admission_recovery_receipts"
        ).fetchone()[0]
    assert row == ("active", record["id"])
    assert receipt_count == 0


def test_expired_owner_with_mismatched_ledger_proof_remains_closed(monkeypatch, tmp_path):
    admission, executions = _stores(monkeypatch, tmp_path)
    from datetime import timedelta

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    mismatched = executions.create_execution(
        "validator", source="builtin", lease_seconds=1
    )
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET pid=?, process_started_at=? WHERE id=?",
            (os.getpid(), -1, mismatched["id"]),
        )
    lease = admission.acquire_job_admission_lease(
        job=_job("validator", "root/validator", ["validation/{task_id}"]),
        task_id="first",
        owner_run_id="first-owner",
        ledger_execution_id="different-ledger",
        lease_seconds=1,
    )
    assert lease is not None
    recovery_calls = []
    recover = executions.recover_interrupted_executions

    def recover_once():
        recovery_calls.append(True)
        return recover()

    monkeypatch.setattr(executions, "recover_interrupted_executions", recover_once)
    clock[0] += timedelta(seconds=2)

    with pytest.raises(admission.ExecutorAdmissionError, match="owner is uncertain"):
        admission.acquire_job_admission_lease(
            job=_job("validator", "root/validator", ["validation/{task_id}"]),
            task_id="second",
            owner_run_id="second-owner",
            ledger_execution_id="second-ledger",
        )

    assert len(recovery_calls) == 1
    assert executions.latest_execution("validator")["status"] == "interrupted"
    with sqlite3.connect(admission._database_path()) as conn:
        row = conn.execute(
            "SELECT state,ledger_execution_id FROM admission_leases"
        ).fetchone()
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM admission_recovery_receipts"
        ).fetchone()[0]
    assert row == ("active", "different-ledger")
    assert receipt_count == 0
