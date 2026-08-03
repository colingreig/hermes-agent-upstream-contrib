#!/usr/bin/env python3
"""Fenced, SQLite-backed validator verdicts for the PR pipeline.

The old ``.validator_verdicts.json`` file is intentionally *not* an authority:
it had no lease, immutable candidate identity, or atomic terminal verdict.  The
runtime validator, wake sweep, and merge gates use this module as their single
read/write adapter over :class:`pr_pipeline.store.TrustStore`.

The JSON file is retained only for explicitly named migration/report readers via
``load_legacy_verdicts``.  No production path may read it as a fallback or write
to it.  A missing/corrupt SQLite ledger therefore means no verdict, which is the
safe answer for both validation and merge ownership.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_HERE = Path(__file__).resolve().parent
if __package__:
    from .identity import IdentityError, TrustedMergeIdentity, canonicalize_repo
    from .policy import CIRun, PolicyError, parse_policy_manifest
    from .review_runner import ReviewRunner
    from .store import (
        Finalization,
        FinalizationConflictError,
        Lease,
        LeaseBusyError,
        StoreError,
        TrustStore,
    )
else:
    # The Mini deploys this authority module as a flat entrypoint alongside the
    # canonical ``pr_pipeline`` package.  Add only that deployed package root;
    # package-qualified imports below preserve one identity/store type rather
    # than accidentally loading duplicate top-level sibling modules.
    # Source loading reaches ``pr_pipeline/validator_verdict.py`` directly,
    # whereas the deployed flat entrypoint lives beside ``pr_pipeline/``.
    _PACKAGE_ROOT = _HERE if (_HERE / "pr_pipeline" / "__init__.py").is_file() else _HERE.parent
    if str(_PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(_PACKAGE_ROOT))
    from pr_pipeline.identity import IdentityError, TrustedMergeIdentity, canonicalize_repo
    from pr_pipeline.policy import CIRun, PolicyError, parse_policy_manifest
    from pr_pipeline.review_runner import ReviewRunner
    from pr_pipeline.store import (
        Finalization,
        FinalizationConflictError,
        Lease,
        LeaseBusyError,
        StoreError,
        TrustStore,
    )

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
# Public ``STORE_PATH`` remains for callers, but now names a SQLite ledger.
STORE_PATH = str(_HERMES_HOME / "scripts" / ".validator_trust.sqlite3")
LEGACY_STORE_PATH = str(_HERMES_HOME / "scripts" / ".validator_verdicts.json")
DEFAULT_MAX_AGE_H = 24
_LEASE_TTL_S = 15 * 60
_STATUS_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,240}$")

# --- shared merge-shadow activation switch (Task: autonomous-merge activation) -
#
# Single source of truth for "is the PR-merge pipeline still in shadow
# (observe-only) mode, or has it been activated?". Every merge gate
# (autonomous_merge._shadow(), merge_guard._shadow(),
# hermes_validate_ops.VALIDATE_SHADOW) AND the verdict writer below
# (``finalize_shadow_review`` / ``_validate_record``'s ``shadow`` stamp) call
# this ONE function so they can never disagree about which mode is active.
#
# FAIL-CLOSED DEFAULT: absence of env means SHADOW. Activation requires an
# explicit HERMES_MERGE_ACTIVE truthy value ({1,true,yes,on},
# case-insensitive). HERMES_MERGE_SHADOW is the emergency override: when
# truthy it forces shadow back ON even if HERMES_MERGE_ACTIVE is set —
# shadow always wins.
_MERGE_SHADOW_ENV = "HERMES_MERGE_SHADOW"
_MERGE_ACTIVE_ENV = "HERMES_MERGE_ACTIVE"
_MERGE_SHADOW_TRUTHY = {"1", "true", "yes", "on"}


def _env_truthy(source: Mapping[str, str], name: str) -> bool:
    return (source.get(name, "") or "").strip().lower() in _MERGE_SHADOW_TRUTHY


def merge_shadow_active(env: Mapping[str, str] | None = None) -> bool:
    """Return True iff the PR-merge pipeline must stay in shadow (observe-only)
    mode. Fail-closed: shadow unless HERMES_MERGE_ACTIVE is explicitly truthy,
    and HERMES_MERGE_SHADOW (the emergency override) forces shadow ON even
    when HERMES_MERGE_ACTIVE is set — shadow always wins. Every merge gate and
    the verdict writer must call this function (not re-implement the env
    parsing) so they cannot drift apart."""
    source = env if env is not None else os.environ
    if _env_truthy(source, _MERGE_SHADOW_ENV):
        return True
    return not _env_truthy(source, _MERGE_ACTIVE_ENV)


class VerdictStoreError(RuntimeError):
    """The authoritative verdict boundary could not safely complete."""


class VerdictBusyError(VerdictStoreError):
    """A different fenced validator currently owns this PR identity."""


@dataclass(frozen=True)
class ShadowReviewSession:
    """One live TrustStore lease around a shadow validation attempt."""

    store: TrustStore
    identity: TrustedMergeIdentity
    lease: Lease | None
    existing: Finalization | None = None


class _ShadowCommandPolicy:
    """ReviewRunner requires a policy object even when execution is disabled."""

    def command_for(self, _candidate: object) -> tuple[str, ...]:
        raise AssertionError("shadow review must not select or execute PR-controlled code")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def _ledger_path(path: str | os.PathLike[str] | None = None) -> Path:
    """Return the SQLite path without treating a legacy JSON path as storage."""
    selected = Path(path or STORE_PATH).expanduser()
    if selected.suffix == ".json":
        # Compatibility callers historically passed STORE_PATH.  Redirecting the
        # name is safe; silently loading/writing the JSON file is not.
        selected = selected.with_suffix(".sqlite3")
    return selected


def _canonical_repo(repo: str) -> str:
    """Strict canonicalization for the authority path (never a network alias)."""
    try:
        return canonicalize_repo(repo)
    except IdentityError as exc:
        raise VerdictStoreError(f"invalid repository identity: {repo!r}") from exc


def canonical_repo(repo: str) -> str:
    """Public strict repository normalization used by new runtime callers."""
    return _canonical_repo(repo)


def _holder() -> str:
    return f"validator:{socket.gethostname()}:{os.getpid()}"


def _record_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    try:
        cleaned = json.loads(json.dumps(dict(record), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise VerdictStoreError("verdict record must be a JSON object") from exc
    if not isinstance(cleaned, dict):  # defensive: json round-trip above should ensure it
        raise VerdictStoreError("verdict record must be a JSON object")
    return cleaned


def _validate_record(identity: TrustedMergeIdentity, record: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = _record_evidence(record)
    verdict = cleaned.get("verdict")
    if verdict not in {"PASS", "BLOCK"}:
        raise VerdictStoreError("verdict must be PASS or BLOCK")
    if cleaned.get("head_sha") != identity.head_sha:
        raise VerdictStoreError("verdict head_sha must equal the fenced immutable identity")
    expected_repo = cleaned.get("expected_repo") or identity.canonical_repo
    if _canonical_repo(str(expected_repo)) != identity.canonical_repo:
        raise VerdictStoreError("verdict expected_repo must equal the fenced immutable identity")
    cleaned["repo"] = identity.canonical_repo
    cleaned["pr"] = identity.pr_number
    cleaned["head_sha"] = identity.head_sha
    cleaned["expected_repo"] = identity.canonical_repo
    # "shadow" is stamped exclusively by finalize_shadow_review() at WRITE time
    # (from merge_shadow_active()), never inferred here or trusted from an
    # arbitrary caller. This function also reconstructs ALREADY-FINALIZED
    # records on READ (see _finalization_record) — an immutable terminal
    # verdict's historical shadow value must not be recomputed against
    # today's env, only validated as the explicit boolean it was written with.
    if not isinstance(cleaned.get("shadow"), bool):
        raise VerdictStoreError("verdict record must carry an explicit boolean 'shadow' flag")
    return cleaned


def _finalization_record(finalization: Finalization) -> dict[str, Any] | None:
    evidence = finalization.evidence
    raw = evidence.get("record")
    if not isinstance(raw, dict):
        return None
    try:
        record = _validate_record(finalization.identity, raw)
    except VerdictStoreError:
        return None
    record.update(
        {
            "identity": finalization.identity.to_record(),
            "fencing_token": finalization.fencing_token,
            "finalized_at_ms": finalization.finalized_at_ms,
            "ts": _iso_from_ms(finalization.finalized_at_ms),
            "store": "sqlite-fenced",
        }
    )
    return record


def _latest_finalization(
    repo: str, pr: int, *, head_sha: str = "", path: str | os.PathLike[str] | None = None
) -> Finalization | None:
    """Read an immutable terminal verdict from SQLite only; never JSON-fallback."""
    canonical = _canonical_repo(repo)
    ledger = _ledger_path(path)
    if not ledger.exists():
        return None
    try:
        with sqlite3.connect(ledger) as conn:
            rows = conn.execute(
                "SELECT i.identity_json,f.verdict,f.evidence_json,f.finalized_at_ms,f.fencing_token "
                "FROM finalizations f JOIN identities i ON i.fingerprint=f.identity_fingerprint "
                "WHERE i.canonical_repo=? AND i.pr_number=? ORDER BY f.finalized_at_ms DESC",
                (canonical, int(pr)),
            ).fetchall()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise VerdictStoreError("authoritative SQLite verdict ledger is unreadable") from exc
    for identity_json, verdict, evidence_json, finalized_at_ms, fencing_token in rows:
        try:
            identity = TrustedMergeIdentity.from_record(json.loads(identity_json))
            if head_sha and identity.head_sha != head_sha:
                continue
            evidence = json.loads(evidence_json)
            if verdict not in {"PASS", "BLOCK"} or not isinstance(evidence, dict):
                raise ValueError("malformed finalization")
            return Finalization(identity, verdict, evidence, int(finalized_at_ms), int(fencing_token))
        except (IdentityError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VerdictStoreError("authoritative SQLite verdict row is malformed") from exc
    return None


def finalization_count(path: str | os.PathLike[str] | None = None) -> int | None:
    """Return the raw terminal-verdict count without creating or repairing a ledger.

    ``None`` means the ledger cannot be read (including a missing file or a
    schemaless SQLite file); a numeric result, including ``0``, is a verified
    count from the authoritative ``finalizations`` table.
    """
    ledger = _ledger_path(path)
    if not ledger.exists():
        return None
    try:
        with sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT COUNT(*) FROM finalizations").fetchone()
    except (OSError, sqlite3.DatabaseError):
        return None
    return int(row[0]) if row is not None else None


def begin_shadow_review(
    identity: TrustedMergeIdentity, *, path: str | os.PathLike[str] | None = None, holder: str | None = None
) -> ShadowReviewSession:
    """Fence a real validator invocation before it reads a PR diff.

    ``ReviewRunner`` is deliberately invoked with ``execute=False``: it creates
    the canonical sandbox review plan but cannot check out or execute PR code on
    the Mini.  The existing vendored validator remains the shadow evidence
    producer, and its terminal PASS/BLOCK is then committed through this lease.
    """
    if not isinstance(identity, TrustedMergeIdentity):
        raise VerdictStoreError("shadow validation needs a TrustedMergeIdentity")
    store = TrustStore(_ledger_path(path))
    existing = store.get_finalization(identity)
    if existing is not None:
        return ShadowReviewSession(store, identity, None, existing)
    try:
        lease = store.acquire(identity, holder=holder or _holder(), ttl_s=_LEASE_TTL_S)
    except LeaseBusyError as exc:
        raise VerdictBusyError("another validator owns this immutable PR identity") from exc
    try:
        planned = ReviewRunner(_ShadowCommandPolicy(), verdict_store=store).run(identity, lease=lease, execute=False)
        if planned.status != "shadow":
            raise VerdictStoreError("shadow review plan was not accepted")
    except Exception:
        store.release(lease)
        raise
    return ShadowReviewSession(store, identity, lease)


def finalize_shadow_review(
    session: ShadowReviewSession,
    record: Mapping[str, Any],
    *,
    validator_review: bool = False,
    force_shadow: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Commit one immutable verdict through the session's active fencing lease.

    ANTI-FORGERY CONTRACT (Task: autonomous-merge activation hardening): a
    NON-SHADOW (merge-eligible) verdict may only be finalized from inside the
    validator's own review flow (``validate_pr.validate`` passes
    ``validator_review=True``) while the session's TrustStore lease is provably
    still live. Every other caller — including the compatibility writer
    ``record_verdict`` and the ``validator_verdict.py write`` CLI — is stamped
    ``shadow=True`` unconditionally, so a bare CLI/process write can never mint
    a merge-eligible PASS regardless of env. ``force_shadow=True`` lets the
    validator itself demand a shadow verdict (``--shadow`` / panel-suppressed
    runs) even when the pipeline is activated.
    """
    if session.existing is not None:
        existing = _finalization_record(session.existing)
        if existing is None:
            raise VerdictStoreError("existing finalization is malformed")
        # Every terminal verdict is immutable for its identity, not just BLOCK.
        # A new review must bind a new immutable candidate/head.
        return existing, True
    if session.lease is None:
        raise VerdictStoreError("shadow review session has no live fencing lease")
    stamped = dict(record)
    # VALIDATOR-ONLY SECRET CHECK (in-process forgery residual close-out): an
    # executor holding a lease can call this function directly with
    # validator_review=True from inline Python — the lease alone doesn't prove
    # the caller IS the validator daemon. HERMES_VALIDATOR_FINALIZE_TOKEN is a
    # secret-shaped env var, so Hermes' own executor-subprocess scrubbing (see
    # skill subprocess secret passthrough) strips it from every executor
    # process; only the validator's own long-lived process env has it. We
    # never compare it against a hard-coded value — there is nothing to leak
    # or replay — its mere PRESENCE in this process's environment is the
    # signal that this process is the validator, not the executor. A missing
    # token must degrade to shadow, never raise (fail-safe: an
    # under-provisioned validator loses merge eligibility, not availability).
    if validator_review and not force_shadow and not (
        os.environ.get("HERMES_VALIDATOR_FINALIZE_TOKEN", "") or ""
    ).strip():
        validator_review = False
    if not validator_review or force_shadow:
        # Not the validator's fenced review flow (or the validator explicitly
        # requested shadow): the verdict is recorded but NEVER merge-eligible.
        stamped["shadow"] = True
    else:
        # Validator review flow: prove the fencing lease is live RIGHT NOW via
        # the store's own lease machinery before stamping a merge-eligible
        # verdict. A stale/superseded/expired lease fails closed and loudly.
        try:
            session.store.heartbeat(session.lease, ttl_s=_LEASE_TTL_S)
        except (StoreError, ValueError, TypeError) as exc:
            raise VerdictStoreError(
                "non-shadow finalization REFUSED: fencing lease is not live "
                "(fail-closed; only the validator's fenced review flow may "
                "finalize a merge-eligible verdict)"
            ) from exc
        # Single source of truth: stamp "shadow" from the SAME env-derived
        # switch every merge gate reads. A new terminal verdict's shadow state
        # must always match the pipeline's CURRENT activation mode at the
        # moment it is finalized — never a caller's guess, and never
        # recomputed later (it becomes immutable the instant it is written;
        # see _validate_record's read-path note above).
        stamped["shadow"] = merge_shadow_active()
    cleaned = _validate_record(session.identity, stamped)
    evidence = {
        "record": cleaned,
        "review_runner": {"mode": "shadow", "executed_pr_code": False},
    }
    try:
        finalization = session.store.finalize(session.lease, cleaned["verdict"], evidence)
    except (StoreError, ValueError) as exc:
        raise VerdictStoreError("fenced SQLite finalization failed") from exc
    authoritative = _finalization_record(finalization)
    if authoritative is None:
        raise VerdictStoreError("fenced SQLite finalization was malformed")
    return authoritative, False


