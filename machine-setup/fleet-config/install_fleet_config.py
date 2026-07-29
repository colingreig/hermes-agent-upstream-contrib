#!/usr/bin/env python3
"""Manifest-verified installer for the declarative fleet-config bundle.

This script is the SOLE writer of three things on the target machine:

  1. ``~/.hermes/config.yaml``       — deep-merges ``config-overlay.yaml`` in.
  2. ``~/.hermes/profiles/<name>/``  — installs the five named profiles
                                        (config.yaml + SOUL.md + bootstrap dirs).
  3. ``~/.hermes/cron/jobs.json``    — wholesale REPLACE with the curated set.

It reads ``fleet_config_manifest.json`` (the declared bundle), verifies every
source file's sha256 against the manifest, snapshots each existing
destination, then atomically installs and re-verifies the deployed bytes.
Nothing outside the manifest's declared destinations is ever touched.

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
from pathlib import Path
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
    """Resolve + verify sources for all three install steps.

    Returns a dict with keys ``config_overlay``, ``profiles``, ``jobs_json``,
    each holding the resolved entries for that step. Raises InstallError on
    any source-hash drift or out-of-bounds destination. Nothing is written.
    """
    allowed_root = home / ALLOWED_DEST_SUBPATH
    verified = _verify_sources(manifest, bundle_root)

    plan: dict[str, Any] = {"config_overlay": None, "profiles": [], "jobs_json": None}

    for entry in verified:
        dest = _expand(entry["dest_abs"], home)
        _check_dest_in_bounds(dest, entry["dest_abs"], allowed_root)
        mode = entry.get("deploy_mode")
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


def _backup(dest: Path, snapshot_dir: Path, tag: str, stamp: str) -> Path | None:
    if not dest.exists():
        return None
    snapshot = snapshot_dir / dest.name
    shutil.copy2(dest, snapshot)
    shutil.copy2(dest, dest.with_name(dest.name + f".bak-{tag}-install-{stamp}"))
    return snapshot


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def install(
    manifest: dict,
    *,
    home: Path,
    bundle_root: Path,
    manifest_path: Path,
    dry_run: bool,
) -> int:
    plan = build_plan(manifest, home=home, bundle_root=bundle_root)

    if dry_run:
        _print_config_plan(plan["config_overlay"], home)
        print()
        _print_profiles_plan(plan["profiles"])
        print()
        _print_jobs_plan(plan["jobs_json"])
        print("\ndry-run: verified all source hashes; wrote nothing.")
        return 0

    stamp = _utc_stamp()
    snapshot_dir = home / ".hermes" / "logs" / "fleet-config-installs" / stamp
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
        snapshot = _backup(dest, snapshot_dir, "fleet-config", stamp)
        _atomic_write(dest, rendered.encode("utf-8"))
        # Re-verify the deployed bytes parse cleanly.
        deployed = yaml.safe_load(dest.read_text(encoding="utf-8"))
        if deployed != merged:
            raise InstallError(f"deployed {dest} did not match the intended merge after write — restoring snapshot")
        receipt["steps"].append({
            "step": "config_overlay",
            "dest": str(dest),
            "snapshot": str(snapshot) if snapshot else None,
            "diff": _diff_keys(current, merged),
            "status": "installed",
        })

        # --- Step 2: profiles/ ---
        for item in plan["profiles"]:
            pdest = item["dest"]
            psnapshot = _backup(pdest, snapshot_dir, f"fleet-config-profile-{pdest.parent.name}", stamp)
            data = item["src"].read_bytes()
            _atomic_write(pdest, data)
            deployed_sha = _sha256(pdest)
            if deployed_sha != item["sha256"]:
                raise InstallError(
                    f"deployed bytes for {pdest} sha256 {deployed_sha} != manifest "
                    f"{item['sha256']} — restoring snapshot"
                )
            receipt["steps"].append({
                "step": "profile_file",
                "dest": str(pdest),
                "snapshot": str(psnapshot) if psnapshot else None,
                "sha256": deployed_sha,
                "status": "installed",
            })

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
        jsnapshot = _backup(jdest, snapshot_dir, "fleet-config-jobs", stamp)
        _atomic_write(jdest, new_bytes)
        deployed_sha = _sha256(jdest)
        if deployed_sha != jobs_item["sha256"]:
            raise InstallError(
                f"deployed bytes for {jdest} sha256 {deployed_sha} != manifest "
                f"{jobs_item['sha256']} — restoring snapshot"
            )
        receipt["steps"].append({
            "step": "jobs_json",
            "dest": str(jdest),
            "snapshot": str(jsnapshot) if jsnapshot else None,
            "job_count": len(parsed["jobs"]),
            "status": "installed",
        })

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
    print("install complete; config.yaml merged, profiles installed, jobs.json replaced.")
    return 0


def _rollback(steps: list[dict]) -> None:
    """Best-effort restore of already-written destinations from their snapshots."""
    for rec in reversed(steps):
        snapshot = rec.get("snapshot")
        dest = rec.get("dest")
        if not snapshot or not dest:
            continue
        try:
            shutil.copy2(snapshot, dest)
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
        )
    except InstallError as exc:
        print(f"install refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
