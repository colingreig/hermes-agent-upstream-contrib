#!/usr/bin/env python3
"""Fail-closed, machine-readable health attestation for the Hermes Mac mini.

The collector intentionally emits only metadata: paths, hashes, states, and
credential names. Secret values and 1Password references are never returned.
The pure ``evaluate_snapshot`` function is the policy boundary and is kept
separate from live collection so adversarial false-green cases are testable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
from urllib.request import urlopen


SCHEMA = "hermes-mini-health-attestation/v1"
CONFIG_RECEIPT_SCHEMA = "hermes-mini-config-migration/v1"
SUCCESSFUL_RELEASE_EVENTS = frozenset(("advanced", "cut", "noop"))
# Hermes core modules whose behaviour this attestation reads. Every one must
# resolve inside the active release so the checks exercise the deployed code,
# not a stale checkout that merely looks similar.
ATTESTED_MODULES = ("hermes_cli.config", "cron.jobs", "cron.executions")
GOVERNED_CONTRACTS = ("launchd-environment", "marketplace-skills", "pr-pipeline")
# releases/vX.Y.Z-<sha> — the trailing fragment binds the directory to a commit.
RELEASE_DIR_RE = re.compile(r"v\d+\.\d+\.\d+-([0-9a-f]{7,40})\Z")
RESEARCH_SUCCESS_STATUSES = frozenset(
    ("disabled-or-smoke-only", "healthy", "insufficient-data")
)
SERVICE_CONTRACTS = {
    "gateway": {
        "label": "ai.hermes.gateway",
        "argv_markers": ("-m", "hermes_cli.main", "gateway", "run"),
        "http": None,
    },
    "dashboard": {
        "label": "com.colingreig.hermes-dashboard",
        "argv_markers": ("-m", "hermes_cli.main", "dashboard"),
        "http": "http://127.0.0.1:9119/health",
    },
}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _atomic_write(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked destination: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _check(
    checks: list[dict[str, Any]],
    check_id: str,
    ok: bool,
    detail: Any,
    *,
    warning: bool = False,
) -> None:
    checks.append(
        {
            "id": check_id,
            "state": "warn" if warning else ("pass" if ok else "fail"),
            "detail": detail,
        }
    )


def evaluate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one normalized snapshot without touching the machine."""
    checks: list[dict[str, Any]] = []

    runtime = snapshot.get("runtime") or {}
    actual_sha = runtime.get("actual_sha")
    expected_sha = runtime.get("expected_sha")
    _check(checks, "runtime.symlink", bool(runtime.get("symlink")), runtime.get("target"))
    _check(
        checks,
        "runtime.release-root",
        bool(runtime.get("target_under_releases")),
        runtime.get("target"),
    )
    _check(
        checks,
        "runtime.commit",
        bool(actual_sha and expected_sha and actual_sha == expected_sha),
        {"actual": actual_sha, "expected": expected_sha},
    )
    dirty_paths = list(runtime.get("dirty_paths") or [])
    _check(checks, "runtime.clean", not dirty_paths, {"dirty_paths": dirty_paths})
    source = runtime.get("source_binding") or {}
    _check(
        checks,
        "runtime.source-binding",
        bool(source.get("bound")),
        {
            "release_dir": source.get("release_dir"),
            "release_name_binds_sha": source.get("release_name_binds_sha"),
            "unbound_sources": sorted(
                name
                for name, item in (source.get("sources") or {}).items()
                if not item.get("under_runtime")
            ),
        },
    )

    services = snapshot.get("services") or {}
    missing_services = [name for name in SERVICE_CONTRACTS if name not in services]
    _check(
        checks,
        "inventory.services",
        bool(services) and not missing_services,
        {"present": sorted(services), "missing": missing_services},
    )
    for name, service in sorted(services.items()):
        base = f"service.{name}"
        _check(checks, f"{base}.registered", bool(service.get("registered")), service.get("label"))
        _check(checks, f"{base}.alive", bool(service.get("alive")), {"pid": service.get("pid")})
        _check(checks, f"{base}.command", bool(service.get("command_ok")), service.get("command_shape"))
        _check(
            checks,
            f"{base}.runtime",
            int(service.get("runtime_code_file_count") or 0) > 0
            or bool(service.get("executable_in_release")),
            {
                "runtime_code_file_count": service.get("runtime_code_file_count"),
                "executable_in_release": service.get("executable_in_release"),
            },
        )
        if service.get("http_required"):
            _check(
                checks,
                f"{base}.http",
                service.get("http_status") == 200,
                {"status": service.get("http_status"), "url": service.get("http_url")},
            )

    config = snapshot.get("config") or {}
    _check(
        checks,
        "config.schema",
        config.get("current") == config.get("latest") and config.get("latest") is not None,
        {"current": config.get("current"), "latest": config.get("latest")},
    )
    _check(
        checks,
        "config.migration-receipt",
        bool(config.get("receipt_valid")),
        config.get("receipt_detail"),
    )

    release = snapshot.get("release") or {}
    _check(
        checks,
        "release.latest-content-address",
        bool(release.get("latest_hash_valid")),
        {"hash": release.get("latest_hash"), "event": release.get("latest_event")},
    )
    last_good = release.get("last_good") or {}
    _check(
        checks,
        "release.last-good",
        bool(
            last_good.get("valid")
            and last_good.get("to_commit")
            and last_good.get("to_commit") == actual_sha
            and last_good.get("certified_source_commit") == actual_sha
            and re.fullmatch(
                r"[0-9a-f]{64}",
                str(last_good.get("pr_pipeline_reconciliation_receipt_id") or ""),
            )
            and last_good.get("review_poll_gate_smoke") == "passed"
        ),
        {
            "event": last_good.get("event"),
            "to_commit": last_good.get("to_commit"),
            "certified_source_commit": last_good.get("certified_source_commit"),
            "pr_pipeline_reconciliation_receipt_id": last_good.get(
                "pr_pipeline_reconciliation_receipt_id"
            ),
            "review_poll_gate_smoke": last_good.get("review_poll_gate_smoke"),
            "receipt_hash": last_good.get("receipt_hash"),
        },
    )
    if release.get("latest_event") == "rejected":
        _check(
            checks,
            "release.latest-attempt",
            True,
            {
                "event": "rejected",
                "from_commit": release.get("latest_from_commit"),
                "last_good_preserved": bool(last_good.get("valid")),
            },
            warning=True,
        )
    else:
        _check(
            checks,
            "release.latest-attempt",
            release.get("latest_event") in SUCCESSFUL_RELEASE_EVENTS,
            {"event": release.get("latest_event")},
        )

    governed_assets = snapshot.get("governed") or {}
    missing_governed = [name for name in GOVERNED_CONTRACTS if name not in governed_assets]
    _check(
        checks,
        "inventory.governed",
        bool(governed_assets) and not missing_governed,
        {"present": sorted(governed_assets), "missing": missing_governed},
    )
    for name, governed in sorted(governed_assets.items()):
        _check(checks, f"governed.{name}", bool(governed.get("ok")), governed.get("detail"))

    execution = snapshot.get("execution") or {}
    summary = execution.get("summary") or {}
    _check(
        checks,
        "execution.inflight-classification",
        all(int(summary.get(key) or 0) == 0 for key in ("stale", "legacy_unfenced", "insufficient_evidence")),
        summary,
    )
    for entry in execution.get("entries") or []:
        execution_id = str(entry.get("execution_id") or "unknown")
        owner = entry.get("owner") or {}
        lease = entry.get("lease") or {}
        _check(
            checks,
            f"execution.{execution_id}",
            bool(
                owner.get("token_present")
                and entry.get("owner_liveness") == "live"
                and entry.get("disposition") == "live_or_unexpired"
                and lease.get("heartbeat_at")
                and lease.get("state") == "current"
            ),
            {
                "job_id": entry.get("job_id"),
                "owner_liveness": entry.get("owner_liveness"),
                "disposition": entry.get("disposition"),
                "lease_state": lease.get("state"),
                "heartbeat_present": bool(lease.get("heartbeat_at")),
                "token_present": bool(owner.get("token_present")),
            },
        )

    skills = snapshot.get("skills") or []
    _check(checks, "inventory.skills", len(skills) > 0, {"count": len(skills)})
    for skill in skills:
        _check(
            checks,
            f"skill.{skill.get('name')}",
            bool(skill.get("available")),
            {"name": skill.get("name"), "available": bool(skill.get("available"))},
        )
    credentials = snapshot.get("credentials") or []
    _check(checks, "inventory.credentials", len(credentials) > 0, {"count": len(credentials)})
    for credential in credentials:
        _check(
            checks,
            f"credential.{credential.get('job')}.{credential.get('name')}",
            bool(credential.get("available")),
            {
                "job": credential.get("job"),
                "name": credential.get("name"),
                "classification": credential.get("classification"),
            },
        )

    cron_jobs = snapshot.get("cron") or []
    enabled_jobs = [job for job in cron_jobs if job.get("enabled")]
    _check(
        checks,
        "inventory.cron",
        len(cron_jobs) > 0 and len(enabled_jobs) > 0,
        {"jobs": len(cron_jobs), "enabled": len(enabled_jobs)},
    )
    for job in cron_jobs:
        if not job.get("enabled"):
            continue
        name = str(job.get("name") or job.get("id") or "unknown")
        latest_status = job.get("latest_execution_status")
        false_green = (
            job.get("last_status") == "ok"
            and latest_status != "completed"
        )
        ok = job.get("last_status") == "ok" and latest_status == "completed"
        monitor_status = job.get("monitor_status")
        if name == "research-stage-monitor":
            ok = ok and monitor_status in RESEARCH_SUCCESS_STATUSES
        _check(
            checks,
            f"cron.{name}",
            ok,
            {
                "last_status": job.get("last_status"),
                "latest_execution_status": latest_status,
                "false_green": false_green,
                "monitor_status": monitor_status,
                "contract": job.get("contract"),
            },
        )

    failures = [item for item in checks if item["state"] == "fail"]
    warnings = [item for item in checks if item["state"] == "warn"]
    return {
        "schema": SCHEMA,
        "generated_at": snapshot.get("generated_at"),
        "healthy": not failures,
        "exit_code": 0 if not failures else 2,
        "summary": {
            "checks": len(checks),
            "passed": sum(item["state"] == "pass" for item in checks),
            "warnings": len(warnings),
            "failed": len(failures),
        },
        "checks": checks,
    }


