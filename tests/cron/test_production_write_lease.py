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


def _acquire_release(leases, *, session="cut", commit=SHA):
    return leases.acquire(
        ["runtime-release", "governed-mini-scripts"], "mini-release-cut",
        session, "workspace", "hermes-agent", commit, "cut", 30,
    )


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


def test_long_mutation_guard_renews_at_completion(monkeypatch, tmp_path):
    """A protected operation may outlive its entry TTL without expiring on exit."""
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    clock = {"now": leases.datetime.fromisoformat(owner.heartbeat_at)}
    monkeypatch.setattr(leases, "_now", lambda: clock["now"])

    with leases.mutation_guard(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token, lease_seconds=2,
    ) as guarded:
        entry_expires = leases.datetime.fromisoformat(guarded.expires_at)
        clock["now"] = entry_expires + timedelta(seconds=5)

    assert guarded.revision == owner.revision + 2
    assert leases.datetime.fromisoformat(guarded.heartbeat_at) == clock["now"]
    assert leases.datetime.fromisoformat(guarded.expires_at) == clock["now"] + timedelta(seconds=2)
    # The refreshed object and durable row agree, and the exact owner can
    # immediately fence its next mutation rather than failing as expired.
    active = leases.status()["active_leases"][0]
    assert active["heartbeat_at"] == guarded.heartbeat_at
    assert active["expires_at"] == guarded.expires_at
    leases.fence_mutation(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token, lease_seconds=2,
    )


def test_failed_long_mutation_guard_keeps_fence_live_for_rollback(monkeypatch, tmp_path):
    """A failed child operation still leaves its exact owner able to roll back."""
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    clock = {"now": leases.datetime.fromisoformat(owner.heartbeat_at)}
    monkeypatch.setattr(leases, "_now", lambda: clock["now"])

    with pytest.raises(RuntimeError, match="child failed"):
        with leases.mutation_guard(
            lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
            fencing_token=owner.fencing_token, lease_seconds=2,
        ):
            clock["now"] += timedelta(seconds=10)
            raise RuntimeError("child failed")

    active = leases.status()["active_leases"][0]
    assert active["heartbeat_at"] == leases._iso(clock["now"])
    leases.fence_mutation(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token, lease_seconds=2,
    )


