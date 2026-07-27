"""Profile-local durable ledger for cron execution attempts.

An execution is owned by one unguessable token for its whole lifetime.  Every
mutation fences on that token, so an old worker, a recycled PID, or a duplicate
finalizer cannot overwrite a newer fact.  Leases make a stopped worker visible
without guessing from process names; the classifier is deliberately read-only
so reconciliation remains an explicit, reviewable operation.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

EXECUTIONS_FILE = get_hermes_home().resolve() / "cron" / "executions.db"
MAX_TERMINAL_EXECUTIONS = 1000
DEFAULT_LEASE_SECONDS = 120
SCHEMA_VERSION = 2
HISTORICAL_RECONCILIATION_SCHEMA = "hermes-cron-historical-reconciliation/v1"
HISTORICAL_RECONCILIATION_REASON = "legacy_unfenced_owner_dead_reconciled"
HISTORICAL_RECONCILIATION_EXECUTION_IDS = (
    "ffa80d66c55b40e3a85f454a83812ab8",
    "4cc3598fd5e3400aa6b317791fe51961",
    "9c5dc41e180a459397a6ff9896d5f6de",
    "5f140a86aafb499c92fdb073d0c50f63",
    "c8948bf7beb840eaab5a3224e75fb2d6",
    "1d0b4a2376a2495bb4f609d39ab7684a",
)
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_CREATE_EXECUTIONS_SQL = """CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    source TEXT NOT NULL,
    process_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_started_at INTEGER,
    owner_token TEXT,
    status TEXT NOT NULL CHECK(status IN
      ('claimed','running','completed','failed','interrupted','unknown')),
    claimed_at TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT,
    lease_expires_at TEXT,
    finished_at TEXT,
    terminal_at TEXT,
    terminal_reason TEXT,
    error TEXT
)"""


def _connect() -> sqlite3.Connection:
    EXECUTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(EXECUTIONS_FILE, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    _migrate_unlocked(conn)
    return conn


def _migrate_unlocked(conn: sqlite3.Connection) -> None:
    """Migrate legacy ledgers without dropping their execution history."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='executions'"
    ).fetchone()
    if not exists:
        conn.execute(_CREATE_EXECUTIONS_SQL)
    else:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(executions)")}
        table_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='executions'"
        ).fetchone()
        table_sql = (table_sql_row["sql"] or "").lower() if table_sql_row else ""
        needs_rebuild = "interrupted" not in table_sql
        if needs_rebuild:
            # SQLite cannot add a CHECK value in place.  Copy every historical
            # row verbatim into the expanded schema inside one transaction.
            conn.execute("ALTER TABLE executions RENAME TO executions_legacy")
            conn.execute(_CREATE_EXECUTIONS_SQL)
            legacy_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(executions_legacy)")
            }
            shared = [
                name for name in (
                    "id", "job_id", "source", "process_id", "pid", "process_started_at",
                    "status", "claimed_at", "started_at", "finished_at", "error",
                ) if name in legacy_columns
            ]
            conn.execute(
                "INSERT INTO executions (" + ", ".join(shared) + ") "
                "SELECT " + ", ".join(shared) + " FROM executions_legacy"
            )
            conn.execute(
                "UPDATE executions SET terminal_at=finished_at, "
                "terminal_reason=CASE status "
                "WHEN 'completed' THEN 'legacy_completed' "
                "WHEN 'failed' THEN 'legacy_failed' "
                "WHEN 'unknown' THEN 'legacy_unknown' END "
                "WHERE status IN ('completed','failed','unknown')"
            )
            conn.execute("DROP TABLE executions_legacy")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(executions)")}
        for name, definition in (
            ("owner_token", "TEXT"),
            ("heartbeat_at", "TEXT"),
            ("lease_expires_at", "TEXT"),
            ("terminal_at", "TEXT"),
            ("terminal_reason", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE executions ADD COLUMN {name} {definition}")
        conn.execute(
            "UPDATE executions SET terminal_at=COALESCE(terminal_at, finished_at), "
            "terminal_reason=COALESCE(terminal_reason, CASE status "
            "WHEN 'completed' THEN 'legacy_completed' "
            "WHEN 'failed' THEN 'legacy_failed' "
            "WHEN 'unknown' THEN 'legacy_unknown' END) "
            "WHERE status IN ('completed','failed','unknown') "
            "AND (terminal_at IS NULL OR terminal_reason IS NULL)"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)


def _record(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _timestamps(lease_seconds: int) -> tuple[str, str]:
    current = _hermes_now()
    return current.isoformat(), (current + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_liveness(pid: int, started_at: Optional[int]) -> str:
    """Return live/dead/unknown without treating a PID number as identity."""
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return "dead"
    except Exception:
        return "unknown"
    if started_at is None:
        return "live" if pid == os.getpid() else "unknown"
    current = _process_start_time(pid)
    if current is None:
        return "unknown"
    return "live" if current == started_at else "dead"


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    limit = max(0, int(MAX_TERMINAL_EXECUTIONS))
    placeholders = ",".join(
        "?" for _ in HISTORICAL_RECONCILIATION_EXECUTION_IDS
    )
    conn.execute(
        f"""DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','interrupted','unknown')
               AND NOT (
                 terminal_reason IS ?
                 AND id IN ({placeholders})
               )
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (
            HISTORICAL_RECONCILIATION_REASON,
            *HISTORICAL_RECONCILIATION_EXECUTION_IDS,
            limit,
        ),
    )


def create_execution(
    job_id: str, *, source: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Dict[str, Any]:
    """Persist an owned, leased attempt before executor/provider dispatch."""
    claimed_at, lease_expires_at = _timestamps(lease_seconds)
    execution_id = uuid.uuid4().hex
    owner_token = uuid.uuid4().hex
    pid = os.getpid()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at, owner_token,
                status, claimed_at, heartbeat_at, lease_expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             _process_start_time(pid), owner_token, claimed_at, claimed_at, lease_expires_at),
        )
        row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return _record(row)  # type: ignore[return-value]


def mark_execution_running(execution_id: str, *, owner_token: str) -> Optional[Dict[str, Any]]:
    """Fence the claimed→running transition to the execution owner."""
    now, lease_expires_at = _timestamps(DEFAULT_LEASE_SECONDS)
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE executions SET status='running', started_at=?, heartbeat_at=?, lease_expires_at=?
               WHERE id=? AND owner_token=? AND status='claimed' AND terminal_at IS NULL""",
            (now, now, lease_expires_at, execution_id, owner_token),
        )
        if cur.rowcount != 1:
            return None
        return _record(conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone())


def heartbeat_execution(
    execution_id: str, *, owner_token: str, lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend an in-flight lease; stale and duplicate owners are fenced out."""
    now, lease_expires_at = _timestamps(lease_seconds)
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE executions SET heartbeat_at=?, lease_expires_at=?
               WHERE id=? AND owner_token=? AND status IN ('claimed','running')
                 AND terminal_at IS NULL""",
            (now, lease_expires_at, execution_id, owner_token),
        )
    return cur.rowcount == 1


def finish_execution(
    execution_id: str, *, owner_token: str, success: bool, error: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically finalize once, returning the existing terminal fact on retry."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    terminal_reason = reason or ("completed" if success else "failed")
    with _lock, _connect() as conn:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, terminal_at=?, terminal_reason=?, error=?,
                   lease_expires_at=?
               WHERE id=? AND owner_token=? AND status IN ('claimed','running')
                 AND terminal_at IS NULL""",
            (status, now, now, terminal_reason, detail, now, execution_id, owner_token),
        )
        if cur.rowcount == 1:
            _prune_unlocked(conn)
            return _record(conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone())
        # Same-owner retries are idempotent.  A different token is deliberately
        # indistinguishable from a missing record to avoid exposing ownership.
        row = conn.execute(
            "SELECT * FROM executions WHERE id=? AND owner_token=? AND terminal_at IS NOT NULL",
            (execution_id, owner_token),
        ).fetchone()
        return _record(row)


def _classify_row(row: sqlite3.Row, *, now: str) -> Dict[str, Any]:
    lease_expires_at = row["lease_expires_at"]
    lease_state = "missing" if not lease_expires_at else ("expired" if lease_expires_at <= now else "current")
    liveness = _owner_liveness(int(row["pid"]), row["process_started_at"])
    has_modern_fence = bool(row["owner_token"] and lease_expires_at)
    # Exact PID start-time mismatch/death is decisive immediately; an expired
    # lease is the additional fence when liveness is otherwise ambiguous.  We
    # never infer death from a process name or from expiry alone.
    proven_dead = has_modern_fence and liveness == "dead"
    if proven_dead:
        disposition = "stale"
        proposed_status = "interrupted"
        proposed_reason = (
            "owner_dead" if lease_state != "expired" else "lease_expired_owner_dead"
        )
    elif not has_modern_fence:
        disposition = "legacy_unfenced"
        proposed_status = None
        proposed_reason = "missing_owner_token_or_lease"
    elif liveness == "unknown":
        disposition = "insufficient_evidence"
        proposed_status = None
        proposed_reason = "owner_liveness_unproven"
    else:
        disposition = "live_or_unexpired"
        proposed_status = None
        proposed_reason = None
    return {
        "execution_id": row["id"], "job_id": row["job_id"], "status": row["status"],
        "claimed_at": row["claimed_at"], "owner": {
            "process_id": row["process_id"], "pid": row["pid"],
            "process_started_at": row["process_started_at"],
            "token_present": bool(row["owner_token"]),
        },
        "lease": {"heartbeat_at": row["heartbeat_at"], "expires_at": lease_expires_at, "state": lease_state},
        "owner_liveness": liveness, "disposition": disposition,
        "proposed_terminal_status": proposed_status, "proposed_terminal_reason": proposed_reason,
    }


def classify_stale_executions() -> Dict[str, Any]:
    """Return a non-mutating, reviewable manifest of every in-flight record."""
    generated_at = _hermes_now().isoformat()
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM executions WHERE status IN ('claimed','running') ORDER BY claimed_at, id"
        ).fetchall()
    entries = [_classify_row(row, now=generated_at) for row in rows]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "mutated": False,
        "entries": entries,
        "summary": {
            "inflight": len(entries),
            "stale": sum(entry["disposition"] == "stale" for entry in entries),
            "legacy_unfenced": sum(entry["disposition"] == "legacy_unfenced" for entry in entries),
            "insufficient_evidence": sum(entry["disposition"] == "insufficient_evidence" for entry in entries),
        },
    }


class HistoricalReconciliationError(ValueError):
    """The reviewed historical-reconciliation contract no longer holds."""


def _historical_manifest_hash(manifest: Dict[str, Any]) -> str:
    content = dict(manifest)
    content.pop("content_hash", None)
    canonical = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _historical_pid_evidence(row: sqlite3.Row) -> Dict[str, Any]:
    """Capture exact PID presence without treating PID reuse as owner death."""
    pid = int(row["pid"])
    try:
        from gateway.status import _pid_exists
        present = bool(_pid_exists(pid))
    except Exception:
        return {
            "pid": pid,
            "state": "unknown",
            "recorded_process_started_at": row["process_started_at"],
            "observed_process_started_at": None,
        }
    if not present:
        return {
            "pid": pid,
            "state": "absent",
            "recorded_process_started_at": row["process_started_at"],
            "observed_process_started_at": None,
        }
    observed_started_at = _process_start_time(pid)
    recorded_started_at = row["process_started_at"]
    if (
        recorded_started_at is not None
        and observed_started_at is not None
        and observed_started_at != recorded_started_at
    ):
        state = "pid_reused"
    elif (
        recorded_started_at is not None
        and observed_started_at is not None
        and observed_started_at == recorded_started_at
    ):
        state = "live_owner"
    else:
        state = "present_identity_unknown"
    return {
        "pid": pid,
        "state": state,
        "recorded_process_started_at": recorded_started_at,
        "observed_process_started_at": observed_started_at,
    }


def _historical_row_eligible(row: sqlite3.Row, evidence: Dict[str, Any]) -> bool:
    return (
        row["status"] == "running"
        and row["finished_at"] is None
        and row["terminal_at"] is None
        and row["owner_token"] is None
        and row["lease_expires_at"] is None
        and evidence["state"] == "absent"
    )


def build_historical_reconciliation_manifest(
    *,
    database_snapshot_sha256: str,
    runtime_release: str,
    runtime_commit: str,
) -> Dict[str, Any]:
    """Build a non-mutating manifest for the one approved legacy repair.

    The fixed allow-list is intentionally not caller-configurable.  The
    database snapshot hash and runtime identity make the review artifact
    attributable to the exact operational state that will later be applied.
    """
    snapshot_hash = str(database_snapshot_sha256).strip().lower()
    if not _SHA256_RE.fullmatch(snapshot_hash):
        raise HistoricalReconciliationError(
            "database_snapshot_sha256 must be a lowercase SHA-256 digest"
        )
    release = str(runtime_release).strip()
    commit = str(runtime_commit).strip()
    if not release or not commit:
        raise HistoricalReconciliationError(
            "runtime_release and runtime_commit are required"
        )

    generated_at = _hermes_now().isoformat()
    placeholders = ",".join("?" for _ in HISTORICAL_RECONCILIATION_EXECUTION_IDS)
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM executions WHERE id IN ({placeholders})",
            HISTORICAL_RECONCILIATION_EXECUTION_IDS,
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    entries = []
    for execution_id in HISTORICAL_RECONCILIATION_EXECUTION_IDS:
        row = by_id.get(execution_id)
        if row is None:
            entries.append({
                "execution_id": execution_id,
                "eligible": False,
                "rejection": "missing",
                "owner_pid_evidence": None,
                "original": None,
            })
            continue
        evidence = _historical_pid_evidence(row)
        eligible = _historical_row_eligible(row, evidence)
        entries.append({
            "execution_id": execution_id,
            "eligible": eligible,
            "rejection": None if eligible else "historical_preconditions_not_met",
            "owner_pid_evidence": evidence,
            "original": dict(row),
        })

    manifest = {
        "schema": HISTORICAL_RECONCILIATION_SCHEMA,
        "generated_at": generated_at,
        "mutated": False,
        "database_snapshot": {"sha256": snapshot_hash},
        "runtime": {"release": release, "commit": commit},
        "approved_execution_ids": list(HISTORICAL_RECONCILIATION_EXECUTION_IDS),
        "entries": entries,
        "summary": {
            "approved": len(HISTORICAL_RECONCILIATION_EXECUTION_IDS),
            "eligible": sum(bool(entry["eligible"]) for entry in entries),
            "rejected": sum(not entry["eligible"] for entry in entries),
        },
    }
    manifest["content_hash"] = _historical_manifest_hash(manifest)
    return manifest


def _validate_historical_manifest(
    manifest: Dict[str, Any],
    *,
    manifest_hash: str,
    database_snapshot_sha256: str,
    runtime_release: str,
    runtime_commit: str,
) -> None:
    expected_ids = list(HISTORICAL_RECONCILIATION_EXECUTION_IDS)
    supplied_hash = str(manifest_hash).strip().lower()
    if not _SHA256_RE.fullmatch(supplied_hash):
        raise HistoricalReconciliationError("manifest_hash must be a SHA-256 digest")
    if manifest.get("content_hash") != supplied_hash:
        raise HistoricalReconciliationError("manifest_hash does not match the manifest")
    if _historical_manifest_hash(manifest) != supplied_hash:
        raise HistoricalReconciliationError("manifest content hash verification failed")
    if manifest.get("schema") != HISTORICAL_RECONCILIATION_SCHEMA:
        raise HistoricalReconciliationError("unsupported historical manifest schema")
    if manifest.get("mutated") is not False:
        raise HistoricalReconciliationError("only a dry-run manifest may be applied")
    if manifest.get("approved_execution_ids") != expected_ids:
        raise HistoricalReconciliationError("manifest allow-list is not the fixed approved set")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or [
        entry.get("execution_id") for entry in entries if isinstance(entry, dict)
    ] != expected_ids:
        raise HistoricalReconciliationError("manifest entries do not match the fixed approved set")
    if not all(entry.get("eligible") is True for entry in entries):
        raise HistoricalReconciliationError("manifest contains an ineligible approved execution")

    snapshot_hash = str(database_snapshot_sha256).strip().lower()
    if manifest.get("database_snapshot") != {"sha256": snapshot_hash}:
        raise HistoricalReconciliationError("database snapshot hash does not match the manifest")
    if manifest.get("runtime") != {
        "release": str(runtime_release).strip(),
        "commit": str(runtime_commit).strip(),
    }:
        raise HistoricalReconciliationError("runtime identity does not match the manifest")


def apply_historical_execution_reconciliation(
    manifest: Dict[str, Any],
    *,
    manifest_hash: str,
    database_snapshot_sha256: str,
    runtime_release: str,
    runtime_commit: str,
) -> Dict[str, Any]:
    """Atomically interrupt the six approved legacy rows, or change nothing.

    This path is deliberately separate from startup recovery.  Apply requires
    the reviewed dry-run manifest, its content hash, the database snapshot
    digest, and the same observed runtime identity.  Every row is re-read in
    one immediate transaction and compared byte-for-byte with the manifest
    before any update occurs.
    """
    canonical_manifest_hash = str(manifest_hash).strip().lower()
    _validate_historical_manifest(
        manifest,
        manifest_hash=canonical_manifest_hash,
        database_snapshot_sha256=database_snapshot_sha256,
        runtime_release=runtime_release,
        runtime_commit=runtime_commit,
    )
    entries = manifest["entries"]
    originals = {entry["execution_id"]: entry["original"] for entry in entries}
    error = (
        "Historical legacy-unfenced execution reconciled from reviewed manifest "
        f"{canonical_manifest_hash}; the recorded owner PID was absent at apply time."
    )

    with _lock:
        conn = _connect()
        try:
            # _connect() may have performed an idempotent schema migration.
            # Commit that separately before the explicit repair transaction.
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in HISTORICAL_RECONCILIATION_EXECUTION_IDS)
            rows = conn.execute(
                f"SELECT * FROM executions WHERE id IN ({placeholders})",
                HISTORICAL_RECONCILIATION_EXECUTION_IDS,
            ).fetchall()
            by_id = {row["id"]: row for row in rows}

            already_reconciled = []
            pending = []
            for execution_id in HISTORICAL_RECONCILIATION_EXECUTION_IDS:
                row = by_id.get(execution_id)
                if row is None:
                    raise HistoricalReconciliationError(
                        f"approved execution disappeared: {execution_id}"
                    )
                if (
                    row["status"] == "interrupted"
                    and row["finished_at"] is not None
                    and row["terminal_at"] is not None
                    and row["finished_at"] == row["terminal_at"]
                    and row["terminal_reason"] == HISTORICAL_RECONCILIATION_REASON
                    and row["error"] == error
                ):
                    already_reconciled.append(row)
                    continue
                if dict(row) != originals[execution_id]:
                    raise HistoricalReconciliationError(
                        f"snapshot preconditions changed: {execution_id}"
                    )
                evidence = _historical_pid_evidence(row)
                if not _historical_row_eligible(row, evidence):
                    raise HistoricalReconciliationError(
                        f"historical preconditions no longer hold: {execution_id} "
                        f"(pid_state={evidence['state']})"
                    )
                pending.append(row)

            if already_reconciled and pending:
                raise HistoricalReconciliationError(
                    "partial historical reconciliation detected; refusing mixed-state apply"
                )
            if already_reconciled:
                conn.commit()
                return {
                    "manifest_hash": canonical_manifest_hash,
                    "mutated": 0,
                    "already_reconciled": len(already_reconciled),
                    "entries": [
                        {
                            "execution_id": row["id"],
                            "status": row["status"],
                            "finished_at": row["finished_at"],
                            "terminal_at": row["terminal_at"],
                            "terminal_reason": row["terminal_reason"],
                        }
                        for row in already_reconciled
                    ],
                }

            terminal_at = _hermes_now().isoformat()
            changed = 0
            for row in pending:
                cur = conn.execute(
                    """UPDATE executions
                       SET status='interrupted', finished_at=?, terminal_at=?,
                           terminal_reason=?, error=?
                       WHERE id=? AND status='running' AND finished_at IS NULL
                         AND terminal_at IS NULL AND owner_token IS NULL
                         AND lease_expires_at IS NULL AND pid=?
                         AND process_id IS ? AND process_started_at IS ?""",
                    (
                        terminal_at,
                        terminal_at,
                        HISTORICAL_RECONCILIATION_REASON,
                        error,
                        row["id"],
                        row["pid"],
                        row["process_id"],
                        row["process_started_at"],
                    ),
                )
                if cur.rowcount != 1:
                    raise HistoricalReconciliationError(
                        f"compare-and-set rejected execution: {row['id']}"
                    )
                changed += 1
            if changed != len(HISTORICAL_RECONCILIATION_EXECUTION_IDS):
                raise HistoricalReconciliationError(
                    "historical reconciliation did not update the complete approved set"
                )
            conn.commit()
            final_rows = conn.execute(
                f"SELECT * FROM executions WHERE id IN ({placeholders})",
                HISTORICAL_RECONCILIATION_EXECUTION_IDS,
            ).fetchall()
            final_by_id = {row["id"]: row for row in final_rows}
            return {
                "manifest_hash": canonical_manifest_hash,
                "mutated": changed,
                "already_reconciled": 0,
                "entries": [
                    {
                        "execution_id": execution_id,
                        "status": final_by_id[execution_id]["status"],
                        "finished_at": final_by_id[execution_id]["finished_at"],
                        "terminal_at": final_by_id[execution_id]["terminal_at"],
                        "terminal_reason": final_by_id[execution_id]["terminal_reason"],
                    }
                    for execution_id in HISTORICAL_RECONCILIATION_EXECUTION_IDS
                ],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def restore_historical_reconciliation_rows_from_snapshot(
    snapshot_path: str | Path,
    manifest: Dict[str, Any],
    *,
    manifest_hash: str,
    database_snapshot_sha256: str,
    runtime_release: str,
    runtime_commit: str,
) -> Dict[str, Any]:
    """Atomically restore the six approved originals from their reviewed backup.

    This is a bounded recovery path for an operational interruption between
    historical reconciliation and durable retention.  It accepts neither a
    caller-provided allow-list nor arbitrary row contents: the backup digest,
    reviewed manifest, fixed IDs, schema, and every original value must agree.
    Existing partial or drifted rows fail closed.  An exact all-six retry is a
    zero-mutation success so a crash after commit can be resumed safely.
    """
    canonical_manifest_hash = str(manifest_hash).strip().lower()
    snapshot_hash = str(database_snapshot_sha256).strip().lower()
    _validate_historical_manifest(
        manifest,
        manifest_hash=canonical_manifest_hash,
        database_snapshot_sha256=snapshot_hash,
        runtime_release=runtime_release,
        runtime_commit=runtime_commit,
    )

    snapshot = Path(snapshot_path).expanduser().resolve()
    if not snapshot.is_file():
        raise HistoricalReconciliationError("historical database snapshot is missing")
    digest = hashlib.sha256()
    try:
        with snapshot.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise HistoricalReconciliationError(
            "historical database snapshot could not be read"
        ) from exc
    if digest.hexdigest() != snapshot_hash:
        raise HistoricalReconciliationError(
            "historical database snapshot hash verification failed"
        )

    expected_ids = list(HISTORICAL_RECONCILIATION_EXECUTION_IDS)
    originals = {
        entry["execution_id"]: entry["original"] for entry in manifest["entries"]
    }
    placeholders = ",".join("?" for _ in expected_ids)

    def schema_identity(conn: sqlite3.Connection) -> Dict[str, Any]:
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='executions'"
        ).fetchone()
        table_sql = table_row["sql"] if table_row is not None else None
        return {
            "table_info": [
                tuple(row) for row in conn.execute("PRAGMA table_info(executions)")
            ],
            "table_sql": (
                " ".join(str(table_sql).split()) if table_sql is not None else None
            ),
            "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        }

    try:
        snapshot_conn = sqlite3.connect(
            f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=5,
        )
        snapshot_conn.row_factory = sqlite3.Row
        snapshot_schema = schema_identity(snapshot_conn)
        snapshot_columns = [
            row[1] for row in snapshot_schema["table_info"]
        ]
        snapshot_rows = snapshot_conn.execute(
            f"SELECT * FROM executions WHERE id IN ({placeholders})",
            expected_ids,
        ).fetchall()
    except sqlite3.Error as exc:
        raise HistoricalReconciliationError(
            "historical database snapshot is not a readable execution ledger"
        ) from exc
    finally:
        if "snapshot_conn" in locals():
            snapshot_conn.close()

    snapshot_by_id = {row["id"]: dict(row) for row in snapshot_rows}
    if (
        len(snapshot_by_id) != len(expected_ids)
        or set(snapshot_by_id) != set(expected_ids)
    ):
        raise HistoricalReconciliationError(
            "historical database snapshot does not contain the fixed approved set"
        )

    with _lock:
        conn = _connect()
        try:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            live_schema = schema_identity(conn)
            if live_schema != snapshot_schema:
                raise HistoricalReconciliationError(
                    "historical snapshot/live execution schemas differ"
                )
            for execution_id in expected_ids:
                if snapshot_by_id[execution_id] != originals[execution_id]:
                    raise HistoricalReconciliationError(
                        f"historical database snapshot content changed: {execution_id}"
                    )
            live_columns = [
                row[1] for row in live_schema["table_info"]
            ]
            live_rows = conn.execute(
                f"SELECT * FROM executions WHERE id IN ({placeholders})",
                expected_ids,
            ).fetchall()
            live_by_id = {row["id"]: dict(row) for row in live_rows}
            if live_by_id:
                if (
                    len(live_by_id) == len(expected_ids)
                    and all(
                        live_by_id[execution_id] == originals[execution_id]
                        for execution_id in expected_ids
                    )
                ):
                    conn.commit()
                    return {
                        "manifest_hash": canonical_manifest_hash,
                        "mutated": 0,
                        "already_restored": len(expected_ids),
                        "execution_ids": expected_ids,
                    }
                raise HistoricalReconciliationError(
                    "historical restore requires all six approved rows to be absent "
                    "or already restored exactly"
                )

            columns_sql = ", ".join(snapshot_columns)
            values_sql = ", ".join("?" for _ in snapshot_columns)
            changed = 0
            for execution_id in expected_ids:
                row = originals[execution_id]
                cur = conn.execute(
                    f"INSERT INTO executions ({columns_sql}) VALUES ({values_sql})",
                    tuple(row[column] for column in snapshot_columns),
                )
                if cur.rowcount != 1:
                    raise HistoricalReconciliationError(
                        f"historical restore rejected execution: {execution_id}"
                    )
                changed += 1
            if changed != len(expected_ids):
                raise HistoricalReconciliationError(
                    "historical restore did not insert the complete approved set"
                )
            conn.commit()
            return {
                "manifest_hash": canonical_manifest_hash,
                "mutated": changed,
                "already_restored": 0,
                "execution_ids": expected_ids,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def recover_interrupted_executions() -> int:
    """Finalize only post-migration records whose exact dead owner proves interruption.

    Legacy rows are intentionally left untouched and appear in the dry-run
    manifest for the explicit reconciliation task.
    """
    manifest = classify_stale_executions()
    changed = 0
    with _lock, _connect() as conn:
        for entry in manifest["entries"]:
            if entry["disposition"] != "stale":
                continue
            now = _hermes_now().isoformat()
            cur = conn.execute(
                """UPDATE executions SET status='interrupted', finished_at=?, terminal_at=?,
                   terminal_reason=?, error=?, lease_expires_at=?
                   WHERE id=? AND status IN ('claimed','running') AND terminal_at IS NULL
                     AND owner_token IS NOT NULL AND lease_expires_at IS NOT NULL""",
                (now, now, entry["proposed_terminal_reason"],
                 "Execution owner died before a durable terminal state.",
                 now, entry["execution_id"]),
            )
            changed += cur.rowcount
        if changed:
            _prune_unlocked(conn)
    return changed


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50,
    before_claimed_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: List[str] = []
    params: List[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        clauses.append("claimed_at < ?")
        params.append(str(before_claimed_at))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where + " ORDER BY claimed_at DESC, id DESC LIMIT ?", params
        ).fetchall()
    return [dict(row) for row in rows]


def latest_execution(job_id: str) -> Optional[Dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def latest_executions(job_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e WHERE e.job_id IN ({placeholders})
                AND e.id=(SELECT e2.id FROM executions e2 WHERE e2.job_id=e.job_id
                          ORDER BY e2.claimed_at DESC, e2.id DESC LIMIT 1)""", clean
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