def abort_shadow_review(session: ShadowReviewSession) -> None:
    """Release an unfinished lease without ever touching a peer's newer lease."""
    if session.lease is not None:
        session.store.release(session.lease)


def record_verdict(
    repo: str,
    pr: int,
    record: Mapping[str, Any],
    *,
    identity: TrustedMergeIdentity,
    path: str | os.PathLike[str] | None = None,
    session: ShadowReviewSession | None = None,
) -> tuple[dict[str, Any], bool]:
    """Compatibility-shaped writer backed exclusively by SQLite finalization.

    Callers that do not provide an immutable identity are rejected.  This is
    intentionally incompatible with the old unlocked JSON setter: there is no
    safe way to infer base, tested merge, policy, and exact CI from a verdict
    payload after review has already run.

    SHADOW-ONLY: this writer never passes ``validator_review=True``, so every
    verdict it records is stamped ``shadow=True`` (never merge-eligible). A
    merge-eligible verdict can only come from the validator's own fenced
    review flow (``validate_pr.validate`` -> ``finalize_shadow_review``).
    """
    canonical = _canonical_repo(repo)
    if identity.canonical_repo != canonical or identity.pr_number != int(pr):
        raise VerdictStoreError("verdict subject does not match immutable identity")
    owned = session is None
    active = session or begin_shadow_review(identity, path=path)
    try:
        return finalize_shadow_review(active, record, validator_review=False)
    except Exception:
        if owned:
            abort_shadow_review(active)
        raise


