"""Behavior contracts for the singleton autonomous-executor admission lease."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest


def _store(monkeypatch, tmp_path):
    import cron.executor_admission as admission

    database = tmp_path / "state" / "executor-admission.db"
    monkeypatch.setattr(admission, "_database_path", lambda: database)
    return admission, database


def test_production_executor_policy_matches_rebuilt_fleet_and_shares_singleton(
    monkeypatch, tmp_path
):
    admission, _database = _store(monkeypatch, tmp_path)
    root = Path(__file__).resolve().parents[2]
    fleet = json.loads(
        (root / "machine-setup" / "fleet-config" / "jobs.json").read_text()
    )["jobs"]
    policy_names = frozenset(admission.PRODUCTION_EXECUTOR_JOBS.values())
    fleet_executors = {
        str(job["id"]): str(job["name"])
        for job in fleet
        if str(job.get("name")) in policy_names
    }

    assert fleet_executors == admission.PRODUCTION_EXECUTOR_JOBS
    assert not admission.RETIRED_EXECUTOR_JOB_IDS.intersection(
        str(job["id"]) for job in fleet
    )
    assert all(
        admission.is_executor_job(job)
        for job in fleet
        if str(job["id"]) in fleet_executors
    )

    code = admission.acquire_executor_lease(
        job_id="62714b869845",
        owner_run_id="code-owner",
        ledger_execution_id="code-ledger",
    )
    assert code is not None
    assert admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        owner_run_id="content-owner",
        ledger_execution_id="content-ledger",
    ) is None
    admission.release_executor_lease(code)
    assert admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        owner_run_id="content-owner",
        ledger_execution_id="content-ledger",
    ) is not None

    retired = {
        "id": "baa3251e033d",
        "name": "clickup-executor-2",
        "skill": "clickup-queue-poller",
    }
    assert admission.is_executor_job(retired)
    with pytest.raises(admission.ExecutorAdmissionError, match="retired"):
        admission.acquire_executor_lease(
            job_id=retired["id"],
            owner_run_id="retired-owner",
            ledger_execution_id="retired-ledger",
        )


def test_lease_contains_required_identity_and_fences_all_executor_jobs(
    monkeypatch, tmp_path
):
    admission, database = _store(monkeypatch, tmp_path)

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
        job_id="dcab830aa41c",
        task_id="another-task",
        owner_run_id="run-secondary",
        ledger_execution_id="ledger-secondary",
    ) is None

    refreshed = admission.heartbeat_executor_lease(lease)
    assert refreshed.fencing_token == lease.fencing_token
    admission.finalize_executor_lease(refreshed, status="completed")
    admission.release_executor_lease(refreshed)

    successor = admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        task_id="another-task",
        owner_run_id="run-secondary",
        ledger_execution_id="ledger-secondary",
    )
    assert successor is not None
    assert successor.fencing_token > lease.fencing_token
    admission.release_executor_lease(successor)
    with sqlite3.connect(database) as conn:
        released = conn.execute(
            "SELECT state,terminal_status,finalized_at,released_at "
            "FROM executor_lease_history WHERE fencing_token=?",
            (successor.fencing_token,),
        ).fetchone()
    assert released[0:2] == ("released", "interrupted")
    assert released[2] == released[3]


def test_history_preserves_immutable_identity_through_lifecycle(
    monkeypatch, tmp_path
):
    admission, database = _store(monkeypatch, tmp_path)
    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="ledger",
    )
    assert lease is not None
    clock[0] += timedelta(seconds=10)
    refreshed = admission.heartbeat_executor_lease(lease)
    clock[0] += timedelta(seconds=10)
    admission.finalize_executor_lease(refreshed, status="completed")
    clock[0] += timedelta(seconds=10)
    admission.release_executor_lease(refreshed)

    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM executor_lease_history WHERE fencing_token=?",
            (lease.fencing_token,),
        ).fetchone()
        assert row is not None
        assert {
            "task_id": row["task_id"],
            "job_id": row["job_id"],
            "owner_run_id": row["owner_run_id"],
            "fencing_token": row["fencing_token"],
            "ledger_execution_id": row["ledger_execution_id"],
        } == {
            "task_id": "task",
            "job_id": "62714b869845",
            "owner_run_id": "owner",
            "fencing_token": lease.fencing_token,
            "ledger_execution_id": "ledger",
        }
        assert row["state"] == "released"
        assert row["terminal_status"] == "completed"
        assert row["finalized_at"] is not None
        assert row["released_at"] is not None
        assert row["revision"] == 4
        with pytest.raises(
            sqlite3.IntegrityError, match="history identity is immutable"
        ):
            conn.execute(
                "UPDATE executor_lease_history SET task_id='changed' "
                "WHERE fencing_token=?",
                (lease.fencing_token,),
            )


def test_v1_store_migrates_current_lease_into_history(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE executor_lease (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                task_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                owner_run_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                ledger_execution_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active', 'finalized')),
                terminal_status TEXT,
                finalized_at TEXT
            );
            INSERT INTO executor_lease VALUES (
                1, 'legacy-task', '62714b869845', 'legacy-owner', 9,
                '2026-07-29T10:00:00+00:00',
                '2026-07-29T10:01:00+00:00',
                '2026-07-29T10:03:00+00:00',
                'legacy-ledger', 'finalized', 'completed',
                '2026-07-29T10:02:00+00:00'
            );
            PRAGMA user_version = 1;
            """
        )
    os.chmod(database, 0o600)

    assert admission.executor_drain_status()["state"] == "finalized"

    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT task_id,owner_run_id,fencing_token,ledger_execution_id,"
            "state,terminal_status,revision FROM executor_lease_history"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert row == (
        "legacy-task",
        "legacy-owner",
        9,
        "legacy-ledger",
        "finalized",
        "completed",
        1,
    )
    assert version == admission._SCHEMA_VERSION == 2


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
        job_id="dcab830aa41c",
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


