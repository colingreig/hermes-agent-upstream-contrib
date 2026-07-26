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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = SCRIPT_DIR / "pr_pipeline" / "manifest.json"
MARKER_NAME = ".pr_pipeline_deployment.json"
SOURCE_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}\Z")
REMOTE_HOST_RE = re.compile(r"[A-Za-z0-9_.@:-]+\Z")


class ManifestError(ValueError):
    """The deployment manifest is invalid or cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedFile:
    source: Path
    destination: Path
    sha256: str
    mode: int


@dataclass(frozen=True)
class ResolvedManifest:
    path: Path
    sha256: str
    files: tuple[ResolvedFile, ...]
    root_patterns: tuple[str, ...]
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


def resolve_manifest(path: Path = DEFAULT_MANIFEST) -> ResolvedManifest:
    """Resolve the stable manifest shape into sorted files and runtime hashes."""
    path = path.resolve()
    data = _read_manifest(path)
    root = path.parent
    raw_flat = data.get("legacy_flat_entrypoints")
    if not isinstance(raw_flat, list) or not raw_flat:
        raise ManifestError("legacy_flat_entrypoints must be a non-empty list")
    flat = tuple(_safe_filename(name, field="legacy_flat_entrypoints item") for name in raw_flat)
    if tuple(sorted(flat)) != flat or len(set(flat)) != len(flat):
        raise ManifestError("legacy_flat_entrypoints must be unique and sorted")

    package_glob = data.get("package_glob")
    if package_glob != "*.py":
        raise ManifestError("package_glob must be the fixed '*.py' source set")
    package_destination = _safe_directory(data.get("package_destination"), field="package_destination")
    root_patterns_raw = data.get("managed_root_patterns")
    if not isinstance(root_patterns_raw, list) or not all(isinstance(item, str) and item for item in root_patterns_raw):
        raise ManifestError("managed_root_patterns must be a non-empty string list")
    root_patterns = tuple(root_patterns_raw)

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

    for name in flat:
        add(root / name, Path(name))

    package_sources = tuple(sorted(candidate for candidate in root.glob(package_glob) if candidate.is_file()))
    if not package_sources or not (root / "__init__.py").is_file():
        raise ManifestError("pr_pipeline package must contain __init__.py and at least one Python file")
    for source in package_sources:
        add(source, Path(package_destination) / source.name)

    return ResolvedManifest(
        path=path,
        sha256=_sha256_path(path),
        files=tuple(sorted(resolved, key=lambda item: item.destination.as_posix())),
        root_patterns=root_patterns,
        package_destination=package_destination,
    )


def _expected_hashes(manifest: ResolvedManifest) -> dict[str, str]:
    return {item.destination.as_posix(): item.sha256 for item in manifest.files}


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


def _atomic_json(destination: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(payload)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _validate_source_commit(value: str | None) -> str:
    if not isinstance(value, str) or not SOURCE_COMMIT_RE.fullmatch(value):
        raise ManifestError("--source-commit must be a lowercase git object id (7-64 hex characters)")
    return value


def install(destination: Path, *, source_commit: str, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    """Atomically write only manifest paths and the deployment marker."""
    source_commit = _validate_source_commit(source_commit)
    manifest = resolve_manifest(manifest_path)
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if not destination.is_dir():
        raise ManifestError(f"destination is not a directory: {destination}")

    for item in manifest.files:
        _atomic_copy(item.source, destination / item.destination, item.mode)

    marker = {
        "schema_version": 1,
        "deployment_mode": "shadow",
        "source_commit": source_commit,
        "manifest_sha256": manifest.sha256,
        "files": _expected_hashes(manifest),
    }
    _atomic_json(destination / MARKER_NAME, marker)
    return verify(destination, manifest_path=manifest_path, expected_source_commit=source_commit)


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

    marker, marker_errors = _read_marker(destination / MARKER_NAME)
    recorded_commit: str | None = None
    if marker is not None:
        recorded_commit = marker.get("source_commit") if isinstance(marker.get("source_commit"), str) else None
        if marker.get("schema_version") != 1:
            marker_errors.append("deployment-marker-schema-mismatch")
        if marker.get("deployment_mode") != "shadow":
            marker_errors.append("deployment-mode-not-shadow")
        if marker.get("manifest_sha256") != manifest.sha256:
            marker_errors.append("deployment-marker-manifest-mismatch")
        if marker.get("files") != expected_hashes:
            marker_errors.append("deployment-marker-file-list-mismatch")
        if recorded_commit is None or not SOURCE_COMMIT_RE.fullmatch(recorded_commit):
            marker_errors.append("deployment-marker-source-commit-invalid")
        elif expected_source_commit is not None and recorded_commit != expected_source_commit:
            marker_errors.append("deployment-marker-source-commit-mismatch")

    extras = _extra_root_files(destination, manifest, set(expected_hashes))
    report = {
        "ok": not missing and not mismatches and not extras and not marker_errors,
        "deployment": str(destination),
        "manifest": str(manifest.path),
        "manifest_sha256": manifest.sha256,
        "expected_files": expected_hashes,
        "missing": missing,
        "hash_mismatches": mismatches,
        "extra": extras,
        "marker_errors": marker_errors,
        "recorded_source_commit": recorded_commit,
        "expected_source_commit": expected_source_commit,
    }
    return report


def _copy_remote_stage(host: str, manifest: ResolvedManifest) -> tuple[str, list[Path]]:
    if not REMOTE_HOST_RE.fullmatch(host):
        raise ManifestError("--host must contain only a hostname, optional user, and optional port separator")
    stage = f"/tmp/hermes-pr-pipeline-{secrets.token_hex(12)}"
    subprocess.run(["ssh", "-o", "BatchMode=yes", host, "mkdir", "-p", f"{stage}/pr_pipeline"], check=True)
    local_files = [SCRIPT_DIR / "reconcile_pr_pipeline.py", manifest.path, *(item.source for item in manifest.files)]
    # Sources appear once as flat entrypoints and once as package members; scp's
    # input is deduplicated while the remote manifest still controls deployment.
    unique_files = list(dict.fromkeys(local_files))
    subprocess.run(
        ["scp", "-q", *(str(path) for path in unique_files), f"{host}:{stage}/pr_pipeline/"],
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
    parser.add_argument("--host", help="SSH host alias; stage and run the same verifier on that host")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.host:
            return _remote_run(args)
        if args.action in {"install", "reconcile"}:
            report = install(args.destination, source_commit=args.source_commit, manifest_path=args.manifest)
            return _print_report(report)
        return _print_report(verify(args.destination, manifest_path=args.manifest, expected_source_commit=args.source_commit))
    except (ManifestError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
