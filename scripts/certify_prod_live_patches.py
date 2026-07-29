#!/usr/bin/env python3
"""Certify exact-SHA promotion from main to prod-live-patches.

This helper is deliberately pure with respect to GitHub and git.  The workflow
collects read-only API evidence, then this module validates the evidence and
emits deterministic certificates and content-addressed receipts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


CERTIFICATE_SCHEMA = "prod_live_patches_certificate/v1"
RECEIPT_SCHEMA = "prod_live_patches_promotion_receipt/v1"
FREEZE_SCHEMA = "prod_live_patches_freeze/v1"
AUTHORITY = ".github/workflows/sync-prod-live-patches.yml"
GOVERNING_WORKFLOW = "CI"
GOVERNING_WORKFLOW_PATH = ".github/workflows/ci.yml"
REQUIRED_AGGREGATE_JOB = "All required checks pass"
PROMOTION_RECEIPT_REF_PREFIX = "refs/tags/prod-live-patches-promotion-"
REQUIRED_JOB_PREFIXES = (
    "Detect affected areas",
    "Python tests",
    "Python lints",
    "JS & TS checks",
    "Docs Site",
    "Check contributors",
    "Check uv.lock",
    "Lint Docker scripts",
    "OSV scan",
)
# These jobs are intentionally conditional inside otherwise-required reusable
# workflows.  Every other member of a required group must be terminal success.
ALLOWED_SKIPPED_REQUIRED_JOBS = {
    "Python lints / ruff + ty diff",
    "Python lints / CI-sensitive file review",
}


class CertificationError(ValueError):
    """Evidence is incomplete, ambiguous, stale, or does not match exactly."""


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _mappings(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise CertificationError(f"{field} is required")
    return str(value)


def _full_sha(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) not in (40, 64) or any(char not in "0123456789abcdef" for char in text):
        raise CertificationError(f"{field} must be a full lowercase object id")
    return text


def _parse_iso(value: object, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CertificationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise CertificationError(f"{field} must include a timezone")
    return text


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _content_id(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def validate_freeze(value: object) -> dict[str, Any]:
    freeze = _mapping(value)
    if freeze.get("schema") != FREEZE_SCHEMA:
        raise CertificationError(f"freeze.schema must equal {FREEZE_SCHEMA}")
    if not isinstance(freeze.get("frozen"), bool):
        raise CertificationError("freeze.frozen must be a boolean")
    normalized = {
        "schema": FREEZE_SCHEMA,
        "frozen": freeze["frozen"],
        "actor": _required_text(freeze.get("actor"), "freeze.actor"),
        "reason": _required_text(freeze.get("reason"), "freeze.reason"),
        "changed_at": _parse_iso(freeze.get("changed_at"), "freeze.changed_at"),
    }
    if normalized["frozen"]:
        raise CertificationError(
            f"promotion is frozen by {normalized['actor']}: {normalized['reason']}"
        )
    return normalized


def _run_order(run: dict[str, Any]) -> tuple[int, int, int]:
    def integer(field: str) -> int:
        value = run.get(field, 0)
        return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else 0

    return (integer("run_number"), integer("run_attempt"), integer("id"))


def _exact_runs(evidence: dict[str, Any], head_sha: str) -> list[dict[str, Any]]:
    runs = _mappings(_mapping(evidence.get("workflow_runs")).get("workflow_runs"))
    return [
        run
        for run in runs
        if run.get("name") == GOVERNING_WORKFLOW
        and run.get("path") == GOVERNING_WORKFLOW_PATH
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
        and run.get("head_sha") == head_sha
    ]


def certify(evidence: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence and return a deterministic exact-head certificate."""
    if not isinstance(evidence, dict):
        raise CertificationError("evidence must be an object")
    repository = _required_text(evidence.get("repository"), "repository")
    current_main_sha = _full_sha(evidence.get("current_main_sha"), "current_main_sha")
    freeze = validate_freeze(evidence.get("freeze"))

    exact_runs = _exact_runs(evidence, current_main_sha)
    if not exact_runs:
        all_runs = _mappings(_mapping(evidence.get("workflow_runs")).get("workflow_runs"))
        observed_shas = sorted(
            {
                str(run.get("head_sha"))
                for run in all_runs
                if run.get("name") == GOVERNING_WORKFLOW and run.get("head_sha")
            }
        )
        raise CertificationError(
            "no exact current-main push CI run; "
            f"current={current_main_sha} observed={observed_shas}"
        )
    run = max(exact_runs, key=_run_order)
    run_id = _required_text(run.get("id"), "ci.run_id")
    run_number = _required_text(run.get("run_number"), "ci.run_number")
    run_attempt = _required_text(run.get("run_attempt"), "ci.run_attempt")
    trigger_run_id = evidence.get("trigger_run_id")
    if trigger_run_id not in (None, "") and str(trigger_run_id) != run_id:
        raise CertificationError(
            f"workflow_run trigger {trigger_run_id} is stale; latest exact run is {run_id}"
        )
    if run.get("status") != "completed":
        raise CertificationError(f"exact CI run status is {run.get('status')!r}, not completed")
    if run.get("conclusion") != "success":
        raise CertificationError(f"exact CI run conclusion is {run.get('conclusion')!r}, not success")

    jobs_payload = _mapping(evidence.get("jobs"))
    jobs = _mappings(jobs_payload.get("jobs"))
    required_jobs: list[dict[str, Any]] = []
    missing_groups: list[str] = []
    for prefix in REQUIRED_JOB_PREFIXES:
        matches = [
            job
            for job in jobs
            if job.get("name") == prefix
            or (
                isinstance(job.get("name"), str)
                and job["name"].startswith(f"{prefix} / ")
            )
        ]
        if not matches:
            missing_groups.append(prefix)
        required_jobs.extend(matches)
    if missing_groups:
        raise CertificationError(
            f"required CI job groups are missing: {', '.join(missing_groups)}"
        )
    rejected_jobs = [
        str(job.get("name"))
        for job in required_jobs
        if not (
            job.get("status") == "completed"
            and (
                job.get("conclusion") == "success"
                or (
                    job.get("conclusion") == "skipped"
                    and job.get("name") in ALLOWED_SKIPPED_REQUIRED_JOBS
                )
            )
        )
    ]
    if rejected_jobs:
        raise CertificationError(
            "required CI jobs are not terminal success: "
            + ", ".join(sorted(rejected_jobs))
        )
    aggregate_jobs = [job for job in jobs if job.get("name") == REQUIRED_AGGREGATE_JOB]
    if len(aggregate_jobs) != 1:
        raise CertificationError(
            f"expected exactly one {REQUIRED_AGGREGATE_JOB!r} job, found {len(aggregate_jobs)}"
        )
    aggregate = aggregate_jobs[0]
    job_id = _required_text(aggregate.get("id"), "aggregate_job.id")
    job_run_id = _required_text(aggregate.get("run_id"), "aggregate_job.run_id")
    if job_run_id != run_id:
        raise CertificationError("aggregate job belongs to a different workflow run")
    if aggregate.get("status") != "completed":
        raise CertificationError(
            f"aggregate job status is {aggregate.get('status')!r}, not completed"
        )
    if aggregate.get("conclusion") != "success":
        raise CertificationError(
            f"aggregate job conclusion is {aggregate.get('conclusion')!r}, not success"
        )

    return {
        "schema": CERTIFICATE_SCHEMA,
        "authority": AUTHORITY,
        "repository": repository,
        "head_sha": current_main_sha,
        "freeze": freeze,
        "ci": {
            "workflow": GOVERNING_WORKFLOW,
            "workflow_path": GOVERNING_WORKFLOW_PATH,
            "run_id": run_id,
            "run_number": run_number,
            "run_attempt": run_attempt,
            "event": run.get("event"),
            "head_sha": run.get("head_sha"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "aggregate_job": {
                "id": job_id,
                "name": aggregate.get("name"),
                "status": aggregate.get("status"),
                "conclusion": aggregate.get("conclusion"),
            },
            "required_jobs": [
                {
                    "id": _required_text(job.get("id"), f"job[{job.get('name')}].id"),
                    "name": job.get("name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                }
                for job in sorted(required_jobs, key=lambda item: str(item.get("name")))
            ],
        },
    }


def validate_certificate(certificate: object) -> dict[str, Any]:
    value = _mapping(certificate)
    if value.get("schema") != CERTIFICATE_SCHEMA:
        raise CertificationError(f"certificate.schema must equal {CERTIFICATE_SCHEMA}")
    if value.get("authority") != AUTHORITY:
        raise CertificationError("certificate authority is not the promotion workflow")
    _full_sha(value.get("head_sha"), "certificate.head_sha")
    validate_freeze(value.get("freeze"))
    ci = _mapping(value.get("ci"))
    if ci.get("head_sha") != value.get("head_sha"):
        raise CertificationError("certificate CI head does not match certificate head")
    if ci.get("workflow") != GOVERNING_WORKFLOW or ci.get("workflow_path") != GOVERNING_WORKFLOW_PATH:
        raise CertificationError("certificate CI authority is not the governing workflow")
    if ci.get("event") != "push":
        raise CertificationError("certificate CI was not triggered by a push")
    _required_text(ci.get("run_id"), "certificate.ci.run_id")
    _required_text(ci.get("run_number"), "certificate.ci.run_number")
    _required_text(ci.get("run_attempt"), "certificate.ci.run_attempt")
    if ci.get("status") != "completed" or ci.get("conclusion") != "success":
        raise CertificationError("certificate CI is not terminal success")
    aggregate = _mapping(ci.get("aggregate_job"))
    if (
        aggregate.get("name") != REQUIRED_AGGREGATE_JOB
        or aggregate.get("status") != "completed"
        or aggregate.get("conclusion") != "success"
    ):
        raise CertificationError("certificate aggregate job is not successful")
    _required_text(aggregate.get("id"), "certificate.aggregate_job.id")
    required_jobs = _mappings(ci.get("required_jobs"))
    if not required_jobs:
        raise CertificationError("certificate required jobs are missing")
    if any(
        job.get("status") != "completed"
        or (
            job.get("conclusion") != "success"
            and not (
                job.get("conclusion") == "skipped"
                and job.get("name") in ALLOWED_SKIPPED_REQUIRED_JOBS
            )
        )
        for job in required_jobs
    ):
        raise CertificationError("certificate contains a non-success required job")
    return value


def promotion_receipt(
    certificate: dict[str, Any],
    *,
    from_sha: str,
    authority_run_id: str,
    authority_run_attempt: str,
) -> dict[str, Any]:
    certificate = validate_certificate(certificate)
    return {
        "schema": RECEIPT_SCHEMA,
        "authority": AUTHORITY,
        "authority_run_id": _required_text(authority_run_id, "authority_run_id"),
        "authority_run_attempt": _required_text(
            authority_run_attempt, "authority_run_attempt"
        ),
        "repository": _required_text(certificate.get("repository"), "repository"),
        "ref": "refs/heads/prod-live-patches",
        "receipt_ref_prefix": PROMOTION_RECEIPT_REF_PREFIX,
        "from_sha": _full_sha(from_sha, "from_sha"),
        "head_sha": _full_sha(certificate.get("head_sha"), "head_sha"),
        "certificate_id": _content_id(certificate),
        "ci": certificate["ci"],
        "freeze": certificate["freeze"],
    }


def validate_promotion_receipt(
    receipt: object,
    *,
    receipt_id: str,
    head_sha: str,
) -> dict[str, Any]:
    value = _mapping(receipt)
    if value.get("schema") != RECEIPT_SCHEMA:
        raise CertificationError(f"receipt.schema must equal {RECEIPT_SCHEMA}")
    if value.get("authority") != AUTHORITY:
        raise CertificationError("receipt authority is not the promotion workflow")
    if value.get("ref") != "refs/heads/prod-live-patches":
        raise CertificationError("receipt does not authorize prod-live-patches")
    if value.get("receipt_ref_prefix") != PROMOTION_RECEIPT_REF_PREFIX:
        raise CertificationError("receipt ref authority prefix is invalid")
    normalized_id = _full_sha(receipt_id, "receipt_id")
    if len(normalized_id) != 64:
        raise CertificationError("receipt_id must be a SHA-256 digest")
    expected_head = _full_sha(head_sha, "head_sha")
    if value.get("head_sha") != expected_head:
        raise CertificationError("receipt head does not match exact requested SHA")
    if _content_id(value) != normalized_id:
        raise CertificationError("receipt content does not match receipt_id")
    _full_sha(value.get("from_sha"), "receipt.from_sha")
    _required_text(value.get("authority_run_id"), "receipt.authority_run_id")
    _required_text(value.get("authority_run_attempt"), "receipt.authority_run_attempt")
    _required_text(value.get("certificate_id"), "receipt.certificate_id")
    validate_freeze(value.get("freeze"))
    return value


def write_certificate(evidence_path: Path, output_path: Path) -> str:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    certificate = certify(evidence)
    content = _canonical_bytes(certificate)
    _atomic_bytes(output_path, content)
    return hashlib.sha256(content).hexdigest()


def write_receipt(
    certificate_path: Path,
    output_dir: Path,
    *,
    from_sha: str,
    authority_run_id: str,
    authority_run_attempt: str,
) -> Path:
    certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    receipt = promotion_receipt(
        certificate,
        from_sha=from_sha,
        authority_run_id=authority_run_id,
        authority_run_attempt=authority_run_attempt,
    )
    content = _canonical_bytes(receipt)
    receipt_id = hashlib.sha256(content).hexdigest()
    path = output_dir / f"promotion-receipt-{receipt_id}.json"
    _atomic_bytes(path, content)
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    certify_parser = subparsers.add_parser("certify")
    certify_parser.add_argument("--evidence", type=Path, required=True)
    certify_parser.add_argument("--output", type=Path, required=True)
    receipt_parser = subparsers.add_parser("receipt")
    receipt_parser.add_argument("--certificate", type=Path, required=True)
    receipt_parser.add_argument("--output-dir", type=Path, required=True)
    receipt_parser.add_argument("--from-sha", required=True)
    receipt_parser.add_argument("--authority-run-id", required=True)
    receipt_parser.add_argument("--authority-run-attempt", required=True)
    verify_receipt_parser = subparsers.add_parser("verify-receipt")
    verify_receipt_parser.add_argument("--receipt", type=Path, required=True)
    verify_receipt_parser.add_argument("--receipt-id", required=True)
    verify_receipt_parser.add_argument("--head-sha", required=True)
    freeze_parser = subparsers.add_parser("validate-freeze")
    freeze_parser.add_argument("--input", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "certify":
            content_id = write_certificate(args.evidence, args.output)
            print(json.dumps({"certificate_id": content_id, "path": str(args.output)}))
        elif args.command == "receipt":
            path = write_receipt(
                args.certificate,
                args.output_dir,
                from_sha=args.from_sha,
                authority_run_id=args.authority_run_id,
                authority_run_attempt=args.authority_run_attempt,
            )
            print(str(path))
        elif args.command == "verify-receipt":
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            value = validate_promotion_receipt(
                receipt,
                receipt_id=args.receipt_id,
                head_sha=args.head_sha,
            )
            print(json.dumps({"head_sha": value["head_sha"], "receipt_id": args.receipt_id}))
        else:
            freeze = json.loads(args.input.read_text(encoding="utf-8"))
            print(json.dumps(validate_freeze(freeze), sort_keys=True))
    except (CertificationError, OSError, json.JSONDecodeError) as exc:
        print(f"certification failed: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
