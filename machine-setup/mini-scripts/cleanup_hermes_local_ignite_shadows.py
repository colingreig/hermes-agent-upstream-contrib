#!/usr/bin/env python3
"""Archive unmanaged Ignite skill shadows from ~/.hermes/skills.

Fleet install governs exactly the paths declared in skills-policy.json.
Copying Ignite skills into ~/.hermes/skills (the old sync_ignite_skills_to_hermes.sh
behavior) breaks manifest counting and shadows the canonical external_dirs tree.

This script moves unmanaged top-level skill directories to a recoverable archive
under ~/.hermes/archives/ignite-local-shadow/ so install_fleet_config.py can run.

Usage:
    cleanup_hermes_local_ignite_shadows.py            # dry-run (default)
    cleanup_hermes_local_ignite_shadows.py --apply
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _resolve_fleet_paths() -> tuple[Path, Path]:
    candidates = (
        SCRIPT_DIR.parent / "fleet-config",
        Path.home() / "dev" / "hermes-agent" / "machine-setup" / "fleet-config",
        Path.home() / ".hermes" / "runtime-current" / "machine-setup" / "fleet-config",
    )
    for fleet_root in candidates:
        installer = fleet_root / "install_fleet_config.py"
        if installer.is_file() and (fleet_root / "skills-policy.json").is_file():
            return installer, fleet_root
    raise SystemExit("could not locate fleet-config bundle (install_fleet_config.py + skills-policy.json)")


def _load_installer():
    installer_path, _fleet_root = _resolve_fleet_paths()
    spec = importlib.util.spec_from_file_location("install_fleet_config", installer_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load fleet installer: {installer_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fleet_config_root() -> Path:
    _installer, fleet_root = _resolve_fleet_paths()
    return fleet_root


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_cleanup(*, home: Path, apply: bool) -> int:
    install_mod = _load_installer()
    fleet_root = _fleet_config_root()
    policy_path = fleet_root / "skills-policy.json"
    policy = install_mod.load_skill_policy(policy_path, bundle_root=fleet_root)

    skills_dir = home / ".hermes" / "skills"
    unmanaged = install_mod._find_unmanaged_skill_manifests(skills_dir, policy)
    shadow_dirs = {manifest.parent for manifest in unmanaged}

    lock_path = skills_dir / ".hub" / "lock.json"
    lock_data = install_mod._load_hub_lock(lock_path)
    for name, spec in policy["hub_shadow_remove"].items():
        path = skills_dir / spec["install_path"]
        if not path.exists() and not path.is_symlink():
            continue
        entry = lock_data["installed"].get(name)
        entry_matches = (
            isinstance(entry, dict)
            and entry.get("install_path") == spec["install_path"]
            and entry.get("source") == spec["source"]
            and entry.get("identifier") == spec["identifier"]
        )
        if not entry_matches:
            shadow_dirs.add(path)

    if not shadow_dirs:
        print(f"OK: no unmanaged skill manifests under {skills_dir}")
        return 0

    shadow_dirs = sorted(shadow_dirs, key=lambda p: p.as_posix())
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: {len(shadow_dirs)} unmanaged skill "
        f"director{'y' if len(shadow_dirs) == 1 else 'ies'} under {skills_dir}"
    )
    for path in shadow_dirs:
        print(f"  {path.relative_to(skills_dir).as_posix()}")

    if not apply:
        print("Re-run with --apply to archive under ~/.hermes/archives/ignite-local-shadow/")
        return 1

    archive_root = home / ".hermes" / "archives" / "ignite-local-shadow" / _utc_stamp()
    archive_root.mkdir(parents=True, exist_ok=True)
    receipt = {
        "archived_at": _utc_stamp(),
        "source_skills_dir": str(skills_dir),
        "policy_id": policy["policy_id"],
        "paths": [],
    }
    for path in shadow_dirs:
        rel = path.relative_to(skills_dir).as_posix()
        dest = archive_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        receipt["paths"].append(rel)
        print(f"archived {rel} -> {dest}")

    receipt_path = archive_root / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(f"receipt: {receipt_path}")
    remaining = install_mod._find_unmanaged_skill_manifests(skills_dir, policy)
    if remaining:
        print(f"ERROR: {len(remaining)} unmanaged manifest(s) remain after cleanup", file=sys.stderr)
        return 1
    print("cleanup complete; fleet install may proceed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=Path.home(), help="home root (default: ~)")
    parser.add_argument("--apply", action="store_true", help="move shadows to archive (default: dry-run)")
    args = parser.parse_args()
    return run_cleanup(home=args.home, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
