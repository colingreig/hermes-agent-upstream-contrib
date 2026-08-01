#!/usr/bin/env python3
"""Manifest-verified installer for the declarative fleet-config bundle.

This script is the SOLE writer of four things on the target machine:

  1. ``~/.hermes/config.yaml``       — deep-merges ``config-overlay.yaml`` in.
  2. ``~/.hermes/profiles/<name>/``  — installs the five named profiles
                                        (config.yaml + SOUL.md + bootstrap dirs).
  3. ``~/.hermes/cron/jobs.json``    — wholesale REPLACE with the curated set.
  4. Profile-local skill trees       — applies ``skills-policy.json`` using
                                        recoverable archives and per-skill
                                        suppression markers.

It reads ``fleet_config_manifest.json`` and the versioned skill policy,
verifies declared source hashes, snapshots every existing destination it will
mutate, then atomically installs and re-verifies the deployed state.

stdlib + PyYAML only. All destination roots derive from ``--home`` (default
``~``), so the whole flow is sandbox-testable against a tmp dir with no
ability to touch a real ``~/.hermes``.

Overlay merge semantics (``merge_overlay``) — deliberately NOT a naive
recursive-dict-merge:

  - dict value, non-empty  -> recurse into the existing subtree (create it if
    the destination doesn't have that key yet).
  - dict value, EMPTY ({}) -> REPLACE the destination key with {} outright.
    This is how ``config-overlay.yaml``'s ``delegation: {}`` means "clear
    this section" rather than "merge nothing" — an empty-dict overlay is a
    deliberate reset, not a no-op. Without this rule a deep merge of {} into
    an existing delegation block would silently do nothing, which is the
    opposite of the intent (see the past delegation.base_url + named-provider
    credential-leak bug this bundle exists to prevent recurring).
  - list / scalar value    -> REPLACE the destination key wholesale (e.g.
    ``fallback_providers`` in the overlay replaces the live chain, it does
    not append to it).

Every other key in the live config — platforms, secrets wiring, security,
approvals, credential_pool_strategies, etc. — is left completely untouched
because the overlay never mentions them.

Usage:
    install_fleet_config.py --dry-run
    install_fleet_config.py
    install_fleet_config.py --home /tmp/fake-hermes-home --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a hard runtime dependency
    print("install_fleet_config.py requires PyYAML (pip install pyyaml)", file=sys.stderr)
    raise

# Every destination must resolve under <home>/.hermes/ — a manifest that
# points anywhere else is rejected before any write happens.
ALLOWED_DEST_SUBPATH = ".hermes"

# Profile bootstrap subdirectories, mirrored verbatim from
# hermes_cli/profiles.py::_PROFILE_DIRS so a profile installed by this bundle
# is indistinguishable from one created by `hermes profile create`.
PROFILE_BOOTSTRAP_DIRS = [
    "memories",
    "sessions",
    "skills",
    "skins",
    "logs",
    "plans",
    "workspace",
    "cron",
    "home",
]

SKILL_POLICY_PROFILES = ("default", "coder", "content", "ops", "design", "research")
SKILL_POLICY_CONFIG_MODE = 0o600


class InstallError(RuntimeError):
    """Fail-closed installer error (drift, out-of-bounds dest, verify failure)."""


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expand(path_str: str, home: Path) -> Path:
    """Expand a manifest dest_abs against an explicit home root.

    ``~`` / ``~/x`` map onto ``home`` so tests/dry-runs can pin everything to
    a tmp dir. Any other (already-absolute) path is returned verbatim.
    """
    if path_str == "~":
        return home
    if path_str.startswith("~/"):
        return home / path_str[2:]
    return Path(path_str)


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _check_dest_in_bounds(dest: Path, dest_abs: str, allowed_root: Path) -> None:
    """Refuse any dest that could resolve outside ``allowed_root``.

    Same two-guard approach as install_self_report.py: reject a literal
    ``..`` component outright (lexical, pre-normalization), then re-check the
    normalized path's nearest existing ancestor against the resolved root so
    a symlinked intermediate directory can't redirect the write outside
    ``allowed_root`` either.
    """
    if ".." in dest.parts:
        raise InstallError(
            f"manifest destination {dest_abs!r} contains a '..' path component — refusing"
        )

    normalized = Path(os.path.normpath(str(dest)))
    try:
        normalized.relative_to(allowed_root)
    except ValueError as exc:
        raise InstallError(
            f"manifest destination {dest_abs!r} resolves to {normalized} "
            f"which is outside {allowed_root} — refusing"
        ) from exc

    resolved_root = allowed_root.resolve()
    ancestor = normalized
    while not ancestor.exists() and ancestor.parent != ancestor:
        ancestor = ancestor.parent
    resolved_ancestor = ancestor.resolve()
    try:
        resolved_ancestor.relative_to(resolved_root)
    except ValueError as exc:
        raise InstallError(
            f"manifest destination {dest_abs!r} resolves (via ancestor {ancestor}) "
            f"to {resolved_ancestor} which is outside {resolved_root} — refusing"
        ) from exc


def load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _verify_sources(manifest: dict, bundle_root: Path) -> list[dict]:
    """Verify every manifest file's sha256 against its source bytes.

    Returns the manifest's file entries augmented with a resolved ``src``
    Path. Raises InstallError on any hash drift or missing source — nothing
    is written here.
    """
    verified: list[dict] = []
    for entry in manifest["files"]:
        src = bundle_root / entry["src_rel"]
        if not src.is_file():
            raise InstallError(f"source missing for {entry['src_rel']!r}: {src}")
        actual = _sha256(src)
        expected = entry["sha256"]
        if actual != expected:
            raise InstallError(
                f"source hash drift for {entry['src_rel']!r}: manifest {expected} "
                f"!= source {actual} — refusing to install unverified bytes"
            )
        merged = dict(entry)
        merged["src"] = src
        verified.append(merged)
    return verified


# ---------------------------------------------------------------------------
# Overlay deep-merge (config.yaml)
# ---------------------------------------------------------------------------

def merge_overlay(base: dict, overlay: dict) -> dict:
    """Deep-merge ``overlay`` onto ``base`` and return a NEW dict.

    See the module docstring for the exact three-case rule. ``base`` is never
    mutated in place — callers get a fresh merged dict back so the original
    live-config dict remains available for diffing/backup.
    """
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict):
            if value:
                existing = result.get(key)
                result[key] = merge_overlay(existing if isinstance(existing, dict) else {}, value)
            else:
                # Explicit empty-dict overlay = reset this section to empty.
                result[key] = {}
        else:
            # Scalars and lists replace wholesale (never appended/merged).
            result[key] = value
    return result


def _diff_keys(before: dict, after: dict, prefix: str = "") -> list[str]:
    """Human-readable list of top-level overlay-affected key changes."""
    lines: list[str] = []
    keys = sorted(set(before.keys()) | set(after.keys()))
    for k in keys:
        b = before.get(k, "<absent>")
        a = after.get(k, "<absent>")
        if b != a:
            lines.append(f"{prefix}{k}: {b!r} -> {a!r}")
    return lines


# ---------------------------------------------------------------------------
# Plan / install
# ---------------------------------------------------------------------------

def build_plan(manifest: dict, *, home: Path, bundle_root: Path) -> dict:
    """Resolve + verify sources for the governed install steps.

    Returns a dict with keys ``config_overlay``, ``profiles``, ``jobs_json``,
    each holding the resolved entries for that step. Raises InstallError on
    any source-hash drift or out-of-bounds destination. Nothing is written.
    """
    allowed_root = home / ALLOWED_DEST_SUBPATH
    verified = _verify_sources(manifest, bundle_root)

    plan: dict[str, Any] = {
        "config_overlay": None,
        "profiles": [],
        "jobs_json": None,
        "skill_policy": None,
    }

    for entry in verified:
        mode = entry.get("deploy_mode")
        if mode == "skill_policy":
            plan["skill_policy"] = entry
            continue
        dest = _expand(entry["dest_abs"], home)
        _check_dest_in_bounds(dest, entry["dest_abs"], allowed_root)
        item = {**entry, "dest": dest}
        if mode == "config_overlay":
            plan["config_overlay"] = item
        elif mode == "profile_file":
            plan["profiles"].append(item)
        elif mode == "jobs_json":
            plan["jobs_json"] = item
        else:
            raise InstallError(f"unknown deploy_mode {mode!r} for {entry['src_rel']!r}")

    if plan["config_overlay"] is None:
        raise InstallError("manifest has no config_overlay entry")
    if plan["jobs_json"] is None:
        raise InstallError("manifest has no jobs_json entry")
    if not plan["profiles"]:
        raise InstallError("manifest has no profile_file entries")

    return plan


def _print_config_plan(item: dict, home: Path) -> None:
    overlay = yaml.safe_load(item["src"].read_text(encoding="utf-8")) or {}
    dest = item["dest"]
    if dest.is_file():
        try:
            current = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InstallError(f"existing {dest} is not valid YAML: {exc}") from exc
    else:
        current = {}
    merged = merge_overlay(current, overlay)
    diff = _diff_keys(current, merged)
    print(f"config overlay -> {dest} (home={home}):")
    if not diff:
        print("  (no effective change)")
    for line in diff:
        print(f"  {line}")
    current_mode = _config_mode(dest)
    if current_mode is None:
        print("  mode: create as 0600")
    elif current_mode != SKILL_POLICY_CONFIG_MODE:
        print(f"  mode: {current_mode:04o} -> {SKILL_POLICY_CONFIG_MODE:04o}")
    else:
        print("  mode: 0600 (already governed)")


def _print_profiles_plan(items: list[dict]) -> None:
    print("profile files:")
    for item in items:
        exists = "overwrite" if item["dest"].exists() else "create"
        print(f"  [{exists}] {item['src']} -> {item['dest']} sha256={item['sha256']}")
    names = sorted({Path(i["dest_abs"]).parts[-2] for i in items})
    print(f"  bootstrap dirs per profile ({', '.join(PROFILE_BOOTSTRAP_DIRS)}) for: {', '.join(names)}")


def _print_jobs_plan(item: dict) -> None:
    dest = item["dest"]
    new_jobs = json.loads(item["src"].read_text(encoding="utf-8")).get("jobs", [])
    print(f"jobs.json -> {dest}:")
    if dest.is_file():
        try:
            old_jobs = json.loads(dest.read_text(encoding="utf-8")).get("jobs", [])
        except json.JSONDecodeError:
            old_jobs = []
        old_names = {j.get("name") for j in old_jobs}
        new_names = {j.get("name") for j in new_jobs}
        for name in sorted(new_names - old_names):
            print(f"  + {name}")
        for name in sorted(old_names - new_names):
            print(f"  - {name}")
        print(f"  ({len(old_jobs)} existing jobs -> {len(new_jobs)} curated jobs)")
    else:
        print(f"  (no existing jobs.json; installing {len(new_jobs)} jobs)")


def _snapshot_path(dest: Path, snapshot_dir: Path, destination_root: Path) -> Path:
    """Return a collision-free central snapshot path derived from ``dest``."""
    relative_dest = dest.relative_to(destination_root)
    return snapshot_dir / "destinations" / relative_dest


def _backup(
    dest: Path,
    snapshot_dir: Path,
    destination_root: Path,
    tag: str,
    stamp: str,
    *,
    private: bool = False,
) -> Path | None:
    if not dest.exists():
        return None
    snapshot = _snapshot_path(dest, snapshot_dir, destination_root)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dest, snapshot)
    sibling = dest.with_name(dest.name + f".bak-{tag}-install-{stamp}")
    shutil.copy2(dest, sibling)
    if private:
        os.chmod(snapshot, SKILL_POLICY_CONFIG_MODE)
        os.chmod(sibling, SKILL_POLICY_CONFIG_MODE)
    return snapshot


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def _frontmatter_name(skill_md: Path) -> str | None:
    """Read a skill's frontmatter name without parsing the markdown body."""
    try:
        lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_frontmatter = False
    for line in lines[:100]:
        stripped = line.strip()
        if stripped == "---":
            if in_frontmatter:
                break
            in_frontmatter = True
            continue
        if in_frontmatter and stripped.startswith("name:"):
            value = stripped.split(":", 1)[1].strip().strip("\"'")
            return value or None
    return None


