"""Durable, fenced admission for the autonomous ClickUp executor.

Every executor start path converges on ``cron.scheduler.run_one_job``.  This
module gives that shared body one profile-local singleton lease, so scheduled,
manual, external-provider, primary, and secondary starts cannot overlap.

The gate does not execute an agent.  It records a pending wake here and marks
the cron job due; the gateway ticker then reaches the same admission point as
every other start source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home


# Production identities come from machine-setup/fleet-config/jobs.json.  Cron
# execution records contain only job_id (not the job name/prompt), so startup
# recovery needs this compact identity policy in core.  A regression test binds
# it to the fleet manifest so a future rebuild cannot silently drift.
PRODUCTION_EXECUTOR_JOBS = {
    "62714b869845": "clickup-executor",
    "dcab830aa41c": "content-lane-executor",
}
EXECUTOR_JOB_IDS = frozenset(PRODUCTION_EXECUTOR_JOBS)
RETIRED_EXECUTOR_JOB_IDS = frozenset({"baa3251e033d"})
DEFAULT_LEASE_SECONDS = 120
_SCHEMA_VERSION = 2
_SQLITE_BUSY_DEADLINE_SECONDS = 1.0
_SQLITE_BUSY_SLICE_SECONDS = 0.05
_SQLITE_BUSY_RETRY_SECONDS = 0.01


class ExecutorAdmissionError(RuntimeError):
    """Admission state could not be proved safe; callers must fail closed."""


@dataclass(frozen=True)
class ExecutorLease:
    task_id: str
    job_id: str
    owner_run_id: str
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    ledger_execution_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_schema_lock = threading.Lock()


def _is_sqlite_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        primary_code = code & 0xFF
        return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


def _retry_sqlite_busy(operation, *, action: str, deadline: float | None = None):
    """Retry only transient SQLite ownership contention within a hard bound."""
    if deadline is None:
        deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise sqlite3.OperationalError(
                    f"{action} remained busy past the bounded admission deadline"
                ) from exc
            time.sleep(min(_SQLITE_BUSY_RETRY_SECONDS, remaining))


def _begin_immediate(
    conn: sqlite3.Connection, *, deadline: float | None = None
) -> None:
    _retry_sqlite_busy(
        lambda: conn.execute("BEGIN IMMEDIATE"),
        action="executor admission write acquisition",
        deadline=deadline,
    )


def _commit(conn: sqlite3.Connection, *, deadline: float | None = None) -> None:
    # SQLite leaves the transaction active when COMMIT returns BUSY, so the
    # exact same transaction can be safely retried until readers drain.
    _retry_sqlite_busy(
        lambda: conn.execute("COMMIT"),
        action="executor admission commit",
        deadline=deadline,
    )


def _bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Apply idempotent migrations under one cross-process write fence."""

    def migrate() -> None:
        try:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS admission_state (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    last_fencing_token INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO admission_state(singleton, last_fencing_token)
                VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS executor_lease (
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

                CREATE TABLE IF NOT EXISTS pending_wakes (
                    job_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requested_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recovery_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    owner_run_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    ledger_execution_id TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recovered_at TEXT NOT NULL,
                    proof_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS executor_lease_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    owner_run_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL UNIQUE,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ledger_execution_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(
                        state IN ('active', 'finalized', 'released', 'recovered')
                    ),
                    terminal_status TEXT,
                    finalized_at TEXT,
                    released_at TEXT,
                    recovered_at TEXT,
                    recovery_receipt_id TEXT,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS executor_lease_history_recent
                ON executor_lease_history(acquired_at DESC);

                CREATE TRIGGER IF NOT EXISTS executor_lease_history_identity_immutable
                BEFORE UPDATE OF
                    task_id, job_id, owner_run_id, fencing_token,
                    acquired_at, ledger_execution_id
                ON executor_lease_history
                BEGIN
                    SELECT RAISE(ABORT, 'executor lease history identity is immutable');
                END;

                INSERT OR IGNORE INTO executor_lease_history(
                    task_id, job_id, owner_run_id, fencing_token, acquired_at,
                    heartbeat_at, expires_at, ledger_execution_id, state,
                    terminal_status, finalized_at
                )
                SELECT
                    task_id, job_id, owner_run_id, fencing_token, acquired_at,
                    heartbeat_at, expires_at, ledger_execution_id, state,
                    terminal_status, finalized_at
                FROM executor_lease;

                UPDATE admission_state
                SET last_fencing_token = MAX(
                    last_fencing_token,
                    COALESCE(
                        (SELECT MAX(fencing_token) FROM executor_lease_history),
                        0
                    )
                )
                WHERE singleton = 1;

                PRAGMA user_version = 2;
                COMMIT;
                """
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    _retry_sqlite_busy(migrate, action="executor admission schema bootstrap")


def _database_path() -> Path:
    return get_hermes_home().resolve() / "state" / "executor-admission.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _assert_owned_safe_path(path: Path, *, kind: str) -> None:
    try:
        stat = path.lstat()
    except OSError as exc:
        raise ExecutorAdmissionError(f"cannot inspect executor admission {kind}: {exc}") from exc
    if path.is_symlink():
        raise ExecutorAdmissionError(f"executor admission {kind} must not be a symlink: {path}")
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise ExecutorAdmissionError(
            "executor admission ownership cannot be verified because the "
            f"POSIX user identity API is unavailable: {path}"
        )
    try:
        current_uid = getuid()
    except Exception as exc:
        raise ExecutorAdmissionError(
            "executor admission ownership cannot be verified because the "
            f"current user ID is unavailable: {path}: {exc}"
        ) from exc
    if stat.st_uid != current_uid:
        raise ExecutorAdmissionError(
            f"executor admission {kind} is not owned by the current user: {path}"
        )
    if stat.st_mode & 0o022:
        raise ExecutorAdmissionError(
            f"executor admission {kind} is group/world-writable: {path}"
        )


def _connect() -> sqlite3.Connection:
    path = _database_path()
    conn: sqlite3.Connection | None = None
    try:
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not parent_existed:
            os.chmod(path.parent, 0o700)
        _assert_owned_safe_path(path.parent, kind="directory")
        existed = path.exists() or path.is_symlink()
        if existed:
            _assert_owned_safe_path(path, kind="database")
        conn = sqlite3.connect(
            path,
            timeout=_SQLITE_BUSY_SLICE_SECONDS,
            isolation_level=None,
        )
        if not existed:
            if path.is_symlink():
                raise ExecutorAdmissionError(
                    f"executor admission database became a symlink: {path}"
                )
            os.chmod(path, 0o600)
        _assert_owned_safe_path(path, kind="database")
        conn.row_factory = sqlite3.Row
        conn.execute(
            f"PRAGMA busy_timeout={int(_SQLITE_BUSY_SLICE_SECONDS * 1000)}"
        )
        conn.execute("PRAGMA synchronous=FULL")
        with _schema_lock:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise ExecutorAdmissionError(
                    "executor admission schema is newer than this runtime "
                    f"({version} > {_SCHEMA_VERSION})"
                )
            if version < _SCHEMA_VERSION:
                _bootstrap_schema(conn)
        return conn
    except ExecutorAdmissionError:
        if conn is not None:
            conn.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if conn is not None:
            conn.close()
        raise ExecutorAdmissionError(
            f"executor admission database unavailable: {exc}"
        ) from exc


def is_executor_job(job: dict[str, Any]) -> bool:
    job_id = str(job.get("id") or "")
    if job_id in EXECUTOR_JOB_IDS:
        return str(job.get("name") or "") == PRODUCTION_EXECUTOR_JOBS[job_id]
    return (
        str(job.get("skill") or "") == "clickup-queue-poller"
        and str(job.get("name") or "").startswith("clickup-executor")
    )


def request_executor_wake(
    *, job_id: str, task_id: Optional[str], reason: str
) -> bool:
    """Record a wake request and mark the executor job due.

    This never launches a process.  A database or jobs-store uncertainty
    rejects the request, leaving execution to the next native schedule.
    """
    if job_id not in EXECUTOR_JOB_IDS:
        if job_id in RETIRED_EXECUTOR_JOB_IDS:
            raise ExecutorAdmissionError(
                f"retired executor job id is not admissible: {job_id}"
            )
        raise ExecutorAdmissionError(f"unrecognized executor job id: {job_id}")
    # A wake persists pending state before making the cron job due. Treat it as
    # an LLM dispatch start and reject it before either write while this profile
    # is draining; the native schedule remains available after cancellation.
    from cron.fleet_drain import cron_job_admission

    decision = cron_job_admission({"id": job_id, "no_agent": False})
    if not decision.allowed:
        raise ExecutorAdmissionError(
            f"executor wake rejected by fleet drain: {decision.reason}"
        )
    normalized_task = str(task_id or "__unclaimed__")
    requested_at = _iso(_now())
    conn = _connect()
    try:
        transaction_deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=transaction_deadline)
        conn.execute(
            """
            INSERT INTO pending_wakes(job_id, task_id, reason, requested_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                task_id=excluded.task_id,
                reason=excluded.reason,
                requested_at=excluded.requested_at
            """,
            (job_id, normalized_task, str(reason), requested_at),
        )
        _commit(conn, deadline=transaction_deadline)
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(f"wake request rejected: {exc}") from exc
    finally:
        conn.close()

    try:
        from cron.jobs import request_job_run_now

        scheduled = request_job_run_now(job_id)
    except Exception as exc:
        raise ExecutorAdmissionError(
            f"wake request recorded but cron scheduling was not provable: {exc}"
        ) from exc
    if not scheduled:
        raise ExecutorAdmissionError(
            "wake request recorded but executor job is missing, disabled, paused, "
            "or already owned"
        )
    return True


def acquire_executor_lease(
    *,
    job_id: str,
    owner_run_id: Optional[str],
    ledger_execution_id: str,
    task_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[ExecutorLease]:
    """Acquire the singleton lease, or return ``None`` for a proven owner.

    Expiry alone never permits takeover: an expired active row is ownership
    uncertainty and therefore remains fenced until its exact owner finalizes
    or releases it.
    """
    if job_id not in EXECUTOR_JOB_IDS:
        if job_id in RETIRED_EXECUTOR_JOB_IDS:
            raise ExecutorAdmissionError(
                f"retired executor job id is not admissible: {job_id}"
            )
        raise ExecutorAdmissionError(f"unrecognized executor job id: {job_id}")
    run_id = str(owner_run_id or uuid.uuid4().hex)
    if not run_id or not ledger_execution_id:
        raise ExecutorAdmissionError(
            "owner_run_id and ledger_execution_id are required"
        )
    now = _now()
    now_iso = _iso(now)
    expires_iso = _iso(now + timedelta(seconds=max(1, int(lease_seconds))))
    conn = _connect()
    try:
        transaction_deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=transaction_deadline)
        current = conn.execute(
            "SELECT * FROM executor_lease WHERE singleton=1"
        ).fetchone()
        # Old callers and new generic scheduler starts share the same
        # executor domain during cutover.  Do not let either generation pass
        # the other; an expired generic owner is equally uncertain.
        try:
            generic = conn.execute(
                "SELECT expires_at FROM admission_leases WHERE state='active' AND admission_profile='root/executor'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            generic = None
        if generic is not None:
            conn.execute("ROLLBACK")
            if generic["expires_at"] < now_iso:
                raise ExecutorAdmissionError(
                    "generic executor lease is expired and its owner is uncertain; exact stale-owner recovery is required"
                )
            return None
        if current is not None and current["state"] == "active":
            conn.execute("ROLLBACK")
            if current["expires_at"] < now_iso:
                raise ExecutorAdmissionError(
                    "active executor lease is expired and its owner is uncertain; "
                    "exact stale-owner recovery is required"
                )
            return None
        if current is not None:
            conn.execute(
                "DELETE FROM executor_lease WHERE singleton=1 AND state='finalized'"
            )

        pending = conn.execute(
            "SELECT task_id FROM pending_wakes WHERE job_id=?", (job_id,)
        ).fetchone()
        resolved_task = str(
            task_id
            or (pending["task_id"] if pending is not None else "")
            or "__scheduled__"
        )
        token_row = conn.execute(
            "SELECT last_fencing_token FROM admission_state WHERE singleton=1"
        ).fetchone()
        if token_row is None:
            raise ExecutorAdmissionError("admission fencing counter is missing")
        fencing_token = int(token_row["last_fencing_token"]) + 1
        conn.execute(
            "UPDATE admission_state SET last_fencing_token=? WHERE singleton=1",
            (fencing_token,),
        )
        conn.execute(
            """
            INSERT INTO executor_lease(
                singleton, task_id, job_id, owner_run_id, fencing_token,
                acquired_at, heartbeat_at, expires_at, ledger_execution_id,
                state
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                resolved_task,
                job_id,
                run_id,
                fencing_token,
                now_iso,
                now_iso,
                expires_iso,
                str(ledger_execution_id),
            ),
        )
        conn.execute(
            """
            INSERT INTO executor_lease_history(
                task_id, job_id, owner_run_id, fencing_token, acquired_at,
                heartbeat_at, expires_at, ledger_execution_id, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                resolved_task,
                job_id,
                run_id,
                fencing_token,
                now_iso,
                now_iso,
                expires_iso,
                str(ledger_execution_id),
            ),
        )
        if pending is not None:
            conn.execute("DELETE FROM pending_wakes WHERE job_id=?", (job_id,))
        _commit(conn, deadline=transaction_deadline)
        return ExecutorLease(
            task_id=resolved_task,
            job_id=job_id,
            owner_run_id=run_id,
            fencing_token=fencing_token,
            acquired_at=now_iso,
            heartbeat_at=now_iso,
            expires_at=expires_iso,
            ledger_execution_id=str(ledger_execution_id),
        )
    except ExecutorAdmissionError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except (ValueError, TypeError, sqlite3.Error) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(f"executor lease acquisition failed: {exc}") from exc
    finally:
        conn.close()


def heartbeat_executor_lease(
    lease: ExecutorLease, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> ExecutorLease:
    now = _now()
    now_iso = _iso(now)
    expires_iso = _iso(now + timedelta(seconds=max(1, int(lease_seconds))))
    conn = _connect()
    try:
        transaction_deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=transaction_deadline)
        cur = conn.execute(
            """
            UPDATE executor_lease
            SET heartbeat_at=?, expires_at=?
            WHERE singleton=1 AND state='active'
              AND job_id=? AND owner_run_id=? AND fencing_token=?
              AND ledger_execution_id=? AND expires_at>=?
            """,
            (
                now_iso,
                expires_iso,
                lease.job_id,
                lease.owner_run_id,
                lease.fencing_token,
                lease.ledger_execution_id,
                now_iso,
            ),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(
                "executor lease heartbeat rejected by fencing CAS"
            )
        history_cur = conn.execute(
            """
            UPDATE executor_lease_history
            SET heartbeat_at=?, expires_at=?, revision=revision+1
            WHERE state='active' AND job_id=? AND owner_run_id=?
              AND fencing_token=? AND ledger_execution_id=?
            """,
            (
                now_iso,
                expires_iso,
                lease.job_id,
                lease.owner_run_id,
                lease.fencing_token,
                lease.ledger_execution_id,
            ),
        )
        if history_cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(
                "executor lease heartbeat history rejected by fencing CAS"
            )
        _commit(conn, deadline=transaction_deadline)
        return ExecutorLease(
            **{
                **lease.as_dict(),
                "heartbeat_at": now_iso,
                "expires_at": expires_iso,
            }
        )
    except ExecutorAdmissionError:
        raise
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(f"executor lease heartbeat failed: {exc}") from exc
    finally:
        conn.close()


def finalize_executor_lease(lease: ExecutorLease, *, status: str) -> None:
    if status not in {"completed", "failed", "interrupted"}:
        raise ExecutorAdmissionError(f"invalid executor terminal status: {status}")
    now_iso = _iso(_now())
    conn = _connect()
    try:
        transaction_deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=transaction_deadline)
        cur = conn.execute(
            """
            UPDATE executor_lease
            SET state='finalized', terminal_status=?, finalized_at=?
            WHERE singleton=1 AND state='active'
              AND job_id=? AND owner_run_id=? AND fencing_token=?
              AND ledger_execution_id=?
            """,
            (
                status,
                now_iso,
                lease.job_id,
                lease.owner_run_id,
                lease.fencing_token,
                lease.ledger_execution_id,
            ),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(
                "executor lease finalization rejected by fencing CAS"
            )
        history_cur = conn.execute(
            """
            UPDATE executor_lease_history
            SET state='finalized', terminal_status=?, finalized_at=?,
                revision=revision+1
            WHERE state='active' AND job_id=? AND owner_run_id=?
              AND fencing_token=? AND ledger_execution_id=?
            """,
            (
                status,
                now_iso,
                lease.job_id,
                lease.owner_run_id,
                lease.fencing_token,
                lease.ledger_execution_id,
            ),
        )
        if history_cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(
                "executor lease finalization history rejected by fencing CAS"
            )
        _commit(conn, deadline=transaction_deadline)
    except ExecutorAdmissionError:
        raise
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(f"executor lease finalization failed: {exc}") from exc
    finally:
        conn.close()


def release_executor_lease(lease: ExecutorLease) -> None:
    now_iso = _iso(_now())
    conn = _connect()
    try:
        transaction_deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=transaction_deadline)
        history_cur = conn.execute(
            """
            UPDATE executor_lease_history
            SET state='released',
                terminal_status=COALESCE(terminal_status, 'interrupted'),
                finalized_at=COALESCE(finalized_at, ?),
                released_at=?, revision=revision+1
            WHERE state IN ('active', 'finalized', 'recovered')
              AND job_id=? AND owner_run_id=? AND fencing_token=?
              AND ledger_execution_id=?
            """,
            (
                now_iso,
                now_iso,
                lease.job_id,
                lease.owner_run_id,
                lease.fencing_token,
                lease.ledger_execution_id,
            ),
        )
        if history_cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(
                "executor lease release rejected by history fencing CAS"
            )
        cur = conn.execute(
            """
            DELETE FROM executor_lease
            WHERE singleton=1 AND job_id=? AND owner_run_id=?
              AND fencing_token=? AND ledger_execution_id=?
            """,
            (
                lease.job_id,
                lease.owner_run_id,
                lease.fencing_token,
                lease.ledger_execution_id,
            ),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(
                "executor lease release rejected by fencing CAS"
            )
        _commit(conn, deadline=transaction_deadline)
    except ExecutorAdmissionError:
        raise
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(f"executor lease release failed: {exc}") from exc
    finally:
        conn.close()


def _reviewed_dead_owner_proof(lease: sqlite3.Row) -> dict[str, Any]:
    """Return exact durable-ledger proof that this lease's owner is dead."""
    try:
        from cron.executions import classify_stale_executions

        manifest = classify_stale_executions()
    except Exception as exc:
        raise ExecutorAdmissionError(
            f"cannot classify executor ledger owner: {exc}"
        ) from exc
    matches = [
        entry
        for entry in manifest.get("entries", [])
        if entry.get("execution_id") == lease["ledger_execution_id"]
        and str(entry.get("job_id")) == str(lease["job_id"])
    ]
    if len(matches) != 1:
        raise ExecutorAdmissionError(
            "expired executor lease lacks one exact ledger owner proof"
        )
    proof = matches[0]
    if (
        proof.get("disposition") != "stale"
        or proof.get("owner_liveness") != "dead"
        or proof.get("proposed_terminal_status") != "interrupted"
    ):
        raise ExecutorAdmissionError(
            "expired executor lease owner is not proven dead by PID/start-time evidence"
        )
    return proof


def _validate_dead_owner_proof(
    lease: sqlite3.Row,
    proof: dict[str, Any],
) -> None:
    """Bind reviewed execution proof to one exact admission owner."""
    if (
        proof.get("execution_id") != lease["ledger_execution_id"]
        or str(proof.get("job_id")) != str(lease["job_id"])
        or proof.get("disposition") != "stale"
        or proof.get("owner_liveness") != "dead"
        or proof.get("proposed_terminal_status") != "interrupted"
    ):
        raise ExecutorAdmissionError(
            "expired executor lease owner is not proven dead by exact "
            "execution/PID/start-time evidence"
        )
    if proof.get("proposed_terminal_reason") not in {
        "owner_dead",
        "lease_expired_owner_dead",
    }:
        raise ExecutorAdmissionError(
            "executor recovery requires an exact dead-owner terminal reason"
        )


def _recover_expired_executor_lease_with_proof(
    conn: sqlite3.Connection,
    current: sqlite3.Row,
    *,
    proof: dict[str, Any],
    reviewed_by: str,
    reason: str,
    now_iso: str,
) -> dict[str, Any]:
    """Persist one exact recovery receipt and fence the matching lease."""
    owner_run_id = str(current["owner_run_id"])
    fencing_token = int(current["fencing_token"])
    ledger_execution_id = str(current["ledger_execution_id"])
    receipt_body = {
        "task_id": current["task_id"],
        "job_id": current["job_id"],
        "owner_run_id": owner_run_id,
        "fencing_token": fencing_token,
        "ledger_execution_id": ledger_execution_id,
        "reviewed_by": reviewed_by,
        "reason": reason,
        "recovered_at": now_iso,
        "proof": proof,
    }
    receipt_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        json.dumps(receipt_body, sort_keys=True, separators=(",", ":")),
    ).hex
    transaction_deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
    _begin_immediate(conn, deadline=transaction_deadline)
    cur = conn.execute(
        """
        UPDATE executor_lease
        SET state='finalized', terminal_status='interrupted', finalized_at=?
        WHERE singleton=1 AND state='active' AND expires_at<?
          AND job_id=? AND owner_run_id=? AND fencing_token=?
          AND ledger_execution_id=?
        """,
        (
            now_iso,
            now_iso,
            current["job_id"],
            owner_run_id,
            fencing_token,
            ledger_execution_id,
        ),
    )
    if cur.rowcount != 1:
        conn.execute("ROLLBACK")
        raise ExecutorAdmissionError(
            "expired executor recovery lost its fencing CAS"
        )
    conn.execute(
        """
        INSERT INTO recovery_receipts(
            receipt_id,task_id,job_id,owner_run_id,fencing_token,
            ledger_execution_id,reviewed_by,reason,recovered_at,proof_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            receipt_id,
            current["task_id"],
            current["job_id"],
            owner_run_id,
            fencing_token,
            ledger_execution_id,
            reviewed_by,
            reason,
            now_iso,
            json.dumps(proof, sort_keys=True, separators=(",", ":")),
        ),
    )
    history_cur = conn.execute(
        """
        UPDATE executor_lease_history
        SET state='recovered', terminal_status='interrupted',
            finalized_at=?, recovered_at=?, recovery_receipt_id=?,
            revision=revision+1
        WHERE state='active' AND job_id=? AND owner_run_id=?
          AND fencing_token=? AND ledger_execution_id=?
        """,
        (
            now_iso,
            now_iso,
            receipt_id,
            current["job_id"],
            owner_run_id,
            fencing_token,
            ledger_execution_id,
        ),
    )
    if history_cur.rowcount != 1:
        conn.execute("ROLLBACK")
        raise ExecutorAdmissionError(
            "expired executor recovery history lost its fencing CAS"
        )
    _commit(conn, deadline=transaction_deadline)
    return {**receipt_body, "receipt_id": receipt_id}


def recover_executor_lease_before_execution_reap(
    proof: dict[str, Any],
) -> bool:
    """Finalize the matching singleton before startup consumes its proof.

    The execution reaper has already classified the durable PID/start-time
    identity.  A matching admission lease must also expire before either row
    is finalized.  A non-matching singleton is unrelated and remains fenced.
    """
    if recover_generic_admission_lease_before_execution_reap(proof):
        return True
    if proof.get("proposed_terminal_reason") not in {
        "owner_dead",
        "lease_expired_owner_dead",
    }:
        return False
    database = _database_path()
    if not database.exists() and not database.is_symlink():
        return False

    now_iso = _iso(_now())
    conn = _connect()
    try:
        current = conn.execute(
            """
            SELECT * FROM executor_lease
            WHERE singleton=1 AND state='active'
              AND job_id=? AND ledger_execution_id=?
            """,
            (str(proof.get("job_id")), str(proof.get("execution_id"))),
        ).fetchone()
        if current is None:
            return False
        if current["expires_at"] >= now_iso:
            raise ExecutorAdmissionError(
                "matching executor lease has not expired; preserving execution proof"
            )
        _validate_dead_owner_proof(current, proof)
        _recover_expired_executor_lease_with_proof(
            conn,
            current,
            proof=proof,
            reviewed_by="cron-startup-reaper",
            reason=(
                "startup execution recovery observed exact dead-owner proof: "
                f"{proof['proposed_terminal_reason']}"
            ),
            now_iso=now_iso,
        )
        return True
    except ExecutorAdmissionError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except (ValueError, TypeError, sqlite3.Error) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(
            f"startup executor recovery failed: {exc}"
        ) from exc
    finally:
        conn.close()


def recover_expired_executor_lease(
    *,
    owner_run_id: str,
    fencing_token: int,
    ledger_execution_id: str,
    reviewed_by: str,
    reason: str,
) -> dict[str, Any]:
    """Finalize one expired owner only with exact CAS and reviewed death proof.

    Expiry alone is never sufficient. The execution ledger must independently
    prove the recorded PID/start-time identity dead, and the caller must bind
    the reviewed action to the exact owner, token, and ledger execution.
    """
    reviewer = str(reviewed_by or "").strip()
    rationale = str(reason or "").strip()
    if not reviewer or not rationale:
        raise ExecutorAdmissionError("reviewed_by and reason are required")
    now_iso = _iso(_now())
    conn = _connect()
    try:
        current = conn.execute(
            "SELECT * FROM executor_lease WHERE singleton=1"
        ).fetchone()
        if (
            current is None
            or current["state"] != "active"
            or current["expires_at"] >= now_iso
            or current["owner_run_id"] != owner_run_id
            or int(current["fencing_token"]) != int(fencing_token)
            or current["ledger_execution_id"] != ledger_execution_id
        ):
            raise ExecutorAdmissionError(
                "expired executor recovery rejected by exact fencing CAS"
            )
        proof = _reviewed_dead_owner_proof(current)
        _validate_dead_owner_proof(current, proof)
        return _recover_expired_executor_lease_with_proof(
            conn,
            current,
            proof=proof,
            reviewed_by=reviewer,
            reason=rationale,
            now_iso=now_iso,
        )
    except ExecutorAdmissionError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    except (ValueError, TypeError, sqlite3.Error) as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise ExecutorAdmissionError(
            f"expired executor recovery failed: {exc}"
        ) from exc
    finally:
        conn.close()


def executor_drain_status(*, database_path: Path | None = None) -> dict[str, Any]:
    """Return truthful cutover evidence for legacy and generic leases.

    The no-argument compatibility form uses the profile-local admission
    connection, including its established path validation and schema bootstrap.
    An explicitly supplied path is the incident-reporting form: it opens only
    that profile's existing store read-only and never creates or migrates it.
    Neither form reaps or recovers an owner.
    """
    empty = {
        "safe_to_cutover": True,
        "state": "idle",
        "error": None,
        "lease": None,                 # backward-compatible legacy key
        "pending_wakes": [],           # backward-compatible key
        "generic_leases": [],
        "generic_recovery_receipts": [],
        "mutated": False,
    }
    explicit_database = database_path is not None
    database = database_path if explicit_database else _database_path()
    if explicit_database and not database.exists() and not database.is_symlink():
        return empty
    try:
        if explicit_database:
            conn = sqlite3.connect(
                f"file:{database}?mode=ro", uri=True,
                timeout=_SQLITE_BUSY_SLICE_SECONDS,
            )
        else:
            conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            quick_check = conn.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"executor admission quick_check failed: {quick_check[0] if quick_check else 'no result'}"
                )
            lease = (
                conn.execute("SELECT * FROM executor_lease WHERE singleton=1").fetchone()
                if "executor_lease" in tables else None
            )
            pending = (
                conn.execute(
                    "SELECT job_id,task_id,reason,requested_at FROM pending_wakes ORDER BY requested_at"
                ).fetchall() if "pending_wakes" in tables else []
            )
            generic = (
                conn.execute(
                    "SELECT * FROM admission_leases WHERE state='active' ORDER BY fencing_token"
                ).fetchall() if "admission_leases" in tables else []
            )
            receipts = (
                conn.execute(
                    "SELECT * FROM admission_recovery_receipts ORDER BY recovered_at DESC LIMIT 20"
                ).fetchall() if "admission_recovery_receipts" in tables else []
            )
        finally:
            conn.close()
    except (ExecutorAdmissionError, sqlite3.Error) as exc:
        return {
            **empty,
            "safe_to_cutover": False,
            "state": "unknown",
            "error": str(exc),
        }
    now_iso = _iso(_now())
    lease_dict = dict(lease) if lease is not None else None
    if lease_dict is not None:
        lease_dict["expired"] = lease_dict["expires_at"] < now_iso
    generic_dicts = []
    for row in generic:
        item = dict(row)
        item["expired"] = item["expires_at"] < now_iso
        try:
            item["mutable_resources"] = json.loads(item.pop("mutable_resources_json"))
        except (json.JSONDecodeError, TypeError):
            item["mutable_resources"] = None
        generic_dicts.append(item)
    occupied = lease is not None or bool(generic_dicts)
    state = (
        "active"
        if generic_dicts
        else ("idle" if lease is None else str(lease["state"]))
    )
    return {
        **empty,
        "safe_to_cutover": not occupied,
        "state": state,
        "lease": lease_dict,
        "pending_wakes": [dict(row) for row in pending],
        "generic_leases": generic_dicts,
        "generic_recovery_receipts": [dict(row) for row in receipts],
    }


# Generic fleet admission ---------------------------------------------------
#
# The original executor singleton above is deliberately retained as a
# compatibility and recovery surface for records written before the fleet was
# generalized.  New starts use these multi-lease tables instead.  Keeping the
# new tables separate avoids turning a schema migration into an implicit stale
# owner recovery: an old active singleton remains fenced and reviewable until
# the existing exact-proof recovery flow settles it.

ADMISSION_PROFILE_CAPACITY = 1
_ADMISSION_RESOURCE_TEMPLATE = "{task_id}"
_SENTINEL_TASK_IDS = frozenset({"", "__scheduled__", "__unclaimed__"})


@dataclass(frozen=True)
class AdmissionLease:
    """One fenced generic LLM admission lease.

    The first eight fields intentionally mirror :class:`ExecutorLease` so
    scheduler cleanup and older test doubles remain structurally compatible.
    """

    task_id: str
    job_id: str
    owner_run_id: str
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    ledger_execution_id: str
    admission_profile: str = "root/legacy"
    mutable_resources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def requires_llm_admission(job: dict[str, Any]) -> bool:
    """Whether ``job`` is an LLM dispatch which must fail closed on metadata."""
    return job.get("no_agent") is not True


def _validate_admission_metadata(
    job: dict[str, Any], *, task_id: Optional[str]
) -> tuple[str, tuple[str, ...]]:
    if not requires_llm_admission(job):
        return "", ()
    if bool(job.get("admission_retired", False)):
        raise ExecutorAdmissionError(
            f"retired LLM admission identity is not admissible: {job.get('id')}"
        )
    profile = job.get("admission_profile")
    if not isinstance(profile, str) or not profile.startswith("root/") or profile == "root/":
        raise ExecutorAdmissionError("LLM job has missing or malformed admission_profile")
    raw_resources = job.get("mutable_resources")
    if not isinstance(raw_resources, list) or not raw_resources:
        raise ExecutorAdmissionError("LLM job has missing or malformed mutable_resources")
    resolved_task = str(task_id or job.get("admission_task_id") or job.get("executor_task_id") or "__scheduled__")
    resources: list[str] = []
    for raw in raw_resources:
        if not isinstance(raw, str) or not raw or raw.strip() != raw:
            raise ExecutorAdmissionError("LLM job has malformed mutable_resources")
        if _ADMISSION_RESOURCE_TEMPLATE in raw:
            # A scheduled pass has not selected a ClickUp task yet.  It cannot
            # claim same-task exclusivity, but it still needs a canonical,
            # brace-free resource so profile/resource admission remains safe.
            raw = raw.replace(
                _ADMISSION_RESOURCE_TEMPLATE,
                resolved_task if resolved_task not in _SENTINEL_TASK_IDS else "*",
            )
        if "{" in raw or "}" in raw:
            raise ExecutorAdmissionError("LLM job has unsupported mutable resource template")
        resources.append(raw)
    if len(resources) != len(set(resources)):
        raise ExecutorAdmissionError("LLM job declares duplicate mutable resources")
    return profile, tuple(resources)


def _ensure_generic_schema(conn: sqlite3.Connection) -> None:
    """Create the additive generic admission tables without changing v2 state."""
    def create() -> None:
        try:
            conn.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS admission_leases (
                    fencing_token INTEGER PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    owner_run_id TEXT NOT NULL,
                    admission_profile TEXT NOT NULL,
                    mutable_resources_json TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ledger_execution_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('active', 'finalized', 'released', 'recovered')),
                    terminal_status TEXT,
                    finalized_at TEXT,
                    released_at TEXT
                );
                CREATE INDEX IF NOT EXISTS admission_leases_active_profile
                    ON admission_leases(state, admission_profile);
                CREATE INDEX IF NOT EXISTS admission_leases_active_task
                    ON admission_leases(state, task_id);
                CREATE TABLE IF NOT EXISTS admission_recovery_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    fencing_token INTEGER NOT NULL UNIQUE,
                    job_id TEXT NOT NULL,
                    owner_run_id TEXT NOT NULL,
                    ledger_execution_id TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recovered_at TEXT NOT NULL,
                    proof_json TEXT NOT NULL
                );
                COMMIT;
                """
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    _retry_sqlite_busy(create, action="generic admission schema bootstrap")


def _generic_connect() -> sqlite3.Connection:
    conn = _connect()
    try:
        _ensure_generic_schema(conn)
        return conn
    except BaseException:
        conn.close()
        raise


def _active_task_conflicts(task_id: str, rows: list[sqlite3.Row]) -> bool:
    return task_id not in _SENTINEL_TASK_IDS and any(row["task_id"] == task_id for row in rows)


def _resource_conflicts(left: tuple[str, ...], right: set[str]) -> bool:
    """Resource wildcards represent an unresolved task and fence its family."""
    for candidate in left:
        family = candidate.split("*", 1)[0]
        for active in right:
            if candidate == active or ("*" in candidate and active.startswith(family)):
                return True
            if "*" in active and candidate.startswith(active.split("*", 1)[0]):
                return True
    return False


def acquire_job_admission_lease(
    *,
    job: dict[str, Any],
    owner_run_id: Optional[str],
    ledger_execution_id: str,
    task_id: Optional[str] = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Optional[AdmissionLease]:
    """Acquire one generic admission lease or return ``None`` for contention.

    An expired owner is never removed based on expiry.  Acquisition gives the
    existing execution-ledger reaper one bounded opportunity to prove and
    recover exact dead owners, then retries under a fresh write transaction.
    """
    if not requires_llm_admission(job):
        return None
    job_id = str(job.get("id") or "")
    run_id = str(owner_run_id or uuid.uuid4().hex)
    if not job_id or not run_id or not ledger_execution_id:
        raise ExecutorAdmissionError("job_id, owner_run_id and ledger_execution_id are required")
    requested_task = str(task_id or job.get("admission_task_id") or job.get("executor_task_id") or "__scheduled__")
    conn = _generic_connect()
    recovery_attempted = False
    try:
        while True:
            deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
            _begin_immediate(conn, deadline=deadline)
            now = _now()
            now_iso = _iso(now)
            expires_iso = _iso(now + timedelta(seconds=max(1, int(lease_seconds))))
            pending = conn.execute(
                "SELECT task_id FROM pending_wakes WHERE job_id=?", (job_id,)
            ).fetchone()
            resolved_task = requested_task
            if resolved_task in _SENTINEL_TASK_IDS and pending is not None:
                resolved_task = str(pending["task_id"])
            profile, resources = _validate_admission_metadata(job, task_id=resolved_task)
            active = conn.execute("SELECT * FROM admission_leases WHERE state='active'").fetchall()
            expired_blocker: str | None = None
            if profile == "root/executor":
                legacy = conn.execute(
                    "SELECT expires_at FROM executor_lease WHERE singleton=1 AND state='active'"
                ).fetchone()
                if legacy is not None:
                    conn.execute("ROLLBACK")
                    if legacy["expires_at"] < now_iso:
                        expired_blocker = "legacy executor lease is expired and its owner is uncertain; exact stale-owner recovery is required"
                    else:
                        return None
            if expired_blocker is None:
                expired = [row for row in active if row["expires_at"] < now_iso]
                if expired:
                    conn.execute("ROLLBACK")
                    expired_blocker = "active admission lease is expired and its owner is uncertain; exact stale-owner recovery is required"
            if expired_blocker is not None:
                if recovery_attempted:
                    raise ExecutorAdmissionError(expired_blocker)
                recovery_attempted = True
                try:
                    from cron.executions import recover_interrupted_executions

                    recover_interrupted_executions()
                except ExecutorAdmissionError:
                    raise
                except Exception as exc:
                    raise ExecutorAdmissionError(
                        f"expired admission recovery failed: {exc}"
                    ) from exc
                continue
            active_resources = {
                resource
                for row in active
                for resource in json.loads(row["mutable_resources_json"])
            }
            if (
                sum(row["admission_profile"] == profile for row in active) >= ADMISSION_PROFILE_CAPACITY
                or _resource_conflicts(resources, active_resources)
                or _active_task_conflicts(resolved_task, active)
            ):
                conn.execute("ROLLBACK")
                return None
            token = int(conn.execute(
                "SELECT last_fencing_token FROM admission_state WHERE singleton=1"
            ).fetchone()[0]) + 1
            conn.execute("UPDATE admission_state SET last_fencing_token=? WHERE singleton=1", (token,))
            conn.execute(
                """INSERT INTO admission_leases(
                     fencing_token,task_id,job_id,owner_run_id,admission_profile,
                     mutable_resources_json,acquired_at,heartbeat_at,expires_at,
                     ledger_execution_id,state
                   ) VALUES (?,?,?,?,?,?,?,?,?,?, 'active')""",
                (token, resolved_task, job_id, run_id, profile, json.dumps(resources), now_iso,
                 now_iso, expires_iso, str(ledger_execution_id)),
            )
            if pending is not None:
                conn.execute("DELETE FROM pending_wakes WHERE job_id=?", (job_id,))
            _commit(conn, deadline=deadline)
            return AdmissionLease(resolved_task, job_id, run_id, token, now_iso, now_iso,
                                  expires_iso, str(ledger_execution_id), profile, resources)
    except ExecutorAdmissionError:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    except (TypeError, ValueError, sqlite3.Error) as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ExecutorAdmissionError(f"generic admission acquisition failed: {exc}") from exc
    finally:
        conn.close()


def _generic_cas(lease: AdmissionLease, *, action: str, assignments: str, values: tuple[Any, ...], states: tuple[str, ...]) -> None:
    conn = _generic_connect()
    try:
        deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=deadline)
        placeholders = ",".join("?" for _ in states)
        cur = conn.execute(
            f"UPDATE admission_leases SET {assignments} WHERE fencing_token=? AND job_id=? "
            f"AND owner_run_id=? AND ledger_execution_id=? AND state IN ({placeholders})",
            (*values, lease.fencing_token, lease.job_id, lease.owner_run_id,
             lease.ledger_execution_id, *states),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError(f"generic admission {action} rejected by fencing CAS")
        _commit(conn, deadline=deadline)
    except ExecutorAdmissionError:
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ExecutorAdmissionError(f"generic admission {action} failed: {exc}") from exc
    finally:
        conn.close()


def heartbeat_job_admission_lease(lease: AdmissionLease, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> AdmissionLease:
    now = _now()
    now_iso = _iso(now)
    expires_iso = _iso(now + timedelta(seconds=max(1, int(lease_seconds))))
    conn = _generic_connect()
    try:
        deadline = time.monotonic() + _SQLITE_BUSY_DEADLINE_SECONDS
        _begin_immediate(conn, deadline=deadline)
        cur = conn.execute(
            "UPDATE admission_leases SET heartbeat_at=?, expires_at=? WHERE fencing_token=? AND job_id=? AND owner_run_id=? AND ledger_execution_id=? AND state='active' AND expires_at>=?",
            (now_iso, expires_iso, lease.fencing_token, lease.job_id, lease.owner_run_id, lease.ledger_execution_id, now_iso),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError("generic admission heartbeat rejected by fencing CAS")
        _commit(conn, deadline=deadline)
    except ExecutorAdmissionError:
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ExecutorAdmissionError(f"generic admission heartbeat failed: {exc}") from exc
    finally:
        conn.close()
    return AdmissionLease(**{**lease.as_dict(), "heartbeat_at": now_iso, "expires_at": expires_iso})


def finalize_job_admission_lease(lease: AdmissionLease, *, status: str) -> None:
    if status not in {"completed", "failed", "interrupted"}:
        raise ExecutorAdmissionError(f"invalid admission terminal status: {status}")
    _generic_cas(lease, action="finalization", assignments="state='finalized', terminal_status=?, finalized_at=?", values=(status, _iso(_now())), states=("active",))


def release_job_admission_lease(lease: AdmissionLease) -> None:
    now_iso = _iso(_now())
    _generic_cas(lease, action="release", assignments="state='released', terminal_status=COALESCE(terminal_status, 'interrupted'), finalized_at=COALESCE(finalized_at, ?), released_at=?", values=(now_iso, now_iso), states=("active", "finalized", "recovered"))


def recover_generic_admission_lease_before_execution_reap(proof: dict[str, Any]) -> bool:
    """Fence one expired generic lease after exact ledger dead-owner proof."""
    if proof.get("proposed_terminal_reason") not in {"owner_dead", "lease_expired_owner_dead"}:
        return False
    database = _database_path()
    if not database.exists() and not database.is_symlink():
        return False
    conn = _generic_connect()
    try:
        now_iso = _iso(_now())
        _begin_immediate(conn)
        row = conn.execute(
            "SELECT * FROM admission_leases WHERE state='active' AND job_id=? AND ledger_execution_id=?",
            (str(proof.get("job_id")), str(proof.get("execution_id"))),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return False
        if row["expires_at"] >= now_iso:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError("matching generic admission lease has not expired; preserving execution proof")
        _validate_dead_owner_proof(row, proof)
        receipt_id = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps({"token": row["fencing_token"], "proof": proof}, sort_keys=True)).hex
        cur = conn.execute(
            "UPDATE admission_leases SET state='recovered', terminal_status='interrupted', finalized_at=? WHERE fencing_token=? AND state='active' AND expires_at<? AND job_id=? AND owner_run_id=? AND ledger_execution_id=?",
            (now_iso, row["fencing_token"], now_iso, row["job_id"], row["owner_run_id"], row["ledger_execution_id"]),
        )
        if cur.rowcount != 1:
            conn.execute("ROLLBACK")
            raise ExecutorAdmissionError("generic admission recovery lost its fencing CAS")
        conn.execute(
            "INSERT INTO admission_recovery_receipts VALUES (?,?,?,?,?,?,?,?,?)",
            (receipt_id, row["fencing_token"], row["job_id"], row["owner_run_id"], row["ledger_execution_id"], "cron-startup-reaper", "exact dead-owner proof", now_iso, json.dumps(proof, sort_keys=True)),
        )
        _commit(conn)
        return True
    except ExecutorAdmissionError:
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise ExecutorAdmissionError(f"generic admission recovery failed: {exc}") from exc
    finally:
        conn.close()
