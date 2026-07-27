#!/usr/bin/env python3
"""The one privileged, fail-closed exact-merge actor.

This module does not discover PRs, choose CI, or execute review code.  The
core reconciler supplies a ``TrustedMergeIdentity``, current GitHub snapshot,
fenced permit, and an explicitly configured service-account pusher.  The
actor then serializes the irreversible operation, rechecks the exact base,
head, and tested merge, and uses a *non-force* push with an exact lease on the
base ref.  It has no default mutation path.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

if __package__:
    from .sandbox import CandidateCheckoutRunner, CandidateIdentity, coerce_candidate, quote_evidence
    from .identity import TrustedMergeIdentity
else:
    # The trust-boundary unit surfaces intentionally load this module directly
    # from its file, with only the sibling sandbox module present.  Identity is
    # annotation-only here, so a flat direct import must not require the package
    # parent or duplicate the canonical identity module.
    from sandbox import CandidateCheckoutRunner, CandidateIdentity, coerce_candidate, quote_evidence
    TrustedMergeIdentity = Any


@dataclass(frozen=True)
class MergeSnapshot:
    """Trusted GitHub state read immediately before and after a merge attempt."""

    repository: str
    pull_number: int
    is_open: bool
    base_sha: str
    head_sha: str
    tested_merge_sha: str
    tested_merge_parents: tuple[str, str]
    ci_policy_id: str
    ci_run_ids: tuple[str, ...]
    required_checks_passed: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MergeSnapshot":
        try:
            parents = tuple(str(item) for item in value["tested_merge_parents"])
            if len(parents) != 2:
                raise ValueError("tested_merge_parents must contain exactly two parents")
            return cls(
                repository=str(value["repository"]),
                pull_number=int(value["pull_number"]),
                is_open=bool(value["is_open"]),
                base_sha=str(value["base_sha"]),
                head_sha=str(value["head_sha"]),
                tested_merge_sha=str(value["tested_merge_sha"]),
                tested_merge_parents=(parents[0], parents[1]),
                ci_policy_id=str(value["ci_policy_id"]),
                ci_run_ids=tuple(str(item) for item in value["ci_run_ids"]),
                required_checks_passed=bool(value["required_checks_passed"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("trusted merge snapshot has an invalid shape") from exc


class SnapshotProvider(Protocol):
    def snapshot_for(self, identity: object) -> MergeSnapshot | Mapping[str, Any]:
        """Return a fresh, authenticated GitHub snapshot for ``identity``."""


class MergePermit(Protocol):
    def __call__(self, identity: object, lease: object) -> bool:
        """Return true only for a live, passed, candidate-bound ledger lease."""


@dataclass(frozen=True)
class CasPushResult:
    success: bool
    stdout_evidence: str = '""'
    stderr_evidence: str = '""'
    reason_evidence: str = '""'


class CasPusher(Protocol):
    def push(self, candidate: CandidateIdentity, workspace: Path, *, execute: bool = False) -> CasPushResult:
        """Perform one authenticated non-force compare-and-swap base update."""


class NonForceCasPusher:
    """Service-account pusher with explicit non-force Git CAS semantics.

    The caller must inject its constrained service-account environment.  This
    class neither reads a user's global Git configuration nor discovers a
    credential helper.  The refspec intentionally has no leading ``+``;
    therefore Git refuses non-fast-forward updates even if a lease bug occurs.
    """

    def __init__(self, environment: Mapping[str, str], *, git_binary: str = "git", timeout_seconds: float = 60.0):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.environment = dict(environment)
        self.git_binary = git_binary
        self.timeout_seconds = timeout_seconds

    def command_for(self, candidate: CandidateIdentity, workspace: Path) -> tuple[str, ...]:
        branch_ref = f"refs/heads/{candidate.base_ref}"
        return (
            self.git_binary,
            "-C",
            str(workspace),
            "push",
            "--porcelain",
            f"--force-with-lease={branch_ref}:{candidate.base_sha}",
            "origin",
            f"{candidate.tested_merge_sha}:{branch_ref}",
        )

    def push(self, candidate: CandidateIdentity, workspace: Path, *, execute: bool = False) -> CasPushResult:
        if not execute:
            return CasPushResult(False, reason_evidence=quote_evidence("CAS push execution disabled"))
        command = self.command_for(candidate, workspace)
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CasPushResult(False, reason_evidence=quote_evidence(exc))
        return CasPushResult(
            completed.returncode == 0,
            stdout_evidence=quote_evidence(completed.stdout),
            stderr_evidence=quote_evidence(completed.stderr),
            reason_evidence=quote_evidence("" if completed.returncode == 0 else "non-force CAS push rejected"),
        )


@dataclass(frozen=True)
class MergeResult:
    candidate: CandidateIdentity
    status: str
    reason_evidence: str = '""'
    push: CasPushResult | None = None

    @property
    def merged(self) -> bool:
        return self.status == "merged" and self.push is not None and self.push.success


def _identity_value(identity: object, name: str, fallback: object = "") -> object:
    if isinstance(identity, Mapping):
        return identity.get(name, fallback)
    return getattr(identity, name, fallback)


def _snapshot_for(provider: SnapshotProvider, identity: object) -> MergeSnapshot:
    value = provider.snapshot_for(identity)
    return value if isinstance(value, MergeSnapshot) else MergeSnapshot.from_mapping(value)


def _matches_exact_candidate(snapshot: MergeSnapshot, candidate: CandidateIdentity, identity: object) -> bool:
    """Check every object that the review verdict and CI gate bind to."""
    expected_policy = str(_identity_value(identity, "ci_policy_id", ""))
    expected_runs = tuple(str(item) for item in (_identity_value(identity, "ci_run_ids", ()) or ()))
    snapshot_runs = tuple(sorted(str(item) for item in snapshot.ci_run_ids))
    return (
        snapshot.is_open
        and snapshot.repository == candidate.repository
        and snapshot.pull_number == candidate.pull_number
        and snapshot.base_sha == candidate.base_sha
        and snapshot.head_sha == candidate.head_sha
        and snapshot.tested_merge_sha == candidate.tested_merge_sha
        and snapshot.tested_merge_parents == (candidate.base_sha, candidate.head_sha)
        and snapshot.required_checks_passed
        and snapshot.ci_policy_id == expected_policy
        and snapshot_runs == expected_runs
    )


@contextmanager
def _exclusive_actor_lock(lock_path: Path):
    """Acquire the local single-actor fence without blocking a second actor."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another privileged merge actor is active") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


