"""Behavior contracts for the singleton autonomous-executor admission lease."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest


def _simultaneous_acquire(start, results, database, job_id, owner, ledger):
    import cron.executor_admission as admission

    admission._database_path = lambda: Path(database)
    start.wait()
    try:
        lease = admission.acquire_executor_lease(
            job_id=job_id,
            owner_run_id=owner,
            ledger_execution_id=ledger,
        )
        results.put(("ok", None if lease is None else lease.as_dict()))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


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

    with pytest.raises(
        admission.ExecutorAdmissionError,
        match="expired.*uncertain",
    ):
        admission.acquire_executor_lease(
            job_id="dcab830aa41c",
            task_id="other",
            owner_run_id="other-owner",
            ledger_execution_id="other-ledger",
        )
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
    with pytest.raises(
        admission.ExecutorAdmissionError,
        match="expired.*uncertain",
    ):
        admission.acquire_executor_lease(
            job_id="dcab830aa41c",
            owner_run_id="successor",
            ledger_execution_id="successor-ledger",
        )

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


def test_bootstrap_failure_fails_closed_without_partial_schema(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE admission_state(singleton INTEGER PRIMARY KEY);
            PRAGMA user_version = 1;
            """
        )
    os.chmod(database, 0o600)

    with pytest.raises(
        admission.ExecutorAdmissionError,
        match="database unavailable",
    ):
        admission.acquire_executor_lease(
            job_id="62714b869845",
            task_id="task",
            owner_run_id="owner",
            ledger_execution_id="ledger",
        )

    with sqlite3.connect(database) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert tables == {"admission_state"}
    assert version == 1


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


def test_simultaneous_first_use_across_both_lanes_has_one_clean_loser(
    monkeypatch, tmp_path
):
    _admission, database = _store(monkeypatch, tmp_path)
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_simultaneous_acquire,
            args=(
                start,
                results,
                str(database),
                job_id,
                owner,
                ledger,
            ),
        )
        for job_id, owner, ledger in (
            ("62714b869845", "code-owner", "code-ledger"),
            ("dcab830aa41c", "content-owner", "content-ledger"),
        )
    ]
    for process in processes:
        process.start()
    start.set()
    observed = [results.get(timeout=5) for _process in processes]
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert [kind for kind, _value in observed] == ["ok", "ok"]
    leases = [value for _kind, value in observed if value is not None]
    assert len(leases) == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM executor_lease WHERE state='active'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM executor_lease_history"
        ).fetchone()[0] == 1


