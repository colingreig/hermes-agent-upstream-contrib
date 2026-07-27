"""Profile-local durable ledger for cron execution attempts.

An execution is owned by one unguessable token for its whole lifetime.  Every
mutation fences on that token, so an old worker, a recycled PID, or a duplicate
finalizer cannot overwrite a newer fact.  Leases make a stopped worker visible
without guessing from process names; the classifier is deliberately read-only
so reconciliation remains an explicit, reviewable operation.
"""

from __future__ import annotations

from datetime import timedelta
import os
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
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex

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
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','interrupted','unknown')
             ORDER BY claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (limit,),
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
