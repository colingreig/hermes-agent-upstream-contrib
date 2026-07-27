"""Durable cron execution-ledger behavior."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_execution_transitions_are_durable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    claimed = executions.create_execution("job-1", source="builtin")
    assert claimed["status"] == "claimed"
    assert claimed["claimed_at"]
    assert claimed["started_at"] is None
    assert claimed["finished_at"] is None

    running = executions.mark_execution_running(claimed["id"], owner_token=claimed["owner_token"])
    assert running["status"] == "running"
    assert running["started_at"]

    completed = executions.finish_execution(claimed["id"], owner_token=claimed["owner_token"], success=True)
    assert completed["status"] == "completed"
    assert completed["finished_at"]
    assert completed["error"] is None

    persisted = executions.list_executions(job_id="job-1")
    assert persisted == [completed]


def test_terminal_execution_cannot_be_rewritten(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("immutable", source="builtin")
    executions.mark_execution_running(record["id"], owner_token=record["owner_token"])
    executions.finish_execution(record["id"], owner_token=record["owner_token"], success=True)

    retry = executions.finish_execution(
        record["id"], owner_token=record["owner_token"], success=False, error="late writer"
    )
    assert retry["status"] == "completed"
    assert retry["error"] is None
    assert executions.latest_execution("immutable")["status"] == "completed"


def test_retention_bounds_terminal_history_but_preserves_inflight(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "MAX_TERMINAL_EXECUTIONS", 3)
    inflight = executions.create_execution("live", source="builtin")
    executions.mark_execution_running(inflight["id"], owner_token=inflight["owner_token"])
    for index in range(8):
        row = executions.create_execution(f"done-{index}", source="builtin")
        executions.finish_execution(row["id"], owner_token=row["owner_token"], success=True)

    records = executions.list_executions(limit=100)
    assert len([row for row in records if row["status"] == "completed"]) == 3
    assert executions.latest_execution("live")["status"] == "running"


def test_corrupt_store_fails_closed_without_overwrite(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    executions.EXECUTIONS_FILE.write_bytes(b"not a sqlite database")

    with __import__("pytest").raises(sqlite3.DatabaseError):
        executions.create_execution("new", source="builtin")
    assert executions.EXECUTIONS_FILE.read_bytes() == b"not a sqlite database"


def test_execution_history_is_paginated(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    ids = []
    for _index in range(5):
        row = executions.create_execution("paged", source="builtin")
        executions.finish_execution(row["id"], owner_token=row["owner_token"], success=True)
        ids.append(row["id"])

    first = executions.list_executions(job_id="paged", limit=2)
    second = executions.list_executions(
        job_id="paged", limit=2, before_claimed_at=first[-1]["claimed_at"]
    )
    assert [row["id"] for row in first] == list(reversed(ids))[:2]
    assert set(row["id"] for row in first).isdisjoint(row["id"] for row in second)


def test_cron_runs_cli_prints_execution_history(monkeypatch, tmp_path, capsys):
    executions = _point_ledger(monkeypatch, tmp_path)
    row = executions.create_execution("cli-job", source="builtin")
    executions.finish_execution(row["id"], owner_token=row["owner_token"], success=False, error="boom")
    from hermes_cli.cron import cron_runs

    cron_runs("cli-job", limit=10)

    output = capsys.readouterr().out
    assert row["id"] in output
    assert "failed" in output
    assert "boom" in output


def test_quick_backup_includes_execution_ledger():
    from hermes_cli.backup import _QUICK_STATE_FILES

    assert "cron/executions.db" in _QUICK_STATE_FILES


def test_failed_execution_keeps_error(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)

    record = executions.create_execution("job-2", source="external")
    failed = executions.finish_execution(record["id"], owner_token=record["owner_token"], success=False, error="provider exploded")

    assert failed["status"] == "failed"
    assert failed["error"] == "provider exploded"


def test_recovery_does_not_mark_live_process_execution_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("still-live", source="builtin")
    executions.mark_execution_running(record["id"], owner_token=record["owner_token"])

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("still-live")["status"] == "running"


def test_recovery_does_not_mark_other_live_owner_unknown(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("other-live", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, pid=? WHERE id=?",
            ("another-import", os.getpid(), record["id"]),
        )

    assert executions.recover_interrupted_executions() == 0
    assert executions.latest_execution("other-live")["status"] == "claimed"


def test_recovery_rejects_recycled_pid(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("recycled", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("old-import", -1, record["id"]),
        )

    assert executions.recover_interrupted_executions() == 1
    assert executions.latest_execution("recycled")["status"] == "interrupted"


def test_restart_marks_interrupted_execution_without_requeue(tmp_path):
    """A restarted process finalizes its provably dead leased execution."""
    home = tmp_path / "home"
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo)

    create = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cron.executions import create_execution, mark_execution_running; "
            "r=create_execution('restart-job', source='builtin'); "
            "mark_execution_running(r['id'], owner_token=r['owner_token']); print(r['id'])",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    execution_id = create.stdout.strip()

    recover = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from cron.executions import recover_interrupted_executions, list_executions; "
            "print(recover_interrupted_executions()); "
            "print(json.dumps(list_executions(job_id='restart-job'))) ",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    lines = recover.stdout.strip().splitlines()
    assert lines[0] == "1"
    records = json.loads(lines[1])
    assert len(records) == 1
    assert records[0]["id"] == execution_id
    assert records[0]["status"] == "interrupted"
    assert records[0]["finished_at"]
    assert "owner died" in records[0]["error"].lower()
    # Recovery only classifies the old attempt. It must not manufacture a new
    # claimed record (which would imply an automatic retry).
    assert [r["status"] for r in records] == ["interrupted"]


def test_generic_submit_failure_finishes_attempt_and_releases_guard(monkeypatch):
    import cron.scheduler as scheduler

    class BrokenPool:
        def submit(self, _callable):
            raise ValueError("executor rejected")

    finished = []
    monkeypatch.setattr(
        scheduler, "create_execution",
        lambda *_args, **_kwargs: {"id": "exec-submit-fail", "owner_token": "owner-submit-fail"},
    )
    monkeypatch.setattr(
        scheduler, "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: [{"id": "submit-fail"}])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda _job_id: None)
    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _workers: BrokenPool())

    assert scheduler.tick(verbose=False, sync=False) == 0
    assert finished == [
        ("exec-submit-fail", {
            "owner_token": "owner-submit-fail",
            "success": False,
            "error": "Executor dispatch failed: executor rejected",
            "reason": "dispatch_failed",
        })
    ]
    assert "submit-fail" not in scheduler.get_running_job_ids()


def test_run_one_job_records_running_then_terminal(monkeypatch):
    import cron.scheduler as scheduler

    events = []
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda execution_id, **kwargs: events.append(("running", execution_id, kwargs)) or {"id": execution_id},
        raising=False,
    )
    monkeypatch.setattr(
        scheduler,
        "finish_execution",
        lambda execution_id, **kwargs: events.append(("finish", execution_id, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, *, defer_agent_teardown=None: (True, "output", "response", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)

    assert scheduler.run_one_job({"id": "job-3", "execution_id": "exec-3", "execution_owner_token": "owner-3"}) is True
    assert events[0] == ("running", "exec-3", {"owner_token": "owner-3"})
    assert events[-1][0:2] == ("finish", "exec-3")
    assert events[-1][2]["success"] is True
    assert events[-1][2]["owner_token"] == "owner-3"
    assert events[-1][2]["reason"] == "completed"


def test_provider_start_recovers_interrupted_records_before_tick(monkeypatch):
    import cron.scheduler_provider as provider

    events = []
    stop = __import__("threading").Event()
    stop.set()
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
        raising=False,
    )
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: events.append("heartbeat"))

    provider.InProcessCronScheduler().start(stop, interval=1)

    assert events[:2] == ["recover", "heartbeat"]


def test_external_provider_start_recovers_interrupted_records(monkeypatch):
    from plugins.cron_providers.chronos import ChronosCronScheduler

    provider = ChronosCronScheduler()
    provider._client = type("Client", (), {"arm": lambda self, **kwargs: None})()
    events = []
    monkeypatch.setattr(
        "cron.executions.recover_interrupted_executions",
        lambda: events.append("recover") or 0,
    )
    monkeypatch.setattr(provider, "reconcile", lambda: events.append("reconcile"))

    provider.start(__import__("threading").Event())

    assert events == ["recover", "reconcile"]


def test_job_listing_exposes_latest_execution(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    executions = _point_ledger(monkeypatch, tmp_path)

    job = jobs.create_job(prompt="audit me", schedule="every 1h", name="audit")
    record = executions.create_execution(job["id"], source="builtin")
    executions.mark_execution_running(record["id"], owner_token=record["owner_token"])

    listed = jobs.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"]["id"] == record["id"]
    assert listed[0]["latest_execution"]["status"] == "running"


def test_migration_preserves_legacy_rows_and_adds_fenced_lease_columns(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.EXECUTIONS_FILE.parent.mkdir(parents=True)
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            """CREATE TABLE executions (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
                process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
                status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
                claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "job", "builtin", "old", 1, 1, "completed", "t0", "t0", "t1", None),
        )

    created = executions.create_execution("new", source="builtin")
    assert created["owner_token"]
    assert created["heartbeat_at"]
    assert created["lease_expires_at"]
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
        legacy = conn.execute("SELECT terminal_at, terminal_reason FROM executions WHERE id='legacy'").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert {"owner_token", "heartbeat_at", "lease_expires_at", "terminal_at", "terminal_reason"} <= columns
    assert legacy == ("t1", "legacy_completed")
    assert version == executions.SCHEMA_VERSION


