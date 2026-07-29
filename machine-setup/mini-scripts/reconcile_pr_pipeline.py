#!/usr/bin/env python3
"""Install and verify the source-controlled Hermes Mini PR pipeline.

The Mini's ``~/.hermes/scripts`` directory is intentionally not a repository.
This tool turns the files in ``pr_pipeline/`` into the sole source of truth:
it resolves a deterministic file-and-hash manifest, installs only those paths,
and records the source commit and exact deployment hashes in a marker.  The
current trust-boundary snapshot is deliberately shadow-only; this tool never
calls a PR merge command.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = SCRIPT_DIR / "pr_pipeline" / "manifest.json"
MARKER_NAME = ".pr_pipeline_deployment.json"
CI_HEALTH_CRON_NAME = "ci-health-watch"
CI_HEALTH_CRON_SCHEDULE = {"kind": "cron", "expr": "*/5 * * * *", "display": "*/5 * * * *"}
CI_HEALTH_CRON_SCHEDULE_DISPLAY = "*/5 * * * *"
CI_HEALTH_CRON_MAX_SECONDS = 300
SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
REMOTE_HOST_RE = re.compile(r"[A-Za-z0-9_.@:-]+\Z")
REVIEW_GATE_ROOT_SHIM = Path("review_poll_gate.py")
REVIEW_GATE_PACKAGE_CLOSURE = (
    Path("pr_pipeline/__init__.py"),
    Path("pr_pipeline/autonomous_merge.py"),
    Path("pr_pipeline/pr_pipeline_event_driven.py"),
    Path("pr_pipeline/pr_pipeline_improvements.py"),
    Path("pr_pipeline/review_poll_gate.py"),
    Path("pr_pipeline/validator_verdict.py"),
)
RETIRED_REVIEW_GATE_FLAT_IMPLEMENTATIONS = frozenset(
    {
        "pr_pipeline_event_driven.py",
        "pr_pipeline_improvements.py",
        "review_poll_gate.py",
    }
)


class ManifestError(ValueError):
    """The deployment manifest is invalid or cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedFile:
    source: Path
    destination: Path
    sha256: str
    mode: int
    install: bool = True
    source_sha256: str | None = None
    local_patch_reason: str | None = None


@dataclass(frozen=True)
class ResolvedManifest:
    path: Path
    sha256: str
    files: tuple[ResolvedFile, ...]
    root_patterns: tuple[str, ...]
    unmanaged_root_exclusions: tuple[str, ...]
    package_destination: str


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ManifestError("manifest must be a schema_version=1 object")
    return data


