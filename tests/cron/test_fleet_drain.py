from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest


def _registry(path):
    path.write_text(json.dumps({
        "actors": [
            {"id": "monitor", "mutability": "read-only", "cron_job_ids": ["monitor-job"]},
            {"id": "writer", "mutability": "mutating", "cron_job_ids": ["writer-job"]},
        ]
    }), encoding="utf-8")


def test_drain_marker_is_profile_local_atomic_and_reversible(monkeypatch, tmp_path):
    import cron.fleet_drain as drain

    home = tmp_path / "profile"
    monkeypatch.setattr(drain, "get_hermes_home", lambda: home)
    begun = drain.begin_fleet_drain(reason="release cut", actor="test")
    assert begun["active"] is True
    assert begun["marker"]["reason"] == "release cut"
    assert drain.fleet_drain_status()["marker"]["drain_id"] == begun["marker"]["drain_id"]
    assert not list((home / "state").glob("*.tmp"))
    cancelled = drain.cancel_fleet_drain()
    assert cancelled["active"] is False
    assert drain.fleet_drain_status()["active"] is False


def test_drain_policy_blocks_llm_and_unknown_or_mutating_scripts_but_allows_declared_monitor(
    monkeypatch, tmp_path
):
    import cron.fleet_drain as drain

    home = tmp_path / "profile"
    registry = tmp_path / "registry.json"
    _registry(registry)
    monkeypatch.setattr(drain, "get_hermes_home", lambda: home)
    monkeypatch.setattr(drain, "_registry_path", lambda: registry)
    drain.begin_fleet_drain(actor="test")

    assert drain.cron_job_admission({"id": "llm", "no_agent": False}).allowed is False
    assert drain.cron_job_admission({"id": "writer-job", "no_agent": True}).allowed is False
    assert drain.cron_job_admission({"id": "unknown", "no_agent": True}).allowed is False
    decision = drain.cron_job_admission({"id": "monitor-job", "no_agent": True})
    assert decision.allowed is True
    assert decision.actor_id == "monitor"


def test_external_fire_checks_drain_before_claim(monkeypatch):
    import cron.scheduler_provider as provider
    import cron.fleet_drain as drain
    import cron.jobs as jobs

    monkeypatch.setattr(drain, "cron_job_admission", lambda job: drain.DrainDecision(False, "fleet_draining"))
    monkeypatch.setattr(jobs, "get_job", lambda job_id: {"id": job_id, "no_agent": False})
    monkeypatch.setattr(jobs, "claim_job_for_fire", lambda job_id: (_ for _ in ()).throw(AssertionError("claimed")))
    class External(provider.CronScheduler):
        @property
        def name(self):
            return "test"

        def start(self, stop_event, **kwargs):
            return None

    assert External().fire_due("job") is False


def test_due_scan_does_not_fast_forward_or_claim_blocked_job(monkeypatch, tmp_path):
    import cron.fleet_drain as drain
    import cron.jobs as jobs

    home = tmp_path / "profile"
    registry = tmp_path / "registry.json"
    _registry(registry)
    monkeypatch.setattr(drain, "get_hermes_home", lambda: home)
    monkeypatch.setattr(drain, "_registry_path", lambda: registry)
    drain.begin_fleet_drain(actor="test")

    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    stored = [{
        "id": "llm",
        "name": "blocked",
        "enabled": True,
        "no_agent": False,
        "schedule": {"kind": "interval", "minutes": 5},
        "next_run_at": (now - timedelta(hours=1)).isoformat(),
    }]
    writes = []
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    monkeypatch.setattr(jobs, "_jobs_lock", lambda: nullcontext())
    monkeypatch.setattr(jobs, "load_jobs", lambda: json.loads(json.dumps(stored)))
    monkeypatch.setattr(jobs, "save_jobs", lambda value: writes.append(value))

    assert jobs.get_due_jobs() == []
    assert writes == []


def test_manual_fire_claim_is_denied_before_store_mutation(monkeypatch, tmp_path):
    import cron.fleet_drain as drain
    import cron.jobs as jobs

    home = tmp_path / "profile"
    registry = tmp_path / "registry.json"
    _registry(registry)
    monkeypatch.setattr(drain, "get_hermes_home", lambda: home)
    monkeypatch.setattr(drain, "_registry_path", lambda: registry)
    drain.begin_fleet_drain(actor="test")
    monkeypatch.setattr(jobs, "_jobs_lock", lambda: nullcontext())
    monkeypatch.setattr(jobs, "load_jobs", lambda: [{
        "id": "llm", "name": "blocked", "enabled": True,
        "no_agent": False, "schedule": {"kind": "interval", "minutes": 5},
    }])
    monkeypatch.setattr(
        jobs, "save_jobs", lambda _value: (_ for _ in ()).throw(AssertionError("mutated"))
    )

    assert jobs.claim_job_for_fire("llm") is False


def test_executor_wake_is_denied_before_admission_db_write(monkeypatch, tmp_path):
    import cron.executor_admission as admission
    import cron.fleet_drain as drain

    home = tmp_path / "profile"
    monkeypatch.setattr(drain, "get_hermes_home", lambda: home)
    drain.begin_fleet_drain(actor="test")
    monkeypatch.setattr(
        admission, "_connect", lambda: (_ for _ in ()).throw(AssertionError("database opened"))
    )

    with pytest.raises(admission.ExecutorAdmissionError, match="fleet drain"):
        admission.request_executor_wake(
            job_id="62714b869845", task_id="task", reason="gate"
        )


def test_executor_drain_status_includes_generic_active_and_recovery_without_writing(
    monkeypatch, tmp_path
):
    import cron.executor_admission as admission

    database = tmp_path / "admission.db"
    monkeypatch.setattr(admission, "_database_path", lambda: database)
    job = {"id": "llm", "no_agent": False, "admission_profile": "root/test", "mutable_resources": ["x"]}
    lease = admission.acquire_job_admission_lease(
        job=job, owner_run_id="owner", ledger_execution_id="ledger"
    )
    assert lease is not None
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO admission_recovery_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            ("receipt", 999, "old", "owner", "old-ledger", "reviewer", "dead", "2026-01-01T00:00:00+00:00", "{}"),
        )
    before = database.stat().st_mtime_ns
    result = admission.executor_drain_status(database_path=database)
    assert result["safe_to_cutover"] is False
    assert result["generic_leases"][0]["ledger_execution_id"] == "ledger"
    assert result["generic_recovery_receipts"][0]["receipt_id"] == "receipt"
    assert result["lease"] is None  # backward-compatible legacy key
    assert result["mutated"] is False
    assert database.stat().st_mtime_ns == before


def test_explicit_executor_drain_store_is_read_only_and_does_not_bootstrap(
    tmp_path,
):
    import cron.executor_admission as admission

    database = tmp_path / "requested-profile" / "state" / "executor-admission.db"

    result = admission.executor_drain_status(database_path=database)

    assert result["safe_to_cutover"] is True
    assert result["state"] == "idle"
    assert result["mutated"] is False
    assert not database.exists()
    assert not database.parent.exists()
