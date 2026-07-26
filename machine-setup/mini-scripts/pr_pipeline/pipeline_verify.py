#!/usr/bin/env python3
"""Fail-closed live verifier for the shadow-only Mini PR trust boundary.

This is intentionally independent of the broad historical patch verifier.  It
checks the manifest marker and the deployed files that make up this one
boundary, then executes only in-memory and temporary-directory proofs.  It
never contacts GitHub, reads credentials, checks out a PR, or invokes a merge.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


MARKER_NAME = ".pr_pipeline_deployment.json"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{7,64}\Z")
_MANAGED_ROOT = (
    "autonomous_merge.py", "ci_base_fix_exempt.json", "ci_health_watch.py",
    "hermes_validate_ops.py", "merge_guard.py", "pr_*.py", "review_poll_gate.py",
    "risk_classify.py", "slack_msg_builder.py", "validate_*.py", "validator_*.py",
)
_UNMANAGED_ROOT = frozenset({"validator_autonomy.py"})
_REQUIRED_FILES = frozenset({
    "autonomous_merge.py", "hermes_validate_ops.py", "merge_guard.py",
    "pr_wake_and_sweep.py", "validate_pr.py", "validator_verdict.py", "verify-hermes-patches.sh",
    "pr_pipeline/__init__.py", "pr_pipeline/identity.py", "pr_pipeline/merge_actor.py",
    "pr_pipeline/policy.py", "pr_pipeline/review_runner.py", "pr_pipeline/sandbox.py",
    "pr_pipeline/store.py", "pr_pipeline/pipeline_verify.py",
})


class VerificationError(RuntimeError):
    """Raised when the deployed boundary is incomplete, altered, or permissive."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise VerificationError("deployment marker contains an unsafe file path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise VerificationError("deployment marker contains an unsafe file path")
    return value


def _managed_root(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _MANAGED_ROOT)


def _load_marker(scripts_dir: Path) -> dict[str, Any]:
    marker_path = scripts_dir / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read deployment marker: {exc}") from exc
    if not isinstance(marker, dict):
        raise VerificationError("deployment marker must be an object")
    if marker.get("schema_version") != 1 or marker.get("deployment_mode") != "shadow":
        raise VerificationError("deployment marker must pin schema 1 and shadow mode")
    if not isinstance(marker.get("source_commit"), str) or not _COMMIT.fullmatch(marker["source_commit"]):
        raise VerificationError("deployment marker has no immutable source commit")
    if not isinstance(marker.get("manifest_sha256"), str) or not _SHA.fullmatch(marker["manifest_sha256"]):
        raise VerificationError("deployment marker has no manifest hash")
    files = marker.get("files")
    if not isinstance(files, dict) or not files:
        raise VerificationError("deployment marker has no file hashes")
    if list(files) != sorted(files):
        raise VerificationError("deployment marker file hashes are not canonical")
    for relative, digest in files.items():
        _safe_relative(relative)
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise VerificationError(f"deployment marker hash is invalid for {relative!r}")
    missing_contract = sorted(_REQUIRED_FILES.difference(files))
    if missing_contract:
        raise VerificationError(f"deployment marker omits trust-boundary files: {', '.join(missing_contract)}")
    return marker


def _verify_hash_parity(scripts_dir: Path, marker: dict[str, Any]) -> None:
    files = marker["files"]
    for relative, expected in files.items():
        deployed = scripts_dir / _safe_relative(relative)
        if not deployed.is_file() or deployed.is_symlink():
            raise VerificationError(f"deployed trust-boundary file is missing or unsafe: {relative}")
        actual = _sha256(deployed)
        if actual != expected:
            raise VerificationError(f"deployed trust-boundary file hash mismatch: {relative}")
    for candidate in scripts_dir.iterdir():
        if not candidate.is_file() or candidate.name == MARKER_NAME:
            continue
        if _managed_root(candidate.name) and candidate.name not in files and candidate.name not in _UNMANAGED_ROOT:
            raise VerificationError(f"unmanifested managed root entrypoint: {candidate.name}")
    package = scripts_dir / "pr_pipeline"
    if not package.is_dir() or package.is_symlink():
        raise VerificationError("deployed pr_pipeline package is missing or unsafe")
    for candidate in package.iterdir():
        relative = f"pr_pipeline/{candidate.name}"
        if candidate.is_file() and candidate.suffix == ".py" and relative not in files:
            raise VerificationError(f"unmanifested package module: {relative}")