def _git_output(runtime: Path, *args: str) -> str:
    result = _run(["git", "-C", str(runtime), *args])
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _source_binding(runtime: Path, actual_sha: str) -> dict[str, Any]:
    """Prove the attestor and the Hermes code it reads are the active release.

    The attestor's own file, every ``ATTESTED_MODULES`` import, and the release
    directory naming must all resolve inside ``runtime`` (the resolved
    ``runtime-current`` → ``releases/vX.Y.Z-<sha>``) at the exact deployed commit
    — not a tree that merely looks similar.
    """
    runtime_resolved = runtime.resolve()
    prefix = str(runtime_resolved) + os.sep

    def _under(path: str | None) -> bool:
        return bool(path and (path == str(runtime_resolved) or path.startswith(prefix)))

    def _resolved_origin(module_name: str) -> str | None:
        loaded = sys.modules.get(module_name)
        origin = getattr(loaded, "__file__", None)
        if origin is None:
            try:
                spec = importlib.util.find_spec(module_name)
            except Exception:
                spec = None
            origin = spec.origin if spec else None
        return str(Path(origin).resolve()) if origin else None

    sources: dict[str, dict[str, Any]] = {}
    attestor = str(Path(__file__).resolve())
    sources["attestor"] = {"path": attestor, "under_runtime": _under(attestor)}
    for module_name in ATTESTED_MODULES:
        origin = _resolved_origin(module_name)
        sources[module_name] = {"path": origin, "under_runtime": _under(origin)}

    name_match = RELEASE_DIR_RE.fullmatch(runtime_resolved.name)
    release_name_binds_sha = bool(name_match and actual_sha.startswith(name_match.group(1)))
    bound = release_name_binds_sha and all(item["under_runtime"] for item in sources.values())
    return {
        "release_dir": runtime_resolved.name,
        "release_name_binds_sha": release_name_binds_sha,
        "sources": sources,
        "bound": bound,
    }


