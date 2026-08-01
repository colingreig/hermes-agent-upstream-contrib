"""Fenced, registry-backed leases for governed production writers.

This is intentionally separate from :mod:`cron.executor_admission`: production
install/release serialization protects persistent machine state, whereas the
executor admission lease protects one logical agent-dispatch surface.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from hermes_constants import get_hermes_home

DEFAULT_LEASE_SECONDS = 120
_BUSY_SECONDS = 1.0
_SCHEMA_VERSION = 2
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_schema_lock = threading.Lock()


class ProductionWriteLeaseError(RuntimeError):
    """A production write cannot be proved exclusive; callers must stop."""


@dataclass(frozen=True)
class ProductionWriteLease:
    lease_id: str
    actor: str
    resources: tuple[str, ...]
    session_id: str
    workspace: str
    repo: str
    commit_sha: str
    reason: str
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    revision: int

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["resources"] = list(self.resources)
        return result


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _database_path() -> Path:
    return get_hermes_home().resolve() / "state" / "production-write-lease.db"


def _registry_path() -> Path:
    return Path(__file__).resolve().parents[1] / "machine-setup" / "production_mutation_registry.json"


def _safe_path(path: Path, *, kind: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ProductionWriteLeaseError(f"cannot inspect production write lease {kind}: {exc}") from exc
    if path.is_symlink():
        raise ProductionWriteLeaseError(f"production write lease {kind} must not be a symlink: {path}")
    if info.st_uid != os.getuid():
        raise ProductionWriteLeaseError(f"production write lease {kind} is not owned by the current user: {path}")
    if info.st_mode & 0o022:
        raise ProductionWriteLeaseError(f"production write lease {kind} is group/world-writable: {path}")


def _load_registry() -> dict[str, set[str]]:
    try:
        payload = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionWriteLeaseError(f"production mutation registry unavailable: {exc}") from exc
    if payload.get("registry_kind") != "hermes-production-mutation-registry":
        raise ProductionWriteLeaseError("production mutation registry has an unexpected kind")
    resource_ids = {row.get("id") for row in payload.get("resources", []) if isinstance(row, dict)}
    result: dict[str, set[str]] = {}
    for row in payload.get("actors", []):
        if not isinstance(row, dict) or row.get("mutability") != "mutating":
            continue
        actor = row.get("id")
        resources = row.get("resources_touched")
        if not isinstance(actor, str) or not _ID.fullmatch(actor) or not isinstance(resources, list):
            raise ProductionWriteLeaseError("production mutation registry has an invalid mutating actor")
        if not all(isinstance(value, str) and value in resource_ids for value in resources):
            raise ProductionWriteLeaseError(f"production mutation registry has invalid resources for {actor}")
        result[actor] = set(resources)
    return result


def _validate_request(*, resources: Iterable[str], actor: str, session_id: str, workspace: str, repo: str, commit_sha: str, reason: str) -> tuple[str, ...]:
    rows = tuple(sorted(set(resources)))
    if not rows or not all(isinstance(value, str) and _ID.fullmatch(value) for value in rows):
        raise ProductionWriteLeaseError("resources must be a non-empty, sorted-safe set of registry IDs")
    if not isinstance(actor, str) or not _ID.fullmatch(actor):
        raise ProductionWriteLeaseError("actor must be a registry ID")
    if not all(isinstance(value, str) and value.strip() for value in (session_id, workspace, repo, reason)):
        raise ProductionWriteLeaseError("session_id, workspace, repo, and reason must be non-empty")
    if not isinstance(commit_sha, str) or not _SHA.fullmatch(commit_sha):
        raise ProductionWriteLeaseError("commit_sha must be a full 40-character lowercase SHA")
    allowed = _load_registry().get(actor)
    if allowed is None:
        raise ProductionWriteLeaseError(f"actor is not a registered mutating writer: {actor}")
    if set(rows) != allowed:
        raise ProductionWriteLeaseError(f"resources for {actor} must exactly match its registry mapping: {sorted(allowed)}")
    return rows


def _connect(database_path: Path | None = None) -> sqlite3.Connection:
    path = database_path or _database_path()
    conn: sqlite3.Connection | None = None
    try:
        parent = path.parent
        existed_parent = parent.exists()
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if not existed_parent:
            os.chmod(parent, 0o700)
        _safe_path(parent, kind="directory")
        existed = path.exists() or path.is_symlink()
        if existed:
            _safe_path(path, kind="database")
        conn = sqlite3.connect(path, timeout=0.05, isolation_level=None)
        if not existed:
            if path.is_symlink():
                raise ProductionWriteLeaseError(f"production write lease database became a symlink: {path}")
            os.chmod(path, 0o600)
        _safe_path(path, kind="database")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=50")
        conn.execute("PRAGMA synchronous=FULL")
        with _schema_lock:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise ProductionWriteLeaseError(f"production write lease schema is newer than this runtime ({version} > {_SCHEMA_VERSION})")
            if version < _SCHEMA_VERSION:
                _bootstrap(conn)
        return conn
    except Exception as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, ProductionWriteLeaseError):
            raise
        raise ProductionWriteLeaseError(f"production write lease database unavailable: {exc}") from exc


def _bootstrap(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS lease_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), last_fencing_token INTEGER NOT NULL);
        INSERT OR IGNORE INTO lease_state(singleton,last_fencing_token) VALUES(1,0);
        CREATE TABLE IF NOT EXISTS active_leases (
          lease_id TEXT PRIMARY KEY, actor TEXT NOT NULL, resources_json TEXT NOT NULL,
          session_id TEXT NOT NULL, workspace TEXT NOT NULL, repo TEXT NOT NULL, commit_sha TEXT NOT NULL,
          reason TEXT NOT NULL, fencing_token INTEGER NOT NULL UNIQUE, acquired_at TEXT NOT NULL,
          heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL, revision INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lease_history (
          history_id INTEGER PRIMARY KEY AUTOINCREMENT, lease_id TEXT NOT NULL, actor TEXT NOT NULL,
          resources_json TEXT NOT NULL, session_id TEXT NOT NULL, workspace TEXT NOT NULL, repo TEXT NOT NULL,
          commit_sha TEXT NOT NULL, reason TEXT NOT NULL, fencing_token INTEGER NOT NULL UNIQUE,
          acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('active','released','recovered','expired')), revision INTEGER NOT NULL,
          released_at TEXT, recovery_receipt_id TEXT
        );
        CREATE TABLE IF NOT EXISTS recovery_receipts (
          receipt_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, actor TEXT NOT NULL, fencing_token INTEGER NOT NULL,
          recovered_by TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL, recovered_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fence_loss_receipts (
          receipt_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, actor TEXT NOT NULL,
          session_id TEXT NOT NULL, fencing_token INTEGER NOT NULL, reason TEXT NOT NULL,
          evidence_json TEXT NOT NULL, observed_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS production_write_recovery_receipts_immutable_update
        BEFORE UPDATE ON recovery_receipts BEGIN SELECT RAISE(ABORT, 'recovery receipts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS production_write_recovery_receipts_immutable_delete
        BEFORE DELETE ON recovery_receipts BEGIN SELECT RAISE(ABORT, 'recovery receipts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS production_write_fence_loss_receipts_immutable_update
        BEFORE UPDATE ON fence_loss_receipts BEGIN SELECT RAISE(ABORT, 'fence-loss receipts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS production_write_fence_loss_receipts_immutable_delete
        BEFORE DELETE ON fence_loss_receipts BEGIN SELECT RAISE(ABORT, 'fence-loss receipts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS production_write_lease_history_identity_immutable
        BEFORE UPDATE OF lease_id, actor, resources_json, session_id, workspace, repo, commit_sha, reason, fencing_token, acquired_at
        ON lease_history BEGIN SELECT RAISE(ABORT, 'production write lease history identity is immutable'); END;
        PRAGMA user_version=2;
        COMMIT;
        """)
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _transaction(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _BUSY_SECONDS
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            if time.monotonic() >= deadline:
                raise ProductionWriteLeaseError("production write lease database remained busy; refusing write") from exc
            time.sleep(0.01)


def _row(row: sqlite3.Row) -> ProductionWriteLease:
    return ProductionWriteLease(
        lease_id=row["lease_id"], actor=row["actor"], resources=tuple(json.loads(row["resources_json"])),
        session_id=row["session_id"], workspace=row["workspace"], repo=row["repo"], commit_sha=row["commit_sha"],
        reason=row["reason"], fencing_token=row["fencing_token"], acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"], expires_at=row["expires_at"], revision=row["revision"],
    )


def acquire(resources: Iterable[str], actor: str, session_id: str, workspace: str, repo: str, commit_sha: str, reason: str, lease_seconds: int = DEFAULT_LEASE_SECONDS, *, database_path: Path | None = None) -> ProductionWriteLease:
    rows = _validate_request(resources=resources, actor=actor, session_id=session_id, workspace=workspace, repo=repo, commit_sha=commit_sha, reason=reason)
    if not isinstance(lease_seconds, int) or lease_seconds <= 0:
        raise ProductionWriteLeaseError("lease_seconds must be a positive integer")
    conn = _connect(database_path)
    try:
        _transaction(conn)
        now = _now(); now_text = _iso(now); expires = _iso(now + timedelta(seconds=lease_seconds))
        active = conn.execute("SELECT lease_id, resources_json, expires_at FROM active_leases").fetchall()
        for item in active:
            if set(json.loads(item["resources_json"])) & set(rows):
                if item["expires_at"] <= now_text:
                    raise ProductionWriteLeaseError(
                        f"expired production write lease requires evidence-backed recover: {item['lease_id']}"
                    )
                raise ProductionWriteLeaseError(f"production write lease conflict: {item['lease_id']} holds overlapping resources")
        token = int(conn.execute("SELECT last_fencing_token FROM lease_state WHERE singleton=1").fetchone()[0]) + 1
        conn.execute("UPDATE lease_state SET last_fencing_token=? WHERE singleton=1", (token,))
        lease_id = str(uuid.uuid4()); payload = json.dumps(rows, separators=(",", ":"))
        values = (lease_id, actor, payload, session_id, workspace, repo, commit_sha, reason, token, now_text, now_text, expires, 1)
        conn.execute("INSERT INTO active_leases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        conn.execute("INSERT INTO lease_history(lease_id,actor,resources_json,session_id,workspace,repo,commit_sha,reason,fencing_token,acquired_at,heartbeat_at,expires_at,state,revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active',?)", values)
        conn.execute("COMMIT")
        return ProductionWriteLease(lease_id, actor, rows, session_id, workspace, repo, commit_sha, reason, token, now_text, now_text, expires, 1)
    except Exception:
        if conn.in_transaction: conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _cas(action: str, *, lease_id: str, actor: str, session_id: str, fencing_token: int, lease_seconds: int | None = None, database_path: Path | None = None) -> ProductionWriteLease | None:
    if not all(isinstance(value, str) and value for value in (lease_id, actor, session_id)) or not isinstance(fencing_token, int):
        raise ProductionWriteLeaseError("lease identity is invalid")
    conn = _connect(database_path)
    try:
        _transaction(conn)
        row = conn.execute("SELECT * FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=?", (lease_id, actor, session_id, fencing_token)).fetchone()
        if row is None:
            raise ProductionWriteLeaseError("production write lease ownership/fence no longer matches")
        now = _now(); now_text = _iso(now)
        if row["expires_at"] <= now_text:
            raise ProductionWriteLeaseError("production write lease has expired")
        revision = int(row["revision"]) + 1
        if action == "heartbeat":
            if not isinstance(lease_seconds, int) or lease_seconds <= 0: raise ProductionWriteLeaseError("lease_seconds must be a positive integer")
            expires = _iso(now + timedelta(seconds=lease_seconds))
            cur = conn.execute("UPDATE active_leases SET heartbeat_at=?, expires_at=?, revision=? WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=? AND revision=?", (now_text, expires, revision, lease_id, actor, session_id, fencing_token, row["revision"]))
            if cur.rowcount != 1: raise ProductionWriteLeaseError("production write lease heartbeat CAS failed")
            conn.execute("UPDATE lease_history SET heartbeat_at=?, expires_at=?, revision=? WHERE fencing_token=? AND state='active'", (now_text, expires, revision, fencing_token))
            conn.execute("COMMIT")
            return _row(conn.execute("SELECT * FROM active_leases WHERE lease_id=?", (lease_id,)).fetchone())
        cur = conn.execute("DELETE FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=? AND revision=?", (lease_id, actor, session_id, fencing_token, row["revision"]))
        if cur.rowcount != 1: raise ProductionWriteLeaseError("production write lease release CAS failed")
        conn.execute("UPDATE lease_history SET state='released', released_at=?, revision=? WHERE fencing_token=? AND state='active'", (now_text, revision, fencing_token))
        conn.execute("COMMIT")
        return None
    except Exception:
        if conn.in_transaction: conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def heartbeat(*, lease_id: str, actor: str, session_id: str, fencing_token: int, lease_seconds: int = DEFAULT_LEASE_SECONDS, database_path: Path | None = None) -> ProductionWriteLease:
    value = _cas("heartbeat", lease_id=lease_id, actor=actor, session_id=session_id, fencing_token=fencing_token, lease_seconds=lease_seconds, database_path=database_path)
    assert value is not None
    return value


def fence_mutation(*, lease_id: str, actor: str, session_id: str, fencing_token: int, lease_seconds: int = DEFAULT_LEASE_SECONDS, database_path: Path | None = None) -> ProductionWriteLease:
    """Renew and prove ownership immediately before a protected write.

    Callers must invoke this at each real filesystem/symlink commit boundary,
    not merely once at startup.  The exact CAS means an expired owner cannot
    continue after another actor receives the next fencing token.
    """
    return heartbeat(
        lease_id=lease_id,
        actor=actor,
        session_id=session_id,
        fencing_token=fencing_token,
        lease_seconds=lease_seconds, database_path=database_path,
    )


def release(*, lease_id: str, actor: str, session_id: str, fencing_token: int, database_path: Path | None = None) -> None:
    _cas("release", lease_id=lease_id, actor=actor, session_id=session_id, fencing_token=fencing_token, database_path=database_path)


def status(*, database_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(database_path)
    try:
        now = _iso(_now())
        leases = [_row(row).as_dict() | {"expired": row["expires_at"] <= now} for row in conn.execute("SELECT * FROM active_leases ORDER BY acquired_at")]
        receipts = [dict(row) | {"evidence": json.loads(row["evidence_json"])} for row in conn.execute("SELECT * FROM recovery_receipts ORDER BY recovered_at DESC")]
        losses = [dict(row) | {"evidence": json.loads(row["evidence_json"])} for row in conn.execute("SELECT * FROM fence_loss_receipts ORDER BY observed_at DESC")]
        return {"database": str(database_path or _database_path()), "active_leases": leases, "recovery_receipts": receipts, "fence_loss_receipts": losses}
    finally:
        conn.close()


def recover_expired(*, lease_id: str, actor: str, session_id: str, fencing_token: int, recovered_by: str, reason: str, evidence: dict[str, Any], database_path: Path | None = None) -> dict[str, Any]:
    if not isinstance(evidence, dict) or not evidence or not all(isinstance(value, str) and value for value in (recovered_by, reason)):
        raise ProductionWriteLeaseError("recovery requires a non-empty immutable evidence object, recovered_by, and reason")
    conn = _connect(database_path)
    try:
        _transaction(conn)
        row = conn.execute("SELECT * FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=?", (lease_id, actor, session_id, fencing_token)).fetchone()
        if row is None: raise ProductionWriteLeaseError("production write lease ownership/fence no longer matches")
        now = _iso(_now())
        if row["expires_at"] > now: raise ProductionWriteLeaseError("production write lease is still active; recovery refused")
        receipt_id = str(uuid.uuid4())
        deleted = conn.execute("DELETE FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=? AND revision=?", (lease_id, actor, session_id, fencing_token, row["revision"]))
        if deleted.rowcount != 1:
            raise ProductionWriteLeaseError("production write lease recovery CAS failed")
        conn.execute("INSERT INTO recovery_receipts VALUES (?,?,?,?,?,?,?,?)", (receipt_id, lease_id, actor, fencing_token, recovered_by, reason, json.dumps(evidence, sort_keys=True, separators=(",", ":")), now))
        conn.execute("UPDATE lease_history SET state='recovered', released_at=?, recovery_receipt_id=?, revision=revision+1 WHERE fencing_token=? AND state='active'", (now, receipt_id, fencing_token))
        conn.execute("COMMIT")
        return {"receipt_id": receipt_id, "lease_id": lease_id, "actor": actor, "fencing_token": fencing_token, "recovered_at": now}
    except Exception:
        if conn.in_transaction: conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def record_fence_loss(*, lease_id: str, actor: str, session_id: str, fencing_token: int, reason: str, evidence: dict[str, Any], database_path: Path | None = None) -> dict[str, Any]:
    """Persist an ownership-loss observation without touching protected state."""
    if not all(isinstance(value, str) and value for value in (lease_id, actor, session_id, reason)) or not isinstance(fencing_token, int) or not isinstance(evidence, dict) or not evidence:
        raise ProductionWriteLeaseError("fence-loss receipt requires exact identity, reason, and evidence")
    conn = _connect(database_path)
    try:
        _transaction(conn)
        receipt_id = str(uuid.uuid4())
        observed_at = _iso(_now())
        conn.execute(
            "INSERT INTO fence_loss_receipts VALUES (?,?,?,?,?,?,?,?)",
            (receipt_id, lease_id, actor, session_id, fencing_token, reason, json.dumps(evidence, sort_keys=True, separators=(",", ":")), observed_at),
        )
        conn.execute("COMMIT")
        return {"receipt_id": receipt_id, "lease_id": lease_id, "actor": actor, "fencing_token": fencing_token, "observed_at": observed_at}
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
