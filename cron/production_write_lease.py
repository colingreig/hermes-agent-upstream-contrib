"""Fenced, registry-backed leases for governed production writers.

This is intentionally separate from :mod:`cron.executor_admission`: production
install/release serialization protects persistent machine state, whereas the
executor admission lease protects one logical agent-dispatch surface.
"""
from __future__ import annotations

from contextlib import contextmanager
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
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise ProductionWriteLeaseError(
            "production write lease ownership cannot be verified because the POSIX user ID API is unavailable"
        )
    if info.st_uid != getuid():
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


@contextmanager
def mutation_guard(*, lease_id: str, actor: str, session_id: str, fencing_token: int,
                   lease_seconds: int = DEFAULT_LEASE_SECONDS,
                   database_path: Path | None = None):
    """Hold ownership and the fencing CAS across one filesystem mutation.

    A heartbeat before a write alone leaves a stale-owner gap.  The guard
    keeps the same SQLite writer transaction used for acquisition/recovery
    open until its body completes, so successor takeover and the filesystem
    commit boundary cannot interleave on this host.
    """
    if not all(isinstance(value, str) and value for value in (lease_id, actor, session_id)) or not isinstance(fencing_token, int):
        raise ProductionWriteLeaseError("lease identity is invalid")
    if not isinstance(lease_seconds, int) or lease_seconds <= 0:
        raise ProductionWriteLeaseError("lease_seconds must be a positive integer")
    conn = _connect(database_path)
    try:
        _transaction(conn)
        row = conn.execute(
            "SELECT * FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=?",
            (lease_id, actor, session_id, fencing_token),
        ).fetchone()
        if row is None:
            raise ProductionWriteLeaseError("production write lease ownership/fence no longer matches")
        now = _now()
        now_text = _iso(now)
        if row["expires_at"] <= now_text:
            raise ProductionWriteLeaseError("production write lease has expired")
        revision = int(row["revision"]) + 1
        expires = _iso(now + timedelta(seconds=lease_seconds))
        cur = conn.execute(
            "UPDATE active_leases SET heartbeat_at=?, expires_at=?, revision=? "
            "WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=? AND revision=?",
            (now_text, expires, revision, lease_id, actor, session_id, fencing_token, row["revision"]),
        )
        if cur.rowcount != 1:
            raise ProductionWriteLeaseError("production write lease mutation guard CAS failed")
        conn.execute(
            "UPDATE lease_history SET heartbeat_at=?, expires_at=?, revision=? "
            "WHERE fencing_token=? AND state='active'",
            (now_text, expires, revision, fencing_token),
        )
        guarded = _row(conn.execute("SELECT * FROM active_leases WHERE lease_id=?", (lease_id,)).fetchone())

        def finish_guard() -> None:
            """Leave a long protected operation with a fresh, truthful lease."""
            completed = _now()
            completed_text = _iso(completed)
            completed_revision = revision + 1
            completed_expires = _iso(completed + timedelta(seconds=lease_seconds))
            cur = conn.execute(
                "UPDATE active_leases SET heartbeat_at=?, expires_at=?, revision=? "
                "WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=? AND revision=?",
                (
                    completed_text, completed_expires, completed_revision,
                    lease_id, actor, session_id, fencing_token, revision,
                ),
            )
            if cur.rowcount != 1:
                raise ProductionWriteLeaseError("production write lease mutation guard completion CAS failed")
            conn.execute(
                "UPDATE lease_history SET heartbeat_at=?, expires_at=?, revision=? "
                "WHERE fencing_token=? AND state='active'",
                (completed_text, completed_expires, completed_revision, fencing_token),
            )
            # ProductionWriteLease is frozen so callers cannot alter its fence
            # identity.  Refresh only its liveness snapshot before the context
            # returns; callers persist this same object in receipts/state.
            object.__setattr__(guarded, "heartbeat_at", completed_text)
            object.__setattr__(guarded, "expires_at", completed_expires)
            object.__setattr__(guarded, "revision", completed_revision)
            conn.execute("COMMIT")

        try:
            yield guarded
        except BaseException:
            # The protected command may have partially mutated durable state
            # before failing.  Retain a live exact fence so its caller can run
            # the governed rollback path; never commit an already-expired row.
            finish_guard()
            raise
        else:
            finish_guard()
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


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
        history = conn.execute(
            "SELECT 1 FROM lease_history "
            "WHERE lease_id=? AND actor=? AND session_id=? AND fencing_token=?",
            (lease_id, actor, session_id, fencing_token),
        ).fetchone()
        if history is None:
            raise ProductionWriteLeaseError("fence-loss receipt identity has no matching lease history")
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


