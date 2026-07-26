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
    # This deployment performs observation only.  A future activation must be a
    # reviewed change that supplies a non-shadow trusted executor.
    cleaned["shadow"] = True
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


def finalize_shadow_review(session: ShadowReviewSession, record: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Commit one immutable verdict through the session's active fencing lease."""
    if session.existing is not None:
        existing = _finalization_record(session.existing)
        if existing is None:
            raise VerdictStoreError("existing finalization is malformed")
        # Every terminal verdict is immutable for its identity, not just BLOCK.
        # A new review must bind a new immutable candidate/head.
        return existing, True
    if session.lease is None:
        raise VerdictStoreError("shadow review session has no live fencing lease")
    cleaned = _validate_record(session.identity, record)
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
    """
    canonical = _canonical_repo(repo)
    if identity.canonical_repo != canonical or identity.pr_number != int(pr):
        raise VerdictStoreError("verdict subject does not match immutable identity")
    owned = session is None
    active = session or begin_shadow_review(identity, path=path)
    try:
        return finalize_shadow_review(active, record)
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
            return f"status:{field}:{token}"
    try:
        canonical = json.dumps(dict(status), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise VerdictStoreError("legacy CI status cannot form stable identity evidence") from exc
    return "status:sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_shadow_identity(repo: str, pr: int, task_id: str = "", expected_repo: str = "") -> TrustedMergeIdentity:
    """Build a strict identity from GitHub's PR, branch policy, and exact CI.

    The branch-protection required-check list is the policy source.  Every one
    must have an exact successful run on GitHub's immutable synthetic merge; an
    inaccessible protection rule, an excluded check, or a stale/head-only run is
    a hard failure rather than a permissive best effort.
    """
    canonical = _canonical_repo(repo)
    if expected_repo and _canonical_repo(expected_repo) != canonical:
        raise VerdictStoreError("resolved expected repo differs from validator repository")
    if not isinstance(pr, int) or isinstance(pr, bool) or pr <= 0:
        raise VerdictStoreError("PR number must be a positive integer")
    details = _gh_json(f"repos/{canonical}/pulls/{pr}")
    base = (details.get("base") or {})
    head = (details.get("head") or {})
    base_sha, head_sha = base.get("sha"), head.get("sha")
    tested_merge_sha = details.get("merge_commit_sha")
    base_ref = base.get("ref")
    if not all(isinstance(value, str) and len(value) in (40, 64) for value in (base_sha, head_sha, tested_merge_sha)):
        raise VerdictStoreError("GitHub PR lacks immutable base, head, or synthetic merge SHA")
    if not isinstance(base_ref, str) or not base_ref:
        raise VerdictStoreError("GitHub PR lacks its protected base branch")
    commit = _gh_json(f"repos/{canonical}/git/commits/{tested_merge_sha}")
    parents = tuple((item or {}).get("sha") for item in (commit.get("parents") or []))
    if parents != (base_sha, head_sha):
        raise VerdictStoreError("synthetic merge does not have the exact reviewed base/head parents")

    protection = _gh_json(f"repos/{canonical}/branches/{base_ref}/protection/required_status_checks")
    contexts = protection.get("contexts") or []
    checks = protection.get("checks") or []
    required = {str(value) for value in contexts if isinstance(value, str) and value}
    required.update(str(item.get("context")) for item in checks if isinstance(item, dict) and item.get("context"))
    if not required:
        raise VerdictStoreError("protected base branch declares no required CI checks")
    manifest = parse_policy_manifest(json.dumps({
        "schema_version": 1,
        "repositories": [canonical],
        "required_checks": sorted(required),
    }))

    candidates: dict[str, CIRun] = {}
    check_runs = _gh_json(f"repos/{canonical}/commits/{tested_merge_sha}/check-runs")
    for item in check_runs.get("check_runs") or []:
        name, run_id = item.get("name"), item.get("id")
        if name not in required or item.get("status") != "completed" or item.get("conclusion") != "success":
            continue
        if item.get("head_sha") != tested_merge_sha:
            continue
        candidates[name] = CIRun(f"check-run:{run_id}", name, "success", tested_merge_sha)
    statuses = _gh_json(f"repos/{canonical}/commits/{tested_merge_sha}/status")
    for item in statuses.get("statuses") or []:
        if not isinstance(item, Mapping):
            raise VerdictStoreError("GitHub returned a malformed legacy CI status")
        name = item.get("context")
        if name not in required or item.get("state") != "success":
            continue
        if item.get("sha") != tested_merge_sha:
            continue
        candidate = CIRun(_status_run_id(item), name, "success", tested_merge_sha)
        # A check run takes precedence over a legacy status.  Multiple legacy
        # statuses for one required context are selected by immutable evidence
        # id, never the API's arbitrary list ordering.
        previous = candidates.get(name)
        if previous is None or (previous.run_id.startswith("status:") and candidate.run_id < previous.run_id):
            candidates[name] = candidate
    try:
        return manifest.bind_identity(
            canonical_repo=canonical,
            pr_number=pr,
            trusted_task_id=task_id or f"pr-{pr}",
            base_sha=base_sha,
            head_sha=head_sha,
            tested_merge_sha=tested_merge_sha,
            runs=tuple(candidates.values()),
        )
    except (IdentityError, PolicyError) as exc:
        raise VerdictStoreError("branch policy and exact CI cannot form a trusted identity") from exc


def cmd_write(args: argparse.Namespace) -> int:
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