def _collect_release(releases_dir: Path, runtime: Path, runtime_sha: str) -> dict[str, Any]:
    latest = releases_dir / ".mini-release-last-receipt.json"
    if not latest.is_file() or latest.is_symlink():
        return {"latest_hash_valid": False, "last_good": {"valid": False}}
    try:
        latest_bytes = latest.read_bytes()
        latest_data = json.loads(latest_bytes)
    except (OSError, json.JSONDecodeError):
        return {"latest_hash_valid": False, "last_good": {"valid": False}}
    latest_hash = _sha256_bytes(latest_bytes)
    latest_sibling = releases_dir / f".mini-release-receipt-{latest_hash}.json"
    latest_valid = (
        latest_sibling.is_file()
        and not latest_sibling.is_symlink()
        and latest_sibling.read_bytes() == latest_bytes
    )

    valid_receipts: list[tuple[float, str, dict[str, Any]]] = []
    receipt_re = re.compile(r"\.mini-release-receipt-([0-9a-f]{64})\.json\Z")
    for path in releases_dir.glob(".mini-release-receipt-*.json"):
        match = receipt_re.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            payload = path.read_bytes()
            if _sha256_bytes(payload) != match.group(1):
                continue
            data = json.loads(payload)
        except (OSError, json.JSONDecodeError):
            continue
        valid_receipts.append((path.stat().st_mtime, match.group(1), data))
    valid_receipts.sort(reverse=True)
    last_good: dict[str, Any] = {"valid": False}
    for _, digest, data in valid_receipts:
        target = Path(str(data.get("runtime_target") or "")).expanduser()
        try:
            target_matches = target.resolve() == runtime.resolve()
        except OSError:
            target_matches = False
        if (
            data.get("event") in SUCCESSFUL_RELEASE_EVENTS
            and data.get("to_commit") == runtime_sha
            and target_matches
        ):
            last_good = {
                "valid": True,
                "event": data.get("event"),
                "to_commit": data.get("to_commit"),
                "certified_source_commit": data.get("certified_source_commit"),
                "pr_pipeline_reconciliation_receipt_id": data.get(
                    "pr_pipeline_reconciliation_receipt_id"
                ),
                "review_poll_gate_smoke": data.get("review_poll_gate_smoke"),
                "receipt_hash": digest,
            }
            break
    return {
        "latest_hash_valid": latest_valid,
        "latest_hash": latest_hash,
        "latest_event": latest_data.get("event"),
        "latest_from_commit": latest_data.get("from_commit"),
        "latest_to_commit": latest_data.get("to_commit"),
        "last_good": last_good,
    }