def test_expired_owner_recovery_requires_reviewed_exact_dead_owner_proof(
    monkeypatch, tmp_path
):
    admission, database = _store(monkeypatch, tmp_path)
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

    monkeypatch.setattr(
        admission,
        "_reviewed_dead_owner_proof",
        lambda _row: (_ for _ in ()).throw(
            admission.ExecutorAdmissionError("owner is not proven dead")
        ),
    )
    with pytest.raises(admission.ExecutorAdmissionError, match="not proven dead"):
        admission.recover_expired_executor_lease(
            owner_run_id=lease.owner_run_id,
            fencing_token=lease.fencing_token,
            ledger_execution_id=lease.ledger_execution_id,
            reviewed_by="validator",
            reason="reviewed exact PID and start-time proof",
        )
    assert admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        owner_run_id="successor",
        ledger_execution_id="successor-ledger",
    ) is None

    proof = {
        "execution_id": "ledger",
        "job_id": "62714b869845",
        "disposition": "stale",
        "owner_liveness": "dead",
        "proposed_terminal_status": "interrupted",
        "proposed_terminal_reason": "lease_expired_owner_dead",
    }
    monkeypatch.setattr(admission, "_reviewed_dead_owner_proof", lambda _row: proof)
    receipt = admission.recover_expired_executor_lease(
        owner_run_id=lease.owner_run_id,
        fencing_token=lease.fencing_token,
        ledger_execution_id=lease.ledger_execution_id,
        reviewed_by="validator",
        reason="reviewed exact PID and start-time proof",
    )
    assert receipt["proof"] == proof
    assert admission.executor_drain_status()["state"] == "finalized"

    successor = admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        owner_run_id="successor",
        ledger_execution_id="successor-ledger",
    )
    assert successor is not None
    assert successor.fencing_token > lease.fencing_token
    with sqlite3.connect(database) as conn:
        stored = conn.execute(
            "SELECT reviewed_by,owner_run_id,fencing_token FROM recovery_receipts"
        ).fetchone()
        history = conn.execute(
            "SELECT state,terminal_status,recovered_at,recovery_receipt_id "
            "FROM executor_lease_history WHERE fencing_token=?",
            (lease.fencing_token,),
        ).fetchone()
    assert stored == ("validator", "owner", lease.fencing_token)
    assert history == (
        "recovered",
        "interrupted",
        receipt["recovered_at"],
        receipt["receipt_id"],
    )


