"""Fail-closed guard for ignite-email-infra poller paths on the Hermes Mini.

Hermes governed installers must not mutate poller staging or deploy surfaces
unless the active production-write lease includes the registry resource
``purelymail-poller-deploy``. Cross-repo deploy ownership lives in
``ignite-email-infra``; this module enforces the Hermes-side half of the
contract documented in ``cross-repo-operating-contract.md``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

MANIFEST_REL = "machine-setup/ignite-email-infra.resource-manifest.json"
REGISTRY_RESOURCE_ID = "purelymail-poller-deploy"


class CrossRepoPollerGuardError(RuntimeError):
    """A Hermes entry point attempted a forbidden poller deploy mutation."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_manifest() -> dict:
    path = _repo_root() / MANIFEST_REL
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossRepoPollerGuardError(f"cross-repo resource manifest unavailable: {exc}") from exc
    if payload.get("manifest_kind") != "hermes-cross-repo-resource-manifest":
        raise CrossRepoPollerGuardError("cross-repo resource manifest has an unexpected kind")
    return payload


def protected_path_prefixes() -> tuple[str, ...]:
    manifest = _load_manifest()
    prefixes: list[str] = []
    for entry in manifest.get("paths", []):
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if isinstance(raw, str) and raw.strip():
            prefixes.append(raw.rstrip("/"))
    lock = manifest.get("runtime_lock")
    if isinstance(lock, dict):
        raw = lock.get("path")
        if isinstance(raw, str) and raw.strip():
            prefixes.append(raw.rstrip("/"))
    deploy_root = next(
        (
            entry.get("path", "").rstrip("/")
            for entry in manifest.get("paths", [])
            if isinstance(entry, dict) and entry.get("id") == "poller-deploy-root"
        ),
        "~/.hermes/deploy/purelymail-poller",
    )
    prefixes.append(deploy_root.rstrip("/"))
    return tuple(dict.fromkeys(prefixes))


def normalize_mini_path(value: str | Path, *, home: Path | None = None) -> str:
    text = str(value).replace("\\", "/")
    if text.startswith("~/"):
        base = (home or Path.home()).expanduser()
        text = str((base / text[2:]).resolve())
    else:
        text = str(Path(text).expanduser().resolve())
    return text.rstrip("/")


def matches_protected_poller_path(dest: str | Path, *, home: Path | None = None) -> bool:
    normalized = normalize_mini_path(dest, home=home)
    for prefix in protected_path_prefixes():
        expanded = normalize_mini_path(prefix, home=home)
        if normalized == expanded or normalized.startswith(expanded + "/"):
            return True
    return False


def assert_destination_allowed(
    dest: str | Path,
    *,
    lease_resources: Iterable[str],
    home: Path | None = None,
) -> None:
    if not matches_protected_poller_path(dest, home=home):
        return
    held = {str(value) for value in lease_resources}
    if REGISTRY_RESOURCE_ID in held:
        return
    raise CrossRepoPollerGuardError(
        "refusing Hermes mutation of ignite-email-infra poller deploy path "
        f"{dest!s} without production-write lease resource {REGISTRY_RESOURCE_ID!r}; "
        "use ignite-email-infra poller/deploy-poller.sh instead"
    )


def scan_sources_for_forbidden_poller_writes(
    sources: Iterable[tuple[str, str]],
) -> list[str]:
    """Return human-readable violations for static CI scans of entry-point text."""
    forbidden_markers = (
        ".hermes/deploy/purelymail-poller",
        "deploy/purelymail-poller/",
        "purelymail-poller/incoming",
        "purelymail-poller/backups",
    )
    violations: list[str] = []
    for label, text in sources:
        for marker in forbidden_markers:
            if marker in text:
                violations.append(f"{label}: contains forbidden poller deploy marker {marker!r}")
    return violations