def _config_receipt_status(
    hermes_home: Path,
    current: int,
    latest: int,
    *,
    runtime: Path | None = None,
    runtime_sha: str | None = None,
) -> tuple[bool, Any]:
    state = hermes_home / "state" / "config-migrations"
    pointer = state / "latest.json"
    if not pointer.is_file() or pointer.is_symlink():
        return False, "latest migration receipt missing"
    try:
        payload = pointer.read_bytes()
        data = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        return False, "latest migration receipt unreadable"
    digest = _sha256_bytes(payload)
    sibling = state / f"receipt-{digest}.json"
    backup_path = Path(str(data.get("backup_path") or ""))
    backup_root = (hermes_home / "backups" / "config-migrations").resolve()
    try:
        backup_resolved = backup_path.resolve(strict=True)
        backup_scoped = backup_resolved.is_relative_to(backup_root)
    except OSError:
        backup_scoped = False
    backup_present = (
        backup_scoped and backup_path.is_file() and not backup_path.is_symlink()
    )
    backup_private = False
    backup_hash_valid = False
    if backup_present:
        try:
            backup_private = backup_path.stat().st_mode & 0o077 == 0
            backup_hash_valid = _sha256_path(backup_path) == data.get("backup_sha256")
        except OSError:
            backup_hash_valid = False
    runtime_commit = data.get("runtime_commit")
    runtime_identity_valid = True
    if runtime is not None and runtime_sha is not None:
        runtime_identity_valid = bool(
            isinstance(runtime_commit, str)
            and re.fullmatch(r"[0-9a-f]{40}", runtime_commit)
            and _run(
                [
                    "git",
                    "-C",
                    str(runtime),
                    "merge-base",
                    "--is-ancestor",
                    runtime_commit,
                    runtime_sha,
                ]
            ).returncode
            == 0
        )
    valid = (
        sibling.is_file()
        and not sibling.is_symlink()
        and sibling.read_bytes() == payload
        and data.get("schema") == CONFIG_RECEIPT_SCHEMA
        and data.get("to_version") == latest
        and current == latest
        and backup_private
        and backup_hash_valid
        and runtime_identity_valid
    )
    return valid, {
        "receipt_hash": digest,
        "from_version": data.get("from_version"),
        "to_version": data.get("to_version"),
        "backup_present": backup_present,
        "backup_scoped": backup_scoped,
        "backup_private": backup_private,
        "backup_hash_valid": backup_hash_valid,
        "runtime_commit": runtime_commit,
        "runtime_identity_valid": runtime_identity_valid,
    }