def test_recovery_receipt_is_immutable(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    monkeypatch.setattr(leases, "_now", lambda: leases.datetime.fromisoformat(owner.expires_at) + timedelta(seconds=1))
    receipt = leases.recover_expired(lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id, fencing_token=owner.fencing_token, recovered_by="operator", reason="handoff", evidence={"proof": "stopped"})
    with sqlite3.connect(leases._database_path()) as conn, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("DELETE FROM recovery_receipts WHERE receipt_id=?", (receipt["receipt_id"],))


def test_fence_loss_receipt_requires_real_history_and_is_immutable(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire(leases)
    receipt = leases.record_fence_loss(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token, reason="heartbeat refused",
        evidence={"operation": "test", "result": "fence-lost"},
    )
    assert leases.status()["fence_loss_receipts"][0]["receipt_id"] == receipt["receipt_id"]
    with sqlite3.connect(leases._database_path()) as conn, pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute("DELETE FROM fence_loss_receipts WHERE receipt_id=?", (receipt["receipt_id"],))

    with pytest.raises(leases.ProductionWriteLeaseError, match="no matching lease history"):
        leases.record_fence_loss(
            lease_id="unknown", actor=owner.actor, session_id=owner.session_id,
            fencing_token=owner.fencing_token + 100, reason="fabricated",
            evidence={"operation": "test", "result": "fabricated"},
        )


def test_stale_cut_lock_recovery_requires_released_owner_and_newer_live_successor(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire_release(leases, session="owner")
    with pytest.raises(leases.ProductionWriteLeaseError, match="still live"):
        leases.authorize_stale_lock_recovery(stale=owner.as_dict(), successor=owner.as_dict())

    leases.release(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token,
    )
    successor = _acquire_release(leases, session="successor", commit="b" * 40)
    proof = leases.authorize_stale_lock_recovery(
        stale=owner.as_dict(), successor=successor.as_dict(),
    )
    assert proof["authorized"] is True
    assert proof["stale_state"] == "released"
    assert proof["stale_fencing_token"] == owner.fencing_token
    assert proof["successor_fencing_token"] == successor.fencing_token

    forged = owner.as_dict() | {"fencing_token": successor.fencing_token + 1}
    with pytest.raises(leases.ProductionWriteLeaseError, match="no exact governed lease history"):
        leases.authorize_stale_lock_recovery(stale=forged, successor=successor.as_dict())


def test_stale_cut_lock_recovery_accepts_exact_evidence_recovered_owner(monkeypatch, tmp_path):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire_release(leases, session="recovered-owner")
    recovery_time = leases.datetime.fromisoformat(owner.expires_at) + timedelta(seconds=1)
    monkeypatch.setattr(leases, "_now", lambda: recovery_time)
    receipt = leases.recover_expired(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token, recovered_by="operator",
        reason="stale cut process stopped", evidence={"proof": "owner process absent"},
    )
    successor = _acquire_release(leases, session="recovered-successor", commit="c" * 40)
    proof = leases.authorize_stale_lock_recovery(
        stale=owner.as_dict(), successor=successor.as_dict(),
    )
    assert proof["stale_state"] == "recovered"
    assert proof["recovery_receipt_id"] == receipt["receipt_id"]


def test_legacy_cut_lock_directory_recovery_binds_unique_released_predecessor_mtime(
    monkeypatch, tmp_path,
):
    leases = _store(monkeypatch, tmp_path)
    clock = {"now": leases.datetime.now(leases.timezone.utc)}
    monkeypatch.setattr(leases, "_now", lambda: clock["now"])
    owner = _acquire_release(leases, session="legacy-owner")
    lock_time = clock["now"] + timedelta(seconds=1)
    clock["now"] += timedelta(seconds=2)
    leases.release(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token,
    )
    clock["now"] += timedelta(seconds=1)
    successor = _acquire_release(leases, session="legacy-successor", commit="d" * 40)

    proof = leases.authorize_legacy_lock_directory_recovery(
        successor=successor.as_dict(),
        directory_mtime_ns=int(lock_time.timestamp() * 1_000_000_000),
    )
    assert proof["authorized"] is True
    assert proof["predecessor_lease_id"] == owner.lease_id
    assert proof["predecessor_state"] == "released"
    assert proof["successor_fencing_token"] > proof["predecessor_fencing_token"]


def test_legacy_cut_lock_directory_recovery_accepts_evidence_recovered_predecessor(
    monkeypatch, tmp_path,
):
    leases = _store(monkeypatch, tmp_path)
    owner = _acquire_release(leases, session="legacy-recovered-owner")
    lock_time = leases.datetime.fromisoformat(owner.acquired_at) + timedelta(seconds=1)
    recovery_time = leases.datetime.fromisoformat(owner.expires_at) + timedelta(seconds=1)
    monkeypatch.setattr(leases, "_now", lambda: recovery_time)
    receipt = leases.recover_expired(
        lease_id=owner.lease_id, actor=owner.actor, session_id=owner.session_id,
        fencing_token=owner.fencing_token, recovered_by="operator",
        reason="legacy directory owner stopped", evidence={"proof": "no cutter process"},
    )
    successor = _acquire_release(leases, session="legacy-recovered-successor", commit="e" * 40)

    proof = leases.authorize_legacy_lock_directory_recovery(
        successor=successor.as_dict(),
        directory_mtime_ns=int(lock_time.timestamp() * 1_000_000_000),
    )
    assert proof["predecessor_state"] == "recovered"
    assert proof["recovery_receipt_id"] == receipt["receipt_id"]


def test_legacy_cut_lock_directory_recovery_rejects_ambiguous_or_live_history(
    monkeypatch, tmp_path,
):
    leases = _store(monkeypatch, tmp_path)
    clock = {"now": leases.datetime.now(leases.timezone.utc)}
    monkeypatch.setattr(leases, "_now", lambda: clock["now"])
    first = _acquire_release(leases, session="legacy-first")
    clock["now"] += timedelta(seconds=1)
    boundary = clock["now"]
    leases.release(
        lease_id=first.lease_id, actor=first.actor, session_id=first.session_id,
        fencing_token=first.fencing_token,
    )
    second = _acquire_release(leases, session="legacy-second", commit="b" * 40)
    clock["now"] += timedelta(seconds=1)
    leases.release(
        lease_id=second.lease_id, actor=second.actor, session_id=second.session_id,
        fencing_token=second.fencing_token,
    )
    clock["now"] += timedelta(seconds=1)
    successor = _acquire_release(leases, session="legacy-third", commit="c" * 40)
    with pytest.raises(leases.ProductionWriteLeaseError, match="exactly one"):
        leases.authorize_legacy_lock_directory_recovery(
            successor=successor.as_dict(),
            directory_mtime_ns=int(boundary.timestamp() * 1_000_000_000),
        )

    # A directory born during the still-active successor has no terminal
    # predecessor lifetime and cannot be interpreted as a legacy lock.
    live_time = clock["now"] + timedelta(microseconds=1)
    with pytest.raises(leases.ProductionWriteLeaseError, match="exactly one"):
        leases.authorize_legacy_lock_directory_recovery(
            successor=successor.as_dict(),
            directory_mtime_ns=int(live_time.timestamp() * 1_000_000_000),
        )


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