def test_locked_store_waits_within_bound_then_acquires(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    idle = admission.executor_drain_status()
    assert idle["safe_to_cutover"] is True

    blocker = sqlite3.connect(database, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    result = {}

    def acquire():
        result["lease"] = admission.acquire_executor_lease(
            job_id="62714b869845",
            task_id="task",
            owner_run_id="owner",
            ledger_execution_id="ledger",
        )

    worker = threading.Thread(target=acquire)
    worker.start()
    time.sleep(0.1)
    blocker.execute("ROLLBACK")
    blocker.close()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result["lease"] is not None


def test_reader_lock_through_commit_waits_then_acquires(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    assert admission.executor_drain_status()["safe_to_cutover"] is True
    reader = sqlite3.connect(database, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM admission_state").fetchall()
    result = {}

    def acquire():
        try:
            result["lease"] = admission.acquire_executor_lease(
                job_id="62714b869845",
                task_id="task",
                owner_run_id="owner",
                ledger_execution_id="ledger",
            )
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=acquire)
    worker.start()
    time.sleep(0.15)
    reader.execute("ROLLBACK")
    reader.close()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert "error" not in result
    assert result["lease"] is not None


def test_reader_lock_through_cleanup_commit_waits_then_releases(
    monkeypatch, tmp_path
):
    admission, database = _store(monkeypatch, tmp_path)
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="ledger",
    )
    assert lease is not None
    reader = sqlite3.connect(database, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM executor_lease").fetchall()
    result = {}

    def release():
        try:
            admission.release_executor_lease(lease)
            result["released"] = True
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=release)
    worker.start()
    time.sleep(0.15)
    reader.execute("ROLLBACK")
    reader.close()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == {"released": True}
    assert admission.executor_drain_status()["state"] == "idle"


def test_locked_store_past_bound_fails_closed_without_mutation(monkeypatch, tmp_path):
    admission, database = _store(monkeypatch, tmp_path)
    assert admission.executor_drain_status()["safe_to_cutover"] is True
    monkeypatch.setattr(admission, "_SQLITE_BUSY_DEADLINE_SECONDS", 0.1)

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

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM executor_lease").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM executor_lease_history"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT last_fencing_token FROM admission_state"
        ).fetchone()[0] == 0


def test_genuine_connect_failure_is_not_retried(monkeypatch, tmp_path):
    admission, _database = _store(monkeypatch, tmp_path)
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(True)
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(admission.sqlite3, "connect", unavailable)

    with pytest.raises(
        admission.ExecutorAdmissionError,
        match="database unavailable",
    ):
        admission.acquire_executor_lease(
            job_id="62714b869845",
            task_id="task",
            owner_run_id="owner",
            ledger_execution_id="ledger",
        )
    assert calls == [True]


def test_shared_run_body_records_clean_no_claim_before_agent_dispatch(monkeypatch):
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
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda *_args, **_kwargs: pytest.fail(
            "a clean no-claim must not mark execution running"
        ),
    )
    saved = {}
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda job_id, output: saved.update(job_id=job_id, output=output),
    )
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded["success"] = outcome.success
        recorded["reason"] = outcome.reason
        recorded["error"] = outcome.error
        return outcome.success

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "skill": "clickup-queue-poller",
        }
    ) is True
    assert recorded == {
        "success": True,
        "reason": "executor_admission_no_claim",
        "error": None,
    }
    assert saved["job_id"] == "62714b869845"
    assert "## Response" in saved["output"]
    assert "Zero ClickUp claims and zero swarms" in saved["output"]
    assert "another positively observed fenced executor owner" in saved["output"]


def test_shared_run_body_keeps_admission_uncertainty_red_without_receipt(
    monkeypatch,
):
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
        scheduler,
        "acquire_executor_lease",
        lambda **_kwargs: (_ for _ in ()).throw(
            scheduler.ExecutorAdmissionError("database is corrupt")
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "save_job_output",
        lambda *_args, **_kwargs: pytest.fail(
            "uncertain admission must not emit a green no-claim receipt"
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "claim_dispatch",
        lambda *_args, **_kwargs: pytest.fail(
            "uncertain admission must not reach dispatch"
        ),
    )
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded.update(
            success=outcome.success,
            reason=outcome.reason,
            error=outcome.error,
        )
        return outcome.success

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "skill": "clickup-queue-poller",
        }
    ) is False
    assert recorded["success"] is False
    assert recorded["reason"] == "executor_admission_uncertain"
    assert "database is corrupt" in recorded["error"]


def test_expired_active_lease_is_scheduler_uncertainty_not_green_no_claim(
    monkeypatch, tmp_path
):
    admission, _database = _store(monkeypatch, tmp_path)
    import cron.scheduler as scheduler

    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="existing-ledger",
        lease_seconds=1,
    )
    assert lease is not None
    clock[0] += timedelta(seconds=2)
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_args, **_kwargs: {
            "id": "new-ledger",
            "owner_token": "new-token",
        },
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler,
        "claim_dispatch",
        lambda *_args, **_kwargs: pytest.fail(
            "expired ownership uncertainty must not dispatch"
        ),
    )
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded.update(
            success=outcome.success,
            reason=outcome.reason,
            error=outcome.error,
        )
        return outcome.success

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)

    assert scheduler.run_one_job(
        {
            "id": "dcab830aa41c",
            "name": "content-lane-executor",
        }
    ) is False
    assert recorded["success"] is False
    assert recorded["reason"] == "executor_admission_uncertain"
    assert "admission_profile" in recorded["error"]
    assert admission.executor_drain_status()["state"] == "active"


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


