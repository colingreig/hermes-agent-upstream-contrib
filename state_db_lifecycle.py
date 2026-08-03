#!/usr/bin/env python3
"""Safe retention operations for Hermes' multi-writer SQLite session store.

The lifecycle operator is deliberately dry-run-first.  It never treats
``state.db`` as a single-writer resource: writes use a short ``BEGIN
IMMEDIATE`` transaction with bounded busy retries, and checkpoints are
PASSIVE so live readers/writers can continue.  Every run creates a consistent
SQLite online-backup artifact, which is the only rollback source reported by
the command (never a copied WAL sidecar).

Budgets are *session storage* budgets: they account for ``state.db`` and each
``profiles/<name>/state.db`` (plus WAL/SHM sidecars) and nothing else, because
retention cannot reclaim release trees, worktrees, or browser caches that also
live under the Hermes home.  A policy is refused outright when its prune
horizon multiplied by the expected growth rate cannot fit inside its size
budget, so an unsatisfiable retention contract fails loudly at load instead of
silently reporting "over budget" forever.

Sessions whose writer died without closing them (``ended_at IS NULL`` on a long
silent row) are reaped into the normal archive/prune pipeline rather than being
treated as live conversations; otherwise every crashed worker leaves a row that
retention can never reach and that permanently blocks VACUUM.

This module is an operational edge rather than a SessionDB hot-path feature.
Keeping it here means normal prompts, toolsets, and active-session behavior
remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home


DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent
    / "machine-setup"
    / "fleet-config"
    / "state_db_retention.json"
)
METRICS_META_KEY = "state_db_lifecycle_metrics_v1"
_RETRY_MIN_SECONDS = 0.020
_RETRY_MAX_SECONDS = 0.150


class LifecycleSafetyError(RuntimeError):
    """Raised when a requested lifecycle operation would violate its guardrails."""


def load_retention_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load and minimally validate the fleet-managed retention policy."""
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleSafetyError(f"cannot load retention policy {path}: {exc}") from exc
    return _validate_retention_config(config)


SUPPORTED_SCHEMA_VERSIONS = (1, 2)