def _run_behavioral_proofs(scripts_dir: Path) -> None:
    # Make the exact deployed package—not a release checkout or a developer
    # tree—the import authority for every proof below.
    sys.path.insert(0, str(scripts_dir))
    try:
        from pr_pipeline.identity import TrustedMergeIdentity
        from pr_pipeline.merge_actor import MergeActor
        from pr_pipeline.policy import CIRun, PolicyError, parse_policy_manifest
        from pr_pipeline.review_runner import ReviewRunner
        from pr_pipeline.sandbox import CandidateCheckoutRunner, CandidateIdentity, SandboxRunner
        from pr_pipeline.store import LeaseBusyError, TrustStore
        from pr_pipeline import validator_verdict
        import autonomous_merge

        sha_a, sha_b, sha_c = "a" * 40, "b" * 40, "c" * 40
        policy = parse_policy_manifest(json.dumps({
            "schema_version": 1, "repositories": ["acme/widget"], "required_checks": ["unit", "lint"],
        }))
        accepted = policy.successful_run_ids(
            repository="acme/widget", tested_merge_sha=sha_c,
            runs=(CIRun("run:unit", "unit", "success", sha_c), CIRun("run:lint", "lint", "success", sha_c)),
        )
        if accepted != ("run:unit", "run:lint"):
            raise VerificationError("required-CI policy did not bind exact successful runs")
        try:
            policy.successful_run_ids(repository="acme/widget", tested_merge_sha=sha_c, runs=())
        except PolicyError:
            pass
        else:
            raise VerificationError("required-CI policy accepted missing evidence")

        identity = TrustedMergeIdentity(
            canonical_repo="acme/widget", pr_number=7, trusted_task_id="86e2gh04e",
            base_sha=sha_a, head_sha=sha_b, tested_merge_sha=sha_c,
            ci_policy_id=policy.policy_id, ci_run_ids=accepted,
        )
        with tempfile.TemporaryDirectory(prefix="hermes-pr-pipeline-verify-") as temporary:
            ledger = Path(temporary) / "trust.sqlite3"
            store = TrustStore(ledger)
            connection = store._connect()
            try:
                mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            finally:
                connection.close()
            if mode != "wal":
                raise VerificationError("verdict authority is not SQLite WAL")
            lease = store.acquire(identity, holder="pipeline-verifier", ttl_s=30, now_ms=1_000)
            try:
                store.acquire(identity, holder="second-worker", ttl_s=30, now_ms=1_000)
            except LeaseBusyError:
                pass
            else:
                raise VerificationError("verdict authority did not fence an overlapping reviewer")
            final = store.finalize(lease, "BLOCK", {"proof": "verifier"}, now_ms=1_001)
            if final.verdict != "BLOCK" or store.get_finalization(identity) is None:
                raise VerificationError("SQLite verdict finalization is not durable")
        try:
            validator_verdict._save({"unsafe": True})
        except validator_verdict.VerdictStoreError:
            pass
        else:
            raise VerificationError("legacy JSON verdict mutation remains available")

        candidate = CandidateIdentity("acme/widget", 7, sha_a, sha_b, sha_c)
        shadow_checkout = CandidateCheckoutRunner().materialize(candidate, execute=False)
        blocked_checkout = CandidateCheckoutRunner().materialize(candidate, execute=True, allow_network_fetch=False)
        if shadow_checkout.status != "shadow" or blocked_checkout.status != "blocked":
            raise VerificationError("candidate checkout is not explicit/default-deny")
        sandbox = SandboxRunner().run(("/bin/true",), shadow_checkout, execute=True)
        if sandbox.status != "blocked":
            raise VerificationError("sandbox accepted a non-disposable checkout")
        shadow_review = ReviewRunner(object()).run(candidate, execute=False)
        if shadow_review.status != "shadow":
            raise VerificationError("review runner executes without explicit authorization")
        shadow_merge = MergeActor(object(), lambda *_args: False).merge(candidate, execute=False)
        if shadow_merge.status != "shadow" or not autonomous_merge._shadow():
            raise VerificationError("a deployed merge surface can escape shadow mode")
    finally:
        try:
            sys.path.remove(str(scripts_dir))
        except ValueError:
            pass


def verify(scripts_dir: Path) -> dict[str, Any]:
    scripts_dir = scripts_dir.expanduser().resolve()
    marker = _load_marker(scripts_dir)
    _verify_hash_parity(scripts_dir, marker)
    _run_behavioral_proofs(scripts_dir)
    return {
        "ok": True,
        "deployment_mode": marker["deployment_mode"],
        "manifest_sha256": marker["manifest_sha256"],
        "source_commit": marker["source_commit"],
        "checks": ["manifest-hash-parity", "sqlite-wal-fence", "json-verdict-retired", "required-ci-fail-closed", "sandbox-default-deny", "shadow-only-merge"],
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scripts-dir", type=Path, default=Path.home() / ".hermes" / "scripts")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.scripts_dir), sort_keys=True))
        return 0
    except (VerificationError, OSError, ValueError, sqlite3.Error, ImportError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