def test_late_heartbeat_error_after_stop_forces_red_before_finalization(
    monkeypatch,
):
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

    class RacingAdmissionHeartbeat:
        def __init__(self, heartbeat_lease):
            self.lease = heartbeat_lease
            self.error = None
            self.cancel_event = threading.Event()

        def start(self):
            pass

        def stop(self):
            self.error = scheduler.ExecutorAdmissionError(
                "late fencing ownership loss"
            )

    class NoopExecutionHeartbeat:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(scheduler, "_ExecutorAdmissionHeartbeat", RacingAdmissionHeartbeat)
    monkeypatch.setattr(scheduler, "_ExecutionLeaseHeartbeat", NoopExecutionHeartbeat)
    monkeypatch.setattr(scheduler, "acquire_executor_lease", lambda **_kwargs: lease)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_args: True)
    monkeypatch.setattr(
        scheduler, "mark_execution_running", lambda *_args, **_kwargs: {"id": "ledger"}
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: (True, "output", "completed work", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    terminal_statuses = []
    monkeypatch.setattr(
        scheduler,
        "finalize_executor_lease",
        lambda _lease, *, status: terminal_statuses.append(status),
    )
    monkeypatch.setattr(scheduler, "release_executor_lease", lambda _lease: None)
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded.update(
            success=outcome.success,
            reason=outcome.reason,
            error=outcome.error,
        )
        return outcome.success

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "execution_id": "ledger",
            "execution_owner_token": "ledger-token",
        }
    ) is False
    assert terminal_statuses == ["failed"]
    assert recorded["success"] is False
    assert recorded["reason"] == "executor_admission_heartbeat_failed"
    assert "late fencing ownership loss" in recorded["error"]


def test_finalize_failure_still_releases_exact_lease_and_records_red(monkeypatch):
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
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded.update(
            success=outcome.success,
            reason=outcome.reason,
            error=outcome.error,
        )
        return outcome.success

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)
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

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "execution_id": "ledger",
            "execution_owner_token": "ledger-token",
        }
    ) is False
    assert reset
    assert released == [True]
    assert recorded["success"] is False
    assert recorded["reason"] == "executor_admission_cleanup_failed"
    assert "finalization failed" in recorded["error"]


def test_failed_finalize_and_release_retry_exact_lease_in_outer_cleanup(
    monkeypatch, tmp_path
):
    admission, _database = _store(monkeypatch, tmp_path)
    import cron.scheduler as scheduler

    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id="ledger",
    )
    assert lease is not None
    monkeypatch.setattr(scheduler, "acquire_executor_lease", lambda **_kwargs: lease)
    monkeypatch.setattr(scheduler._ExecutorAdmissionHeartbeat, "start", lambda _self: None)
    monkeypatch.setattr(scheduler._ExecutorAdmissionHeartbeat, "stop", lambda _self: None)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_args: False)
    real_finalize = admission.finalize_executor_lease
    real_release = admission.release_executor_lease
    finalize_calls = []
    release_calls = []

    def flaky_finalize(observed_lease, *, status):
        finalize_calls.append((observed_lease, status))
        if len(finalize_calls) == 1:
            raise scheduler.ExecutorAdmissionError("finalize database busy")
        real_finalize(observed_lease, status=status)

    def flaky_release(observed_lease):
        release_calls.append(observed_lease)
        if len(release_calls) == 1:
            raise scheduler.ExecutorAdmissionError("release database busy")
        real_release(observed_lease)

    monkeypatch.setattr(scheduler, "finalize_executor_lease", flaky_finalize)
    monkeypatch.setattr(scheduler, "release_executor_lease", flaky_release)
    recorded = {}

    def record(_job, _execution_id, _owner_token, outcome, **_kwargs):
        recorded.update(
            success=outcome.success,
            reason=outcome.reason,
            error=outcome.error,
        )
        return outcome.success

    monkeypatch.setattr(scheduler, "_record_cron_outcome", record)

    assert scheduler.run_one_job(
        {
            "id": "62714b869845",
            "name": "clickup-executor",
            "execution_id": "ledger",
            "execution_owner_token": "ledger-token",
        }
    ) is False
    assert finalize_calls == [(lease, "failed"), (lease, "failed")]
    assert release_calls == [lease, lease]
    assert recorded["success"] is False
    assert recorded["reason"] == "executor_admission_cleanup_failed"
    assert admission.executor_drain_status()["state"] == "idle"

    successor = admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        task_id="next",
        owner_run_id="next-owner",
        ledger_execution_id="next-ledger",
    )
    assert successor is not None


