"""Focused contracts for the governed persistent-write fence."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sqlite3
import threading

import pytest


SHA = "a" * 40
FLEET = ["fleet-config", "cron-jobs", "skills-policy"]


def _store(monkeypatch, tmp_path):
    from cron import production_write_lease as leases

    monkeypatch.setattr(leases, "_database_path", lambda: tmp_path / "state" / "production-write-lease.db")
    return leases


def _acquire(leases, *, session="one", resources=FLEET):
    return leases.acquire(resources, "fleet-config-installer", session, "workspace", "hermes-agent", SHA, "test", 30)


def test_registry_backed_resources_are_canonicalized_and_exact(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    value = _acquire(leases, resources=list(reversed(FLEET)))
    assert value.resources == tuple(sorted(FLEET))
    with pytest.raises(leases.ProductionWriteLeaseError, match="exactly match"):
        _acquire(leases, session="bad", resources=["fleet-config"])


def test_overlapping_resources_conflict_but_disjoint_registered_writer_can_enter(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    _acquire(leases)
    with pytest.raises(leases.ProductionWriteLeaseError, match="conflict"):
        _acquire(leases, session="two")
    release = leases.acquire(["runtime-release", "governed-mini-scripts"], "mini-release-cut", "cut", "workspace", "hermes-agent", SHA, "cut", 30)
    assert release.actor == "mini-release-cut"


def test_heartbeat_and_release_require_exact_owner_and_fence(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    value = _acquire(leases)
    with pytest.raises(leases.ProductionWriteLeaseError, match="ownership/fence"):
        leases.heartbeat(lease_id=value.lease_id, actor=value.actor, session_id="wrong", fencing_token=value.fencing_token)
    updated = leases.heartbeat(lease_id=value.lease_id, actor=value.actor, session_id=value.session_id, fencing_token=value.fencing_token)
    assert updated.revision == 2
    leases.release(lease_id=value.lease_id, actor=value.actor, session_id=value.session_id, fencing_token=value.fencing_token)
    assert leases.status()["active_leases"] == []


def test_expired_recovery_requires_evidence_and_writes_receipt(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    value = _acquire(leases)
    monkeypatch.setattr(leases, "_now", lambda: leases.datetime.fromisoformat(value.expires_at) + timedelta(seconds=1))
    with pytest.raises(leases.ProductionWriteLeaseError, match="evidence"):
        leases.recover_expired(lease_id=value.lease_id, actor=value.actor, session_id=value.session_id, fencing_token=value.fencing_token, recovered_by="operator", reason="test", evidence={})
    with pytest.raises(leases.ProductionWriteLeaseError, match="requires evidence-backed recover"):
        _acquire(leases, session="successor")
    receipt = leases.recover_expired(lease_id=value.lease_id, actor=value.actor, session_id=value.session_id, fencing_token=value.fencing_token, recovered_by="operator", reason="test", evidence={"observation": "expired"})
    assert receipt["fencing_token"] == value.fencing_token
    status = leases.status()
    assert status["active_leases"] == []
    assert status["recovery_receipts"][0]["evidence"] == {"observation": "expired"}


def test_expired_owner_is_fenced_before_any_successor_mutation(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    monkeypatch.setattr(leases, "_now", lambda: leases.datetime.fromisoformat(owner.expires_at) + timedelta(seconds=1))
    leases.recover_expired(lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id, fencing_token=owner.fencing_token, recovered_by="operator", reason="handoff", evidence={"proof": "owner stopped"})
    successor = _acquire(leases, session="successor")
    assert successor.fencing_token > owner.fencing_token
    with pytest.raises(leases.ProductionWriteLeaseError, match="ownership/fence"):
        leases.fence_mutation(lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id, fencing_token=owner.fencing_token)


def test_paused_owner_cannot_resume_a_protected_write_after_successor_takeover(monkeypatch, tmp_path):
    """A guard blocks takeover while paused, then fences its resumed owner."""
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    entered = threading.Event()
    resume = threading.Event()
    protected = tmp_path / "write-inside-live-guard"

    def paused_mutation():
        with leases.mutation_guard(
            lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
            fencing_token=owner.fencing_token,
        ):
            entered.set()
            assert resume.wait(timeout=5)
            protected.write_text("owned write", encoding="utf-8")

    worker = threading.Thread(target=paused_mutation)
    worker.start()
    assert entered.wait(timeout=5)
    # The open guard holds the SQLite writer transaction, so a successor
    # cannot acquire/recover and write while the protected mutation is paused.
    with pytest.raises(leases.ProductionWriteLeaseError, match="database remained busy"):
        _acquire(leases, session="blocked-successor")
    resume.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert protected.read_text(encoding="utf-8") == "owned write"

    owner = leases.status()["active_leases"][0]
    # The paused writer expires and an evidence-backed operator recovery
    # authorizes successor takeover; it must still fail at the exact write.
    monkeypatch.setattr(
        leases, "_now", lambda: leases.datetime.fromisoformat(owner["expires_at"]) + timedelta(seconds=1)
    )
    leases.recover_expired(
        lease_id=owner["lease_id"], actor=owner["actor"], session_id=owner["session_id"],
        fencing_token=owner["fencing_token"], recovered_by="operator",
        reason="paused owner takeover", evidence={"proof": "owner paused after heartbeat"},
    )
    successor = _acquire(leases, session="successor")
    target = tmp_path / "must-not-be-written-by-old-owner"
    with pytest.raises(leases.ProductionWriteLeaseError, match="ownership/fence"):
        with leases.mutation_guard(
            lease_id=owner["lease_id"], actor=owner["actor"], session_id=owner["session_id"],
            fencing_token=owner["fencing_token"],
        ):
            target.write_text("stale write", encoding="utf-8")
    assert successor.fencing_token > owner["fencing_token"]
    assert not target.exists()


def test_recovery_receipt_is_immutable(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    monkeypatch.setattr(leases, "_now", lambda: leases.datetime.fromisoformat(owner.expires_at) + timedelta(seconds=1))
    receipt = leases.recover_expired(lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id, fencing_token=owner.fencing_token, recovered_by="operator", reason="handoff", evidence={"proof": "stopped"})
    with sqlite3.connect(leases._database_path()) as conn, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("DELETE FROM recovery_receipts WHERE receipt_id=?", (receipt["receipt_id"],))


def test_database_symlink_is_rejected(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    database = tmp_path / "state" / "production-write-lease.db"
    database.parent.mkdir()
    target = tmp_path / "target.db"
    target.write_text("not sqlite")
    database.symlink_to(target)
    with pytest.raises(leases.ProductionWriteLeaseError, match="must not be a symlink"):
        leases.status()


def test_missing_posix_uid_api_fails_closed(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    monkeypatch.delattr(leases.os, "getuid", raising=False)
    with pytest.raises(leases.ProductionWriteLeaseError, match="POSIX user ID API is unavailable"):
        leases.status()
