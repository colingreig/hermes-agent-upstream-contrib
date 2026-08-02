"""Coverage for the dry-run-first state DB retention operator."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
import state_db_lifecycle as lifecycle
from state_db_lifecycle import LifecycleSafetyError, run_lifecycle


def _config(*, wal_max_bytes: int = 1024 * 1024) -> dict:
    return {
        "schema_version": 1,
        "root": {"max_bytes": 1024 * 1024 * 1024, "max_retained_age_days": 730},
        "profile": {"max_bytes": 1024 * 1024 * 1024, "archive_after_days": 7, "prune_after_days": 30},
        "wal_checkpoint": {"max_bytes": wal_max_bytes, "mode": "PASSIVE", "max_busy_retries": 2},
        "backup": {"max_count": 3, "max_bytes": 1024 * 1024 * 1024, "max_age_days": 30},
    }


def _db_with_sessions(tmp_path: Path) -> tuple[Path, SessionDB]:
    path = tmp_path / "state.db"
    db = SessionDB(path)
    return path, db


def _set_ended_at(path: Path, session_id: str, ended_at: float, *, archived: int = 0) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = 'test', archived = ? WHERE id = ?",
            (ended_at, archived, session_id),
        )


def test_lifecycle_dry_run_is_read_only_without_retention_mutation(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    db.append_message("old", "user", "retained searchable text")
    _set_ended_at(path, "old", time.time() - 40 * 86400)

    report = run_lifecycle(path, config=_config(), backup_dir=tmp_path / "backups")

    assert report["dry_run"] is True
    assert report["archive_ids"] == ["old"]
    assert report["prune_ids"] == []
    assert report["applied"] == {"archived": 0, "pruned": 0}
    assert report["backup_path"] is None
    assert not (tmp_path / "backups").exists()
    assert db.get_session("old")["archived"] == 0
    db.close()


def test_lifecycle_root_budget_includes_sibling_named_profiles(tmp_path: Path) -> None:
    root = tmp_path / "hermes"
    path = root / "profiles" / "coder" / "state.db"
    db = SessionDB(path)
    sibling_bytes = 4096
    sibling = root / "profiles" / "writer" / "state.db"
    sibling.parent.mkdir(parents=True)
    sibling.write_bytes(b"x" * sibling_bytes)

    config = _config()
    config["root"]["max_bytes"] = 1
    report = run_lifecycle(path, config=config)

    assert report["hermes_root"] == str(root)
    assert report["profile_home"] == str(path.parent)
    assert report["metrics"]["root_bytes"] >= report["metrics"]["profile_bytes"] + sibling_bytes
    assert report["budget_status"]["root_over_bytes"] is True
    db.close()


def test_lifecycle_apply_bounds_backup_artifacts(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400)
    backups = tmp_path / "backups"
    backups.mkdir()
    stale_backup = backups / "state.lifecycle-previous.sqlite"
    stale_backup.write_bytes(b"old backup")
    config = _config()
    config["backup"]["max_count"] = 1

    report = run_lifecycle(path, config=config, apply=True, backup_dir=backups)

    assert str(stale_backup) in report["backup_retention_removed"]
    assert len(list(backups.glob("state.lifecycle-*.sqlite"))) == 1
    assert Path(report["backup_path"]).is_file()
    db.close()


def test_lifecycle_preserves_existing_backup_when_replacement_fails(tmp_path: Path, monkeypatch) -> None:
    path, db = _db_with_sessions(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    existing = backups / "state.lifecycle-existing.sqlite"
    existing.write_bytes(b"known rollback artifact")

    def fail_backup(*args, **kwargs):
        raise LifecycleSafetyError("simulated backup failure")

    monkeypatch.setattr(lifecycle, "create_online_backup", fail_backup)
    with pytest.raises(LifecycleSafetyError, match="simulated backup failure"):
        run_lifecycle(path, config=_config(), apply=True, backup_dir=backups)

    assert existing.is_file()
    db.close()


def test_lifecycle_preserves_existing_backup_when_replacement_exceeds_cap(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    existing = backups / "state.lifecycle-existing.sqlite"
    existing.write_bytes(b"known rollback artifact")
    config = _config()
    config["backup"]["max_bytes"] = 1

    with pytest.raises(LifecycleSafetyError, match="preserving existing backups"):
        run_lifecycle(path, config=config, apply=True, backup_dir=backups)

    assert existing.is_file()
    db.close()


def test_lifecycle_archives_then_prunes_ended_session_and_keeps_fts_consistent(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("archive", "cli")
    db.append_message("archive", "user", "lifecycle fts needle")
    _set_ended_at(path, "archive", time.time() - 10 * 86400)
    first = run_lifecycle(path, config=_config(), apply=True, backup_dir=tmp_path / "backups")
    assert first["applied"]["archived"] == 1
    assert db.get_session("archive")["archived"] == 1

    _set_ended_at(path, "archive", time.time() - 40 * 86400, archived=1)
    second = run_lifecycle(path, config=_config(), apply=True, backup_dir=tmp_path / "backups")
    assert second["applied"]["pruned"] == 1
    assert db.get_session("archive") is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'needle'").fetchone()[0] == 0
    db.close()


def test_lifecycle_reports_and_bounds_wal_checkpoint_to_passive_mode(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400)

    report = run_lifecycle(path, config=_config(wal_max_bytes=0), apply=True, backup_dir=tmp_path / "backups")

    assert report["checkpoint"]["mode"] == "PASSIVE"
    assert "busy" in report["checkpoint"]
    assert report["metrics"]["wal_bytes"] >= 0
    db.close()


def test_lifecycle_never_archives_or_prunes_active_sessions(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("active", "cli")
    db.append_message("active", "user", "still in prompt cache")
    db.create_session("ended-parent", "cli")
    db.end_session("ended-parent", "test")
    _set_ended_at(path, "ended-parent", time.time() - 40 * 86400, archived=1)
    db.create_session("active-child", "cli", parent_session_id="ended-parent")

    report = run_lifecycle(path, config=_config(), apply=True, backup_dir=tmp_path / "backups")

    assert report["active_session_count"] == 2
    assert report["active_session_exclusion_verified"] is True
    assert report["archive_ids"] == []
    assert report["prune_ids"] == []
    assert db.get_session("active")["ended_at"] is None
    assert db.get_session("active")["archived"] == 0
    assert db.get_session("ended-parent") is not None
    assert db.get_session("active-child")["parent_session_id"] == "ended-parent"
    db.close()


def test_lifecycle_rechecks_new_active_child_inside_prune_transaction(tmp_path: Path, monkeypatch) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("parent", "cli")
    _set_ended_at(path, "parent", time.time() - 40 * 86400, archived=1)
    original_backup = lifecycle.create_online_backup

    def create_backup_after_new_child(*args, **kwargs):
        db.create_session("late-active-child", "cli", parent_session_id="parent")
        return original_backup(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "create_online_backup", create_backup_after_new_child)
    report = run_lifecycle(path, config=_config(), apply=True, backup_dir=tmp_path / "backups")

    assert report["prune_ids"] == ["parent"]
    assert report["applied"]["pruned"] == 0
    assert report["applied"]["prune_skipped_after_recheck"] == 1
    assert db.get_session("parent") is not None
    assert db.get_session("late-active-child")["parent_session_id"] == "parent"
    db.close()


def test_lifecycle_excludes_sessions_referenced_by_inflight_delegations(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    for session_id in ("origin-protected", "parent-protected"):
        db.create_session(session_id, "cli")
        _set_ended_at(path, session_id, time.time() - 40 * 86400, archived=1)
    with sqlite3.connect(path) as conn:
        now = time.time()
        conn.execute(
            "INSERT INTO async_delegations "
            "(delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at) "
            "VALUES (?, ?, ?, 'running', ?, ?)",
            ("origin-delegation", "origin-protected", None, now, now),
        )
        conn.execute(
            "INSERT INTO async_delegations "
            "(delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at) "
            "VALUES (?, ?, ?, 'finalizing', ?, ?)",
            ("parent-delegation", "other", "parent-protected", now, now),
        )

    report = run_lifecycle(path, config=_config(), apply=True, backup_dir=tmp_path / "backups")

    assert report["prune_ids"] == []
    assert db.get_session("origin-protected") is not None
    assert db.get_session("parent-protected") is not None
    db.close()
