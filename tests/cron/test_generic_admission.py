"""Contracts for profile/resource LLM admission in the shared cron path."""

from __future__ import annotations

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
