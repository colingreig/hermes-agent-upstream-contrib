"""Strict, content-addressed CI-policy manifests for the merge boundary."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

if __package__:
    from .identity import IdentityError, TrustedMergeIdentity, canonicalize_repo
else:
    from identity import IdentityError, TrustedMergeIdentity, canonicalize_repo


class PolicyError(ValueError):
    """A policy or selected CI evidence is incomplete or permissive."""


_SCHEMA_VERSION = 1
_FIELDS = frozenset({"schema_version", "repositories", "required_checks"})
_CHECK = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")


def _check(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not _CHECK.fullmatch(value):
        raise PolicyError("required check ids must be non-empty trimmed printable strings")
    return value


def _run(value: object) -> str:
    if not isinstance(value, str) or not _RUN.fullmatch(value):
        raise PolicyError("CI run ids must be stable opaque ids")
    return value


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PolicyError(f"duplicate policy-manifest key: {key!r}")
        out[key] = value
    return out


def _full_sha(value: object) -> str:
    """Use identity validation rather than duplicate its SHA grammar."""
    try:
        return TrustedMergeIdentity(
            canonical_repo="github/policy-validation", pr_number=1,
            trusted_task_id="policy", base_sha=value, head_sha=value,
            tested_merge_sha=value, ci_policy_id="policy", ci_run_ids=("policy",),
        ).tested_merge_sha
    except IdentityError as exc:
        raise PolicyError("tested_merge_sha must be a full lowercase object id") from exc


@dataclass(frozen=True, slots=True)
class CIRun:
    """One selected required CI result, bound to its immutable evidence SHA."""

    run_id: str
    check_id: str
    conclusion: str
    ci_evidence_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _run(self.run_id))
        object.__setattr__(self, "check_id", _check(self.check_id))
        # There is deliberately no config field that can admit neutral,
        # skipped, cancelled, or action_required status conclusions.
        if self.conclusion != "success":
            raise PolicyError("only a literal CI conclusion of 'success' is accepted")
        object.__setattr__(self, "ci_evidence_sha", _full_sha(self.ci_evidence_sha))


@dataclass(frozen=True, slots=True)
class CIPolicyManifest:
    """The complete required-check policy; no defaults and no exclusions."""

    schema_version: int
    repositories: tuple[str, ...]
    required_checks: tuple[str, ...]
    policy_id: str

    def require_repo(self, repository: str) -> str:
        canonical = canonicalize_repo(repository)
        if canonical not in self.repositories:
            raise PolicyError(f"repository {canonical!r} is not explicitly allowlisted")
        return canonical

    def successful_run_ids(
        self, *, repository: str, tested_merge_sha: str,
        ci_evidence_sha: str | None = None, runs: Iterable[CIRun]
    ) -> tuple[str, ...]:
        """Return run IDs only if every required check is exact-success.

        The evidence is intentionally exact rather than "at least one passed":
        a caller cannot omit a failed/unknown required check or substitute a
        stale run.  Run IDs returned here must be copied into the identity.
        """
        self.require_repo(repository)
        _full_sha(tested_merge_sha)
        evidence_sha = _full_sha(ci_evidence_sha or tested_merge_sha)
        selected = tuple(runs)
        if len(selected) != len(self.required_checks):
            raise PolicyError("CI evidence must include exactly one run per required check")
        by_check: dict[str, CIRun] = {}
        seen_ids: set[str] = set()
        for run in selected:
            if not isinstance(run, CIRun):
                raise PolicyError("CI evidence must contain CIRun values")
            if run.check_id not in self.required_checks:
                raise PolicyError(f"CI evidence contains unrequired check {run.check_id!r}")
            if run.check_id in by_check or run.run_id in seen_ids:
                raise PolicyError("CI evidence contains duplicate checks or run ids")
            if run.conclusion != "success" or run.ci_evidence_sha != evidence_sha:
                raise PolicyError("required CI run is not an exact successful evidence-SHA run")
            by_check[run.check_id] = run
            seen_ids.add(run.run_id)
        if set(by_check) != set(self.required_checks):
            raise PolicyError("a required CI check has no accepted run")
        return tuple(by_check[check].run_id for check in self.required_checks)

    def bind_identity(
        self, *, canonical_repo: str, pr_number: int, trusted_task_id: str,
        base_sha: str, head_sha: str, tested_merge_sha: str,
        ci_evidence_sha: str | None = None, runs: Iterable[CIRun],
    ) -> TrustedMergeIdentity:
        return TrustedMergeIdentity(
            canonical_repo=canonical_repo, pr_number=pr_number, trusted_task_id=trusted_task_id,
            base_sha=base_sha, head_sha=head_sha, tested_merge_sha=tested_merge_sha,
            ci_policy_id=self.policy_id,
            ci_run_ids=self.successful_run_ids(
                repository=canonical_repo, tested_merge_sha=tested_merge_sha,
                ci_evidence_sha=ci_evidence_sha, runs=runs,
            ),
        )

    def assert_identity(self, identity: TrustedMergeIdentity) -> None:
        if not isinstance(identity, TrustedMergeIdentity):
            raise PolicyError("expected TrustedMergeIdentity")
        self.require_repo(identity.canonical_repo)
        if identity.ci_policy_id != self.policy_id:
            raise PolicyError("identity belongs to a different policy manifest")


def parse_policy_manifest(raw: str | bytes) -> CIPolicyManifest:
    """Parse JSON only, rejecting duplicate keys, drift, and weak policies.

    A permissive YAML parser or ignored future fields could make different
    workers choose a different policy.  The manifest therefore has exactly
    three fields and a content-addressed policy id derived from all of them.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyError("policy manifest must be UTF-8 JSON") from exc
    if not isinstance(raw, str):
        raise PolicyError("policy manifest must be JSON text")
    try:
        data = json.loads(raw, object_pairs_hook=_pairs_no_duplicates)
    except PolicyError:
        raise
    except (ValueError, TypeError) as exc:
        raise PolicyError("policy manifest is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise PolicyError("policy manifest has unknown or missing fields")
    version = data["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != _SCHEMA_VERSION:
        raise PolicyError(f"unsupported policy schema_version: {version!r}")
    repos_raw = data["repositories"]
    if not isinstance(repos_raw, list) or not repos_raw:
        raise PolicyError("repositories must be a non-empty array")
    try:
        repos = tuple(sorted(canonicalize_repo(repo) for repo in repos_raw))
    except IdentityError as exc:
        raise PolicyError("repositories must be explicit canonical GitHub repositories") from exc
    if len(set(repos)) != len(repos):
        raise PolicyError("repositories must not contain duplicates")
    checks_raw = data["required_checks"]
    if not isinstance(checks_raw, list) or not checks_raw:
        raise PolicyError("required_checks must be a non-empty array")
    checks = tuple(_check(check) for check in checks_raw)
    if len(set(checks)) != len(checks):
        raise PolicyError("required_checks must not contain duplicates")
    normalized = {"schema_version": version, "repositories": list(repos), "required_checks": list(checks)}
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return CIPolicyManifest(version, repos, checks, "sha256:" + hashlib.sha256(payload).hexdigest())


def load_policy_manifest(path: str | Path) -> CIPolicyManifest:
    manifest = Path(path)
    try:
        if not manifest.is_file():
            raise PolicyError("policy manifest path is not a regular file")
        return parse_policy_manifest(manifest.read_bytes())
    except OSError as exc:
        raise PolicyError(f"cannot read policy manifest: {manifest}") from exc