def _validate_retention_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Validate both on-disk policy and test/operator supplied mappings.

    Schema 2 adds ``profile.stale_session_after_hours`` and the ``growth``
    section.  Schema 1 documents stay loadable so a lagging deployment keeps
    working: the new controls simply stay off rather than failing the run
    closed on a policy the operator cannot edit from the release tree.
    """
    if config.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise LifecycleSafetyError("unsupported state DB retention policy schema")
    for section in ("root", "profile", "wal_checkpoint", "backup"):
        if not isinstance(config.get(section), dict):
            raise LifecycleSafetyError(f"retention policy is missing {section}")
    growth = config.get("growth", {})
    if not isinstance(growth, dict):
        raise LifecycleSafetyError("retention policy growth section must be a mapping")
    for section, keys in {
        "root": ("max_bytes", "max_retained_age_days"),
        "profile": ("max_bytes", "archive_after_days", "prune_after_days"),
        "wal_checkpoint": ("max_bytes", "max_busy_retries"),
        "backup": ("max_count", "max_bytes", "max_age_days"),
    }.items():
        for key in keys:
            value = config[section].get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise LifecycleSafetyError(f"retention policy {section}.{key} must be non-negative")
    for section, key in (("profile", "stale_session_after_hours"), ("growth", "expected_bytes_per_day")):
        value = dict(config.get(section) or {}).get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise LifecycleSafetyError(f"retention policy {section}.{key} must be non-negative")
    if str(config["wal_checkpoint"].get("mode", "")).upper() != "PASSIVE":
        raise LifecycleSafetyError("only PASSIVE WAL checkpoints are safe for live session writers")
    if config["profile"]["prune_after_days"] < config["profile"]["archive_after_days"]:
        raise LifecycleSafetyError("profile.prune_after_days must not precede archive_after_days")
    if config["backup"]["max_count"] < 1 or config["backup"]["max_bytes"] < 1:
        raise LifecycleSafetyError("backup.max_count and backup.max_bytes must be positive")
    expected_per_day = growth.get("expected_bytes_per_day")
    if expected_per_day:
        horizon_bytes = expected_per_day * config["profile"]["prune_after_days"]
        if horizon_bytes > config["root"]["max_bytes"]:
            raise LifecycleSafetyError(
                "retention policy is unsatisfiable: profile.prune_after_days x "
                "growth.expected_bytes_per_day exceeds root.max_bytes, so retention "
                "can never hold the size budget"
            )
    return dict(config)


def _sidecar_path(db_path: Path, suffix: str) -> Path:
    return db_path.with_name(db_path.name + suffix)


def _path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _retention_scope(db_path: Path) -> tuple[Path, Path, bool]:
    """Return ``(hermes_root, active_profile_home, is_named_profile)``.

    Named profiles live in ``<root>/profiles/<name>/state.db``.  Root budgets
    must include the default profile *and every named profile*, while profile
    budgets stay scoped to the active profile directory.  For the default
    profile the two scopes coincide, so the (smaller) per-profile size budget
    is not applied on top of the root budget -- otherwise the default profile
    would silently inherit a stricter ceiling than the fleet-wide one.
    """
    profile_home = db_path.parent
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent, profile_home, True
    return profile_home, profile_home, False


def _session_db_group_size(db_path: Path) -> int:
    """Bytes held by one session database, including its WAL/SHM sidecars."""
    return sum(_path_size(candidate) for candidate in _session_db_group(db_path))


def _session_db_group(db_path: Path) -> list[Path]:
    return [db_path, _sidecar_path(db_path, "-wal"), _sidecar_path(db_path, "-shm")]


def _session_databases(hermes_root: Path, db_path: Path) -> list[Path]:
    """Every session database inside a Hermes root.

    The root budget is a *session storage* budget, not a disk-usage budget for
    ``~/.hermes``.  A live Hermes root also holds release trees, worktrees,
    bare mirrors, kanban data, and browser profiles, none of which retention
    can reclaim; summing them made the root budget permanently breached and
    forced an ``rglob`` over tens of gigabytes on every dry run.  Only the
    default ``state.db`` and each ``profiles/<name>/state.db`` count here.
    """
    databases: list[Path] = []
    seen: set[Path] = set()
    for candidate in (hermes_root / db_path.name, *sorted((hermes_root / "profiles").glob(f"*/{db_path.name}"))):
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            databases.append(candidate)
    if db_path.is_file() and db_path not in seen:
        databases.append(db_path)
    return databases


def _root_size(hermes_root: Path, db_path: Path) -> int:
    return sum(_session_db_group_size(candidate) for candidate in _session_databases(hermes_root, db_path))


def _backup_artifacts(backup_dir: Path, db_path: Path) -> list[Path]:
    prefix = f"{db_path.stem}.lifecycle-"
    try:
        return sorted(
            (path for path in backup_dir.glob(f"{prefix}*.sqlite") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return []


def _prune_backup_artifacts(
    backup_dir: Path,
    db_path: Path,
    backup_policy: Mapping[str, Any],
    *,
    now: float,
    retain_path: Path,
) -> list[str]:
    """Bound older rollback artifacts while retaining the verified replacement."""
    max_count = int(backup_policy["max_count"])
    max_bytes = int(backup_policy["max_bytes"])
    cutoff = now - float(backup_policy["max_age_days"]) * 86400
    artifacts = [path for path in _backup_artifacts(backup_dir, db_path) if path != retain_path]
    removed: list[str] = []
    for artifact in list(artifacts):
        if artifact.stat().st_mtime >= cutoff:
            continue
        artifact.unlink()
        artifacts.remove(artifact)
        removed.append(str(artifact))
    replacement_bytes = _path_size(retain_path)
    while artifacts and (
        len(artifacts) + 1 > max_count
        or sum(_path_size(path) for path in artifacts) + replacement_bytes > max_bytes
    ):
        artifact = artifacts.pop(0)
        artifact.unlink()
        removed.append(str(artifact))
    if replacement_bytes > max_bytes:
        raise LifecycleSafetyError("verified state DB backup exceeds backup.max_bytes")
    return removed


def _utc_stamp(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_online_backup(db_path: Path, *, backup_dir: Optional[Path] = None, now: Optional[float] = None) -> Path:
    """Create a consistent backup using SQLite's online backup API.

    Copying the DB file or its WAL sidecars is not safe while Hermes is
    running.  SQLite's backup API snapshots committed pages while concurrent
    WAL writers proceed normally.
    """
    if not db_path.is_file():
        raise LifecycleSafetyError(f"state database does not exist: {db_path}")
    stamp = _utc_stamp(time.time() if now is None else now)
    destination_dir = backup_dir or db_path.parent / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    backup_path = destination_dir / f"{db_path.stem}.lifecycle-{stamp}.sqlite"
    suffix = 1
    while backup_path.exists():
        backup_path = destination_dir / f"{db_path.stem}.lifecycle-{stamp}-{suffix}.sqlite"
        suffix += 1
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
    destination = sqlite3.connect(str(backup_path), timeout=1.0)
    try:
        source.backup(destination)
    except sqlite3.Error as exc:
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise LifecycleSafetyError(f"online backup failed: {exc}") from exc
    finally:
        destination.close()
        source.close()
    return backup_path


def _verify_online_backup(backup_path: Path) -> None:
    """Prove that a newly-created rollback artifact is readable and complete."""
    try:
        conn = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            if any(str(row[0]).lower() != "ok" for row in rows):
                raise LifecycleSafetyError("online backup integrity check failed")
            conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise LifecycleSafetyError(f"online backup verification failed: {exc}") from exc


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=1.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    return "busy" in str(exc).lower() or "locked" in str(exc).lower()


def _run_write(
    conn: sqlite3.Connection,
    operation,
    *,
    max_retries: int,
) -> tuple[Any, int, float]:
    """Run one short write transaction using SessionDB-compatible contention policy."""
    retries = 0
    started = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = operation()
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            return result, retries, (time.monotonic() - started) * 1000
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt >= max_retries:
                raise LifecycleSafetyError(f"state DB remained busy during lifecycle write: {exc}") from exc
            retries += 1
            time.sleep(random.uniform(_RETRY_MIN_SECONDS, _RETRY_MAX_SECONDS))
    raise AssertionError("unreachable")


FTS_TABLES = ("messages_fts", "messages_fts_trigram")


def _present_fts_tables(conn: sqlite3.Connection) -> list[str]:
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    return [table for table in FTS_TABLES if table in tables]


def _fts_delete_triggers_present(conn: sqlite3.Connection) -> bool:
    """Fail closed if a prune would leave an existing FTS index stale."""
    for table in _present_fts_tables(conn):
        trigger = f"{table}_delete"
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?", (trigger,)
        ).fetchone()
        if row is None:
            return False
    return True


def _verify_fts_consistency(conn: sqlite3.Connection) -> dict[str, str]:
    """Prove the FTS shadow tables still match ``messages`` after a prune.

    ``messages_fts``/``messages_fts_trigram`` are standalone FTS5 tables, not
    ``content=''`` external-content tables, so they hold their own copy of the
    indexed text and stay in sync only through the hand-written ``AFTER
    DELETE`` triggers.  Checking that the triggers *exist* before a prune is
    necessary but not sufficient; FTS5's ``'integrity-check'`` command is the
    only thing that proves the delete actually landed in the index.
    """
    results: dict[str, str] = {}
    for table in _present_fts_tables(conn):
        try:
            conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
        except sqlite3.DatabaseError as exc:
            results[table] = f"failed: {exc}"
        else:
            results[table] = "ok"
    return results


def _inflight_delegation_clause(session_alias: str = "s") -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM async_delegations d "
        "WHERE d.state IN ('running', 'finalizing') "
        f"AND (d.origin_session = {session_alias}.id OR d.parent_session_id = {session_alias}.id))"
    )


_LAST_MESSAGE_AT = "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id)"
STALE_END_REASON = "lifecycle-stale-reap"


def _stale_session_clause() -> str:
    """Sessions whose owning process is gone but which were never closed.

    ``ended_at IS NULL`` is treated as "active" everywhere else in this
    module, which is correct for a live conversation and badly wrong for a
    session whose writer was killed days ago.  Those rows are permanently
    exempt from archive/prune (both require ``ended_at IS NOT NULL``) *and*
    permanently block VACUUM, so a fleet that reaps crashed workers slowly
    accumulates sessions retention can never touch.  A session only counts as
    stale when it is old, silent, holds no unexpired compression lock, and is
    not referenced by an in-flight delegation.
    """
    return (
        "s.ended_at IS NULL AND s.started_at < :stale_before "
        f"AND COALESCE({_LAST_MESSAGE_AT}, s.started_at) < :stale_before "
        "AND NOT EXISTS (SELECT 1 FROM compression_locks l "
        "WHERE l.session_id = s.id AND l.expires_at > :lock_now) "
        f"AND {_inflight_delegation_clause()}"
    )


def _stale_session_rows(conn: sqlite3.Connection, *, stale_before: float, now: float) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT s.id, s.started_at, {_LAST_MESSAGE_AT} AS last_message_at FROM sessions s "
            f"WHERE {_stale_session_clause()} ORDER BY s.started_at ASC",
            {"stale_before": stale_before, "lock_now": now},
        ).fetchall()
    ]


def _eligible_stale_ids_in_transaction(
    conn: sqlite3.Connection, ids: list[str], *, stale_before: float, now: float
) -> list[str]:
    """Recheck staleness under the write lock, so a session that just woke up survives."""
    if not ids:
        return []
    placeholders = ",".join(f":id{index}" for index in range(len(ids)))
    params: dict[str, Any] = {f"id{index}": value for index, value in enumerate(ids)}
    params.update({"stale_before": stale_before, "lock_now": now})
    rows = conn.execute(
        f"SELECT s.id FROM sessions s WHERE s.id IN ({placeholders}) AND {_stale_session_clause()}",
        params,
    ).fetchall()
    found = {str(row[0]) for row in rows}
    return [session_id for session_id in ids if session_id in found]


def _candidate_rows(conn: sqlite3.Connection, *, archive_before: float, prune_before: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    active_count = int(conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0])
    archived = [
        dict(row)
        for row in conn.execute(
            "SELECT s.id, s.ended_at FROM sessions s "
            "WHERE s.ended_at IS NOT NULL AND s.archived = 0 AND s.ended_at < ? "
            f"AND {_inflight_delegation_clause()} "
            "ORDER BY s.ended_at ASC",
            (archive_before,),
        ).fetchall()
    ]
    pruned = [
        dict(row)
        for row in conn.execute(
            "SELECT s.id, s.ended_at FROM sessions s "
            "WHERE s.ended_at IS NOT NULL AND s.archived = 1 AND s.ended_at < ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM sessions child "
            "  WHERE child.parent_session_id = s.id AND child.ended_at IS NULL"
            f") AND {_inflight_delegation_clause()} ORDER BY s.ended_at ASC",
            (prune_before,),
        ).fetchall()
    ]
    return archived, pruned, active_count


def _eligible_ids_in_transaction(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    archived: bool,
    require_no_active_child: bool,
) -> list[str]:
    """Recheck lifecycle eligibility after BEGIN IMMEDIATE owns the write lock."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    active_child_clause = (
        " AND NOT EXISTS (SELECT 1 FROM sessions child "
        "WHERE child.parent_session_id = s.id AND child.ended_at IS NULL)"
        if require_no_active_child
        else ""
    )
    rows = conn.execute(
        f"SELECT s.id FROM sessions s WHERE s.id IN ({placeholders}) "
        "AND s.ended_at IS NOT NULL AND s.archived = ? "
        f"AND {_inflight_delegation_clause()}{active_child_clause}",
        [*ids, 1 if archived else 0],
    ).fetchall()
    found = {str(row[0]) for row in rows}
    return [session_id for session_id in ids if session_id in found]