def authorize_stale_lock_recovery(*, stale: dict[str, Any], successor: dict[str, Any],
                                  database_path: Path | None = None) -> dict[str, Any]:
    """Prove a successor may remove one exact stale release-cut lock.

    This is deliberately proof-only. The caller must compare the lock bytes
    again and unlink them inside ``mutation_guard`` using the same successor.
    """
    required = (
        "lease_id", "actor", "resources", "session_id", "workspace", "repo",
        "commit_sha", "reason", "fencing_token", "acquired_at",
    )
    for name, value in (("stale", stale), ("successor", successor)):
        if not isinstance(value, dict) or not all(key in value for key in required):
            raise ProductionWriteLeaseError(f"{name} cut-lock lease identity is incomplete")
        if not all(isinstance(value[key], str) and value[key] for key in ("lease_id", "actor", "session_id", "commit_sha")):
            raise ProductionWriteLeaseError(f"{name} cut-lock lease identity is invalid")
        if not isinstance(value["fencing_token"], int):
            raise ProductionWriteLeaseError(f"{name} cut-lock fencing token is invalid")
        if not isinstance(value["resources"], list) or not all(isinstance(item, str) for item in value["resources"]):
            raise ProductionWriteLeaseError(f"{name} cut-lock resources are invalid")
    conn = _connect(database_path)
    try:
        _transaction(conn)
        current = conn.execute(
            "SELECT * FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? "
            "AND fencing_token=? AND commit_sha=?",
            (
                successor["lease_id"], successor["actor"], successor["session_id"],
                successor["fencing_token"], successor["commit_sha"],
            ),
        ).fetchone()
        now = _iso(_now())
        if current is None or current["expires_at"] <= now:
            raise ProductionWriteLeaseError("cut-lock successor lease is not current")
        expected_resources = {"runtime-release", "governed-mini-scripts"}
        current_identity_matches = (
            set(successor["resources"]) == set(json.loads(current["resources_json"]))
            and all(successor[key] == current[key] for key in ("workspace", "repo", "reason", "acquired_at"))
        )
        if (current["actor"] != "mini-release-cut"
                or set(json.loads(current["resources_json"])) != expected_resources
                or not current_identity_matches):
            raise ProductionWriteLeaseError("cut-lock successor is not the governed release writer")
        old = conn.execute(
            "SELECT * FROM lease_history WHERE lease_id=? AND actor=? AND session_id=? "
            "AND fencing_token=? AND commit_sha=?",
            (
                stale["lease_id"], stale["actor"], stale["session_id"],
                stale["fencing_token"], stale["commit_sha"],
            ),
        ).fetchone()
        old_identity_matches = old is not None and (
            set(stale["resources"]) == set(json.loads(old["resources_json"]))
            and all(stale[key] == old[key] for key in ("workspace", "repo", "reason", "acquired_at"))
        )
        if (old is None or old["actor"] != "mini-release-cut"
                or set(json.loads(old["resources_json"])) != expected_resources
                or not old_identity_matches):
            raise ProductionWriteLeaseError("cut-lock owner has no exact governed lease history")
        if old["state"] not in {"released", "recovered"}:
            raise ProductionWriteLeaseError("cut-lock owner lease is still live or was not governed-recovered")
        if int(old["fencing_token"]) >= int(current["fencing_token"]):
            raise ProductionWriteLeaseError("cut-lock successor fencing token does not supersede its owner")
        recovery_receipt_id = old["recovery_receipt_id"]
        if old["state"] == "recovered":
            recovery = conn.execute(
                "SELECT 1 FROM recovery_receipts WHERE receipt_id=? AND lease_id=? AND actor=? AND fencing_token=?",
                (recovery_receipt_id, old["lease_id"], old["actor"], old["fencing_token"]),
            ).fetchone()
            if recovery is None:
                raise ProductionWriteLeaseError("cut-lock owner recovery receipt is missing")
        conn.execute("COMMIT")
        return {
            "authorized": True,
            "stale_lease_id": old["lease_id"],
            "stale_fencing_token": old["fencing_token"],
            "stale_state": old["state"],
            "recovery_receipt_id": recovery_receipt_id,
            "successor_lease_id": current["lease_id"],
            "successor_fencing_token": current["fencing_token"],
            "authorized_at": now,
        }
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def authorize_legacy_lock_directory_recovery(*, successor: dict[str, Any],
                                             directory_mtime_ns: int,
                                             database_path: Path | None = None) -> dict[str, Any]:
    """Prove one old empty-directory cut lock belongs to one terminal owner.

    The legacy cutter left no owner bytes in its mkdir-based lock.  Its inode
    timestamp is therefore the only immutable identity available: it must
    fall inside exactly one earlier governed cutter lease lifetime.  The
    caller still has to re-check the inode, owner, emptiness, timestamp, and
    absence of another cutter while removing it inside ``mutation_guard``.
    """
    required = (
        "lease_id", "actor", "resources", "session_id", "workspace", "repo",
        "commit_sha", "reason", "fencing_token", "acquired_at",
    )
    if not isinstance(successor, dict) or not all(key in successor for key in required):
        raise ProductionWriteLeaseError("legacy cut-lock successor lease identity is incomplete")
    if not all(isinstance(successor[key], str) and successor[key]
               for key in ("lease_id", "actor", "session_id", "commit_sha")):
        raise ProductionWriteLeaseError("legacy cut-lock successor lease identity is invalid")
    if not isinstance(successor["fencing_token"], int):
        raise ProductionWriteLeaseError("legacy cut-lock successor fencing token is invalid")
    if (not isinstance(successor["resources"], list)
            or not all(isinstance(item, str) for item in successor["resources"])):
        raise ProductionWriteLeaseError("legacy cut-lock successor resources are invalid")
    if not isinstance(directory_mtime_ns, int) or directory_mtime_ns <= 0:
        raise ProductionWriteLeaseError("legacy cut-lock directory timestamp is invalid")

    conn = _connect(database_path)
    try:
        _transaction(conn)
        current = conn.execute(
            "SELECT * FROM active_leases WHERE lease_id=? AND actor=? AND session_id=? "
            "AND fencing_token=? AND commit_sha=?",
            (
                successor["lease_id"], successor["actor"], successor["session_id"],
                successor["fencing_token"], successor["commit_sha"],
            ),
        ).fetchone()
        now = _now()
        now_text = _iso(now)
        if current is None or current["expires_at"] <= now_text:
            raise ProductionWriteLeaseError("legacy cut-lock successor lease is not current")
        expected_resources = {"runtime-release", "governed-mini-scripts"}
        current_identity_matches = (
            set(successor["resources"]) == set(json.loads(current["resources_json"]))
            and all(successor[key] == current[key]
                    for key in ("workspace", "repo", "reason", "acquired_at"))
        )
        if (current["actor"] != "mini-release-cut"
                or set(json.loads(current["resources_json"])) != expected_resources
                or not current_identity_matches):
            raise ProductionWriteLeaseError(
                "legacy cut-lock successor is not the governed release writer"
            )

        lock_time = datetime.fromtimestamp(directory_mtime_ns / 1_000_000_000, timezone.utc)
        matches: list[sqlite3.Row] = []
        for row in conn.execute(
            "SELECT * FROM lease_history WHERE actor='mini-release-cut' "
            "AND fencing_token<? AND state IN ('released','recovered')",
            (current["fencing_token"],),
        ):
            if set(json.loads(row["resources_json"])) != expected_resources:
                continue
            terminal_at = row["released_at"]
            if not terminal_at:
                continue
            acquired = datetime.fromisoformat(row["acquired_at"])
            terminal = datetime.fromisoformat(terminal_at)
            if acquired <= lock_time <= terminal:
                if row["state"] == "recovered":
                    recovery = conn.execute(
                        "SELECT 1 FROM recovery_receipts WHERE receipt_id=? AND lease_id=? "
                        "AND actor=? AND fencing_token=?",
                        (
                            row["recovery_receipt_id"], row["lease_id"],
                            row["actor"], row["fencing_token"],
                        ),
                    ).fetchone()
                    if recovery is None:
                        raise ProductionWriteLeaseError(
                            "legacy cut-lock predecessor recovery receipt is missing"
                        )
                matches.append(row)
        if len(matches) != 1:
            raise ProductionWriteLeaseError(
                "legacy cut-lock timestamp does not identify exactly one terminal predecessor"
            )
        predecessor = matches[0]
        conn.execute("COMMIT")
        return {
            "authorized": True,
            "legacy_directory_mtime_ns": directory_mtime_ns,
            "predecessor_lease_id": predecessor["lease_id"],
            "predecessor_fencing_token": predecessor["fencing_token"],
            "predecessor_state": predecessor["state"],
            "recovery_receipt_id": predecessor["recovery_receipt_id"],
            "successor_lease_id": current["lease_id"],
            "successor_fencing_token": current["fencing_token"],
            "authorized_at": now_text,
        }
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