def test_recovery_rejects_wrong_owner_fence_or_ledger(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        admission,
        "_reviewed_dead_owner_proof",
        lambda _row: pytest.fail("wrong identity must fail before proof review"),
    )

    for owner, fence, ledger in (
        ("wrong", lease.fencing_token, "ledger"),
        ("owner", lease.fencing_token + 1, "ledger"),
        ("owner", lease.fencing_token, "wrong"),
    ):
        with pytest.raises(admission.ExecutorAdmissionError, match="exact fencing"):
            admission.recover_expired_executor_lease(
                owner_run_id=owner,
                fencing_token=fence,
                ledger_execution_id=ledger,
                reviewed_by="validator",
                reason="review",
            )


def test_admission_rejects_symlink_and_unsafe_permissions(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    database.parent.mkdir(parents=True)
    target = tmp_path / "attacker.db"
    target.write_bytes(b"not trusted")
    database.symlink_to(target)

    with pytest.raises(admission.ExecutorAdmissionError, match="symlink"):
        admission.acquire_executor_lease(
            job_id="62714b869845",
            owner_run_id="owner",
            ledger_execution_id="ledger",
        )


def test_admission_rejects_symlinked_parent(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    attacker_directory = tmp_path / "attacker"
    attacker_directory.mkdir()
    database.parent.symlink_to(attacker_directory, target_is_directory=True)

    result = admission.executor_drain_status()
    assert result["state"] == "unknown"
    assert "symlink" in result["error"]


@pytest.mark.parametrize("unsafe_target", ["directory", "database"])
def test_admission_store_rejects_group_or_world_writable_path(
    monkeypatch, tmp_path, unsafe_target
):
    admission, database = _store(monkeypatch, tmp_path)
    if unsafe_target == "directory":
        database.parent.mkdir(parents=True)
        os.chmod(database.parent, 0o777)
    else:
        admission.executor_drain_status()
        os.chmod(database, 0o666)

    result = admission.executor_drain_status()
    assert result["state"] == "unknown"
    assert "group/world-writable" in result["error"]


def test_admission_store_accepts_matching_posix_owner(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    database.parent.mkdir(parents=True)
    owner_uid = database.parent.lstat().st_uid
    monkeypatch.setattr(admission.os, "getuid", lambda: owner_uid, raising=False)

    assert admission.executor_drain_status()["state"] == "idle"


def test_admission_store_rejects_non_owner(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    database.parent.mkdir(parents=True)
    owner_uid = database.parent.lstat().st_uid
    monkeypatch.setattr(admission.os, "getuid", lambda: owner_uid + 1, raising=False)

    result = admission.executor_drain_status()
    assert result["state"] == "unknown"
    assert "not owned by the current user" in result["error"]


def test_admission_store_rejects_unverifiable_owner_without_posix_uid_api(
    monkeypatch, tmp_path
):
    admission, _database = _store(monkeypatch, tmp_path)
    monkeypatch.delattr(admission.os, "getuid", raising=False)

    result = admission.executor_drain_status()
    assert result["state"] == "unknown"
    assert result["safe_to_cutover"] is False
    assert "ownership cannot be verified" in result["error"]
    assert "POSIX user identity API is unavailable" in result["error"]


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


def test_heartbeat_ownership_loss_signals_running_executor_promptly(monkeypatch):
    import cron.scheduler as scheduler

    lease = scheduler.ExecutorLease(
        task_id="task",
        job_id="62714b869845",
        owner_run_id="owner",
        fencing_token=1,
        acquired_at="2026-07-29T12:00:00+00:00",
        heartbeat_at="2026-07-29T12:00:00+00:00",
        expires_at="2026-07-29T12:02:00+00:00",
        ledger_execution_id="ledger",
    )
    monkeypatch.setattr(scheduler, "_EXECUTION_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(
        scheduler,
        "heartbeat_executor_lease",
        lambda _lease: (_ for _ in ()).throw(
            scheduler.ExecutorAdmissionError("fencing ownership lost")
        ),
    )
    heartbeat = scheduler._ExecutorAdmissionHeartbeat(lease)

    heartbeat.start()
    assert heartbeat.cancel_event.wait(timeout=0.5)
    heartbeat.stop()
    assert heartbeat.error is not None


def test_executor_run_receives_heartbeat_cancellation_event(monkeypatch):
    import cron.scheduler as scheduler

    lease = scheduler.ExecutorLease(
        task_id="task",
        job_id="62714b869845",
        owner_run_id="owner",
        fencing_token=1,
        acquired_at="2026-07-29T12:00:00+00:00",
        heartbeat_at="2026-07-29T12:00:00+00:00",
        expires_at="2026-07-29T12:02:00+00:00",
        ledger_execution_id="ledger",
    )
    monkeypatch.setattr(scheduler, "_EXECUTION_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(scheduler, "acquire_executor_lease", lambda **_kwargs: lease)
    monkeypatch.setattr(
        scheduler,
        "heartbeat_executor_lease",
        lambda _lease: (_ for _ in ()).throw(
            scheduler.ExecutorAdmissionError("fencing ownership lost")
        ),
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_args: True)
    monkeypatch.setattr(
        scheduler, "mark_execution_running", lambda *_args, **_kwargs: {"id": "ledger"}
    )

    class NoopExecutionHeartbeat:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(scheduler, "_ExecutionLeaseHeartbeat", NoopExecutionHeartbeat)
    observed = {}

    def fake_run_job(
        _job, *, defer_agent_teardown=None, cancellation_event=None
    ):
        started = time.monotonic()
        assert cancellation_event is not None
        assert cancellation_event.wait(timeout=0.5)
        observed["elapsed"] = time.monotonic() - started
        return False, "", "", "cancelled"

    monkeypatch.setattr(scheduler, "run_job", fake_run_job)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler,
        "_record_cron_outcome",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(scheduler, "finalize_executor_lease", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "release_executor_lease", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "execution_id": "ledger",
            "execution_owner_token": "ledger-token",
        }
    ) is False
    assert observed["elapsed"] < 0.5


def test_env_scope_reset_runs_even_when_executor_finalize_fails(monkeypatch):
    import cron.scheduler as scheduler
    from tools import env_passthrough

    lease = scheduler.ExecutorLease(
        task_id="task",
        job_id="62714b869845",
        owner_run_id="owner",
        fencing_token=1,
        acquired_at="2026-07-29T12:00:00+00:00",
        heartbeat_at="2026-07-29T12:00:00+00:00",
        expires_at="2026-07-29T12:02:00+00:00",
        ledger_execution_id="ledger",
    )
    monkeypatch.setattr(scheduler, "acquire_executor_lease", lambda **_kwargs: lease)
    monkeypatch.setattr(scheduler._ExecutorAdmissionHeartbeat, "start", lambda _self: None)
    monkeypatch.setattr(scheduler._ExecutorAdmissionHeartbeat, "stop", lambda _self: None)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_args: False)
    monkeypatch.setattr(
        scheduler,
        "_record_cron_outcome",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        scheduler,
        "finalize_executor_lease",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            scheduler.ExecutorAdmissionError("finalization failed")
        ),
    )
    released = []
    reset = []
    monkeypatch.setattr(
        scheduler,
        "release_executor_lease",
        lambda *_args, **_kwargs: released.append(True),
    )
    monkeypatch.setattr(
        env_passthrough,
        "reset_env_passthrough_scope",
        lambda token: reset.append(token),
    )

    with pytest.raises(scheduler.ExecutorAdmissionError, match="finalization failed"):
        scheduler.run_one_job(
            {
                "id": "62714b869845",
                "name": "clickup-executor",
                "execution_id": "ledger",
                "execution_owner_token": "ledger-token",
            }
        )
    assert reset
    assert released == []