def _safe_filename(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."}:
        raise ManifestError(f"{field} must be a plain filename")
    return value


def _safe_directory(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value or value in {".", ".."}:
        raise ManifestError(f"{field} must be a plain directory name")
    return value


def _safe_destination(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise ManifestError(f"{field} must be a safe relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManifestError(f"{field} must be a safe relative path")
    return path.as_posix()


def _safe_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ManifestError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _expected_local_patch_specs(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = data.get("expected_local_patches", {})
    if not isinstance(raw, dict):
        raise ManifestError("expected_local_patches must be an object")
    if list(raw) != sorted(raw):
        raise ManifestError("expected_local_patches must be sorted by destination")
    specs: dict[str, dict[str, str]] = {}
    for raw_destination, raw_spec in raw.items():
        destination = _safe_destination(raw_destination, field="expected_local_patches key")
        if not isinstance(raw_spec, dict):
            raise ManifestError(f"expected_local_patches[{destination}] must be an object")
        if raw_spec.get("sync") is not False:
            raise ManifestError(f"expected_local_patches[{destination}].sync must be false")
        reason = raw_spec.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ManifestError(f"expected_local_patches[{destination}].reason must be non-empty")
        source_sha256 = _safe_sha256(
            raw_spec.get("source_sha256"),
            field=f"expected_local_patches[{destination}].source_sha256",
        )
        deployed_sha256 = _safe_sha256(
            raw_spec.get("deployed_sha256"),
            field=f"expected_local_patches[{destination}].deployed_sha256",
        )
        if deployed_sha256 == source_sha256:
            raise ManifestError(f"expected_local_patches[{destination}] must differ from the source hash")
        specs[destination] = {
            "deployed_sha256": deployed_sha256,
            "reason": reason,
            "source_sha256": source_sha256,
        }
    return specs


def resolve_manifest(path: Path = DEFAULT_MANIFEST) -> ResolvedManifest:
    """Resolve the stable manifest shape into sorted files and runtime hashes."""
    path = path.resolve()
    data = _read_manifest(path)
    root = path.parent
    source_root = root.parent
    raw_source_root = data.get("source_root_entrypoints")
    if not isinstance(raw_source_root, list) or not raw_source_root:
        raise ManifestError("source_root_entrypoints must be a non-empty list")
    source_root_entrypoints = tuple(
        _safe_filename(name, field="source_root_entrypoints item") for name in raw_source_root
    )
    if tuple(sorted(source_root_entrypoints)) != source_root_entrypoints or len(set(source_root_entrypoints)) != len(source_root_entrypoints):
        raise ManifestError("source_root_entrypoints must be unique and sorted")
    if REVIEW_GATE_ROOT_SHIM.name not in source_root_entrypoints:
        raise ManifestError("source_root_entrypoints must include the review-poll compatibility shim")
    raw_flat = data.get("legacy_flat_entrypoints")
    if not isinstance(raw_flat, list) or not raw_flat:
        raise ManifestError("legacy_flat_entrypoints must be a non-empty list")
    flat = tuple(_safe_filename(name, field="legacy_flat_entrypoints item") for name in raw_flat)
    if tuple(sorted(flat)) != flat or len(set(flat)) != len(flat):
        raise ManifestError("legacy_flat_entrypoints must be unique and sorted")
    retired_flat = RETIRED_REVIEW_GATE_FLAT_IMPLEMENTATIONS.intersection(flat)
    if retired_flat:
        raise ManifestError(
            "review-poll implementation modules must be package-only, not legacy flat entrypoints: "
            + ", ".join(sorted(retired_flat))
        )

    package_glob = data.get("package_glob")
    if package_glob != "*.py":
        raise ManifestError("package_glob must be the fixed '*.py' source set")
    package_destination = _safe_directory(data.get("package_destination"), field="package_destination")
    if package_destination != "pr_pipeline":
        raise ManifestError("package_destination must be the canonical 'pr_pipeline' package")
    root_patterns_raw = data.get("managed_root_patterns")
    if not isinstance(root_patterns_raw, list) or not all(isinstance(item, str) and item for item in root_patterns_raw):
        raise ManifestError("managed_root_patterns must be a non-empty string list")
    root_patterns = tuple(root_patterns_raw)
    exclusions_raw = data.get("unmanaged_root_exclusions", [])
    if not isinstance(exclusions_raw, list):
        raise ManifestError("unmanaged_root_exclusions must be a string list")
    unmanaged_root_exclusions = tuple(
        _safe_filename(name, field="unmanaged_root_exclusions item")
        for name in exclusions_raw
    )
    if tuple(sorted(unmanaged_root_exclusions)) != unmanaged_root_exclusions or len(set(unmanaged_root_exclusions)) != len(unmanaged_root_exclusions):
        raise ManifestError("unmanaged_root_exclusions must be unique and sorted")
    if any(not any(fnmatch.fnmatchcase(name, pattern) for pattern in root_patterns) for name in unmanaged_root_exclusions):
        raise ManifestError("unmanaged_root_exclusions must match managed_root_patterns")
    expected_local_patches = _expected_local_patch_specs(data)

    resolved: list[ResolvedFile] = []
    destination_names: set[Path] = set()

    def add(source: Path, destination: Path) -> None:
        if not source.is_file() or source.is_symlink():
            raise ManifestError(f"manifest source must be a regular non-symlink file: {source}")
        if destination in destination_names:
            raise ManifestError(f"duplicate deployment destination: {destination}")
        destination_names.add(destination)
        resolved.append(
            ResolvedFile(
                source=source,
                destination=destination,
                sha256=_sha256_path(source),
                mode=source.stat().st_mode & 0o777,
            )
        )

    for name in source_root_entrypoints:
        add(source_root / name, Path(name))

    for name in flat:
        add(root / name, Path(name))

    package_sources = tuple(sorted(candidate for candidate in root.glob(package_glob) if candidate.is_file()))
    if not package_sources or not (root / "__init__.py").is_file():
        raise ManifestError("pr_pipeline package must contain __init__.py and at least one Python file")
    for source in package_sources:
        add(source, Path(package_destination) / source.name)

    required_review_gate_paths = (REVIEW_GATE_ROOT_SHIM, *REVIEW_GATE_PACKAGE_CLOSURE)
    missing_review_gate_sources = [
        relative.as_posix()
        for relative in required_review_gate_paths
        if relative not in destination_names
    ]
    if missing_review_gate_sources:
        raise ManifestError(
            "review-poll runtime closure is incomplete: "
            + ", ".join(missing_review_gate_sources)
        )

    indexes = {item.destination.as_posix(): index for index, item in enumerate(resolved)}
    unmanaged_patches = sorted(set(expected_local_patches).difference(indexes))
    if unmanaged_patches:
        raise ManifestError(
            "expected_local_patches must reference manifest destinations: "
            + ", ".join(unmanaged_patches)
        )
    for relative, spec in expected_local_patches.items():
        index = indexes[relative]
        item = resolved[index]
        if item.sha256 != spec["source_sha256"]:
            raise ManifestError(
                f"expected_local_patches[{relative}].source_sha256 does not match the source file"
            )
        resolved[index] = replace(
            item,
            install=False,
            local_patch_reason=spec["reason"],
            sha256=spec["deployed_sha256"],
            source_sha256=item.sha256,
        )

    return ResolvedManifest(
        path=path,
        sha256=_sha256_path(path),
        files=tuple(sorted(resolved, key=lambda item: item.destination.as_posix())),
        root_patterns=root_patterns,
        unmanaged_root_exclusions=unmanaged_root_exclusions,
        package_destination=package_destination,
    )


def _expected_hashes(manifest: ResolvedManifest) -> dict[str, str]:
    return {item.destination.as_posix(): item.sha256 for item in manifest.files}


def _expected_local_patches(manifest: ResolvedManifest) -> dict[str, dict[str, str | bool]]:
    return {
        item.destination.as_posix(): {
            "deployed_sha256": item.sha256,
            "reason": item.local_patch_reason or "",
            "source_sha256": item.source_sha256 or "",
            "sync": False,
        }
        for item in manifest.files
        if not item.install
    }


def _atomic_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_json(destination: Path, value: Any) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _cron_jobs_path(destination: Path) -> Path:
    return destination.parent / "cron" / "jobs.json"


def _managed_ci_health_script(destination: Path) -> str:
    return "ci_health_watch.py"


def _load_cron_jobs_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ManifestError(f"cron jobs {path} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read cron jobs {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"cron jobs {path} must be a JSON object")
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
        raise ManifestError(f"cron jobs {path}.jobs must be a JSON list of objects")
    return data


def _load_cron_jobs(path: Path) -> list[dict[str, Any]]:
    return _load_cron_jobs_document(path)["jobs"]


def _schedule_seconds(schedule: object) -> int | None:
    if isinstance(schedule, dict):
        if schedule.get("kind") != "cron":
            return None
        schedule = schedule.get("expr")
    if not isinstance(schedule, str):
        return None
    value = schedule.strip().lower()
    match = re.fullmatch(r"every\s+(\d+)\s*([mh])", value)
    if match:
        count = int(match.group(1))
        return count * (60 if match.group(2) == "m" else 3600)
    match = re.fullmatch(r"\*/(\d+)\s+\*\s+\*\s+\*\s+\*", value)
    if match:
        return int(match.group(1)) * 60
    return None


def _ci_health_schedule_errors(destination: Path, jobs: list[dict[str, Any]]) -> list[str]:
    matches = [job for job in jobs if job.get("name") == CI_HEALTH_CRON_NAME]
    if len(matches) != 1:
        return ["ci-health-cron-missing" if not matches else "ci-health-cron-ambiguous"]
    job = matches[0]
    errors: list[str] = []
    if job.get("script") != _managed_ci_health_script(destination):
        errors.append("ci-health-cron-script-drift")
    if job.get("no_agent") is not True:
        errors.append("ci-health-cron-no-agent-drift")
    seconds = _schedule_seconds(job.get("schedule"))
    if seconds is None or seconds > CI_HEALTH_CRON_MAX_SECONDS:
        errors.append("ci-health-cron-schedule-drift")
    elif job.get("schedule") != CI_HEALTH_CRON_SCHEDULE or job.get("schedule_display") != CI_HEALTH_CRON_SCHEDULE_DISPLAY:
        errors.append("ci-health-cron-schedule-contract-drift")
    if job.get("enabled") is not True:
        errors.append("ci-health-cron-disabled")
    if job.get("enabled") is True and job.get("state") != "scheduled":
        errors.append("ci-health-cron-state-drift")
    return errors


def _ci_health_job_index(jobs: list[dict[str, Any]]) -> int:
    matches = [index for index, job in enumerate(jobs) if job.get("name") == CI_HEALTH_CRON_NAME]
    if len(matches) != 1:
        detail = "missing" if not matches else "ambiguous"
        raise ManifestError(f"cron job name is {detail}: {CI_HEALTH_CRON_NAME}")
    return matches[0]


def _next_ci_health_run_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()


def _repair_ci_health_schedule(destination: Path) -> None:
    jobs_path = _cron_jobs_path(destination)
    document = _load_cron_jobs_document(jobs_path)
    jobs = document["jobs"]
    index = _ci_health_job_index(jobs)
    repaired = dict(jobs[index])
    repaired.update({
        "name": CI_HEALTH_CRON_NAME,
        "schedule": CI_HEALTH_CRON_SCHEDULE,
        "script": _managed_ci_health_script(destination),
        "schedule_display": CI_HEALTH_CRON_SCHEDULE_DISPLAY,
        "enabled": True,
        "no_agent": True,
        "state": "scheduled",
        "next_run_at": _next_ci_health_run_at(),
    })
    jobs[index] = repaired
    document["jobs"] = jobs
    _atomic_json(jobs_path, document)


def _validate_source_commit(value: str | None) -> str:
    if not isinstance(value, str) or not SOURCE_COMMIT_RE.fullmatch(value):
        raise ManifestError("--source-commit must be a full lowercase git object id (40-64 hex characters)")
    return value


def _smoke_review_gate(destination: Path, runtime_python: Path) -> dict[str, Any]:
    """Import the actual deployed root command from `/` with a minimal env."""
    runtime_python = runtime_python.expanduser().resolve()
    if not runtime_python.is_file() or runtime_python.is_symlink():
        raise ManifestError(f"runtime Python is missing or symlinked: {runtime_python}")
    entrypoint = destination / REVIEW_GATE_ROOT_SHIM
    env = {
        "HOME": str(destination.parent.parent),
        "HERMES_HOME": str(destination.parent),
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "HERMES_REVIEW_POLL_GATE_IMPORT_SMOKE": "1",
    }
    result = subprocess.run(
        [str(runtime_python), str(entrypoint)],
        cwd="/",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout.strip()
    if result.returncode != 0 or output != "review-poll-gate-import-smoke: ok":
        raise ManifestError(
            "deployed review-poll root command smoke failed "
            f"(exit={result.returncode}, stdout={output!r}, stderr={result.stderr.strip()!r})"
        )
    return {
        "ok": True,
        "runtime_python": str(runtime_python),
        "entrypoint": str(entrypoint),
        "cwd": "/",
        "environment_keys": sorted(env),
        "output": output,
    }


def _marker_receipt_id(marker: dict[str, Any]) -> str:
    canonical = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def install(
    destination: Path,
    *,
    source_commit: str,
    manifest_path: Path = DEFAULT_MANIFEST,
    runtime_python: Path | None = None,
) -> dict[str, Any]:
    """Atomically write only manifest paths and the deployment marker."""
    source_commit = _validate_source_commit(source_commit)
    manifest = resolve_manifest(manifest_path)
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ManifestError(f"destination is not a directory: {destination}")

    _ci_health_job_index(_load_cron_jobs(_cron_jobs_path(destination)))
    for item in manifest.files:
        if item.install:
            _atomic_copy(item.source, destination / item.destination, item.mode)
    _repair_ci_health_schedule(destination)
    smoke = _smoke_review_gate(destination, runtime_python or Path(sys.executable))

    marker = {
        "schema_version": 2,
        "deployment_mode": "shadow",
        "source_commit": source_commit,
        "manifest_sha256": manifest.sha256,
        "files": _expected_hashes(manifest),
        "expected_local_patches": _expected_local_patches(manifest),
        "review_gate_smoke": smoke,
    }
    marker["reconciliation_receipt_id"] = _marker_receipt_id(marker)
    _atomic_json(destination / MARKER_NAME, marker)
    return verify(
        destination,
        manifest_path=manifest_path,
        expected_source_commit=source_commit,
    )


def _read_marker(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    if not path.is_file():
        return None, ["deployment-marker-missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"deployment-marker-invalid:{exc}"]
    if not isinstance(data, dict):
        return None, ["deployment-marker-invalid:root-not-object"]
    return data, []


def _extra_root_files(destination: Path, manifest: ResolvedManifest, expected: set[str]) -> list[str]:
    extras: set[str] = set()
    if not destination.is_dir():
        return []
    for candidate in destination.iterdir():
        if not candidate.is_file() or candidate.name == MARKER_NAME:
            continue
        if any(fnmatch.fnmatchcase(candidate.name, pattern) for pattern in manifest.root_patterns):
            relative = candidate.name
            if relative in manifest.unmanaged_root_exclusions:
                continue
            if relative not in expected:
                extras.add(relative)
    package_root = destination / manifest.package_destination
    if package_root.is_dir():
        for candidate in package_root.iterdir():
            if candidate.is_file() and candidate.suffix == ".py":
                relative = f"{manifest.package_destination}/{candidate.name}"
                if relative not in expected:
                    extras.add(relative)
    return sorted(extras)


def verify(
    destination: Path,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    """Return a machine-readable deployment parity report without changing files."""
    if expected_source_commit is not None:
        expected_source_commit = _validate_source_commit(expected_source_commit)
    manifest = resolve_manifest(manifest_path)
    destination = destination.expanduser().resolve()
    expected_hashes = _expected_hashes(manifest)
    missing: list[str] = []
    mismatches: dict[str, dict[str, str]] = {}
    for relative, expected_hash in expected_hashes.items():
        deployed = destination / relative
        if not deployed.is_file() or deployed.is_symlink():
            missing.append(relative)
            continue
        actual = _sha256_path(deployed)
        if actual != expected_hash:
            mismatches[relative] = {"expected": expected_hash, "actual": actual}

    review_gate_errors: list[str] = []
    for required in (REVIEW_GATE_ROOT_SHIM, *REVIEW_GATE_PACKAGE_CLOSURE):
        relative = required.as_posix()
        if relative not in expected_hashes:
            review_gate_errors.append(f"review-gate-manifest-missing:{relative}")
        elif relative in missing:
            review_gate_errors.append(f"review-gate-runtime-missing:{relative}")
        elif relative in mismatches:
            review_gate_errors.append(f"review-gate-hash-mismatch:{relative}")

    marker, marker_errors = _read_marker(destination / MARKER_NAME)
    recorded_commit: str | None = None
    if marker is not None:
        recorded_commit = marker.get("source_commit") if isinstance(marker.get("source_commit"), str) else None
        if marker.get("schema_version") != 2:
            marker_errors.append("deployment-marker-schema-mismatch")
        if marker.get("deployment_mode") != "shadow":
            marker_errors.append("deployment-mode-not-shadow")
        if marker.get("manifest_sha256") != manifest.sha256:
            marker_errors.append("deployment-marker-manifest-mismatch")
        if marker.get("files") != expected_hashes:
            marker_errors.append("deployment-marker-file-list-mismatch")
        marker_without_receipt = dict(marker)
        recorded_receipt_id = marker_without_receipt.pop("reconciliation_receipt_id", None)
        if (
            not isinstance(recorded_receipt_id, str)
            or not SHA256_RE.fullmatch(recorded_receipt_id)
            or recorded_receipt_id != _marker_receipt_id(marker_without_receipt)
        ):
            marker_errors.append("deployment-marker-receipt-invalid")
        smoke = marker.get("review_gate_smoke")
        if not isinstance(smoke, dict) or smoke.get("ok") is not True:
            marker_errors.append("deployment-marker-review-gate-smoke-invalid")
        elif (
            smoke.get("cwd") != "/"
            or smoke.get("output") != "review-poll-gate-import-smoke: ok"
            or "HERMES_REVIEW_POLL_GATE_IMPORT_SMOKE"
            not in (smoke.get("environment_keys") or [])
        ):
            marker_errors.append("deployment-marker-review-gate-smoke-contract-drift")
        if recorded_commit is None or not SOURCE_COMMIT_RE.fullmatch(recorded_commit):
            marker_errors.append("deployment-marker-source-commit-invalid")
        elif expected_source_commit is not None and recorded_commit != expected_source_commit:
            marker_errors.append("deployment-marker-source-commit-mismatch")

    extras = _extra_root_files(destination, manifest, set(expected_hashes))
    cron_errors: list[str]
    try:
        cron_errors = _ci_health_schedule_errors(destination, _load_cron_jobs(_cron_jobs_path(destination)))
    except ManifestError as exc:
        cron_errors = [f"ci-health-cron-invalid:{exc}"]
    report = {
        "ok": (
            not missing
            and not mismatches
            and not extras
            and not marker_errors
            and not cron_errors
            and not review_gate_errors
        ),
        "deployment": str(destination),
        "manifest": str(manifest.path),
        "manifest_sha256": manifest.sha256,
        "expected_files": expected_hashes,
        "expected_local_patches": _expected_local_patches(manifest),
        "missing": missing,
        "hash_mismatches": mismatches,
        "extra": extras,
        "marker_errors": marker_errors,
        "cron_errors": cron_errors,
        "review_gate_errors": review_gate_errors,
        "managed_cron": {
            "name": CI_HEALTH_CRON_NAME,
            "max_interval_seconds": CI_HEALTH_CRON_MAX_SECONDS,
            "script": _managed_ci_health_script(destination),
        },
        "recorded_source_commit": recorded_commit,
        "expected_source_commit": expected_source_commit,
        "reconciliation_receipt_id": (
            marker.get("reconciliation_receipt_id") if marker is not None else None
        ),
        "review_gate_smoke": (
            marker.get("review_gate_smoke") if marker is not None else None
        ),
    }
    return report


def _copy_remote_stage(host: str, manifest: ResolvedManifest) -> tuple[str, list[Path]]:
    if not REMOTE_HOST_RE.fullmatch(host):
        raise ManifestError("--host must contain only a hostname, optional user, and optional port separator")
    stage = f"/tmp/hermes-pr-pipeline-{secrets.token_hex(12)}"
    subprocess.run(["ssh", "-o", "BatchMode=yes", host, "mkdir", "-p", f"{stage}/pr_pipeline"], check=True)
    package_sources = [
        SCRIPT_DIR / "reconcile_pr_pipeline.py", manifest.path,
        *(item.source for item in manifest.files if item.source.parent == manifest.path.parent),
    ]
    source_root_sources = [
        item.source for item in manifest.files if item.source.parent == SCRIPT_DIR
    ]
    # Sources appear once as flat entrypoints and once as package members; scp's
    # input is deduplicated while the remote manifest still controls deployment.
    unique_files = list(dict.fromkeys([*package_sources, *source_root_sources]))
    subprocess.run(
        ["scp", "-q", *(str(path) for path in dict.fromkeys(package_sources)), f"{host}:{stage}/pr_pipeline/"],
        check=True,
    )
    if source_root_sources:
        subprocess.run(
            ["scp", "-q", *(str(path) for path in dict.fromkeys(source_root_sources)), f"{host}:{stage}/"],
            check=True,
        )
    # The reconciler belongs beside the staged package, not inside it.
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, "mv", f"{stage}/pr_pipeline/reconcile_pr_pipeline.py", f"{stage}/reconcile_pr_pipeline.py"],
        check=True,
    )
    return stage, unique_files


def _remote_run(args: argparse.Namespace) -> int:
    manifest = resolve_manifest(args.manifest)
    stage, _ = _copy_remote_stage(args.host, manifest)
    destination = args.destination or "$HOME/.hermes/scripts"
    source_commit = args.source_commit
    command = [
        "python3",
        f"{stage}/reconcile_pr_pipeline.py",
        args.action,
        "--manifest",
        f"{stage}/pr_pipeline/manifest.json",
        "--destination",
        destination,
    ]
    if source_commit:
        command.extend(["--source-commit", source_commit])
    if args.runtime_python:
        command.extend(["--runtime-python", str(args.runtime_python)])
    try:
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", args.host, *command], check=False)
        return result.returncode
    finally:
        subprocess.run(["ssh", "-o", "BatchMode=yes", args.host, "rm", "-rf", stage], check=False)


def _print_report(report: dict[str, Any]) -> int:
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "verify", "reconcile"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--destination", type=Path, default=Path.home() / ".hermes" / "scripts")
    parser.add_argument("--source-commit", help="required for install/reconcile; recorded on the Mini")
    parser.add_argument(
        "--runtime-python",
        type=Path,
        help="exact release Python used for the sanitized deployed root-command smoke",
    )
    parser.add_argument("--host", help="SSH host alias; stage and run the same verifier on that host")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.host:
            return _remote_run(args)
        if args.action in {"install", "reconcile"}:
            report = install(
                args.destination,
                source_commit=args.source_commit,
                manifest_path=args.manifest,
                runtime_python=args.runtime_python,
            )
            return _print_report(report)
        return _print_report(verify(args.destination, manifest_path=args.manifest, expected_source_commit=args.source_commit))
    except (ManifestError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