def test_heartbeat_and_finalization_are_fenced_and_idempotent(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("fenced", source="builtin")
    owner = record["owner_token"]
    assert executions.mark_execution_running(record["id"], owner_token=owner)
    assert not executions.heartbeat_execution(record["id"], owner_token="stale-owner")
    assert executions.heartbeat_execution(record["id"], owner_token=owner)

    assert executions.finish_execution(
        record["id"], owner_token="stale-owner", success=False, error="late", reason="late_writer"
    ) is None
    terminal = executions.finish_execution(
        record["id"], owner_token=owner, success=False, error="timeout", reason="timeout"
    )
    retried = executions.finish_execution(
        record["id"], owner_token=owner, success=True, reason="completed"
    )
    assert terminal["status"] == "failed"
    assert terminal["terminal_reason"] == "timeout"
    assert retried == terminal


def test_classifier_is_non_mutating_and_requires_durable_owner_evidence(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    modern = executions.create_execution("dead", source="builtin")
    legacy = executions.create_execution("legacy", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("dead-process", -1, modern["id"]),
        )
        conn.execute(
            "UPDATE executions SET owner_token=NULL, lease_expires_at=NULL, process_id=?, process_started_at=? WHERE id=?",
            ("old-process", -1, legacy["id"]),
        )

    manifest = executions.classify_stale_executions()
    by_id = {entry["execution_id"]: entry for entry in manifest["entries"]}
    assert manifest["mutated"] is False
    assert by_id[modern["id"]]["disposition"] == "stale"
    assert by_id[modern["id"]]["proposed_terminal_status"] == "interrupted"
    assert by_id[legacy["id"]]["disposition"] == "legacy_unfenced"
    assert executions.latest_execution("dead")["status"] == "claimed"
    assert executions.latest_execution("legacy")["status"] == "claimed"


def test_recovery_finalizes_only_modern_proven_dead_records(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    modern = executions.create_execution("dead", source="builtin")
    legacy = executions.create_execution("legacy", source="builtin")
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
            ("dead-process", -1, modern["id"]),
        )
        conn.execute(
            "UPDATE executions SET owner_token=NULL, lease_expires_at=NULL, process_id=?, process_started_at=? WHERE id=?",
            ("old-process", -1, legacy["id"]),
        )
    assert executions.recover_interrupted_executions() == 1
    assert executions.latest_execution("dead")["terminal_reason"] == "owner_dead"
    assert executions.latest_execution("legacy")["status"] == "claimed"


def _seed_historical_reconciliation_rows(executions, *, outside_id="outside-approved-set"):
    with executions._connect() as conn:
        for index, execution_id in enumerate(
            executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS
        ):
            conn.execute(
                """INSERT INTO executions
                   (id, job_id, source, process_id, pid, process_started_at,
                    status, claimed_at, started_at)
                   VALUES (?, ?, 'builtin', ?, ?, ?, 'running', ?, ?)""",
                (
                    execution_id,
                    f"legacy-{index}",
                    f"legacy-process-{index}",
                    900000 + index,
                    1000 + index,
                    f"2026-01-01T00:00:0{index}+00:00",
                    f"2026-01-01T00:00:0{index}+00:00",
                ),
            )
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, started_at)
               VALUES (?, 'outside', 'builtin', 'outside-process', 999999, 999,
                       'running', '2026-01-01T00:01:00+00:00',
                       '2026-01-01T00:01:00+00:00')""",
            (outside_id,),
        )
    return outside_id


def _historical_manifest(executions):
    return executions.build_historical_reconciliation_manifest(
        database_snapshot_sha256="a" * 64,
        runtime_release="mini-release-20260727",
        runtime_commit="1" * 40,
    )


def _apply_historical_manifest(executions, manifest, *, manifest_hash=None):
    return executions.apply_historical_execution_reconciliation(
        manifest,
        manifest_hash=(
            manifest["content_hash"] if manifest_hash is None else manifest_hash
        ),
        database_snapshot_sha256="a" * 64,
        runtime_release="mini-release-20260727",
        runtime_commit="1" * 40,
    )


def test_historical_manifest_is_fixed_allow_list_and_non_mutating(
    monkeypatch, tmp_path,
):
    executions = _point_ledger(monkeypatch, tmp_path)
    outside_id = _seed_historical_reconciliation_rows(executions)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    before = executions.list_executions(limit=100)

    manifest = _historical_manifest(executions)

    assert executions.list_executions(limit=100) == before
    assert manifest["mutated"] is False
    assert manifest["approved_execution_ids"] == list(
        executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS
    )
    assert [entry["execution_id"] for entry in manifest["entries"]] == list(
        executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS
    )
    assert all(entry["eligible"] for entry in manifest["entries"])
    assert outside_id not in manifest["approved_execution_ids"]
    assert manifest["content_hash"] == executions._historical_manifest_hash(manifest)


def test_historical_apply_rejects_snapshot_drift_without_partial_mutation(
    monkeypatch, tmp_path,
):
    executions = _point_ledger(monkeypatch, tmp_path)
    _seed_historical_reconciliation_rows(executions)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    manifest = _historical_manifest(executions)
    changed_id = executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS[-1]
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET heartbeat_at='drifted' WHERE id=?",
            (changed_id,),
        )

    with __import__("pytest").raises(
        executions.HistoricalReconciliationError,
        match="snapshot preconditions changed",
    ):
        _apply_historical_manifest(executions, manifest)

    rows = executions.list_executions(limit=100)
    approved = {
        row["id"]: row for row in rows
        if row["id"] in executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS
    }
    assert {row["status"] for row in approved.values()} == {"running"}
    assert all(row["terminal_at"] is None for row in approved.values())


def test_historical_apply_refuses_pid_reuse_or_live_owner(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    _seed_historical_reconciliation_rows(executions)
    reused_pid = 900002
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == reused_pid)
    monkeypatch.setattr(
        executions,
        "_process_start_time",
        lambda pid: 7777 if pid == reused_pid else None,
    )

    manifest = _historical_manifest(executions)
    by_id = {entry["execution_id"]: entry for entry in manifest["entries"]}
    reused_id = executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS[2]
    assert by_id[reused_id]["owner_pid_evidence"]["state"] == "pid_reused"
    assert by_id[reused_id]["eligible"] is False
    with __import__("pytest").raises(
        executions.HistoricalReconciliationError,
        match="ineligible approved execution",
    ):
        _apply_historical_manifest(executions, manifest)
    assert {
        row["status"] for row in executions.list_executions(limit=100)
    } == {"running"}


def test_historical_apply_rejects_manifest_allow_list_tampering(
    monkeypatch, tmp_path,
):
    executions = _point_ledger(monkeypatch, tmp_path)
    _seed_historical_reconciliation_rows(executions)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    manifest = _historical_manifest(executions)
    manifest["approved_execution_ids"][-1] = "outside-approved-set"
    manifest["entries"][-1]["execution_id"] = "outside-approved-set"
    manifest["content_hash"] = executions._historical_manifest_hash(manifest)

    with __import__("pytest").raises(
        executions.HistoricalReconciliationError,
        match="fixed approved set",
    ):
        _apply_historical_manifest(executions, manifest)
    assert executions.latest_execution("outside")["status"] == "running"


def test_historical_apply_requires_exact_hash_snapshot_and_runtime_identity(
    monkeypatch, tmp_path,
):
    executions = _point_ledger(monkeypatch, tmp_path)
    _seed_historical_reconciliation_rows(executions)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    manifest = _historical_manifest(executions)

    attempts = (
        {"manifest_hash": "b" * 64},
        {"database_snapshot_sha256": "b" * 64},
        {"runtime_release": "different-release"},
        {"runtime_commit": "2" * 40},
    )
    base = {
        "manifest_hash": manifest["content_hash"],
        "database_snapshot_sha256": "a" * 64,
        "runtime_release": "mini-release-20260727",
        "runtime_commit": "1" * 40,
    }
    for override in attempts:
        with __import__("pytest").raises(executions.HistoricalReconciliationError):
            executions.apply_historical_execution_reconciliation(
                manifest, **(base | override)
            )

    approved = [
        row for row in executions.list_executions(limit=100)
        if row["id"] in executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS
    ]
    assert all(row["status"] == "running" for row in approved)
    assert all(row["terminal_at"] is None for row in approved)


def test_historical_apply_rolls_back_all_rows_on_update_failure(
    monkeypatch, tmp_path,
):
    executions = _point_ledger(monkeypatch, tmp_path)
    _seed_historical_reconciliation_rows(executions)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    manifest = _historical_manifest(executions)
    blocked_id = executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS[3]
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            f"""CREATE TRIGGER reject_historical_update
                BEFORE UPDATE ON executions
                WHEN NEW.id='{blocked_id}'
                BEGIN SELECT RAISE(ABORT, 'fixture update failure'); END"""
        )

    with __import__("pytest").raises(sqlite3.IntegrityError, match="fixture update failure"):
        _apply_historical_manifest(executions, manifest)

    approved = [
        row for row in executions.list_executions(limit=100)
        if row["id"] in executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS
    ]
    assert len(approved) == len(executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS)
    assert all(row["status"] == "running" for row in approved)
    assert all(row["terminal_at"] is None for row in approved)


def test_historical_apply_is_atomic_idempotent_across_hash_case_and_preserves_outside_rows(
    monkeypatch, tmp_path,
):
    executions = _point_ledger(monkeypatch, tmp_path)
    outside_id = _seed_historical_reconciliation_rows(executions)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: False)
    manifest = _historical_manifest(executions)

    first = _apply_historical_manifest(
        executions,
        manifest,
        manifest_hash=manifest["content_hash"].upper(),
    )
    after_first = {
        row["id"]: row for row in executions.list_executions(limit=100)
    }
    second = _apply_historical_manifest(executions, manifest)
    after_second = {
        row["id"]: row for row in executions.list_executions(limit=100)
    }

    assert first["mutated"] == 6
    assert first["already_reconciled"] == 0
    assert first["manifest_hash"] == manifest["content_hash"]
    assert second["mutated"] == 0
    assert second["already_reconciled"] == 6
    assert second["entries"] == first["entries"]
    for execution_id in executions.HISTORICAL_RECONCILIATION_EXECUTION_IDS:
        row = after_second[execution_id]
        assert row == after_first[execution_id]
        assert row["status"] == "interrupted"
        assert row["finished_at"] == row["terminal_at"]
        assert (
            row["terminal_reason"]
            == executions.HISTORICAL_RECONCILIATION_REASON
        )
        assert manifest["content_hash"] in row["error"]
    assert after_second[outside_id] == after_first[outside_id]
    assert after_second[outside_id]["status"] == "running"


def test_concurrent_finalizers_keep_one_terminal_fact(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    record = executions.create_execution("race", source="builtin")
    assert executions.mark_execution_running(record["id"], owner_token=record["owner_token"])

    def finalize(reason):
        return executions.finish_execution(
            record["id"], owner_token=record["owner_token"], success=False,
            error=reason, reason=reason,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finalize, ("timeout", "gateway_child_died")))
    terminal = executions.latest_execution("race")
    assert terminal["terminal_reason"] in {"timeout", "gateway_child_died"}
    assert all(result == terminal for result in results)


def test_runner_terminal_reasons_cover_timeout_signal_and_gateway_child_death(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    import cron.scheduler as scheduler

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "_summarize_cron_failure_for_delivery", lambda *_args: "failed")
    monkeypatch.setattr(scheduler, "_ExecutionLeaseHeartbeat", type("Lease", (), {
        "__init__": lambda self, *_args: None,
        "start": lambda self: None,
        "stop": lambda self: None,
    }))

    def run_with(result, *, interrupted=False):
        record = executions.create_execution("reason", source="direct")
        monkeypatch.setattr(scheduler, "run_job", lambda _job: result)
        monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: interrupted)
        monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: interrupted)
        job = {
            "id": "reason", "execution_id": record["id"],
            "execution_owner_token": record["owner_token"],
        }
        return scheduler.run_one_job(job), executions.latest_execution("reason")

    ok, timeout = run_with((False, "", "", "hard timeout while running"))
    assert ok is False
    assert timeout["terminal_reason"] == "timeout"

    ok, child_died = run_with((True, "output", "response", None), interrupted=True)
    assert ok is False
    assert child_died["terminal_reason"] == "gateway_child_died"

    record = executions.create_execution("signal", source="direct")
    monkeypatch.setattr(scheduler, "run_job", lambda _job: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: False)
    with __import__("pytest").raises(KeyboardInterrupt):
        scheduler.run_one_job({
            "id": "signal", "execution_id": record["id"],
            "execution_owner_token": record["owner_token"],
        })
    assert executions.latest_execution("signal")["terminal_reason"] == "signal"


def test_outer_runner_exception_preserves_normalized_timeout_and_interrupt_reason(monkeypatch, tmp_path):
    """The exception boundary must not flatten q8 outcomes to runner_exception."""
    executions = _point_ledger(monkeypatch, tmp_path)
    import cron.scheduler as scheduler

    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "_ExecutionLeaseHeartbeat", type("Lease", (), {
        "__init__": lambda self, *_args: None,
        "start": lambda self: None,
        "stop": lambda self: None,
    }))

    def run_raising(job_id, error, *, interrupted=False):
        record = executions.create_execution(job_id, source="direct")
        monkeypatch.setattr(
            scheduler, "run_job", lambda _job: (_ for _ in ()).throw(RuntimeError(error))
        )
        monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: interrupted)
        monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: interrupted)
        assert scheduler.run_one_job({
            "id": job_id,
            "execution_id": record["id"],
            "execution_owner_token": record["owner_token"],
        }) is False
        return executions.latest_execution(job_id)

    timeout = run_raising("outer-timeout", "provider timeout")
    interrupted = run_raising("outer-interrupted", "worker stopped", interrupted=True)
    assert timeout["terminal_reason"] == "timeout"
    assert interrupted["terminal_reason"] == "gateway_child_died"