def verdict_for(
    repo: str, pr: int, path: str | os.PathLike[str] | None = None, *, head_sha: str = ""
) -> dict[str, Any] | None:
    finalization = _latest_finalization(repo, pr, head_sha=head_sha, path=path)
    return _finalization_record(finalization) if finalization is not None else None


def load_verdicts(path: str | os.PathLike[str] | None = None) -> dict[str, dict[str, Any]]:
    """Return the latest immutable SQLite verdict per mutable repo/PR subject."""
    ledger = _ledger_path(path)
    if not ledger.exists():
        return {}
    try:
        with sqlite3.connect(ledger) as conn:
            rows = conn.execute(
                "SELECT i.canonical_repo,i.pr_number,i.identity_json,f.verdict,f.evidence_json,"
                "f.finalized_at_ms,f.fencing_token FROM finalizations f "
                "JOIN identities i ON i.fingerprint=f.identity_fingerprint "
                "ORDER BY f.finalized_at_ms DESC"
            ).fetchall()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise VerdictStoreError("authoritative SQLite verdict ledger is unreadable") from exc
    records: dict[str, dict[str, Any]] = {}
    for repo, pr, identity_json, verdict, evidence_json, finalized_at_ms, fencing_token in rows:
        key = f"{repo}#{pr}"
        if key in records:
            continue
        try:
            identity = TrustedMergeIdentity.from_record(json.loads(identity_json))
            finalization = Finalization(identity, verdict, json.loads(evidence_json), int(finalized_at_ms), int(fencing_token))
        except (IdentityError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VerdictStoreError("authoritative SQLite verdict row is malformed") from exc
        record = _finalization_record(finalization)
        if record is not None:
            records[key] = record
    return records


def load_legacy_verdicts(path: str | os.PathLike[str] = LEGACY_STORE_PATH) -> dict[str, Any]:
    """Read historical JSON only for migration/report tooling, never authority."""
    try:
        with Path(path).expanduser().open() as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(_data: Mapping[str, Any], _path: str = LEGACY_STORE_PATH) -> None:
    """Retired JSON mutator retained only to make unsafe callers fail loudly."""
    raise VerdictStoreError("JSON verdict writes are retired; use a fenced SQLite review session")


def _age_hours(iso_ts: str) -> float:
    try:
        parsed = datetime.fromisoformat(iso_ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return float("inf")


def is_pass_fresh(
    repo: str,
    pr: int,
    head_sha: str = "",
    max_age_h: float = DEFAULT_MAX_AGE_H,
    path: str | os.PathLike[str] | None = None,
    *,
    current_base_sha: str | None = None,
) -> tuple[bool, str]:
    """Default deny unless the ledger has a current, exact, non-shadow PASS."""
    if not head_sha:
        return False, "exact current PR head is required (fail-closed)"
    verdict = verdict_for(repo, pr, path, head_sha=head_sha)
    if not verdict:
        return False, "no fenced SQLite verdict for the exact current head (fail-closed)"
    if verdict.get("verdict") != "PASS":
        return False, f"validator verdict is {verdict.get('verdict')!r}, not PASS"
    if verdict.get("shadow") is not False:
        return False, "verdict is shadow-only; live merge ownership is disabled"
    if current_base_sha is not None:
        identity = verdict.get("identity")
        trusted_base_sha = identity.get("base_sha") if isinstance(identity, Mapping) else None
        if not current_base_sha:
            return False, "current protected-base tip is unreadable (fail-closed); re-validate"
        if trusted_base_sha != current_base_sha:
            return False, (
                "protected base advanced after verdict finalization; PASS does not cover "
                "the current base; re-validate"
            )
    age = _age_hours(str(verdict.get("ts", "")))
    if age > max_age_h:
        return False, f"verdict is stale ({age:.1f}h > {max_age_h}h); re-validate"
    return True, f"fenced PASS ({age:.1f}h old, policy={verdict['identity']['ci_policy_id']})"


def _gh_json(path: str) -> Any:
    try:
        completed = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerdictStoreError(f"GitHub identity query failed: {exc}") from exc
    if completed.returncode != 0:
        raise VerdictStoreError(f"GitHub identity query failed: {completed.stderr.strip()[:160]}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise VerdictStoreError("GitHub identity query returned malformed JSON") from exc


def _gh_paginated_items(path: str, key: str | None = None) -> list[Any]:
    """Read every REST page, rejecting malformed or implausibly large evidence."""
    items: list[Any] = []
    expected_total: int | None = None
    separator = "&" if "?" in path else "?"
    for page in range(1, 1001):
        payload = _gh_json(f"{path}{separator}per_page=100&page={page}")
        if key is None:
            page_items = payload
        else:
            if not isinstance(payload, Mapping):
                raise VerdictStoreError("GitHub returned malformed paginated CI evidence")
            total_count = payload.get("total_count")
            if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
                raise VerdictStoreError("GitHub returned malformed paginated CI evidence count")
            if total_count > 100_000:
                raise VerdictStoreError("GitHub CI evidence exceeded the fail-closed pagination limit")
            if expected_total is None:
                expected_total = total_count
            elif total_count != expected_total:
                raise VerdictStoreError("GitHub paginated CI evidence count changed between pages")
            page_items = payload.get(key)
        if not isinstance(page_items, list):
            raise VerdictStoreError("GitHub returned malformed paginated CI evidence")
        items.extend(page_items)
        if expected_total is not None and len(items) > expected_total:
            raise VerdictStoreError("GitHub paginated CI evidence exceeded its total count")
        if len(page_items) < 100:
            if expected_total is not None and len(items) != expected_total:
                raise VerdictStoreError("GitHub paginated CI evidence did not match its total count")
            return items
    raise VerdictStoreError("GitHub CI evidence exceeded the fail-closed pagination limit")


def _status_run_id(status: Mapping[str, Any]) -> str:
    """Return an order-independent, candidate-safe legacy-status identifier.

    GitHub legacy commit-status responses are an unordered collection.  Their
    list position is not evidence and must never enter the immutable candidate
    identity: a reordered API response would otherwise create a second verdict
    for identical code and CI.  Use GitHub's immutable status id/node id when
    it fits the ledger's opaque-id grammar.  Older or synthetic status payloads
    sometimes lack both, so canonicalize the complete status object and bind a
    SHA-256 digest instead.  Two duplicate contexts then select the same
    lexicographically smallest evidence id regardless of response order.
    """
    for field in ("id", "node_id"):
        value = status.get(field)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            continue
        token = str(value)
        if _STATUS_PROVIDER_ID.fullmatch(token):
            return f"status:merge:{field}:{token}"
    try:
        canonical = json.dumps(dict(status), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise VerdictStoreError("legacy CI status cannot form stable identity evidence") from exc
    return "status:merge:sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _has_exact_pr_association(
    payload: Mapping[str, Any], *, pr: int, base_sha: str, head_sha: str,
) -> bool:
    associations = payload.get("pull_requests")
    if not isinstance(associations, list) or len(associations) != 1:
        return False
    association = associations[0]
    if not isinstance(association, Mapping):
        return False
    associated_base = association.get("base")
    associated_head = association.get("head")
    if not isinstance(associated_base, Mapping) or not isinstance(associated_head, Mapping):
        return False
    return (
        association.get("number") == pr
        and associated_base.get("sha") == base_sha
        and associated_head.get("sha") == head_sha
    )


def _positive_provider_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_run_app_id(item: Mapping[str, Any]) -> int:
    app = item.get("app")
    if not isinstance(app, Mapping) or not _positive_provider_id(app.get("id")):
        raise VerdictStoreError("GitHub returned a malformed check-run app")
    return int(app["id"])


def _head_actions_candidate(
    canonical: str, item: Mapping[str, Any], *, name: str, expected_app_id: int,
    pr: int, base_sha: str, head_sha: str,
) -> CIRun | None:
    run_id = item.get("id")
    suite = item.get("check_suite")
    suite_id = suite.get("id") if isinstance(suite, Mapping) else None
    if not _positive_provider_id(run_id) or not _positive_provider_id(suite_id):
        raise VerdictStoreError("GitHub returned a malformed head check-run id")
    app_id = _check_run_app_id(item)
    if (
        item.get("name") != name
        or item.get("status") != "completed"
        or item.get("conclusion") != "success"
        or item.get("head_sha") != head_sha
        or app_id != expected_app_id
        or not _has_exact_pr_association(item, pr=pr, base_sha=base_sha, head_sha=head_sha)
    ):
        return None
    workflow_runs = _gh_paginated_items(
        f"repos/{canonical}/actions/runs?check_suite_id={suite_id}", "workflow_runs"
    )
    if len(workflow_runs) != 1:
        return None
    workflow = workflow_runs[0]
    if not isinstance(workflow, Mapping):
        raise VerdictStoreError("GitHub returned a malformed workflow run")
    workflow_id = workflow.get("id")
    if not _positive_provider_id(workflow_id):
        raise VerdictStoreError("GitHub returned a malformed workflow-run id")
    if (
        workflow.get("event") != "pull_request"
        or workflow.get("status") != "completed"
        or workflow.get("conclusion") != "success"
        or workflow.get("head_sha") != head_sha
        or not _has_exact_pr_association(workflow, pr=pr, base_sha=base_sha, head_sha=head_sha)
    ):
        return None
    return CIRun(f"check-run:head:{run_id}", name, "success", head_sha)


def resolve_shadow_identity(repo: str, pr: int, task_id: str = "", expected_repo: str = "") -> TrustedMergeIdentity:
    """Build a strict identity from GitHub's PR, branch policy, and exact CI.

    The branch-protection required-check list is the policy source. Merge-SHA
    evidence is preferred. Only a completely evidence-free synthetic merge may
    use a fully associated, app-bound pull-request Actions run on the exact head.
    Every incomplete, ambiguous, stale, or mismatched result fails closed.
    """
    canonical = _canonical_repo(repo)
    if expected_repo and _canonical_repo(expected_repo) != canonical:
        raise VerdictStoreError("resolved expected repo differs from validator repository")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise VerdictStoreError("PR number must be a positive integer")
    details = _gh_json(f"repos/{canonical}/pulls/{pr}")
    base = (details.get("base") or {})
    head = (details.get("head") or {})
    head_sha = head.get("sha")
    tested_merge_sha = details.get("merge_commit_sha")
    base_ref = base.get("ref")
    if not all(isinstance(value, str) and len(value) in (40, 64) for value in (head_sha, tested_merge_sha)):
        raise VerdictStoreError("GitHub PR lacks immutable head or synthetic merge SHA")
    if not isinstance(base_ref, str) or not base_ref:
        raise VerdictStoreError("GitHub PR lacks its protected base branch")
    # BUG THIS REPLACES (2026-07-26).  ``pull.base.sha`` is the base tip recorded
    # when the PR was opened; GitHub never advances it.  The synthetic merge, by
    # contrast, is computed against a *live* base tip.  The previous check
    # compared the two, so it could only ever succeed on a base branch that had
    # not moved since the PR was created.  Every other PR was refused with
    # "synthetic merge does not have the exact reviewed base/head parents", no
    # identity could be minted, and therefore no verdict could ever reach the
    # SQLite ledger.  Measured 2026-07-26: this rejected 100% of the open-PR
    # fleet, including the only allowlisted PR with a protected base.
    #
    # The base that matters for trust is the one CI actually merged against —
    # the synthetic merge's FIRST parent. Bind that, and keep the boundary
    # fail-closed by proving:
    #   (a) the tested merge is an exact two-parent merge,
    #   (b) its second parent is byte-identical to the reviewed PR head, and
    #   (c) its first parent is still the exact protected-base tip before and
    #       after all CI evidence is resolved.
    commit = _gh_json(f"repos/{canonical}/git/commits/{tested_merge_sha}")
    parents = tuple((item or {}).get("sha") for item in (commit.get("parents") or []))
    if len(parents) != 2 or not all(isinstance(value, str) and len(value) in (40, 64) for value in parents):
        raise VerdictStoreError("synthetic merge is not an exact two-parent base/head merge")
    base_sha, merged_head_sha = parents
    if merged_head_sha != head_sha:
        raise VerdictStoreError("synthetic merge does not have the exact reviewed head parent")
    live_base = _gh_json(f"repos/{canonical}/git/ref/heads/{base_ref}")
    live_base_sha = ((live_base or {}).get("object") or {}).get("sha")
    if not isinstance(live_base_sha, str) or len(live_base_sha) not in (40, 64):
        raise VerdictStoreError("protected base branch has no readable immutable tip")
    if base_sha != live_base_sha:
        raise VerdictStoreError("tested merge base is not the current protected base tip")

    protection = _gh_json(f"repos/{canonical}/branches/{base_ref}/protection/required_status_checks")
    # The explicit base-tip checks fence identity resolution. Requiring GitHub's
    # strict up-to-date check policy closes the later validation-to-merge race:
    # GitHub itself atomically rejects the merge if the protected base advances.
    if not isinstance(protection, dict) or protection.get("strict") is not True:
        raise VerdictStoreError(
            "protected base must require strict up-to-date status checks"
        )
    contexts = protection.get("contexts") or []
    checks = protection.get("checks") or []
    required = {str(value) for value in contexts if isinstance(value, str) and value}
    required_apps: dict[str, int | None] = {name: None for name in required}
    check_apps: dict[str, int | None] = {}
    for item in checks:
        if not isinstance(item, Mapping) or not isinstance(item.get("context"), str) or not item.get("context"):
            raise VerdictStoreError("protected base branch returned a malformed required check")
        name = str(item["context"])
        app_id = item.get("app_id")
        if app_id is not None and (isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0):
            raise VerdictStoreError("protected base branch returned an invalid required-check app id")
        if name in check_apps and check_apps[name] != app_id:
            raise VerdictStoreError("protected base branch returned ambiguous required-check app ids")
        check_apps[name] = app_id
        required.add(name)
        required_apps[name] = app_id
    if not required:
        raise VerdictStoreError("protected base branch declares no required CI checks")
    manifest = parse_policy_manifest(json.dumps({
        "schema_version": 1,
        "repositories": [canonical],
        "required_checks": sorted(required),
    }))

    candidates: dict[str, CIRun] = {}
    merge_checks = _gh_paginated_items(
        f"repos/{canonical}/commits/{tested_merge_sha}/check-runs", "check_runs"
    )
    merge_statuses = _gh_paginated_items(
        f"repos/{canonical}/commits/{tested_merge_sha}/statuses"
    )
    ci_evidence_sha = tested_merge_sha
    for name in required:
        if sum(1 for item in merge_checks if isinstance(item, Mapping) and item.get("name") == name) > 1:
            raise VerdictStoreError("GitHub returned ambiguous required-check evidence")
    observed_check_contexts: set[str] = set()
    for item in merge_checks:
        if not isinstance(item, Mapping):
            raise VerdictStoreError("GitHub returned a malformed check run")
        name, run_id = item.get("name"), item.get("id")
        if name not in required:
            continue
        # Provider precedence is context-wide, not success-candidate-wide. Once
        # GitHub reports a Check Run for a required context, that provider is
        # authoritative even when the run failed, is pending, or is malformed.
        # A successful legacy status with the same context must never revive it.
        observed_check_contexts.add(str(name))
        if not _positive_provider_id(run_id):
            raise VerdictStoreError("GitHub returned a malformed merge check-run id")
        app_id = _check_run_app_id(item)
        if item.get("status") != "completed" or item.get("conclusion") != "success":
            continue
        if item.get("head_sha") != tested_merge_sha:
            continue
        expected_app_id = required_apps.get(name)
        if expected_app_id is not None and app_id != expected_app_id:
            continue
        candidates[name] = CIRun(f"check-run:merge:{run_id}", name, "success", tested_merge_sha)
    latest_statuses: dict[str, Mapping[str, Any]] = {}
    for item in merge_statuses:
        if not isinstance(item, Mapping):
            raise VerdictStoreError("GitHub returned a malformed legacy CI status")
        name = item.get("context")
        if isinstance(name, str) and name not in latest_statuses:
            # GitHub returns commit statuses newest-first. Only the first status
            # for a context is authoritative; older successes cannot revive a
            # context whose latest state is failure or pending.
            latest_statuses[name] = item
    for name, item in latest_statuses.items():
        if name not in required or item.get("state") != "success":
            continue
        if required_apps.get(name) is not None:
            continue
        if item.get("sha") != tested_merge_sha:
            continue
        # Any observed Check Run for the required context is authoritative,
        # including a non-successful or malformed run that produced no candidate.
        if name not in observed_check_contexts:
            candidates[name] = CIRun(_status_run_id(item), name, "success", tested_merge_sha)
    if not merge_checks and not merge_statuses:
        ci_evidence_sha = head_sha
        head_checks = _gh_paginated_items(
            f"repos/{canonical}/commits/{head_sha}/check-runs", "check_runs"
        )
        by_name: dict[str, list[Mapping[str, Any]]] = {name: [] for name in required}
        for item in head_checks:
            if not isinstance(item, Mapping):
                raise VerdictStoreError("GitHub returned a malformed head check run")
            if item.get("name") in by_name:
                by_name[str(item["name"])].append(item)
        for name in sorted(required):
            expected_app_id = required_apps.get(name)
            evidence = by_name[name]
            if expected_app_id is None or len(evidence) != 1:
                continue
            candidate = _head_actions_candidate(
                canonical, evidence[0], name=name, expected_app_id=expected_app_id,
                pr=pr, base_sha=base_sha, head_sha=head_sha,
            )
            if candidate is not None:
                candidates[name] = candidate
    final_live_base = _gh_json(f"repos/{canonical}/git/ref/heads/{base_ref}")
    final_live_base_sha = ((final_live_base or {}).get("object") or {}).get("sha")
    if final_live_base_sha != base_sha:
        raise VerdictStoreError("protected base branch moved during identity resolution")
    try:
        return manifest.bind_identity(
            canonical_repo=canonical,
            pr_number=pr,
            trusted_task_id=task_id or f"pr-{pr}",
            base_sha=base_sha,
            head_sha=head_sha,
            tested_merge_sha=tested_merge_sha,
            ci_evidence_sha=ci_evidence_sha,
            runs=tuple(candidates.values()),
        )
    except (IdentityError, PolicyError) as exc:
        raise VerdictStoreError("branch policy and exact CI cannot form a trusted identity") from exc


def cmd_write(args: argparse.Namespace) -> int:
    # SECURITY: the CLI write path is SHADOW-ONLY by construction. The
    # caller-supplied --identity JSON is only structurally validated, so a CLI
    # write can never be trusted as the validator itself; record_verdict()
    # stamps shadow=True unconditionally and no flag on this command can
    # change that. Merge-eligible (non-shadow) verdicts exist only via the
    # validator's fenced review flow. Say so loudly, every time.
    print(
        "NOTICE: CLI-written verdicts are ALWAYS shadow (never merge-eligible). "
        "A merge-eligible verdict can only be finalized by the validator's own "
        "fenced review flow (validate_pr.py).",
        file=sys.stderr,
    )
    try:
        identity = TrustedMergeIdentity.from_record(json.loads(Path(args.identity).read_text(encoding="utf-8")))
        record = {
            "verdict": args.verdict,
            "tier": args.tier,
            "task_id": args.task,
            "head_sha": identity.head_sha,
            "expected_repo": args.expected_repo or identity.canonical_repo,
            "model_used": args.model,
            "findings": json.loads(Path(args.findings).read_text(encoding="utf-8")) if args.findings else [],
            # Overwritten by finalize_shadow_review(): CLI writes are stamped
            # shadow=True unconditionally (see record_verdict docstring).
            "shadow": True,
            "ts": _now_iso(),
        }
        kept, immutable = record_verdict(args.repo, args.pr, record, identity=identity)
    except (OSError, ValueError, VerdictStoreError, IdentityError) as exc:
        print(f"verdict refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(kept, indent=2))
    return 1 if immutable or kept["verdict"] != "PASS" else 0


def cmd_get(args: argparse.Namespace) -> int:
    verdict = verdict_for(args.repo, args.pr, head_sha=args.head or "")
    if verdict is None:
        print("{}")
        return 1
    print(json.dumps(verdict, indent=2))
    allowed, _ = is_pass_fresh(args.repo, args.pr, args.head or "")
    return 0 if allowed else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Fenced SQLite validator verdict ledger.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    write = sub.add_parser("write")
    write.add_argument("--repo", required=True)
    write.add_argument("--pr", required=True, type=int)
    write.add_argument("--identity", required=True, help="JSON TrustedMergeIdentity record")
    write.add_argument("--task", default="")
    write.add_argument("--expected-repo", default="")
    write.add_argument("--verdict", required=True, choices=["PASS", "BLOCK"])
    write.add_argument("--tier", required=True, choices=["low", "medium", "high"])
    write.add_argument("--model", default="")
    write.add_argument("--findings")
    write.set_defaults(fn=cmd_write)
    get = sub.add_parser("get")
    get.add_argument("--repo", required=True)
    get.add_argument("--pr", required=True, type=int)
    get.add_argument("--head", default="")
    get.set_defaults(fn=cmd_get)
    parsed = parser.parse_args()
    raise SystemExit(parsed.fn(parsed))


if __name__ == "__main__":
    main()