def _validate_skill_map(label: str, value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise InstallError(f"skill policy {label!r} must be a non-empty object")
    result: dict[str, str] = {}
    for name, rel_text in value.items():
        if not isinstance(name, str) or not name or not isinstance(rel_text, str):
            raise InstallError(f"skill policy {label!r} contains an invalid name/path pair")
        rel = PurePosixPath(rel_text)
        if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
            raise InstallError(f"skill policy path {rel_text!r} for {name!r} is unsafe")
        result[name] = rel.as_posix()
    if len(set(result.values())) != len(result):
        raise InstallError(f"skill policy {label!r} contains duplicate paths")
    return result


def _validate_relative_path(label: str, value: object) -> str:
    if not isinstance(value, str):
        raise InstallError(f"skill policy {label!r} must be a relative path")
    rel = PurePosixPath(value)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        raise InstallError(f"skill policy {label!r} contains unsafe path {value!r}")
    return rel.as_posix()


def _validate_hub_remove(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or not value:
        raise InstallError("skill policy 'hub_shadow_remove' must be a non-empty object")
    result: dict[str, dict[str, str]] = {}
    for name, spec in value.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise InstallError("skill policy 'hub_shadow_remove' has an invalid entry")
        if set(spec) != {"install_path", "source", "identifier"}:
            raise InstallError(
                f"skill policy hub removal {name!r} must pin install_path, source, and identifier"
            )
        install_path = _validate_relative_path(
            f"hub_shadow_remove.{name}.install_path", spec.get("install_path")
        )
        source = spec.get("source")
        identifier = spec.get("identifier")
        if not isinstance(source, str) or not source or not isinstance(identifier, str) or not identifier:
            raise InstallError(
                f"skill policy hub removal {name!r} must have non-empty source and identifier"
            )
        result[name] = {
            "install_path": install_path,
            "source": source,
            "identifier": identifier,
        }
    paths = [spec["install_path"] for spec in result.values()]
    if len(paths) != len(set(paths)):
        raise InstallError("skill policy 'hub_shadow_remove' contains duplicate install paths")
    return result


def _validate_reference_consolidations(
    value: object,
    *,
    local_remove: dict[str, str],
    local_keep: dict[str, str],
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise InstallError("skill policy local_reference_consolidations must be a non-empty list")
    required = {
        "source_skill",
        "source_rel",
        "source_sha256",
        "destination_skill",
        "destination_rel",
    }
    result: list[dict[str, str]] = []
    destinations: set[tuple[str, str]] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) != required:
            raise InstallError(
                f"skill policy reference consolidation {index} must contain exactly {sorted(required)}"
            )
        source_skill = raw.get("source_skill")
        destination_skill = raw.get("destination_skill")
        if not isinstance(source_skill, str) or source_skill not in local_remove:
            raise InstallError(
                f"reference consolidation {index} source skill {source_skill!r} is not locally removed"
            )
        if not isinstance(destination_skill, str) or destination_skill not in local_keep:
            raise InstallError(
                f"reference consolidation {index} destination skill {destination_skill!r} is not kept"
            )
        source_rel = _validate_relative_path(
            f"local_reference_consolidations[{index}].source_rel", raw.get("source_rel")
        )
        destination_rel = _validate_relative_path(
            f"local_reference_consolidations[{index}].destination_rel",
            raw.get("destination_rel"),
        )
        expected_sha = raw.get("source_sha256")
        if (
            not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or any(ch not in "0123456789abcdef" for ch in expected_sha)
        ):
            raise InstallError(f"reference consolidation {index} has an invalid source_sha256")
        destination_key = (destination_skill, destination_rel)
        if destination_key in destinations:
            raise InstallError(f"reference consolidation {index} duplicates a destination")
        destinations.add(destination_key)
        result.append({
            "source_skill": source_skill,
            "source_rel": source_rel,
            "source_sha256": expected_sha,
            "destination_skill": destination_skill,
            "destination_rel": destination_rel,
        })
    return result


def load_skill_policy(policy_path: Path, *, bundle_root: Path) -> dict[str, Any]:
    """Load and fail-closed validate the Mini skill policy against this checkout."""
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"could not load skill policy {policy_path}: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise InstallError("skill policy must be a version 1 object")
    if not isinstance(policy.get("policy_id"), str) or not policy["policy_id"]:
        raise InstallError("skill policy has no policy_id")

    bundled = policy.get("bundled")
    if not isinstance(bundled, dict):
        raise InstallError("skill policy has no bundled section")
    remove = _validate_skill_map("bundled.remove", bundled.get("remove"))
    keep = _validate_skill_map("bundled.keep", bundled.get("keep"))
    if set(remove) & set(keep):
        raise InstallError("skill policy bundled remove/keep sets overlap")
    if len(remove) != 52 or len(keep) != 21:
        raise InstallError(
            f"skill policy must classify 52 removals and 21 keeps; got {len(remove)} and {len(keep)}"
        )

    source_rel = policy.get("bundled_skills_root")
    if not isinstance(source_rel, str):
        raise InstallError("skill policy has no bundled_skills_root")
    source_root = (bundle_root / source_rel).resolve()
    if not source_root.is_dir():
        raise InstallError(f"bundled skill source is missing: {source_root}")
    discovered: dict[str, str] = {}
    for skill_md in sorted(source_root.rglob("SKILL.md")):
        name = _frontmatter_name(skill_md)
        if not name:
            raise InstallError(f"bundled skill has no frontmatter name: {skill_md}")
        rel = skill_md.parent.relative_to(source_root).as_posix()
        if name in discovered:
            raise InstallError(f"duplicate bundled skill name {name!r} in source tree")
        discovered[name] = rel
    classified = {**remove, **keep}
    if classified != discovered:
        missing = sorted(set(discovered) - set(classified))
        extra = sorted(set(classified) - set(discovered))
        wrong = sorted(name for name in set(discovered) & set(classified) if discovered[name] != classified[name])
        raise InstallError(
            "skill policy does not exactly classify the bundled source tree "
            f"(missing={missing}, extra={extra}, wrong_paths={wrong})"
        )

    local_remove = _validate_skill_map("local_remove", policy.get("local_remove"))
    hub_remove = _validate_hub_remove(policy.get("hub_shadow_remove"))
    local_keep = _validate_skill_map("required_local_keep", policy.get("required_local_keep"))
    consolidations = _validate_reference_consolidations(
        policy.get("local_reference_consolidations"),
        local_remove=local_remove,
        local_keep=local_keep,
    )
    all_names = set(classified) | set(local_remove) | set(hub_remove) | set(local_keep)
    if len(all_names) != len(classified) + len(local_remove) + len(hub_remove) + len(local_keep):
        raise InstallError("skill policy has overlapping bundled/local/hub names")

    profiles = policy.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(SKILL_POLICY_PROFILES):
        raise InstallError(f"skill policy profiles must be exactly {list(SKILL_POLICY_PROFILES)}")
    for profile, spec in profiles.items():
        if not isinstance(spec, dict) or spec.get("bundled_mode") not in {"keep", "empty"}:
            raise InstallError(f"skill policy profile {profile!r} has invalid bundled_mode")
        expected = spec.get("expected_active_manifests")
        if not isinstance(expected, int) or expected < 0:
            raise InstallError(f"skill policy profile {profile!r} has invalid expected count")
        derived = len(keep) if spec["bundled_mode"] == "keep" else 0
        if profile == "default":
            derived += len(local_keep)
        if expected != derived:
            raise InstallError(
                f"skill policy profile {profile!r} expected count {expected} != derived {derived}"
            )

    policy["bundled"]["remove"] = remove
    policy["bundled"]["keep"] = keep
    policy["local_remove"] = local_remove
    policy["local_reference_consolidations"] = consolidations
    policy["hub_shadow_remove"] = hub_remove
    policy["required_local_keep"] = local_keep
    policy["_source_root"] = source_root
    policy["_path"] = policy_path
    return policy


def _profile_home(home: Path, profile: str) -> Path:
    return home / ".hermes" if profile == "default" else home / ".hermes" / "profiles" / profile


def _active_skill_manifests(skills_dir: Path) -> list[Path]:
    if not skills_dir.exists():
        return []
    return [
        path for path in skills_dir.rglob("SKILL.md")
        if not any(part.startswith(".") for part in path.relative_to(skills_dir).parts)
    ]


def _read_suppressed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return {
            line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError as exc:
        raise InstallError(f"could not read suppression file {path}: {exc}") from exc


def _validate_installed_skill(path: Path, expected_name: str) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing to archive symlinked skill path: {path}")
    skill_md = path / "SKILL.md"
    if not path.is_dir() or not skill_md.is_file():
        raise InstallError(f"skill policy target is not a skill directory: {path}")
    actual_name = _frontmatter_name(skill_md)
    if actual_name != expected_name:
        raise InstallError(
            f"skill policy target {path} has name {actual_name!r}, expected {expected_name!r}"
        )


def _bundled_target_is_active(
    path: Path,
    expected_name: str,
    source_path: Path,
) -> bool:
    """Validate one installed bundled target and report whether it is active.

    Hermes skill sync suppresses a pruned bundled ``SKILL.md`` tree, but its
    category-metadata pass still restores ``DESCRIPTION.md`` files.  A bundled
    skill that is also a category directory can therefore legitimately remain
    as an inactive, metadata-only directory.  Accept only that exact
    source-identical residue; every other incomplete target remains a hard
    failure.
    """
    if path.is_symlink():
        raise InstallError(f"refusing to archive symlinked skill path: {path}")
    if not path.is_dir():
        raise InstallError(f"skill policy target is not a skill directory: {path}")

    skill_md = path / "SKILL.md"
    if skill_md.is_file():
        _validate_installed_skill(path, expected_name)
        return True

    try:
        nested_manifests = sorted(
            candidate for candidate in path.rglob("SKILL.md")
            if candidate != skill_md
        )
        entries = sorted(path.iterdir(), key=lambda candidate: candidate.name)
    except OSError as exc:
        raise InstallError(
            f"could not inspect bundled skill residue {path}: {exc}"
        ) from exc
    if nested_manifests:
        raise InstallError(
            f"bundled skill residue contains nested skill manifests: {path}"
        )
    if [entry.name for entry in entries] != ["DESCRIPTION.md"]:
        raise InstallError(
            f"bundled skill residue must contain only DESCRIPTION.md: {path}"
        )

    installed_description = entries[0]
    source_description = source_path / "DESCRIPTION.md"
    if (
        installed_description.is_symlink()
        or not installed_description.is_file()
    ):
        raise InstallError(
            "bundled skill residue DESCRIPTION.md is not a regular file: "
            f"{installed_description}"
        )
    if source_description.is_symlink() or not source_description.is_file():
        raise InstallError(
            f"bundled skill residue has no regular source DESCRIPTION.md: {source_description}"
        )
    installed_sha = _sha256(installed_description)
    source_sha = _sha256(source_description)
    if installed_sha != source_sha:
        raise InstallError(
            f"bundled skill residue DESCRIPTION.md does not match source: "
            f"{installed_description} sha256 {installed_sha} != {source_sha}"
        )
    return False


def _load_hub_lock(lock_path: Path) -> dict[str, Any]:
    if not lock_path.exists():
        return {"version": 1, "installed": {}}
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"could not read hub lock {lock_path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("installed"), dict):
        raise InstallError(f"hub lock has invalid shape: {lock_path}")
    return data


def build_skill_policy_plan(policy: dict[str, Any], *, home: Path) -> list[dict[str, Any]]:
    """Resolve policy actions and prove the resulting active manifest counts."""
    actions: list[dict[str, Any]] = []
    remove = policy["bundled"]["remove"]
    keep = policy["bundled"]["keep"]
    all_bundled = {**remove, **keep}
    destination_root = home / ALLOWED_DEST_SUBPATH
    for profile in SKILL_POLICY_PROFILES:
        profile_home = _profile_home(home, profile)
        skills_dir = profile_home / "skills"
        _check_dest_in_bounds(skills_dir, str(skills_dir), destination_root)
        spec = policy["profiles"][profile]
        bundled_targets = remove if spec["bundled_mode"] == "keep" else all_bundled
        suppression_names = set(bundled_targets)
        target_rows: list[dict[str, Any]] = []
        for name, rel in bundled_targets.items():
            path = skills_dir / rel
            _check_dest_in_bounds(path, str(path), destination_root)
            if path.exists() or path.is_symlink():
                source_path = policy["_source_root"] / rel
                if _bundled_target_is_active(path, name, source_path):
                    target_rows.append({
                        "name": name,
                        "rel": rel,
                        "path": path,
                        "kind": "bundled",
                    })

        if profile == "default":
            for name, rel in policy["local_remove"].items():
                path = skills_dir / rel
                _check_dest_in_bounds(path, str(path), destination_root)
                if path.exists() or path.is_symlink():
                    _validate_installed_skill(path, name)
                    target_rows.append({"name": name, "rel": rel, "path": path, "kind": "local"})

            consolidation_rows: list[dict[str, Any]] = []
            for spec_row in policy["local_reference_consolidations"]:
                source = (
                    skills_dir
                    / policy["local_remove"][spec_row["source_skill"]]
                    / spec_row["source_rel"]
                )
                destination = (
                    skills_dir
                    / policy["required_local_keep"][spec_row["destination_skill"]]
                    / spec_row["destination_rel"]
                )
                _check_dest_in_bounds(source, str(source), destination_root)
                _check_dest_in_bounds(destination, str(destination), destination_root)
                expected_sha = spec_row["source_sha256"]
                source_valid = False
                if source.exists() or source.is_symlink():
                    if source.is_symlink() or not source.is_file():
                        raise InstallError(
                            f"reference consolidation source is not a regular file: {source}"
                        )
                    actual_sha = _sha256(source)
                    if actual_sha != expected_sha:
                        raise InstallError(
                            f"reference consolidation source {source} sha256 {actual_sha} "
                            f"!= pinned {expected_sha}"
                        )
                    source_valid = True

                destination_valid = False
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not destination.is_file():
                        raise InstallError(
                            f"reference consolidation destination is not a regular file: {destination}"
                        )
                    actual_sha = _sha256(destination)
                    if actual_sha != expected_sha:
                        raise InstallError(
                            f"reference consolidation destination {destination} sha256 {actual_sha} "
                            f"!= pinned {expected_sha}"
                        )
                    destination_valid = True

                if not source_valid and not destination_valid:
                    raise InstallError(
                        "historical reference is missing from both source and kept archive: "
                        f"{source} -> {destination}"
                    )
                if source_valid and not destination_valid:
                    consolidation_rows.append({
                        **spec_row,
                        "source": source,
                        "destination": destination,
                    })

            lock_path = skills_dir / ".hub" / "lock.json"
            _check_dest_in_bounds(lock_path, str(lock_path), destination_root)
            lock_data = _load_hub_lock(lock_path)
            hub_lock_removals: list[str] = []
            for name, hub_spec in policy["hub_shadow_remove"].items():
                rel = hub_spec["install_path"]
                path = skills_dir / rel
                _check_dest_in_bounds(path, str(path), destination_root)
                entry = lock_data["installed"].get(name)
                entry_matches = (
                    isinstance(entry, dict)
                    and entry.get("install_path") == rel
                    and entry.get("source") == hub_spec["source"]
                    and entry.get("identifier") == hub_spec["identifier"]
                )
                if path.exists() or path.is_symlink():
                    _validate_installed_skill(path, name)
                    if not entry_matches:
                        raise InstallError(
                            f"refusing to remove {path}: no matching hub provenance in {lock_path}"
                        )
                    target_rows.append({"name": name, "rel": rel, "path": path, "kind": "hub"})
                if entry is not None:
                    if not entry_matches:
                        raise InstallError(
                            f"hub lock entry for {name!r} does not match pinned source, "
                            "identifier, and install_path"
                        )
                    hub_lock_removals.append(name)

            for name, rel in policy["required_local_keep"].items():
                path = skills_dir / rel
                _check_dest_in_bounds(path, str(path), destination_root)
                if not path.exists():
                    raise InstallError(f"required local skill is missing: {path}")
                _validate_installed_skill(path, name)
        else:
            consolidation_rows = []
            hub_lock_removals = []

        current = len(_active_skill_manifests(skills_dir))
        predicted = current - len({row["path"] for row in target_rows})
        expected = spec["expected_active_manifests"]
        if predicted != expected:
            raise InstallError(
                f"skill policy would leave {profile} with {predicted} active manifests; expected {expected}"
            )
        actions.append({
            "profile": profile,
            "home": profile_home,
            "skills_dir": skills_dir,
            "targets": target_rows,
            "consolidations": consolidation_rows,
            "hub_lock_removals": hub_lock_removals,
            "suppression_names": suppression_names,
            "suppression_path": skills_dir / ".curator_suppressed",
            "current_count": current,
            "predicted_count": predicted,
            "expected_count": expected,
        })
    return actions


def _print_skill_policy_plan(policy: dict[str, Any], actions: list[dict[str, Any]]) -> None:
    print(f"skill policy {policy['policy_id']} sha256={_sha256(policy['_path'])}:")
    for action in actions:
        suppressed = _read_suppressed(action["suppression_path"])
        additions = action["suppression_names"] - suppressed
        print(
            f"  {action['profile']}: archive {len(action['targets'])}, "
            f"consolidate {len(action['consolidations'])}, "
            f"suppress +{len(additions)}, active {action['current_count']} -> {action['predicted_count']}"
        )
    print("  broad .no-bundled-skills marker: untouched")


def _snapshot_skill_targets(action: dict[str, Any], dest: Path) -> Path:
    """Create a curator-style pre-change tarball of every policy-mutated byte."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    skills_dir = action["skills_dir"]
    members = {row["path"] for row in action["targets"]}
    for metadata in (action["suppression_path"], skills_dir / ".hub" / "lock.json"):
        if metadata.exists():
            members.add(metadata)
    with tarfile.open(dest, "w:gz", compresslevel=6) as archive:
        for path in sorted(members, key=str):
            archive.add(path, arcname=str(path.relative_to(skills_dir)), recursive=True)
    return dest


def _snapshot_for_write(dest: Path, snapshot_dir: Path, destination_root: Path) -> tuple[Path | None, bool]:
    if not dest.exists():
        return None, True
    snapshot = _snapshot_path(dest, snapshot_dir, destination_root)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(dest, snapshot)
    return snapshot, False


def _write_private(dest: Path, data: bytes) -> None:
    _atomic_write(dest, data)
    os.chmod(dest, SKILL_POLICY_CONFIG_MODE)


def _remove_empty_skill_parents(path: Path, skills_dir: Path) -> None:
    parent = path.parent
    while parent != skills_dir:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _apply_skill_policy(
    policy: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    destination_root: Path,
    snapshot_dir: Path,
    stamp: str,
    receipt_steps: list[dict[str, Any]],
) -> None:
    archive_base = destination_root / "archives" / "fleet-skill-policy" / policy["policy_id"] / stamp

    # Every affected profile gets a pre-change tarball before the first move/write.
    for action in actions:
        needs_change = (
            bool(action["targets"])
            or bool(action["consolidations"])
            or bool(action["hub_lock_removals"])
            or bool(action["suppression_names"] - _read_suppressed(action["suppression_path"]))
        )
        if not needs_change:
            continue
        backup = _snapshot_skill_targets(
            action,
            snapshot_dir / "skill-policy-prechange" / f"{action['profile']}-skills.tar.gz",
        )
        receipt_steps.append({
            "step": "skill_policy_backup",
            "profile": action["profile"],
            "snapshot": str(backup),
            "status": "created",
        })

    for action in actions:
        profile = action["profile"]
        # Preserve approved historical reference bytes inside the kept umbrella
        # skill before moving the standalone source skill out of the active tree.
        for row in action["consolidations"]:
            source = row["source"]
            destination = row["destination"]
            receipt = {
                "step": "skill_policy_reference_consolidation",
                "profile": profile,
                "source_skill": row["source_skill"],
                "source": str(source),
                "source_sha256": row["source_sha256"],
                "destination_skill": row["destination_skill"],
                "dest": str(destination),
                "snapshot": None,
                "created": True,
                "cleanup_root": str(
                    action["skills_dir"] / policy["required_local_keep"][row["destination_skill"]]
                ),
                "status": "installing",
            }
            receipt_steps.append(receipt)
            _atomic_write(destination, source.read_bytes())
            deployed_sha = _sha256(destination)
            if deployed_sha != row["source_sha256"]:
                raise InstallError(
                    f"consolidated reference {destination} sha256 {deployed_sha} "
                    f"!= pinned {row['source_sha256']}"
                )
            receipt["status"] = "installed"

        for row in action["targets"]:
            src = row["path"]
            archive_dest = archive_base / profile / row["kind"] / row["rel"]
            _check_dest_in_bounds(archive_dest, str(archive_dest), destination_root)
            archive_dest.parent.mkdir(parents=True, exist_ok=True)
            if archive_dest.exists():
                raise InstallError(f"skill policy archive collision: {archive_dest}")
            shutil.move(str(src), str(archive_dest))
            receipt_steps.append({
                "step": "skill_policy_archive",
                "profile": profile,
                "name": row["name"],
                "kind": row["kind"],
                "source": str(src),
                "archive_dest": str(archive_dest),
                "rollback": "move-back",
                "status": "archived",
            })
            _remove_empty_skill_parents(src, action["skills_dir"])

        suppression_path = action["suppression_path"]
        before = _read_suppressed(suppression_path)
        after = before | action["suppression_names"]
        if after != before:
            snapshot, created = _snapshot_for_write(suppression_path, snapshot_dir, destination_root)
            _write_private(suppression_path, ("\n".join(sorted(after)) + "\n").encode("utf-8"))
            receipt_steps.append({
                "step": "skill_policy_suppression",
                "profile": profile,
                "dest": str(suppression_path),
                "snapshot": str(snapshot) if snapshot else None,
                "created": created,
                "added": sorted(after - before),
                "status": "installed",
            })

        if profile == "default":
            lock_path = action["skills_dir"] / ".hub" / "lock.json"
            lock_data = _load_hub_lock(lock_path)
            changed = False
            for name in action["hub_lock_removals"]:
                if name in lock_data["installed"]:
                    lock_data["installed"].pop(name)
                    changed = True
            if changed:
                snapshot, created = _snapshot_for_write(lock_path, snapshot_dir, destination_root)
                rendered = (json.dumps(lock_data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
                _write_private(lock_path, rendered)
                receipt_steps.append({
                    "step": "skill_policy_hub_lock",
                    "dest": str(lock_path),
                    "snapshot": str(snapshot) if snapshot else None,
                    "created": created,
                    "removed": sorted(policy["hub_shadow_remove"]),
                    "status": "installed",
                })

        actual = len(_active_skill_manifests(action["skills_dir"]))
        if actual != action["expected_count"]:
            raise InstallError(
                f"skill policy left {profile} with {actual} active manifests; "
                f"expected {action['expected_count']}"
            )


def _config_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def install(
    manifest: dict,
    *,
    home: Path,
    bundle_root: Path,
    manifest_path: Path,
    dry_run: bool,
    skill_policy_path: Path | None = None,
) -> int:
    plan = build_plan(manifest, home=home, bundle_root=bundle_root)
    skill_policy = None
    skill_actions: list[dict[str, Any]] = []
    if skill_policy_path is not None:
        manifest_policy = plan.get("skill_policy")
        if manifest_policy is None:
            raise InstallError(
                f"--skills-policy {skill_policy_path} was supplied but the fleet manifest "
                "has no skill_policy entry"
            )
        if manifest_policy["src"].resolve() != skill_policy_path.resolve():
            raise InstallError(
                f"--skills-policy {skill_policy_path} does not match manifest source "
                f"{manifest_policy['src']}"
            )
        skill_policy = load_skill_policy(skill_policy_path, bundle_root=bundle_root)
        skill_actions = build_skill_policy_plan(skill_policy, home=home)

    if dry_run:
        _print_config_plan(plan["config_overlay"], home)
        print()
        _print_profiles_plan(plan["profiles"])
        print()
        _print_jobs_plan(plan["jobs_json"])
        if skill_policy is not None:
            print()
            _print_skill_policy_plan(skill_policy, skill_actions)
        print("\ndry-run: verified all source hashes; wrote nothing.")
        return 0

    stamp = _utc_stamp()
    destination_root = home / ALLOWED_DEST_SUBPATH
    snapshot_dir = destination_root / "logs" / "fleet-config-installs" / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest_path, snapshot_dir / manifest_path.name)

    receipt: dict[str, Any] = {
        "bundle": manifest.get("bundle", "fleet-config"),
        "source_task": manifest.get("source_task"),
        "timestamp": stamp,
        "home": str(home),
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "snapshot_dir": str(snapshot_dir),
        "skill_policy_path": str(skill_policy_path) if skill_policy_path else None,
        "skill_policy_sha256": _sha256(skill_policy_path) if skill_policy_path else None,
        "result": "success",
        "failure_detail": None,
        "steps": [],
    }

    try:
        # --- Step 1: config.yaml overlay (fail-closed YAML validate) ---
        cfg_item = plan["config_overlay"]
        overlay = yaml.safe_load(cfg_item["src"].read_text(encoding="utf-8")) or {}
        dest = cfg_item["dest"]
        if dest.is_file():
            try:
                current = yaml.safe_load(dest.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise InstallError(f"refusing to merge: existing {dest} is not valid YAML: {exc}") from exc
        else:
            current = {}
        merged = merge_overlay(current, overlay)
        rendered = yaml.safe_dump(merged, sort_keys=False, default_flow_style=False)
        # Re-parse before writing anything — fail closed on a bad render.
        reparsed = yaml.safe_load(rendered)
        if reparsed != merged:
            raise InstallError("rendered config.yaml did not round-trip through YAML parse — refusing to write")
        snapshot = _backup(
            dest,
            snapshot_dir,
            destination_root,
            "fleet-config",
            stamp,
            private=True,
        )
        step = {
            "step": "config_overlay",
            "dest": str(dest),
            "snapshot": str(snapshot) if snapshot else None,
            "created": snapshot is None,
            "diff": _diff_keys(current, merged),
            "mode": "0600",
            "status": "installing",
        }
        receipt["steps"].append(step)
        _write_private(dest, rendered.encode("utf-8"))
        # Re-verify the deployed bytes parse cleanly.
        deployed = yaml.safe_load(dest.read_text(encoding="utf-8"))
        if deployed != merged:
            raise InstallError(f"deployed {dest} did not match the intended merge after write — restoring snapshot")
        if _config_mode(dest) != SKILL_POLICY_CONFIG_MODE:
            raise InstallError(f"deployed {dest} mode is not 0600 — restoring snapshot")
        step["status"] = "installed"

        # --- Step 2: profiles/ ---
        for item in plan["profiles"]:
            pdest = item["dest"]
            psnapshot = _backup(
                pdest,
                snapshot_dir,
                destination_root,
                f"fleet-config-profile-{pdest.parent.name}",
                stamp,
            )
            data = item["src"].read_bytes()
            step = {
                "step": "profile_file",
                "dest": str(pdest),
                "snapshot": str(psnapshot) if psnapshot else None,
                "created": psnapshot is None,
                "sha256": None,
                "status": "installing",
            }
            receipt["steps"].append(step)
            _atomic_write(pdest, data)
            deployed_sha = _sha256(pdest)
            if deployed_sha != item["sha256"]:
                raise InstallError(
                    f"deployed bytes for {pdest} sha256 {deployed_sha} != manifest "
                    f"{item['sha256']} — restoring snapshot"
                )
            step["sha256"] = deployed_sha
            step["status"] = "installed"

        profile_names = sorted({Path(i["dest_abs"]).parts[-2] for i in plan["profiles"]})
        for name in profile_names:
            profile_dir = home / ".hermes" / "profiles" / name
            for subdir in PROFILE_BOOTSTRAP_DIRS:
                (profile_dir / subdir).mkdir(parents=True, exist_ok=True)
            receipt["steps"].append({
                "step": "profile_bootstrap_dirs",
                "profile": name,
                "dirs": PROFILE_BOOTSTRAP_DIRS,
                "status": "ensured",
            })

        # --- Step 3: cron/jobs.json (fail-closed JSON validate) ---
        jobs_item = plan["jobs_json"]
        jdest = jobs_item["dest"]
        new_bytes = jobs_item["src"].read_bytes()
        try:
            parsed = json.loads(new_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise InstallError(f"bundled jobs.json is not valid JSON: {exc}") from exc
        if "jobs" not in parsed or not isinstance(parsed["jobs"], list):
            raise InstallError("bundled jobs.json has no top-level 'jobs' list — refusing")
        jsnapshot = _backup(jdest, snapshot_dir, destination_root, "fleet-config-jobs", stamp)
        step = {
            "step": "jobs_json",
            "dest": str(jdest),
            "snapshot": str(jsnapshot) if jsnapshot else None,
            "created": jsnapshot is None,
            "job_count": len(parsed["jobs"]),
            "status": "installing",
        }
        receipt["steps"].append(step)
        _atomic_write(jdest, new_bytes)
        deployed_sha = _sha256(jdest)
        if deployed_sha != jobs_item["sha256"]:
            raise InstallError(
                f"deployed bytes for {jdest} sha256 {deployed_sha} != manifest "
                f"{jobs_item['sha256']} — restoring snapshot"
            )
        step["status"] = "installed"

        # --- Step 4: Mini-specific skill policy ---
        # Runs only after jobs.json is installed, so the direct-executor fleet
        # contract is in place before humanizer is removed from active homes.
        if skill_policy is not None:
            _apply_skill_policy(
                skill_policy,
                skill_actions,
                destination_root=destination_root,
                snapshot_dir=snapshot_dir,
                stamp=stamp,
                receipt_steps=receipt["steps"],
            )

    except InstallError as exc:
        receipt["result"] = "failed"
        receipt["failure_detail"] = str(exc)
        _rollback(receipt["steps"])

    except Exception as exc:
        # Broadened beyond `except InstallError` (the pre-fix scope) — any
        # OTHER exception raised during the write phase (e.g. an unexpected
        # OSError mid-copy, a permissions error, disk full) must still
        # trigger rollback of whatever was already written; previously only
        # our own fail-closed InstallError checks did. Write the receipt here
        # (the success-path receipt write below is skipped once we re-raise)
        # and re-raise so the underlying failure isn't swallowed — callers
        # and tests must still see the real exception, not a silent 1.
        receipt["result"] = "failed"
        receipt["failure_detail"] = str(exc)
        _rollback(receipt["steps"])
        receipt_path = snapshot_dir / "install-receipt.json"
        with open(receipt_path, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2)
            fh.write("\n")
        print(f"receipt: {receipt_path}")
        print(f"install FAILED and was rolled back where possible: {receipt['failure_detail']}", file=sys.stderr)
        raise

    receipt_path = snapshot_dir / "install-receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2)
        fh.write("\n")

    print(f"receipt: {receipt_path}")
    if receipt["result"] != "success":
        print(f"install FAILED and was rolled back where possible: {receipt['failure_detail']}", file=sys.stderr)
        return 1
    print("install complete; config.yaml merged, profiles installed, jobs.json replaced, skill policy applied.")
    return 0


def _rollback(steps: list[dict]) -> None:
    """Best-effort restore of already-written destinations from their snapshots."""
    for rec in reversed(steps):
        if rec.get("rollback") == "move-back":
            source = rec.get("source")
            archive_dest = rec.get("archive_dest")
            if not source or not archive_dest:
                continue
            try:
                source_path = Path(source)
                archive_path = Path(archive_dest)
                if archive_path.exists() and not source_path.exists():
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(archive_path), str(source_path))
                rec["status"] = "rolled-back"
            except OSError as exc:  # pragma: no cover - best-effort rollback
                print(f"  ROLLBACK WARNING: could not restore {source}: {exc}", file=sys.stderr)
            continue
        snapshot = rec.get("snapshot")
        dest = rec.get("dest")
        if not dest:
            continue
        try:
            if snapshot:
                Path(dest).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snapshot, dest)
            elif rec.get("created"):
                path = Path(dest)
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
                cleanup_root = rec.get("cleanup_root")
                if cleanup_root:
                    _remove_empty_skill_parents(path, Path(cleanup_root))
            else:
                continue
            rec["status"] = "rolled-back"
        except OSError as exc:  # pragma: no cover - best-effort rollback
            print(f"  ROLLBACK WARNING: could not restore {dest}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--manifest", type=Path, default=here / "fleet_config_manifest.json",
        help="path to fleet_config_manifest.json (default: alongside this script)",
    )
    parser.add_argument(
        "--home", type=Path, default=Path(os.path.expanduser("~")),
        help="root that ~ in dest_abs expands to (default: current user home)",
    )
    parser.add_argument(
        "--bundle-root", type=Path, default=None,
        help="root for manifest src_rel files (default: the manifest's directory)",
    )
    parser.add_argument(
        "--skills-policy", type=Path, default=here / "skills-policy.json",
        help="path to the governed Mini skills policy (default: alongside this script)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="verify source hashes, print the plan/diff, write nothing",
    )
    args = parser.parse_args(argv)

    manifest_path = args.manifest.resolve()
    bundle_root = (args.bundle_root or manifest_path.parent).resolve()

    try:
        manifest = load_manifest(manifest_path)
        return install(
            manifest,
            home=args.home.resolve(),
            bundle_root=bundle_root,
            manifest_path=manifest_path,
            dry_run=args.dry_run,
            skill_policy_path=args.skills_policy.resolve(),
        )
    except InstallError as exc:
        print(f"install refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