def record_config_migration(hermes_home: Path, runtime_sha: str) -> dict[str, Any]:
    """Run the ordinary safe migration and record durable content-addressed proof."""
    from hermes_cli.config import (
        check_config_version,
        get_config_path,
        get_env_path,
        migrate_config,
    )

    current, latest = check_config_version()
    config_path = get_config_path()
    env_path = get_env_path()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = hermes_home / "backups" / "config-migrations" / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(backup_dir, 0o700)
    backups: dict[Path, Path] = {}
    for source in (config_path, env_path):
        if source.is_file() and not source.is_symlink():
            destination = backup_dir / source.name
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
            backups[source] = destination
    try:
        if current < latest:
            migrate_config(interactive=False, quiet=True)
        after, latest_after = check_config_version()
        if after != latest_after:
            raise RuntimeError(f"config migration remained behind ({after}/{latest_after})")
    except Exception:
        for source, backup in backups.items():
            shutil.copy2(backup, source)
        raise
    config_backup = backups.get(config_path)
    if config_backup is None:
        raise RuntimeError("config migration backup was not created")
    receipt = {
        "schema": CONFIG_RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "from_version": current,
        "to_version": latest_after,
        "runtime_commit": runtime_sha,
        "backup_path": str(config_backup),
        "backup_sha256": _sha256_path(config_backup),
    }
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = _sha256_bytes(payload)
    state = hermes_home / "state" / "config-migrations"
    sibling = state / f"receipt-{digest}.json"
    if sibling.exists() and sibling.read_bytes() != payload:
        raise RuntimeError("config migration receipt hash collision")
    if not sibling.exists():
        _atomic_write(sibling, payload)
    _atomic_write(state / "latest.json", payload)
    return {"receipt_hash": digest, "from_version": current, "to_version": latest_after}


def _collect_service(
    name: str,
    contract: dict[str, Any],
    runtime: Path,
    hermes_home: Path,
) -> dict[str, Any]:
    label = str(contract["label"])
    launch = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    pid_match = re.search(r"^\s*pid = (\d+)\s*$", launch.stdout, flags=re.MULTILINE)
    pid = int(pid_match.group(1)) if pid_match else None
    command = ""
    alive = False
    runtime_code_file_count = 0
    executable_in_release = False
    if pid:
        process = _run(["ps", "-p", str(pid), "-o", "command="])
        command = process.stdout.strip()
        alive = process.returncode == 0 and bool(command)
        # -Ff (file descriptor) + -Fn (name): only ``txt`` (program text /
        # executable) and ``mem`` (mapped shared library) entries prove loaded
        # code. A ``cwd`` entry or an ordinary open file inside the release does
        # NOT bind the service to it, so those descriptors are ignored.
        opened = _run(["lsof", "-a", "-p", str(pid), "-Ffn"])
        runtime_resolved = str(runtime.resolve())
        runtime_prefix = runtime_resolved + os.sep
        fd = ""
        for line in opened.stdout.splitlines():
            if not line:
                continue
            tag, value = line[0], line[1:]
            if tag == "f":
                fd = value
            elif tag == "n" and fd in ("txt", "mem"):
                if value == runtime_resolved or value.startswith(runtime_prefix):
                    runtime_code_file_count += 1
                    if fd == "txt":
                        executable_in_release = True
    markers = tuple(str(marker) for marker in contract["argv_markers"])
    command_ok = bool(command) and str(hermes_home / "runtime-current") in command
    command_ok = command_ok and all(marker in command for marker in markers)
    http_url = contract.get("http")
    http_status: int | None = None
    if http_url:
        try:
            with urlopen(str(http_url), timeout=3) as response:
                http_status = response.status
        except Exception:
            http_status = None
    return {
        "label": label,
        "registered": launch.returncode == 0 and pid is not None,
        "pid": pid,
        "alive": alive,
        "command_ok": command_ok,
        "command_shape": {"runtime_current": "present" if command_ok else "invalid", "markers": list(markers)},
        "runtime_code_file_count": runtime_code_file_count,
        "executable_in_release": executable_in_release,
        "http_required": bool(http_url),
        "http_url": http_url,
        "http_status": http_status,
    }


