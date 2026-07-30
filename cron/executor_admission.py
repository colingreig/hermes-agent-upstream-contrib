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
        conn = sqlite3.connect(path, timeout=0, isolation_level=None)
        if not existed:
            if path.is_symlink():
                raise ExecutorAdmissionError(
                    f"executor admission database became a symlink: {path}"
                )
            os.chmod(path, 0o600)
        _assert_owned_safe_path(path, kind="database")
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
                """
            )
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
    now_iso = _iso(_now())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
    conn.execute("BEGIN IMMEDIATE")
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
    conn.execute("COMMIT")
    return {**receipt_body, "receipt_id": receipt_id}


def recover_executor_lease_before_execution_reap(
    proof: dict[str, Any],
) -> bool:
    """Finalize the matching singleton before startup consumes its proof.

    The execution reaper has already classified the durable PID/start-time
    identity.  A matching admission lease must also expire before either row
    is finalized.  A non-matching singleton is unrelated and remains fenced.
    """
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
