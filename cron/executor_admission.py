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
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home


EXECUTOR_JOB_IDS = frozenset({"62714b869845", "baa3251e033d"})
DEFAULT_LEASE_SECONDS = 120
_SCHEMA_VERSION = 1


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


def _database_path() -> Path:
    return get_hermes_home().resolve() / "state" / "executor-admission.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _connect() -> sqlite3.Connection:
    path = _database_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=0")
        conn.execute("PRAGMA synchronous=FULL")
        with _schema_lock:
            conn.executescript(
                """
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
                PRAGMA user_version = 1;
                """
            )
        return conn
    except (OSError, sqlite3.Error) as exc:
        raise ExecutorAdmissionError(
            f"executor admission database unavailable: {exc}"
        ) from exc


def is_executor_job(job: dict[str, Any]) -> bool:
    job_id = str(job.get("id") or "")
    if job_id in EXECUTOR_JOB_IDS:
        return True
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
        raise ExecutorAdmissionError(f"unrecognized executor job id: {job_id}")
    normalized_task = str(task_id or "__unclaimed__")
    requested_at = _iso(_now())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute("COMMIT")
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
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM executor_lease WHERE singleton=1"
        ).fetchone()
        if current is not None and current["state"] == "active":
            conn.execute("ROLLBACK")
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
        if pending is not None:
            conn.execute("DELETE FROM pending_wakes WHERE job_id=?", (job_id,))
        conn.execute("COMMIT")
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
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute("COMMIT")
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
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute("COMMIT")
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
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute("COMMIT")
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


def executor_drain_status() -> dict[str, Any]:
    """Return non-mutating cutover evidence; never reaps or kills an owner."""
    try:
        conn = _connect()
        try:
            lease = conn.execute(
                "SELECT * FROM executor_lease WHERE singleton=1"
            ).fetchone()
            pending = conn.execute(
                "SELECT job_id,task_id,reason,requested_at "
                "FROM pending_wakes ORDER BY requested_at"
            ).fetchall()
        finally:
            conn.close()
    except ExecutorAdmissionError as exc:
        return {
            "safe_to_cutover": False,
            "state": "unknown",
            "error": str(exc),
            "lease": None,
            "pending_wakes": [],
            "mutated": False,
        }
    now_iso = _iso(_now())
    lease_dict = dict(lease) if lease is not None else None
    if lease_dict is not None:
        lease_dict["expired"] = lease_dict["expires_at"] < now_iso
    return {
        "safe_to_cutover": lease is None,
        "state": "idle" if lease is None else str(lease["state"]),
        "error": None,
        "lease": lease_dict,
        "pending_wakes": [dict(row) for row in pending],
        "mutated": False,
    }
