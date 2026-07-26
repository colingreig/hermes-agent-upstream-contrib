"""SQLite/WAL terminal-verdict ledger with monotonic fencing leases.

All mutating operations run in one ``BEGIN IMMEDIATE`` transaction.  This is
the pre-merge trust boundary: a worker may finalize only its still-live fencing
token, and an immutable identity receives at most one terminal verdict.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from .identity import IdentityError, TrustedMergeIdentity
else:
    from identity import IdentityError, TrustedMergeIdentity


class StoreError(RuntimeError):
    pass


class LeaseBusyError(StoreError):
    pass


class LeaseLostError(StoreError):
    pass


class FinalizationConflictError(StoreError):
    pass


class LedgerIntegrityError(StoreError):
    pass


_VERSION = 1
_VERDICTS = frozenset({"PASS", "BLOCK"})
_REVIEW_VERDICTS = {
    "PASS": "PASS", "passed": "PASS", "ALLOW": "PASS",
    "BLOCK": "BLOCK", "failed": "BLOCK", "timed_out": "BLOCK",
    "output_limited": "BLOCK", "blocked": "BLOCK",
}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ttl_ms(value: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("ttl_s must be positive")
    result = int(value * 1000)
    if result <= 0 or result > 86_400_000:
        raise ValueError("ttl_s must be between 1ms and 24 hours")
    return result


def _holder(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 256:
        raise ValueError("holder must be a non-empty trimmed identifier")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("holder contains a control character")
    return value


def _verdict(value: object) -> str:
    if not isinstance(value, str) or value not in _REVIEW_VERDICTS:
        raise ValueError("verdict must be PASS/BLOCK or a known sandbox terminal status")
    return _REVIEW_VERDICTS[value]


def _json_object(value: Mapping[str, Any] | None, name: str) -> str:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    try:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only JSON values") from exc


def _decode_object(value: object, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise LedgerIntegrityError(f"stored {name} is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise LedgerIntegrityError(f"stored {name} is not a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class Lease:
    """A candidate-bound, expiring ownership token for one review actor."""

    identity: TrustedMergeIdentity
    holder: str
    fencing_token: int
    expires_at_ms: int

    @property
    def candidate_key(self) -> str:
        return self.identity.candidate_key

    @property
    def fence_token(self) -> str:
        """ReviewRunner compatibility spelling; token remains monotonic integer."""
        return str(self.fencing_token)


@dataclass(frozen=True, slots=True)
class Finalization:
    identity: TrustedMergeIdentity
    verdict: str
    evidence: dict[str, Any]
    finalized_at_ms: int
    fencing_token: int

    @property
    def accepted(self) -> bool:
        """A successful return is always a committed compare-and-swap result."""
        return True


class TrustStore:
    """A process-safe local ledger, not an unlocked JSON verdict file."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be a non-negative integer")
        self._busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=max(self._busy_timeout_ms / 1000, .001), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA trusted_schema = OFF")
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            conn.close()
            raise StoreError("trust store requires SQLite WAL mode")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        statements = (
            "CREATE TABLE IF NOT EXISTS identities ("
            "fingerprint TEXT PRIMARY KEY, canonical_repo TEXT NOT NULL, pr_number INTEGER NOT NULL CHECK(pr_number > 0), "
            "identity_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS subjects ("
            "canonical_repo TEXT NOT NULL, pr_number INTEGER NOT NULL CHECK(pr_number > 0), "
            "next_fencing_token INTEGER NOT NULL CHECK(next_fencing_token >= 0), "
            "PRIMARY KEY(canonical_repo, pr_number))",
            "CREATE TABLE IF NOT EXISTS leases ("
            "canonical_repo TEXT NOT NULL, pr_number INTEGER NOT NULL CHECK(pr_number > 0), "
            "identity_fingerprint TEXT NOT NULL REFERENCES identities(fingerprint), holder TEXT NOT NULL, "
            "fencing_token INTEGER NOT NULL CHECK(fencing_token > 0), expires_at_ms INTEGER NOT NULL, "
            "PRIMARY KEY(canonical_repo, pr_number))",
            "CREATE TABLE IF NOT EXISTS finalizations ("
            "identity_fingerprint TEXT PRIMARY KEY REFERENCES identities(fingerprint), "
            "verdict TEXT NOT NULL CHECK(verdict IN ('PASS','BLOCK')), evidence_json TEXT NOT NULL, "
            "finalized_at_ms INTEGER NOT NULL, fencing_token INTEGER NOT NULL CHECK(fencing_token > 0))",
        )
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version not in (0, _VERSION):
                raise StoreError(f"unsupported trust-store schema version: {version}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {_VERSION}")
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            conn.rollback()

    @staticmethod
    def _lease_valid(lease: Lease) -> None:
        if not isinstance(lease, Lease) or not isinstance(lease.identity, TrustedMergeIdentity):
            raise TypeError("lease must be a TrustStore Lease")
        _holder(lease.holder)
        if isinstance(lease.fencing_token, bool) or not isinstance(lease.fencing_token, int) or lease.fencing_token <= 0:
            raise ValueError("lease fencing_token must be positive")

    @staticmethod
    def _stored_identity(row: sqlite3.Row, fingerprint: str) -> TrustedMergeIdentity:
        try:
            identity = TrustedMergeIdentity.from_record(_decode_object(row["identity_json"], "identity"))
        except IdentityError as exc:
            raise LedgerIntegrityError("stored identity has an invalid shape") from exc
        if identity.fingerprint != fingerprint:
            raise LedgerIntegrityError("stored identity does not match its fingerprint")
        return identity

    def _ensure_identity(self, conn: sqlite3.Connection, identity: TrustedMergeIdentity, now: int) -> None:
        row = conn.execute("SELECT identity_json FROM identities WHERE fingerprint = ?", (identity.fingerprint,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO identities(fingerprint, canonical_repo, pr_number, identity_json, created_at_ms) VALUES (?, ?, ?, ?, ?)",
                (identity.fingerprint, identity.canonical_repo, identity.pr_number,
                 _json_object(identity.to_record(), "identity"), now),
            )
        elif self._stored_identity(row, identity.fingerprint) != identity:
            raise LedgerIntegrityError("identity fingerprint collision or corrupted row")

    def acquire(self, identity: TrustedMergeIdentity, *, holder: str, ttl_s: float, now_ms: int | None = None) -> Lease:
        """Acquire the next fencing token, or fail on a live competing lease."""
        if not isinstance(identity, TrustedMergeIdentity):
            raise TypeError("identity must be TrustedMergeIdentity")
        holder, ttl = _holder(holder), _ttl_ms(ttl_s)
        now = _now_ms() if now_ms is None else int(now_ms)
        if now < 0:
            raise ValueError("now_ms must be non-negative")
        conn = self._connect()
        try:
            self._begin(conn)
            self._ensure_identity(conn, identity, now)
            if conn.execute("SELECT 1 FROM finalizations WHERE identity_fingerprint = ?", (identity.fingerprint,)).fetchone():
                raise FinalizationConflictError("identity already has a terminal verdict")
            current = conn.execute(
                "SELECT expires_at_ms FROM leases WHERE canonical_repo = ? AND pr_number = ?", identity.key
            ).fetchone()
            if current is not None and int(current["expires_at_ms"]) > now:
                raise LeaseBusyError("a live lease already owns this repository/PR")
            subject = conn.execute(
                "SELECT next_fencing_token FROM subjects WHERE canonical_repo = ? AND pr_number = ?", identity.key
            ).fetchone()
            token = 1 if subject is None else int(subject["next_fencing_token"]) + 1
            if subject is None:
                conn.execute("INSERT INTO subjects(canonical_repo, pr_number, next_fencing_token) VALUES (?, ?, ?)", (*identity.key, token))
            else:
                conn.execute("UPDATE subjects SET next_fencing_token = ? WHERE canonical_repo = ? AND pr_number = ?", (token, *identity.key))
            expiry = now + ttl
            conn.execute(
                "INSERT INTO leases(canonical_repo,pr_number,identity_fingerprint,holder,fencing_token,expires_at_ms) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(canonical_repo,pr_number) DO UPDATE SET "
                "identity_fingerprint=excluded.identity_fingerprint,holder=excluded.holder,"
                "fencing_token=excluded.fencing_token,expires_at_ms=excluded.expires_at_ms",
                (*identity.key, identity.fingerprint, holder, token, expiry),
            )
            conn.commit()
            return Lease(identity, holder, token, expiry)
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def heartbeat(self, lease: Lease, *, ttl_s: float, now_ms: int | None = None) -> Lease:
        """CAS-renew only a still-live token; expired tokens never resurrect."""
        self._lease_valid(lease)
        ttl, now = _ttl_ms(ttl_s), (_now_ms() if now_ms is None else int(now_ms))
        conn = self._connect()
        try:
            self._begin(conn)
            expiry = now + ttl
            changed = conn.execute(
                "UPDATE leases SET expires_at_ms=? WHERE canonical_repo=? AND pr_number=? AND "
                "identity_fingerprint=? AND holder=? AND fencing_token=? AND expires_at_ms > ?",
                (expiry, *lease.identity.key, lease.identity.fingerprint, lease.holder, lease.fencing_token, now),
            ).rowcount
            if changed != 1:
                raise LeaseLostError("lease is stale, superseded, or expired")
            conn.commit()
            return Lease(lease.identity, lease.holder, lease.fencing_token, expiry)
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def release(self, lease: Lease) -> bool:
        """CAS-release without ever deleting a peer's newer lease."""
        self._lease_valid(lease)
        conn = self._connect()
        try:
            self._begin(conn)
            changed = conn.execute(
                "DELETE FROM leases WHERE canonical_repo=? AND pr_number=? AND identity_fingerprint=? AND holder=? AND fencing_token=?",
                (*lease.identity.key, lease.identity.fingerprint, lease.holder, lease.fencing_token),
            ).rowcount
            conn.commit()
            return changed == 1
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def finalize(
        self, lease: Lease, verdict: str, evidence: Mapping[str, Any] | None = None, *, now_ms: int | None = None
    ) -> Finalization:
        """Atomically record one PASS/BLOCK verdict for the exact active lease.

        Checking the active lease and inserting the verdict are inseparable.
        A stale worker therefore cannot overwrite a newer review with BLOCK,
        and no same-identity caller can replace an existing terminal decision.
        """
        self._lease_valid(lease)
        verdict, evidence_json = _verdict(verdict), _json_object(evidence, "evidence")
        now = _now_ms() if now_ms is None else int(now_ms)
        conn = self._connect()
        try:
            self._begin(conn)
            if conn.execute("SELECT 1 FROM finalizations WHERE identity_fingerprint = ?", (lease.identity.fingerprint,)).fetchone():
                raise FinalizationConflictError("terminal verdict already exists for immutable identity")
            active = conn.execute(
                "SELECT 1 FROM leases WHERE canonical_repo=? AND pr_number=? AND identity_fingerprint=? AND "
                "holder=? AND fencing_token=? AND expires_at_ms > ?",
                (*lease.identity.key, lease.identity.fingerprint, lease.holder, lease.fencing_token, now),
            ).fetchone()
            if active is None:
                raise LeaseLostError("lease is stale, superseded, or expired")
            conn.execute(
                "INSERT INTO finalizations(identity_fingerprint,verdict,evidence_json,finalized_at_ms,fencing_token) VALUES(?,?,?,?,?)",
                (lease.identity.fingerprint, verdict, evidence_json, now, lease.fencing_token),
            )
            # A defensive second CAS.  The transaction's writer lock makes a
            # peer impossible here; this protects against future code drift.
            deleted = conn.execute(
                "DELETE FROM leases WHERE canonical_repo=? AND pr_number=? AND identity_fingerprint=? AND holder=? AND fencing_token=?",
                (*lease.identity.key, lease.identity.fingerprint, lease.holder, lease.fencing_token),
            ).rowcount
            if deleted != 1:
                raise LeaseLostError("lease changed before finalization commit")
            conn.commit()
            return Finalization(lease.identity, verdict, _decode_object(evidence_json, "evidence"), now, lease.fencing_token)
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    def get_finalization(self, identity: TrustedMergeIdentity) -> Finalization | None:
        if not isinstance(identity, TrustedMergeIdentity):
            raise TypeError("identity must be TrustedMergeIdentity")
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT i.identity_json,f.verdict,f.evidence_json,f.finalized_at_ms,f.fencing_token FROM finalizations f "
                "JOIN identities i ON i.fingerprint=f.identity_fingerprint WHERE f.identity_fingerprint=?",
                (identity.fingerprint,),
            ).fetchone()
            if row is None:
                return None
            stored = self._stored_identity(row, identity.fingerprint)
            if stored != identity:
                raise LedgerIntegrityError("stored terminal verdict belongs to another identity")
            try:
                verdict = _verdict(row["verdict"])
            except ValueError as exc:
                raise LedgerIntegrityError("stored terminal verdict is invalid") from exc
            return Finalization(stored, verdict, _decode_object(row["evidence_json"], "evidence"),
                                int(row["finalized_at_ms"]), int(row["fencing_token"]))
        finally:
            conn.close()

    def close(self) -> None:
        """Connections are transaction-scoped; retained for context-manager parity."""

    def __enter__(self) -> "TrustStore":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
