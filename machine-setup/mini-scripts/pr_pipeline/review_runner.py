#!/usr/bin/env python3
"""Exact-candidate review execution for the PR reconciler.

The reconciler owns discovery, test selection, and the fenced verdict ledger.
This module owns only the unsafe boundary between those trusted controls and
PR-controlled code.  Consequently, it accepts a pre-built argv from a trusted
command policy, checks out exactly the reviewed synthetic merge, and executes
only through :mod:`sandbox`.  Both checkout and execution are shadow-only by
default.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

if __package__:
    from .sandbox import (
        CandidateCheckoutRunner,
        CandidateIdentity,
        CandidateValidationError,
        SandboxResult,
        SandboxRunner,
        coerce_candidate,
        quote_evidence,
    )
else:
    from sandbox import (
        CandidateCheckoutRunner,
        CandidateIdentity,
        CandidateValidationError,
        SandboxResult,
        SandboxRunner,
        coerce_candidate,
        quote_evidence,
    )


@dataclass(frozen=True)
class ReviewLease:
    """Fenced ledger lease supplied by the reconciler's core state module."""

    candidate_key: str
    fence_token: str

    def __post_init__(self) -> None:
        if not self.candidate_key or not self.fence_token:
            raise ValueError("a review lease needs a candidate key and fence token")


@dataclass(frozen=True)
class ReviewResult:
    """A durable-review-ready result; output remains JSON-quoted evidence."""

    candidate: CandidateIdentity
    status: str
    sandbox: SandboxResult | None = None
    reason_evidence: str = '""'
    verdict_recorded: bool = False

    @property
    def passed(self) -> bool:
        return self.status == "passed" and self.sandbox is not None and self.sandbox.passed and self.verdict_recorded


class LockedVerdictStore(Protocol):
    """The core ``TrustStore`` contract; an unlocked JSON ledger is insufficient."""

    def finalize(self, lease: object, verdict: str, evidence: Mapping[str, object]) -> object:
        """Atomically finalize only the currently-held review lease."""


class TrustedCommandPolicy(Protocol):
    """Core-owned test selection.  PR metadata must never choose this argv."""

    def command_for(self, candidate: CandidateIdentity) -> Sequence[str]:
        """Return an argv sequence, not a shell string."""


def candidate_key(candidate: CandidateIdentity) -> str:
    """Stable candidate tuple used by the fenced core ledger."""
    return ":".join((candidate.repository, str(candidate.pull_number), candidate.base_sha, candidate.head_sha, candidate.tested_merge_sha))


def resolve_core_contracts() -> tuple[object | None, object | None]:
    """Optionally discover the core contract modules without making imports fatal.

    Unit tests and this safety slice are useful before the reconciler's ledger
    lands.  Production wiring calls this helper, obtains the core policy/store,
    and passes them explicitly to :class:`ReviewRunner`.
    """
    contracts = ledger = None
    for module_name, target in (("pr_pipeline.policy", "contracts"), ("pr_pipeline.store", "ledger")):
        try:
            module = importlib.import_module(module_name)
        except (ImportError, ModuleNotFoundError):
            continue
        if target == "contracts":
            contracts = module
        else:
            ledger = module
    return contracts, ledger


class ReviewRunner:
    """Run a trusted review command against exactly one candidate merge.

    A real execution requires all of: ``execute=True``, a live fenced lease,
    a locked verdict store, a trusted command policy, explicit checkout network
    ingress, and an available default-deny sandbox backend.  Failure in any
    prerequisite returns a blocked result and records no verdict.
    """

    def __init__(
        self,
        command_policy: TrustedCommandPolicy,
        *,
        checkout_runner: CandidateCheckoutRunner | None = None,
        sandbox_runner: SandboxRunner | None = None,
        verdict_store: LockedVerdictStore | None = None,
    ):
        self.command_policy = command_policy
        self.checkout_runner = checkout_runner or CandidateCheckoutRunner()
        self.sandbox_runner = sandbox_runner or SandboxRunner()
        self.verdict_store = verdict_store

    def run(
        self,
        candidate: CandidateIdentity | Mapping[str, Any] | object,
        *,
        lease: ReviewLease | None = None,
        execute: bool = False,
    ) -> ReviewResult:
        identity = coerce_candidate(candidate)
        if not execute:
            return ReviewResult(identity, "shadow", reason_evidence=quote_evidence("review execution disabled"))
        if self.verdict_store is None:
            return ReviewResult(identity, "blocked", reason_evidence=quote_evidence("locked verdict store is required"))
        if lease is None or _lease_candidate_key(lease) != candidate_key(identity):
            return ReviewResult(identity, "blocked", reason_evidence=quote_evidence("live candidate-bound review lease is required"))
        try:
            command = tuple(self.command_policy.command_for(identity))
        except Exception as exc:
            return ReviewResult(identity, "blocked", reason_evidence=quote_evidence(f"trusted command policy failed: {exc}"))

        # This is intentionally the only explicit network ingress.  The test
        # itself is bwrap-isolated with an unshared (empty) network namespace.
        checkout = self.checkout_runner.materialize(identity, execute=True, allow_network_fetch=True)
        if not checkout.ready:
            return ReviewResult(identity, "blocked", reason_evidence=checkout.reason_evidence)
        with checkout:
            sandbox_result = self.sandbox_runner.run(command, checkout, execute=True)
        if sandbox_result.status not in {"passed", "failed", "timed_out", "output_limited"}:
            return ReviewResult(identity, sandbox_result.status, sandbox=sandbox_result, reason_evidence=sandbox_result.reason_evidence)

        provisional = ReviewResult(identity, sandbox_result.status, sandbox=sandbox_result)
        evidence: dict[str, object] = {
            "candidate": identity.evidence(),
            "status": sandbox_result.status,
            "returncode": sandbox_result.returncode,
            "stdout": sandbox_result.stdout_evidence,
            "stderr": sandbox_result.stderr_evidence,
            "reason": sandbox_result.reason_evidence,
            "elapsed_seconds": sandbox_result.elapsed_seconds,
        }
        try:
            finalization = self.verdict_store.finalize(lease, sandbox_result.status, evidence)
            # An unrecognised finalization shape is never evidence of a durable
            # verdict.  Core's Finalization contract must opt in explicitly.
            recorded = bool(getattr(finalization, "accepted", False))
        except Exception as exc:
            return ReviewResult(identity, "blocked", sandbox=sandbox_result, reason_evidence=quote_evidence(f"locked verdict write failed: {exc}"))
        if not recorded:
            return ReviewResult(identity, "blocked", sandbox=sandbox_result, reason_evidence=quote_evidence("review lease was stale or verdict write was rejected"))
        return ReviewResult(identity, sandbox_result.status, sandbox=sandbox_result, verdict_recorded=True)


def _lease_candidate_key(lease: object) -> str:
    """Accept the core Lease only when it exposes a candidate-bound key.

    Production ``TrustStore`` wiring may put the immutable identity directly on
    its Lease; the small ReviewLease adapter keeps this module independently
    testable.  Unknown lease shapes are intentionally rejected.
    """
    direct = getattr(lease, "candidate_key", None)
    if isinstance(direct, str):
        return direct
    identity = getattr(lease, "identity", None)
    if identity is not None:
        try:
            return candidate_key(coerce_candidate(identity))
        except CandidateValidationError:
            return ""
    return ""