def _safe_probe(callback: Callable[[], Any]) -> dict[str, Any]:
    try:
        detail = callback()
        return {"ok": True, "detail": detail if detail is not None else "verified"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def collect_live(home: Path, hermes_home: Path) -> dict[str, Any]:
    current_link = hermes_home / "runtime-current"
    runtime = current_link.resolve()
    releases_dir = hermes_home / "releases"
    actual_sha = _git_output(runtime, "rev-parse", "HEAD")
    dirty_paths = [
        line[3:] for line in _git_output(runtime, "status", "--porcelain").splitlines() if line
    ]
    release = _collect_release(releases_dir, runtime, actual_sha)
    expected_sha = (release.get("last_good") or {}).get("to_commit")

    from hermes_cli.config import check_config_version
    from cron.executions import classify_stale_executions
    from cron.jobs import list_jobs

    current_version, latest_version = check_config_version()
    receipt_valid, receipt_detail = _config_receipt_status(
        hermes_home,
        current_version,
        latest_version,
        runtime=runtime,
        runtime_sha=actual_sha,
    )
    jobs = list_jobs()

    script_dir = Path(__file__).resolve().parent
    governed: dict[str, dict[str, Any]] = {}

    def verify_launchd() -> str:
        from reconcile_launchd_environment import Reconciler

        Reconciler(source_root=script_dir, home=home, hermes_home=hermes_home).verify()
        return "source/deployed launchd assets match"

    def verify_marketplace() -> str:
        from reconcile_marketplace_skills import Reconciler

        Reconciler(source_root=script_dir, home=home, hermes_home=hermes_home).verify()
        return "source/deployed marketplace assets match"

    def verify_pr_pipeline() -> str:
        from reconcile_pr_pipeline import verify

        report = verify(hermes_home / "scripts")
        if not report.get("ok"):
            raise RuntimeError("PR pipeline manifest/deployment mismatch")
        recorded = report.get("recorded_source_commit")
        if not recorded:
            raise RuntimeError("PR pipeline source commit missing")
        if recorded != actual_sha:
            raise RuntimeError(
                "PR pipeline source commit does not exactly equal the active runtime"
            )
        receipt_id = report.get("reconciliation_receipt_id")
        if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_id):
            raise RuntimeError("PR pipeline reconciliation receipt is invalid")
        smoke = report.get("review_gate_smoke") or {}
        if smoke.get("ok") is not True:
            raise RuntimeError("PR pipeline review-gate smoke receipt is invalid")
        return "manifest hashes, exact source commit, reconciliation receipt, and root smoke match"

    governed["launchd-environment"] = _safe_probe(verify_launchd)
    governed["marketplace-skills"] = _safe_probe(verify_marketplace)
    governed["pr-pipeline"] = _safe_probe(verify_pr_pipeline)

    skill_names = sorted(
        {
            str(name)
            for job in jobs
            for name in ([job.get("skill")] + list(job.get("skills") or []))
            if name and not job.get("no_agent")
        }
    )
    try:
        from agent.skill_commands import scan_skill_commands

        commands = scan_skill_commands()
    except Exception:
        commands = {}
    skills = [
        {"name": name, "available": f"/{name}" in commands}
        for name in skill_names
    ]

    try:
        from agent.lazy_secret_resolver import RequiredSecretError, get_required
    except Exception:
        RequiredSecretError = ()  # type: ignore[misc,assignment]
        get_required = None  # type: ignore[assignment]

    credentials: list[dict[str, Any]] = []
    for job in jobs:
        for entry in job.get("required_environment_variables") or []:
            # Entries are shape-preserving (cron.jobs._normalize_required_environment_variables,
            # 86e2m2c1g): a plain string is a required var name, a dict
            # carries {"name": ..., "optional": ...}. Extract the name
            # explicitly instead of str()-ing the raw entry, which would
            # otherwise stringify a dict into a garbage credential name.
            if isinstance(entry, dict):
                name = entry.get("name")
                optional = bool(entry.get("optional"))
            else:
                name = entry
                optional = False
            if not isinstance(name, str) or not name:
                continue
            available = False
            classification = "fatal"
            try:
                value = get_required(name) if get_required is not None else None
                available = bool(value)
                if get_required is not None:
                    classification = "available" if available else "missing"
            except RequiredSecretError as exc:
                classification = exc.classification
            except Exception:
                classification = "fatal"
            credentials.append(
                {
                    "job": job.get("name") or job.get("id"),
                    "name": name,
                    "optional": optional,
                    "available": available,
                    "classification": classification,
                }
            )

    cron_rows: list[dict[str, Any]] = []
    for job in jobs:
        latest_execution = job.get("latest_execution") or {}
        row = {
            "id": job.get("id"),
            "name": job.get("name"),
            "enabled": bool(job.get("enabled", True)),
            "last_status": job.get("last_status"),
            "latest_execution_status": latest_execution.get("status"),
        }
        if job.get("name") == "research-stage-monitor":
            state_path = Path(tempfile.gettempdir()) / f"research-stage-attestation-{os.getpid()}.json"
            production_state = hermes_home / "state" / "research-stage-monitor.json"
            if production_state.is_file() and not production_state.is_symlink():
                shutil.copy2(production_state, state_path)
            result = _run(
                [
                    sys.executable,
                    str(script_dir / "research_stage_monitor.py"),
                    "--state",
                    str(state_path),
                ]
            )
            state_path.unlink(missing_ok=True)
            try:
                monitor = json.loads(result.stdout)
                row["monitor_status"] = monitor.get("status")
                row["contract"] = {
                    key: monitor.get(key)
                    for key in (
                        "served_rate",
                        "degraded_rate",
                        "ungrounded_rate",
                        "fetch_success_rate",
                    )
                    if key in monitor
                }
            except json.JSONDecodeError:
                row["monitor_status"] = "invalid-output"
                row["contract"] = {"exit_code": result.returncode}
        cron_rows.append(row)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "symlink": current_link.is_symlink(),
            "target": str(runtime),
            "target_under_releases": runtime.parent == releases_dir.resolve(),
            "actual_sha": actual_sha,
            "expected_sha": expected_sha,
            "dirty_paths": dirty_paths,
            "source_binding": _source_binding(runtime, actual_sha),
        },
        "services": {
            name: _collect_service(name, contract, runtime, hermes_home)
            for name, contract in SERVICE_CONTRACTS.items()
        },
        "config": {
            "current": current_version,
            "latest": latest_version,
            "receipt_valid": receipt_valid,
            "receipt_detail": receipt_detail,
        },
        "release": release,
        "governed": governed,
        "execution": classify_stale_executions(),
        "skills": skills,
        "credentials": credentials,
        "cron": cron_rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument(
        "--migrate-config",
        action="store_true",
        help="run the ordinary safe config migration and write a content-addressed receipt",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="evaluate an existing normalized JSON snapshot instead of collecting live state",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    home = args.home.expanduser().resolve()
    hermes_home = (
        args.hermes_home.expanduser().resolve()
        if args.hermes_home
        else Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser().resolve()
    )
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    else:
        runtime = (hermes_home / "runtime-current").resolve()
        runtime_sha = _git_output(runtime, "rev-parse", "HEAD")
        if args.migrate_config:
            record_config_migration(hermes_home, runtime_sha)
        snapshot = collect_live(home, hermes_home)
    report = evaluate_snapshot(snapshot)
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
