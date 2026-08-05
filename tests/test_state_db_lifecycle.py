"""Coverage for the dry-run-first state DB retention operator."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_state import SessionDB
import state_db_lifecycle as lifecycle
from state_db_lifecycle import LifecycleSafetyError, run_lifecycle


@contextmanager
def _noop_guard(phase: str):
    """Stand-in for the fenced production write lease in unit tests."""
    assert isinstance(phase, str) and phase
    yield


def _recording_guard(record: list[str]):
    @contextmanager
    def guard(phase: str):
        record.append(phase)
        yield

    return guard


def _config(*, wal_max_bytes: int = 1024 * 1024, stale_hours: float | None = 48) -> dict:
    return {
        "schema_version": 2,
        "root": {"max_bytes": 1024 * 1024 * 1024, "max_retained_age_days": 730},
        "profile": {
            "max_bytes": 1024 * 1024 * 1024,
            "archive_after_days": 7,
            "prune_after_days": 30,
            "stale_session_after_hours": stale_hours,
        },
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


def _set_started_at(path: Path, session_id: str, started_at: float) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (started_at, session_id))


def _set_message_timestamps(path: Path, session_id: str, timestamp: float) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (timestamp, session_id))


def test_lifecycle_dry_run_is_read_only_without_retention_mutation(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    db.append_message("old", "user", "retained searchable text")
    _set_ended_at(path, "old", time.time() - 40 * 86400)

    report = run_lifecycle(path, config=_config(), backup_dir=tmp_path / "backups")

    assert report["dry_run"] is True
    assert report["archive_ids"] == ["old"]
    assert report["prune_ids"] == []
    assert report["applied"] == {"archived": 0, "pruned": 0, "stale_reaped": 0}
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

    report = run_lifecycle(path, config=config, apply=True, mutation_guard_factory=_noop_guard, backup_dir=backups)

    assert str(stale_backup) in report["backup_retention_removed"]
    assert len(list(backups.glob("state.lifecycle-*.sqlite"))) == 1
    assert Path(report["backup_path"]).is_file()
    db.close()


def test_production_policy_allows_apply_for_2_5_gib_db_and_bounds_backups(
    tmp_path: Path, monkeypatch
) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400)
    policy = lifecycle.load_retention_config()
    gib = 1024**3

    # 86e2kt3yt: a backup cap below the root budget made --apply structurally
    # impossible on a full-size DB.  The cap must stay above the budget, and
    # the whole retained backup set must still fit the mini's free space.
    assert policy["backup"]["max_bytes"] == policy["root"]["max_bytes"] + gib
    assert policy["backup"]["max_count"] >= 1
    assert policy["backup"]["max_count"] * policy["backup"]["max_bytes"] <= 24 * gib

    actual_path_size = lifecycle._path_size

    def production_realistic_path_size(candidate: Path) -> int:
        if Path(candidate) == path:
            return 5 * gib // 2
        return actual_path_size(candidate)

    monkeypatch.setattr(lifecycle, "_path_size", production_realistic_path_size)
    report = run_lifecycle(path, config=policy, apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    backup_path = Path(report["backup_path"])
    assert backup_path.is_file()
    assert report["rollback"]["backup_path"] == str(backup_path)
    assert report["applied"]["archived"] == 1
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
        run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=backups)

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
        run_lifecycle(path, config=config, apply=True, mutation_guard_factory=_noop_guard, backup_dir=backups)

    assert existing.is_file()
    db.close()


def test_lifecycle_archives_then_prunes_ended_session_and_keeps_fts_consistent(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("archive", "cli")
    db.append_message("archive", "user", "lifecycle fts needle")
    _set_ended_at(path, "archive", time.time() - 10 * 86400)
    first = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")
    assert first["applied"]["archived"] == 1
    assert db.get_session("archive")["archived"] == 1

    _set_ended_at(path, "archive", time.time() - 40 * 86400, archived=1)
    second = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")
    assert second["applied"]["pruned"] == 1
    assert db.get_session("archive") is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'needle'").fetchone()[0] == 0
    db.close()


def test_lifecycle_reports_and_bounds_wal_checkpoint_to_passive_mode(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400)

    report = run_lifecycle(path, config=_config(wal_max_bytes=0), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

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

    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

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
    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

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

    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["prune_ids"] == []
    assert db.get_session("origin-protected") is not None
    assert db.get_session("parent-protected") is not None
    db.close()


def test_root_budget_counts_session_databases_not_the_whole_hermes_home(tmp_path: Path) -> None:
    """Release trees, worktrees, and caches must not consume the retention budget."""
    root = tmp_path / "hermes"
    path = root / "state.db"
    db = SessionDB(path)
    named = root / "profiles" / "coder" / "state.db"
    named.parent.mkdir(parents=True)
    named.write_bytes(b"x" * 4096)
    unrelated = root / "releases" / "v1" / "blob.bin"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"y" * (32 * 1024 * 1024))

    report = run_lifecycle(path, config=_config())

    assert report["metrics"]["root_bytes"] == lifecycle._session_db_group_size(path) + 4096
    assert report["metrics"]["root_bytes"] < unrelated.stat().st_size
    assert report["budget_status"]["root_over_bytes"] is False
    db.close()


def test_default_profile_reports_its_own_size_budget_breach(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    config = _config()
    config["profile"]["max_bytes"] = 1

    report = run_lifecycle(path, config=config)

    assert report["scope"] == "root"
    assert report["metrics"]["profile_bytes"] > report["budgets"]["profile_max_bytes"]
    assert report["budget_status"]["profile_over_bytes"] is True
    db.close()


def test_named_profile_still_enforces_its_own_size_budget(tmp_path: Path) -> None:
    path = tmp_path / "hermes" / "profiles" / "coder" / "state.db"
    db = SessionDB(path)
    config = _config()
    config["profile"]["max_bytes"] = 1

    report = run_lifecycle(path, config=config)

    assert report["scope"] == "named-profile"
    assert report["budget_status"]["profile_over_bytes"] is True
    db.close()


def test_policy_with_prune_horizon_larger_than_size_budget_is_rejected(tmp_path: Path) -> None:
    config = _config()
    config["growth"] = {"expected_bytes_per_day": config["root"]["max_bytes"]}
    config["profile"]["prune_after_days"] = 30

    with pytest.raises(LifecycleSafetyError, match="unsatisfiable"):
        run_lifecycle(tmp_path / "state.db", config=config)


def test_policy_rejects_profile_budget_below_retained_growth_horizon(tmp_path: Path) -> None:
    config = _config()
    config["root"]["max_bytes"] = 64 * 1024 * 1024
    config["profile"]["max_bytes"] = 1 * 1024 * 1024
    config["profile"]["prune_after_days"] = 30
    config["growth"] = {"expected_bytes_per_day": 1 * 1024 * 1024}

    with pytest.raises(LifecycleSafetyError, match="profile.max_bytes"):
        run_lifecycle(tmp_path / "state.db", config=config)


def test_shipped_production_policy_is_self_consistent() -> None:
    policy = lifecycle.load_retention_config()

    horizon = policy["growth"]["expected_bytes_per_day"] * policy["profile"]["prune_after_days"]
    assert horizon <= policy["root"]["max_bytes"]
    # The default profile may hold the fleet's entire observed growth. Its
    # budget therefore must not alarm before its retained horizon is eligible
    # for pruning.
    assert horizon <= policy["profile"]["max_bytes"]
    assert policy["profile"]["stale_session_after_hours"] > 0


def test_schema_v1_policy_still_loads_with_new_controls_disabled(tmp_path: Path) -> None:
    legacy = _config(stale_hours=None)
    legacy["schema_version"] = 1
    del legacy["profile"]["stale_session_after_hours"]
    path, db = _db_with_sessions(tmp_path)
    db.create_session("abandoned", "cli")
    _set_started_at(path, "abandoned", time.time() - 30 * 86400)

    report = run_lifecycle(path, config=legacy, apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["stale_ids"] == []
    assert report["applied"]["stale_reaped"] == 0
    assert db.get_session("abandoned")["ended_at"] is None
    db.close()


def test_abandoned_session_is_closed_so_retention_can_reach_it(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("abandoned", "cli")
    db.append_message("abandoned", "user", "last thing the dead worker said")
    stale_at = time.time() - 30 * 86400
    _set_started_at(path, "abandoned", stale_at)
    _set_message_timestamps(path, "abandoned", stale_at)

    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["stale_ids"] == ["abandoned"]
    assert report["applied"]["stale_reaped"] == 1
    row = db.get_session("abandoned")
    assert row["end_reason"] == lifecycle.STALE_END_REASON
    # Closed at last activity, not at "now", so the age clock does not restart.
    assert row["ended_at"] == pytest.approx(stale_at, abs=1)
    db.close()


def test_recently_active_open_session_is_never_reaped(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("busy", "cli")
    _set_started_at(path, "busy", time.time() - 30 * 86400)
    db.append_message("busy", "user", "still talking right now")

    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["stale_ids"] == []
    assert db.get_session("busy")["ended_at"] is None
    db.close()


def test_open_session_holding_a_compression_lock_is_never_reaped(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("compressing", "cli")
    _set_started_at(path, "compressing", time.time() - 30 * 86400)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) "
            "VALUES (?, 'worker', ?, ?)",
            ("compressing", time.time(), time.time() + 3600),
        )

    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["stale_ids"] == []
    assert db.get_session("compressing")["ended_at"] is None
    db.close()


def test_reaper_rechecks_a_session_that_wakes_up_before_the_write_lock(
    tmp_path: Path, monkeypatch
) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("waking", "cli")
    _set_started_at(path, "waking", time.time() - 30 * 86400)
    original_backup = lifecycle.create_online_backup

    def speak_before_backup(*args, **kwargs):
        db.append_message("waking", "user", "I am back")
        return original_backup(*args, **kwargs)

    monkeypatch.setattr(lifecycle, "create_online_backup", speak_before_backup)
    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["stale_ids"] == ["waking"]
    assert report["applied"]["stale_reaped"] == 0
    assert report["applied"]["stale_skipped_after_recheck"] == 1
    assert db.get_session("waking")["ended_at"] is None
    db.close()


def test_reaping_can_be_disabled(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("abandoned", "cli")
    _set_started_at(path, "abandoned", time.time() - 30 * 86400)

    report = run_lifecycle(path, config=_config(), reap_stale=False)

    assert report["stale_ids"] == []
    db.close()


def test_prune_verifies_fts_integrity_and_clears_orphaned_rows(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("doomed", "cli")
    db.append_message("doomed", "user", "orphanable needle")
    _set_ended_at(path, "doomed", time.time() - 40 * 86400, archived=1)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO compression_locks (session_id, holder, acquired_at, expires_at) "
            "VALUES (?, 'dead-worker', ?, ?)",
            ("doomed", time.time() - 40 * 86400, time.time() - 39 * 86400),
        )
        conn.execute(
            "INSERT INTO async_delegations "
            "(delegation_id, origin_session, parent_session_id, state, dispatched_at, updated_at) "
            "VALUES ('done', 'doomed', NULL, 'completed', ?, ?)",
            (time.time() - 40 * 86400, time.time() - 40 * 86400),
        )

    report = run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")

    assert report["applied"]["pruned"] == 1
    assert report["fts_consistency"] == {"messages_fts": "ok", "messages_fts_trigram": "ok"}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM compression_locks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM async_delegations").fetchone()[0] == 0
    db.close()


def test_prune_refuses_to_finish_when_fts_integrity_fails(tmp_path: Path, monkeypatch) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("doomed", "cli")
    db.append_message("doomed", "user", "needle")
    _set_ended_at(path, "doomed", time.time() - 40 * 86400, archived=1)
    monkeypatch.setattr(
        lifecycle, "_verify_fts_consistency", lambda conn: {"messages_fts": "failed: corrupt"}
    )

    with pytest.raises(LifecycleSafetyError, match="FTS integrity check failed"):
        run_lifecycle(path, config=_config(), apply=True, mutation_guard_factory=_noop_guard, backup_dir=tmp_path / "backups")
    db.close()


def test_metrics_ledger_gives_dry_runs_a_growth_rate_without_touching_the_db(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("s", "cli")
    ledger = tmp_path / "logs" / "state-db-lifecycle.jsonl"
    now = time.time()

    first = run_lifecycle(path, config=_config(), metrics_out=ledger, now=now - 3600)
    assert first["metrics"]["growth_bytes_per_hour"] is None

    db.append_message("s", "user", "x" * 400000)
    second = run_lifecycle(path, config=_config(), metrics_out=ledger, now=now)

    rows = [json.loads(line) for line in ledger.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["dry_run"] is True
    assert rows[0]["metrics"]["observed_at"] == now - 3600
    assert second["metrics"]["growth_bytes_per_hour"] > 0
    assert second["metrics"]["growth_bytes_per_day"] == second["metrics"]["growth_bytes_per_hour"] * 24
    # A dry run must still leave no in-database checkpoint behind.
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM state_meta WHERE key = ?", (lifecycle.METRICS_META_KEY,)
            ).fetchone()[0]
            == 0
        )
    db.close()


def test_dry_run_reports_budget_forecast_and_vacuum_payoff(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("s", "cli")
    config = _config()
    config["growth"] = {"expected_bytes_per_day": 1024 * 1024}

    report = run_lifecycle(path, config=config)

    forecast = report["metrics"]["budget_forecast"]
    assert forecast["policy_satisfiable"] is True
    assert forecast["expected_bytes_per_day"] == 1024 * 1024
    assert forecast["days_until_root_budget"] > 0
    assert report["metrics"]["vacuum_reclaimable_bytes"] >= 0
    assert report["metrics"]["active_session_count"] == 1
    db.close()


def test_budget_forecast_is_unsatisfiable_when_profile_horizon_breaches_only_profile() -> None:
    forecast = lifecycle._budget_forecast(
        root_bytes=100,
        profile_bytes=75,
        growth_bytes_per_day=20,
        root_max_bytes=1000,
        profile_max_bytes=100,
        prune_after_days=6,
        expected_bytes_per_day=None,
    )

    assert forecast["retention_horizon_bytes"] == 120
    assert forecast["root_policy_satisfiable"] is True
    assert forecast["profile_policy_satisfiable"] is False
    assert forecast["policy_satisfiable"] is False
    assert forecast["days_until_root_budget"] == 45.0
    assert forecast["days_until_profile_budget"] == 1.25


def test_cli_dry_run_flag_is_explicit_and_mutually_exclusive_with_apply(
    tmp_path: Path, capsys
) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.close()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_config()), encoding="utf-8")

    assert lifecycle.main(["--db", str(path), "--config", str(policy), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True

    with pytest.raises(SystemExit):
        lifecycle.main(["--db", str(path), "--config", str(policy), "--dry-run", "--apply"])


def test_apply_is_refused_without_a_mutation_guard(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.close()

    with pytest.raises(LifecycleSafetyError, match="production write lease"):
        run_lifecycle(path, config=_config(), apply=True, backup_dir=tmp_path / "backups")

    # Fail-closed means refused before any mutation: no backup artifact appears.
    assert not (tmp_path / "backups").exists()


def test_dry_run_requires_no_mutation_guard(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.close()

    report = run_lifecycle(path, config=_config())

    assert report["dry_run"] is True


def test_every_durable_apply_phase_is_fenced(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("doomed", "cli")
    db.append_message("doomed", "user", "needle")
    _set_ended_at(path, "doomed", time.time() - 40 * 86400, archived=1)
    phases: list[str] = []

    run_lifecycle(
        path,
        config=_config(wal_max_bytes=0),
        apply=True,
        backup_dir=tmp_path / "backups",
        mutation_guard_factory=_recording_guard(phases),
    )

    assert phases == ["backup", "retention-write", "checkpoint", "metrics"]
    db.close()


def test_fence_loss_fails_closed_before_further_mutation(tmp_path: Path) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.create_session("doomed", "cli")
    db.append_message("doomed", "user", "needle")
    _set_ended_at(path, "doomed", time.time() - 40 * 86400, archived=1)

    @contextmanager
    def lose_fence_after_backup(phase: str):
        if phase == "retention-write":
            raise LifecycleSafetyError("production write fence lost during retention-write")
        yield

    with pytest.raises(LifecycleSafetyError, match="fence lost during retention-write"):
        run_lifecycle(
            path,
            config=_config(),
            apply=True,
            backup_dir=tmp_path / "backups",
            mutation_guard_factory=lose_fence_after_backup,
        )

    # The backup phase completed, but no session row was mutated afterwards.
    assert list((tmp_path / "backups").glob("*.sqlite"))
    row = db.get_session("doomed")
    assert row is not None and row["archived"] == 1
    db.close()


def test_apply_end_to_end_with_real_production_write_lease(tmp_path: Path) -> None:
    from cron import production_write_lease as pwl

    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400, archived=1)
    lease_db = tmp_path / "production-write-lease.db"

    with lifecycle.production_write_guards(
        hermes_root=tmp_path,
        commit_sha="a" * 40,
        reason="lifecycle lease integration test",
        lease_database_path=lease_db,
    ) as (guard, lease):
        assert lease.actor == lifecycle.PRODUCTION_WRITE_ACTOR
        assert set(lease.resources) == set(lifecycle.PRODUCTION_WRITE_RESOURCES)
        # Exactly one incompatible writer may hold the resource at a time.
        with pytest.raises(pwl.ProductionWriteLeaseError, match="conflict"):
            pwl.acquire(
                ["session-db"],
                "state-db-lifecycle",
                "competing-session",
                str(tmp_path),
                "hermes-agent",
                "b" * 40,
                "competing lifecycle operator",
                database_path=lease_db,
            )
        report = run_lifecycle(
            path,
            config=_config(),
            apply=True,
            backup_dir=tmp_path / "backups",
            mutation_guard_factory=guard,
        )

    assert report["applied"]["pruned"] == 1
    status = pwl.status(database_path=lease_db)
    assert status["active_leases"] == []
    assert status["fence_loss_receipts"] == []
    db.close()


def test_lost_real_fence_aborts_apply_and_records_a_receipt(tmp_path: Path) -> None:
    from cron import production_write_lease as pwl

    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400, archived=1)
    lease_db = tmp_path / "production-write-lease.db"

    with lifecycle.production_write_guards(
        hermes_root=tmp_path,
        commit_sha="a" * 40,
        reason="lifecycle fence loss test",
        lease_database_path=lease_db,
    ) as (guard, lease):
        # The owner loses its lease before the first durable phase.
        pwl.release(
            lease_id=lease.lease_id,
            actor=lease.actor,
            session_id=lease.session_id,
            fencing_token=lease.fencing_token,
            database_path=lease_db,
        )
        with pytest.raises(LifecycleSafetyError, match="fence lost during backup"):
            run_lifecycle(
                path,
                config=_config(),
                apply=True,
                backup_dir=tmp_path / "backups",
                mutation_guard_factory=guard,
            )

    status = pwl.status(database_path=lease_db)
    assert status["active_leases"] == []
    assert len(status["fence_loss_receipts"]) == 1
    receipt = status["fence_loss_receipts"][0]
    assert receipt["evidence"] == {"operation": "state-db-lifecycle-apply", "phase": "backup"}
    # No session mutation happened and no backup artifact was committed.
    assert db.get_session("old")["archived"] == 1
    assert not list((tmp_path / "backups").glob("*.sqlite")) if (tmp_path / "backups").exists() else True
    db.close()


def test_cli_apply_acquires_and_releases_the_production_write_lease(
    tmp_path: Path, capsys
) -> None:
    from cron import production_write_lease as pwl

    path, db = _db_with_sessions(tmp_path)
    db.create_session("old", "cli")
    _set_ended_at(path, "old", time.time() - 40 * 86400, archived=1)
    db.close()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_config()), encoding="utf-8")
    lease_db = tmp_path / "production-write-lease.db"

    rc = lifecycle.main(
        [
            "--db", str(path),
            "--config", str(policy),
            "--apply",
            "--commit-sha", "a" * 40,
            "--lease-db", str(lease_db),
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["production_write_lease"]["actor"] == "state-db-lifecycle"
    assert report["production_write_lease"]["resources"] == ["session-db"]
    assert report["production_write_lease"]["commit_sha"] == "a" * 40
    assert report["applied"]["pruned"] == 1
    status = pwl.status(database_path=lease_db)
    assert status["active_leases"] == []


def test_cli_apply_refuses_an_invalid_commit_sha(tmp_path: Path, capsys) -> None:
    path, db = _db_with_sessions(tmp_path)
    db.close()
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(_config()), encoding="utf-8")

    rc = lifecycle.main(
        ["--db", str(path), "--config", str(policy), "--apply", "--commit-sha", "not-a-sha"]
    )

    assert rc == 2
    assert "40-character commit SHA" in capsys.readouterr().err