def _database_fingerprint(db_path: Path) -> str:
    stat = db_path.stat()
    return hashlib.sha256(f"{db_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()


def _oldest_retained_session(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT id, started_at, ended_at FROM sessions ORDER BY started_at ASC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


def _prior_metrics(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    try:
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (METRICS_META_KEY,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None


def _vacuum_reclaimable_bytes(conn: sqlite3.Connection) -> int:
    """Free-list bytes a VACUUM could actually return to the filesystem.

    Reported so an operator can see, before scheduling a disruptive exclusive
    rewrite, whether VACUUM is worth it.  A busy Hermes DB is usually large
    because of live rows and FTS shadow tables, not because of free pages.
    """
    try:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    except (sqlite3.Error, TypeError, IndexError):
        return 0
    return max(0, page_size * freelist)


def _budget_forecast(
    *,
    root_bytes: int,
    growth_bytes_per_day: Optional[float],
    root_max_bytes: int,
    prune_after_days: float,
    expected_bytes_per_day: Optional[float],
) -> dict[str, Any]:
    """Turn size + age budgets into a satisfiability statement.

    A retention policy is only meaningful if the newest ``prune_after_days``
    of data actually fit inside the size budget.  When they do not, retention
    can never hold the budget and the operator's "over budget" flag is noise
    rather than an actionable signal -- exactly the failure mode where a
    two-gibibyte ceiling was paired with a one-year prune horizon on a store
    growing at roughly a gibibyte a week.
    """
    forecast: dict[str, Any] = {
        "growth_bytes_per_day": growth_bytes_per_day,
        "expected_bytes_per_day": expected_bytes_per_day,
        "policy_satisfiable": None,
        "retention_horizon_bytes": None,
        "days_until_root_budget": None,
    }
    rate = growth_bytes_per_day if growth_bytes_per_day is not None else expected_bytes_per_day
    if rate is not None and rate > 0:
        forecast["retention_horizon_bytes"] = int(rate * prune_after_days)
        forecast["policy_satisfiable"] = forecast["retention_horizon_bytes"] <= root_max_bytes
        headroom = root_max_bytes - root_bytes
        forecast["days_until_root_budget"] = round(headroom / rate, 2)
    return forecast


def _last_ledger_metrics(path: Path) -> Optional[dict[str, Any]]:
    """Most recent metrics row from the JSONL ledger, if one exists.

    Growth rate needs a prior observation.  The in-database checkpoint is only
    written by ``--apply``, so a read-only monitoring cadence would never
    produce a growth number; the ledger gives dry runs a history without
    writing to ``state.db``.
    """
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        metrics = row.get("metrics") if isinstance(row, dict) else None
        if isinstance(metrics, dict) and metrics.get("observed_at") is not None:
            return metrics
    return None


def _append_metrics_ledger(path: Path, plan: Mapping[str, Any]) -> None:
    """Append one observability row, mirroring the fleet's other JSONL ledgers."""
    row = {
        "ts": datetime.fromtimestamp(plan["generated_at"], tz=timezone.utc).isoformat(),
        "db_path": plan["db_path"],
        "scope": plan.get("scope"),
        "dry_run": plan["dry_run"],
        "applied": plan.get("applied"),
        "budget_status": plan.get("budget_status"),
        "fts_consistency": plan.get("fts_consistency"),
        "metrics": plan.get("metrics"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _metrics(
    *,
    conn: sqlite3.Connection,
    db_path: Path,
    profile_bytes: int,
    root_size_bytes: int,
    busy_retries: int,
    write_latency_samples_ms: list[float],
    observed_at: float,
    active_session_count: int,
    stale_session_count: int,
    previous: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    database_bytes = _path_size(db_path)
    if previous is None:
        previous = _prior_metrics(conn)
    growth_per_hour: Optional[float] = None
    if previous:
        old_time = previous.get("observed_at")
        # Growth must track the quantity the budget is measured against.
        # ``database_bytes`` alone misses everything still resident in the WAL
        # between checkpoints, which reads as zero growth on a busy store.
        old_size = previous.get("root_bytes", previous.get("database_bytes"))
        if isinstance(old_time, (int, float)) and isinstance(old_size, (int, float)) and observed_at > old_time:
            growth_per_hour = (root_size_bytes - old_size) / ((observed_at - old_time) / 3600)
    return {
        "observed_at": observed_at,
        "database_bytes": database_bytes,
        "profile_bytes": profile_bytes,
        "root_bytes": root_size_bytes,
        "growth_bytes_per_hour": growth_per_hour,
        "growth_bytes_per_day": None if growth_per_hour is None else growth_per_hour * 24,
        "write_latency_samples_ms": [round(sample, 3) for sample in write_latency_samples_ms],
        "wal_bytes": _path_size(_sidecar_path(db_path, "-wal")),
        "busy_retries": busy_retries,
        "active_session_count": active_session_count,
        "stale_session_count": stale_session_count,
        "vacuum_reclaimable_bytes": _vacuum_reclaimable_bytes(conn),
        "oldest_retained_session": _oldest_retained_session(conn),
    }


def _persist_metrics(conn: sqlite3.Connection, metrics: dict[str, Any], *, max_retries: int) -> tuple[int, float]:
    def operation() -> None:
        conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (METRICS_META_KEY, json.dumps(metrics, sort_keys=True)),
        )

    _, retries, latency = _run_write(conn, operation, max_retries=max_retries)
    return retries, latency


def _load_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleSafetyError(f"cannot read dry-run plan {path}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("dry_run") is not True:
        raise LifecycleSafetyError("vacuum requires a report produced by a lifecycle dry run")
    return plan


def run_lifecycle(
    db_path: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
    apply: bool = False,
    backup_dir: Optional[Path] = None,
    vacuum: bool = False,
    dry_run_plan: Optional[Mapping[str, Any]] = None,
    archive: bool = True,
    prune: bool = True,
    reap_stale: bool = True,
    metrics_out: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Plan or apply state DB retention safely.

    ``apply=False`` is the default.  Archive and prune candidates are always
    restricted to rows with ``ended_at IS NOT NULL``; ``reap_stale`` is what
    lets an abandoned session ever acquire that timestamp, and it closes the
    row at its last observed activity so the age clock does not restart.
    Pruning also requires a completed archive state, so there is a reversible
    soft-hide interval.
    Vacuum is intentionally exceptional: it requires apply mode, a prior
    dry-run report for the same DB fingerprint, a newly-created online backup,
    and zero active sessions.
    """
    db_path = Path(db_path).expanduser().resolve()
    policy = _validate_retention_config(config) if config is not None else load_retention_config()
    runtime_now = time.time() if now is None else now
    profile = policy["profile"]
    root = policy["root"]
    checkpoint = policy["wal_checkpoint"]
    backup_policy = policy["backup"]
    if str(checkpoint.get("mode", "")).upper() != "PASSIVE":
        raise LifecycleSafetyError("only PASSIVE WAL checkpoints are safe for live session writers")
    archive_days = min(float(profile["archive_after_days"]), float(root["max_retained_age_days"]))
    prune_days = min(float(profile["prune_after_days"]), float(root["max_retained_age_days"]))
    if prune_days < archive_days:
        raise LifecycleSafetyError("effective prune budget must not precede archive budget")

    stale_hours = profile.get("stale_session_after_hours")
    expected_bytes_per_day = dict(policy.get("growth") or {}).get("expected_bytes_per_day")

    hermes_root, profile_home, is_named_profile = _retention_scope(db_path)
    root_size_before = _root_size(hermes_root, db_path)
    profile_bytes_before = _session_db_group_size(db_path)
    wal_bytes_before = _path_size(_sidecar_path(db_path, "-wal"))
    conn = _connect(db_path)
    try:
        previous_metrics = _last_ledger_metrics(metrics_out) if metrics_out is not None else None
        if previous_metrics is None:
            previous_metrics = _prior_metrics(conn)
        archive_rows, prune_rows, active_count = _candidate_rows(
            conn,
            archive_before=runtime_now - archive_days * 86400,
            prune_before=runtime_now - prune_days * 86400,
        )
        stale_rows: list[dict[str, Any]] = []
        if reap_stale and stale_hours:
            stale_rows = _stale_session_rows(
                conn, stale_before=runtime_now - float(stale_hours) * 3600, now=runtime_now
            )
        plan = {
            "dry_run": not apply,
            "generated_at": runtime_now,
            "db_path": str(db_path),
            "hermes_root": str(hermes_root),
            "profile_home": str(profile_home),
            "db_fingerprint": _database_fingerprint(db_path),
            "scope": "named-profile" if is_named_profile else "root",
            "archive_ids": [row["id"] for row in archive_rows] if archive else [],
            "prune_ids": [row["id"] for row in prune_rows] if prune else [],
            "stale_ids": [row["id"] for row in stale_rows],
            "active_session_count": active_count,
            "active_session_exclusion_verified": True,
            "budgets": {
                "root_max_bytes": root["max_bytes"],
                "root_max_retained_age_days": root["max_retained_age_days"],
                "profile_max_bytes": profile["max_bytes"],
                "archive_after_days": archive_days,
                "prune_after_days": prune_days,
                "stale_session_after_hours": stale_hours,
                "wal_checkpoint_max_bytes": checkpoint["max_bytes"],
                "backup_max_count": backup_policy["max_count"],
                "backup_max_bytes": backup_policy["max_bytes"],
            },
            "budget_status": {
                "root_over_bytes": root_size_before > root["max_bytes"],
                # The default profile and the root are the same scope, so the
                # narrower per-profile ceiling only applies to named profiles.
                "profile_over_bytes": bool(
                    is_named_profile and profile_bytes_before > profile["max_bytes"]
                ),
                "wal_over_bytes": wal_bytes_before > checkpoint["max_bytes"],
            },
        }

        backup_path: Optional[Path] = None
        backup_dir = backup_dir or profile_home / "backups"
        # Refuse an invalid vacuum request before any filesystem or retention
        # mutation. A fresh active-session read is repeated immediately before
        # VACUUM below, because another writer may create a session while this
        # plan is being reviewed/applied.
        if vacuum:
            if not apply:
                raise LifecycleSafetyError("vacuum requires apply mode")
            if dry_run_plan is None:
                raise LifecycleSafetyError("vacuum requires --plan from an earlier dry-run")
            if dry_run_plan.get("dry_run") is not True or dry_run_plan.get("db_path") != str(db_path):
                raise LifecycleSafetyError("vacuum plan does not describe this state database")
            if dry_run_plan.get("db_fingerprint") != plan["db_fingerprint"]:
                raise LifecycleSafetyError("state DB changed since the dry-run plan; create a fresh plan before vacuum")
            if active_count != 0 or not dry_run_plan.get("active_session_exclusion_verified"):
                raise LifecycleSafetyError("vacuum is forbidden while an active session exists")

        if apply:
            # Do not remove a prior rollback artifact until a replacement is
            # both created by SQLite's online API and independently verified.
            # A known-over-cap source fails before touching the backup folder.
            if _path_size(db_path) > int(backup_policy["max_bytes"]):
                raise LifecycleSafetyError("state DB backup exceeds backup.max_bytes; preserving existing backups")
            backup_path = create_online_backup(db_path, backup_dir=backup_dir, now=runtime_now)
            try:
                _verify_online_backup(backup_path)
                if _path_size(backup_path) > int(backup_policy["max_bytes"]):
                    raise LifecycleSafetyError("verified state DB backup exceeds backup.max_bytes")
            except BaseException:
                try:
                    backup_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            removed_backups = _prune_backup_artifacts(
                backup_dir, db_path, backup_policy, now=runtime_now, retain_path=backup_path
            )
            plan["backup_path"] = str(backup_path)
            plan["backup_retention_removed"] = removed_backups
            plan["rollback"] = {
                "backup_path": str(backup_path),
                "procedure": (
                    "Stop Hermes writers, preserve the current state.db for forensics, "
                    f"then restore this SQLite backup to {db_path}. Do not copy WAL/SHM sidecars."
                ),
            }
        else:
            # Dry-run means no database *or filesystem* mutation.  The apply
            # report will contain a newly created SQLite online backup path.
            plan["backup_path"] = None
            plan["rollback"] = {
                "backup_path": None,
                "procedure": "Apply mode creates a bounded SQLite online backup before any lifecycle mutation.",
            }

        write_latencies: list[float] = []
        busy_retries = 0
        if apply:
            if prune and plan["prune_ids"] and not _fts_delete_triggers_present(conn):
                raise LifecycleSafetyError("refusing to prune: an existing FTS table lacks its delete trigger")

            archive_ids = plan["archive_ids"]
            prune_ids = plan["prune_ids"]
            stale_ids = plan["stale_ids"]
            stale_before = runtime_now - float(stale_hours or 0) * 3600

            def operation() -> dict[str, int]:
                archived_count = 0
                pruned_count = 0
                reaped_count = 0
                safe_archive_ids = _eligible_ids_in_transaction(
                    conn, archive_ids, archived=False, require_no_active_child=False
                )
                safe_prune_ids = _eligible_ids_in_transaction(
                    conn, prune_ids, archived=True, require_no_active_child=True
                )
                safe_stale_ids = _eligible_stale_ids_in_transaction(
                    conn, stale_ids, stale_before=stale_before, now=runtime_now
                )
                if safe_stale_ids:
                    placeholders = ",".join("?" for _ in safe_stale_ids)
                    # Close the abandoned session at its last observed activity
                    # rather than "now", so it enters the archive/prune pipeline
                    # on its real age instead of restarting the clock.
                    cursor = conn.execute(
                        "UPDATE sessions SET ended_at = COALESCE("
                        "  (SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = sessions.id),"
                        "  started_at), end_reason = ? "
                        f"WHERE id IN ({placeholders}) AND ended_at IS NULL",
                        [STALE_END_REASON, *safe_stale_ids],
                    )
                    reaped_count = int(cursor.rowcount if cursor.rowcount >= 0 else 0)
                if safe_archive_ids:
                    placeholders = ",".join("?" for _ in safe_archive_ids)
                    cursor = conn.execute(
                        f"UPDATE sessions SET archived = 1 WHERE id IN ({placeholders}) "
                        "AND ended_at IS NOT NULL AND archived = 0",
                        safe_archive_ids,
                    )
                    archived_count = int(cursor.rowcount if cursor.rowcount >= 0 else 0)
                if safe_prune_ids:
                    placeholders = ",".join("?" for _ in safe_prune_ids)
                    # Preserve existing lineage behavior: retained children of a
                    # pruned session become roots rather than dangling FKs.
                    conn.execute(
                        f"UPDATE sessions SET parent_session_id = NULL "
                        f"WHERE parent_session_id IN ({placeholders})",
                        safe_prune_ids,
                    )
                    conn.execute(f"DELETE FROM messages WHERE session_id IN ({placeholders})", safe_prune_ids)
                    # session_model_usage cascades on the sessions delete, but
                    # compression_locks and async_delegations declare no
                    # foreign key at all, so their rows would outlive the
                    # session forever.  Delegations still in flight are already
                    # excluded from the candidate set; guard again anyway.
                    conn.execute(
                        f"DELETE FROM compression_locks WHERE session_id IN ({placeholders})",
                        safe_prune_ids,
                    )
                    conn.execute(
                        "DELETE FROM async_delegations WHERE state NOT IN ('running', 'finalizing') "
                        f"AND (origin_session IN ({placeholders}) "
                        f"OR parent_session_id IN ({placeholders}))",
                        [*safe_prune_ids, *safe_prune_ids],
                    )
                    cursor = conn.execute(
                        f"DELETE FROM sessions WHERE id IN ({placeholders}) "
                        "AND ended_at IS NOT NULL AND archived = 1",
                        safe_prune_ids,
                    )
                    pruned_count = int(cursor.rowcount if cursor.rowcount >= 0 else 0)
                return {
                    "archived": archived_count,
                    "pruned": pruned_count,
                    "stale_reaped": reaped_count,
                    "archive_skipped_after_recheck": len(archive_ids) - len(safe_archive_ids),
                    "prune_skipped_after_recheck": len(prune_ids) - len(safe_prune_ids),
                    "stale_skipped_after_recheck": len(stale_ids) - len(safe_stale_ids),
                }

            counts, retry_count, latency = _run_write(
                conn, operation, max_retries=int(checkpoint["max_busy_retries"])
            )
            busy_retries += retry_count
            write_latencies.append(latency)
            plan["applied"] = counts

            if counts["pruned"]:
                fts_status = _verify_fts_consistency(conn)
                plan["fts_consistency"] = fts_status
                broken = sorted(name for name, status in fts_status.items() if status != "ok")
                if broken:
                    raise LifecycleSafetyError(
                        f"FTS integrity check failed after prune for {', '.join(broken)}; "
                        f"restore the backup at {backup_path} and run hermes state repair"
                    )
            else:
                plan["fts_consistency"] = {"skipped": "no rows pruned"}

            # A PASSIVE checkpoint never blocks writers or readers.  It is a
            # bounded maintenance attempt, not a destructive WAL truncate.
            if wal_bytes_before > checkpoint["max_bytes"]:
                checkpoint_row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                plan["checkpoint"] = {
                    "mode": "PASSIVE",
                    "busy": int(checkpoint_row[0]),
                    "log_frames": int(checkpoint_row[1]),
                    "checkpointed_frames": int(checkpoint_row[2]),
                }
            else:
                plan["checkpoint"] = {"mode": "PASSIVE", "skipped": "within WAL budget"}

            if vacuum:
                if backup_path is None or not backup_path.is_file():
                    raise LifecycleSafetyError("vacuum is forbidden without a verified online backup artifact")
                active_before_vacuum = int(
                    conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
                )
                if active_before_vacuum != 0:
                    raise LifecycleSafetyError("vacuum is forbidden while an active session exists")
                started = time.monotonic()
                conn.execute("VACUUM")
                write_latencies.append((time.monotonic() - started) * 1000)
                plan["vacuum"] = "completed"

            observed_at = time.time()
            metrics = _metrics(
                conn=conn,
                db_path=db_path,
                profile_bytes=_session_db_group_size(db_path),
                root_size_bytes=_root_size(hermes_root, db_path),
                busy_retries=busy_retries,
                write_latency_samples_ms=write_latencies,
                observed_at=observed_at,
                active_session_count=int(
                    conn.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
                ),
                stale_session_count=len(stale_rows) - counts.get("stale_reaped", 0),
                previous=previous_metrics,
            )
            metrics_retries, metrics_latency = _persist_metrics(
                conn, metrics, max_retries=int(checkpoint["max_busy_retries"])
            )
            metrics["busy_retries"] += metrics_retries
            metrics["write_latency_samples_ms"].append(round(metrics_latency, 3))
            plan["metrics"] = metrics
        else:
            plan["applied"] = {"archived": 0, "pruned": 0, "stale_reaped": 0}
            plan["checkpoint"] = {"mode": "PASSIVE", "planned": wal_bytes_before > checkpoint["max_bytes"]}
            plan["fts_consistency"] = {"skipped": "dry run performs no delete"}
            plan["metrics"] = _metrics(
                conn=conn,
                db_path=db_path,
                profile_bytes=profile_bytes_before,
                root_size_bytes=root_size_before,
                busy_retries=0,
                write_latency_samples_ms=[],
                observed_at=runtime_now,
                active_session_count=active_count,
                stale_session_count=len(stale_rows),
                previous=previous_metrics,
            )
        plan["metrics"]["budget_forecast"] = _budget_forecast(
            root_bytes=plan["metrics"]["root_bytes"],
            growth_bytes_per_day=plan["metrics"]["growth_bytes_per_day"],
            root_max_bytes=int(root["max_bytes"]),
            prune_after_days=prune_days,
            expected_bytes_per_day=expected_bytes_per_day,
        )
        if metrics_out is not None:
            _append_metrics_ledger(metrics_out, plan)
        return plan
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run-first Hermes state.db lifecycle operator")
    parser.add_argument("--db", type=Path, default=get_hermes_home() / "state.db")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--backup-dir", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="plan only and mutate nothing (the default when --apply is absent)",
    )
    mode.add_argument("--apply", action="store_true", help="perform the planned archive/prune actions")
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument("--no-prune", action="store_true")
    parser.add_argument(
        "--no-reap-stale",
        action="store_true",
        help="do not close abandoned sessions whose writer is gone",
    )
    parser.add_argument("--vacuum", action="store_true", help="requires --apply and --plan from a prior dry run")
    parser.add_argument("--plan", type=Path, help="prior dry-run JSON report required for --vacuum")
    parser.add_argument("--plan-out", type=Path, help="write the JSON report to this path")
    parser.add_argument(
        "--metrics-out",
        type=Path,
        help="append one JSONL observability row (also supplies growth history for dry runs)",
    )
    args = parser.parse_args(argv)
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply")
    try:
        report = run_lifecycle(
            args.db,
            config=load_retention_config(args.config),
            apply=args.apply,
            backup_dir=args.backup_dir,
            vacuum=args.vacuum,
            dry_run_plan=_load_plan(args.plan) if args.plan else None,
            archive=not args.no_archive,
            prune=not args.no_prune,
            reap_stale=not args.no_reap_stale,
            metrics_out=args.metrics_out,
        )
    except LifecycleSafetyError as exc:
        print(f"state-db lifecycle refused: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.plan_out:
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        args.plan_out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