def test_persistent_cleanup_contention_preserves_startup_recovery_proof(
    monkeypatch, tmp_path
):
    admission, database = _store(monkeypatch, tmp_path)
    import cron.executions as executions
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        executions,
        "EXECUTIONS_FILE",
        tmp_path / "cron" / "executions.db",
    )
    clock = [admission._now()]
    monkeypatch.setattr(admission, "_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    execution = executions.create_execution(
        "62714b869845",
        source="builtin",
        lease_seconds=1,
    )
    lease = admission.acquire_executor_lease(
        job_id="62714b869845",
        task_id="task",
        owner_run_id="owner",
        ledger_execution_id=execution["id"],
        lease_seconds=1,
    )
    assert lease is not None
    monkeypatch.setattr(admission, "_SQLITE_BUSY_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(scheduler, "acquire_executor_lease", lambda **_kwargs: lease)
    monkeypatch.setattr(scheduler._ExecutorAdmissionHeartbeat, "start", lambda _self: None)
    monkeypatch.setattr(scheduler._ExecutorAdmissionHeartbeat, "stop", lambda _self: None)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_args: True)

    class NoopExecutionHeartbeat:
        def __init__(self, *_args):
            pass

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(scheduler, "_ExecutionLeaseHeartbeat", NoopExecutionHeartbeat)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: (True, "output", "completed work", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: "/tmp/out")
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    job_outcomes = []

    def mark_job_run(_job_id, success, error, **_kwargs):
        job_outcomes.append((success, error))

    monkeypatch.setattr(scheduler, "mark_job_run", mark_job_run)
    reader = sqlite3.connect(database, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM executor_lease").fetchall()
    try:
        assert scheduler.run_one_job(
            {
                "id": "62714b869845",
                "name": "clickup-executor",
                "execution_id": execution["id"],
                "execution_owner_token": execution["owner_token"],
            }
        ) is False
    finally:
        reader.execute("ROLLBACK")
        reader.close()

    stranded = admission.executor_drain_status()
    assert stranded["state"] == "active"
    assert stranded["lease"]["owner_run_id"] == lease.owner_run_id
    assert stranded["lease"]["fencing_token"] == lease.fencing_token
    assert stranded["lease"]["ledger_execution_id"] == execution["id"]
    preserved = executions.latest_execution("62714b869845")
    assert preserved["status"] == "running"
    assert preserved["terminal_at"] is None
    assert job_outcomes
    assert job_outcomes[-1][0] is False
    assert "Executor admission cleanup failed closed" in job_outcomes[-1][1]

    clock[0] += timedelta(seconds=2)
    monkeypatch.setattr(executions, "_owner_liveness", lambda *_args: "dead")
    assert executions.recover_interrupted_executions() == 1
    recovered = executions.latest_execution("62714b869845")
    assert recovered["status"] == "interrupted"
    assert admission.executor_drain_status()["state"] == "finalized"

    successor = admission.acquire_executor_lease(
        job_id="dcab830aa41c",
        task_id="next",
        owner_run_id="next-owner",
        ledger_execution_id="next-ledger",
    )
    assert successor is not None
    assert successor.fencing_token > lease.fencing_token
    assert job_outcomes[-1][0] is False