class MergeActor:
    """Serialize and execute an exact merge only after trusted revalidation."""

    def __init__(
        self,
        snapshot_provider: SnapshotProvider,
        merge_permit: MergePermit,
        *,
        pusher: CasPusher | None = None,
        checkout_runner: CandidateCheckoutRunner | None = None,
        lock_path: Path | None = None,
    ):
        self.snapshot_provider = snapshot_provider
        self.merge_permit = merge_permit
        self.pusher = pusher
        self.checkout_runner = checkout_runner or CandidateCheckoutRunner()
        self.lock_path = lock_path

    def merge(self, identity: TrustedMergeIdentity | CandidateIdentity | Mapping[str, Any] | object, *, lease: object | None = None, execute: bool = False) -> MergeResult:
        candidate = coerce_candidate(identity)
        if not execute:
            return MergeResult(candidate, "shadow", quote_evidence("merge execution disabled"))
        if lease is None:
            return MergeResult(candidate, "blocked", quote_evidence("a candidate-bound passed verdict lease is required"))
        if self.lock_path is None:
            return MergeResult(candidate, "blocked", quote_evidence("a single-actor lock path is required"))
        if self.pusher is None:
            return MergeResult(candidate, "blocked", quote_evidence("an explicit service-account CAS pusher is required"))

        try:
            with _exclusive_actor_lock(self.lock_path):
                if not bool(self.merge_permit(identity, lease)):
                    return MergeResult(candidate, "blocked", quote_evidence("merge permit is absent, stale, or not passed"))
                snapshot = _snapshot_for(self.snapshot_provider, identity)
                if not _matches_exact_candidate(snapshot, candidate, identity):
                    return MergeResult(candidate, "blocked", quote_evidence("base/head/tested-merge/CI snapshot no longer matches the reviewed candidate"))

                # No PR code is run here.  This second disposable checkout only
                # proves the pushed tested-merge object still has exact parents.
                checkout = self.checkout_runner.materialize(candidate, execute=True, allow_network_fetch=True)
                if not checkout.ready:
                    return MergeResult(candidate, "blocked", checkout.reason_evidence)
                with checkout:
                    assert checkout.workspace is not None
                    push = self.pusher.push(candidate, checkout.workspace, execute=True)
                if not push.success:
                    return MergeResult(candidate, "blocked", push.reason_evidence, push)

                # Confirm the base ref now names precisely the tested merge.
                post_merge = _snapshot_for(self.snapshot_provider, identity)
                if post_merge.base_sha != candidate.tested_merge_sha:
                    return MergeResult(candidate, "unknown", quote_evidence("CAS push returned success but post-merge base did not match tested merge"), push)
                return MergeResult(candidate, "merged", push=push)
        except (OSError, RuntimeError, ValueError) as exc:
            return MergeResult(candidate, "blocked", quote_evidence(exc))
