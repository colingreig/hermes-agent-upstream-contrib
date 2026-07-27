"""The immutable object identity on which one PR merge decision is based.

``repository + PR number`` alone is a mutable name.  A review is trustworthy
only for its exact task, base, head, tested merge, policy, and CI runs.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


class IdentityError(ValueError):
    """An identity value was ambiguous, mutable, or malformed."""


_REPO = re.compile(r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]{0,98})/(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,98})$")
_HTTPS = re.compile(r"^https://github\.com/(?P<repo>[^/?#]+/[^/?#]+?)(?:\.git)?/?$")
_SSH = re.compile(r"^git@github\.com:(?P<repo>[^\s]+?)(?:\.git)?$")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
_TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def canonicalize_repo(value: str) -> str:
    """Normalize only explicit GitHub owner/repo values to lowercase.

    Host aliases and arbitrary URLs are refused: choosing a repository is a
    trust decision, so this helper must not guess one.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise IdentityError("canonical_repo must be a non-empty trimmed string")
    match = _HTTPS.fullmatch(value) or _SSH.fullmatch(value)
    if match:
        value = match.group("repo")
    match = _REPO.fullmatch(value)
    if not match:
        raise IdentityError("canonical_repo must be an exact GitHub owner/repository")
    return f"{match.group('owner').lower()}/{match.group('name').lower()}"


def _sha(name: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise IdentityError(f"{name} must be a full lowercase 40- or 64-hex object id")
    return value


def _opaque(name: str, value: object) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise IdentityError(f"{name} must be a stable opaque id")
    return value


@dataclass(frozen=True, slots=True)
class TrustedMergeIdentity:
    """Every immutable fact that authorizes exactly one terminal verdict.

    ``ci_policy_id`` is a content-addressed value from ``policy.py`` and
    ``ci_run_ids`` are the exact successful runs selected under it.  The
    dataclass is frozen and every sequence is converted to a tuple, so callers
    cannot mutate an identity after it has been leased or persisted.
    """

    canonical_repo: str
    pr_number: int
    trusted_task_id: str
    base_sha: str
    head_sha: str
    tested_merge_sha: str
    ci_policy_id: str
    ci_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical_repo", canonicalize_repo(self.canonical_repo))
        if isinstance(self.pr_number, bool) or not isinstance(self.pr_number, int) or self.pr_number <= 0:
            raise IdentityError("pr_number must be a positive integer")
        if not isinstance(self.trusted_task_id, str) or not _TASK.fullmatch(self.trusted_task_id):
            raise IdentityError("trusted_task_id must be an id, never a URL or display label")
        for name in ("base_sha", "head_sha", "tested_merge_sha"):
            object.__setattr__(self, name, _sha(name, getattr(self, name)))
        object.__setattr__(self, "ci_policy_id", _opaque("ci_policy_id", self.ci_policy_id))
        if isinstance(self.ci_run_ids, str):
            raise IdentityError("ci_run_ids must be a sequence, not a string")
        try:
            runs = tuple(_opaque("ci_run_id", value) for value in self.ci_run_ids)
        except TypeError as exc:
            raise IdentityError("ci_run_ids must be a sequence") from exc
        if not runs or len(runs) > 128 or len(set(runs)) != len(runs):
            raise IdentityError("ci_run_ids must be non-empty, bounded, and unique")
        object.__setattr__(self, "ci_run_ids", tuple(sorted(runs)))

    @property
    def key(self) -> tuple[str, int]:
        """The mutable PR subject whose lease token sequence is monotonic."""
        return self.canonical_repo, self.pr_number

    @property
    def candidate_key(self) -> str:
        """Compatibility key for the sandbox runner's candidate-bound lease."""
        return ":".join((self.canonical_repo, str(self.pr_number), self.base_sha, self.head_sha, self.tested_merge_sha))

    def to_record(self) -> dict[str, Any]:
        return {
            "canonical_repo": self.canonical_repo, "pr_number": self.pr_number,
            "trusted_task_id": self.trusted_task_id, "base_sha": self.base_sha,
            "head_sha": self.head_sha, "tested_merge_sha": self.tested_merge_sha,
            "ci_policy_id": self.ci_policy_id, "ci_run_ids": list(self.ci_run_ids),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "TrustedMergeIdentity":
        required = {
            "canonical_repo", "pr_number", "trusted_task_id", "base_sha", "head_sha",
            "tested_merge_sha", "ci_policy_id", "ci_run_ids",
        }
        if not isinstance(record, Mapping) or set(record) != required:
            raise IdentityError("identity record has unknown or missing fields")
        return cls(**dict(record))

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.to_record(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("ascii")).hexdigest()
