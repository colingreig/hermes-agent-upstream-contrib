#!/usr/bin/env python3
"""Verify Ignite skills are wired through external_dirs, not ~/.hermes/skills copies.

Canonical Ignite skills live in ~/dev/ignite-skills-live and are pulled by
ignite-skills-pull.sh (launchd). Hermes discovers them via skills.external_dirs.

Usage:
    verify_ignite_skills_external.py
    verify_ignite_skills_external.py --home ~
"""

from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("verify_ignite_skills_external.py requires PyYAML", file=sys.stderr)
    raise

SCRIPT_DIR = Path(__file__).resolve().parent

REQUIRED_IGNITE_EXTERNAL_DIRS = (
    Path("/Users/colingreig/dev/ignite-skills-live/skills"),
    Path("/Users/colingreig/dev/ignite-skills-live/ignite-content/skills"),
)


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


def _expand_config_path(raw: str, home: Path) -> Path:
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def _read_external_dirs(config_path: Path, home: Path) -> list[Path]:
    if not config_path.is_file():
        return []
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills = data.get("skills") or {}
    raw_dirs = skills.get("external_dirs") or []
    if not isinstance(raw_dirs, list):
        return []
    return [_expand_config_path(str(item), home).resolve() for item in raw_dirs]


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=Path.home(), help="home root (default: ~)")
    args = parser.parse_args()

    errors: list[str] = []
    home = args.home
    hermes_home = home / ".hermes"

    for ignite_root in REQUIRED_IGNITE_EXTERNAL_DIRS:
        if not ignite_root.is_dir():
            errors.append(f"missing Ignite checkout path: {ignite_root}")

    pull_receipt = hermes_home / "state" / "skill-pulls" / "ignite-skills-live-success.json"
    if pull_receipt.is_file():
        try:
            receipt = json.loads(pull_receipt.read_text(encoding="utf-8"))
            commit = receipt.get("commit", "unknown")
        except json.JSONDecodeError:
            commit = "unreadable"
            errors.append(f"invalid JSON in pull receipt: {pull_receipt}")
    else:
        commit = "unknown"
        errors.append(f"missing pull receipt: {pull_receipt}")

    config_path = hermes_home / "config.yaml"
    external_dirs = _read_external_dirs(config_path, home)
    external_resolved = {path for path in external_dirs}
    for required in REQUIRED_IGNITE_EXTERNAL_DIRS:
        if required.resolve() not in external_resolved:
            errors.append(
                f"{required} not listed in skills.external_dirs ({config_path}); "
                "run reconcile_marketplace_skills.py"
            )

    install_mod = _load_installer()
    fleet_root = _fleet_config_root()
    policy = install_mod.load_skill_policy(fleet_root / "skills-policy.json", bundle_root=fleet_root)
    skills_dir = hermes_home / "skills"
    unmanaged = install_mod._find_unmanaged_skill_manifests(skills_dir, policy)
    if unmanaged:
        sample = sorted({m.parent.relative_to(skills_dir).as_posix() for m in unmanaged})[:5]
        errors.append(
            f"{len(unmanaged)} unmanaged local skill copy/copies under {skills_dir} "
            f"(sample: {', '.join(sample)}); run cleanup_hermes_local_ignite_shadows.py --apply"
        )

    verify_receipt = {
        "verified_at": _utc_now(),
        "ignite_commit": commit,
        "external_dirs_ok": not any("external_dirs" in e for e in errors),
        "checkout_ok": not any("missing Ignite checkout" in e for e in errors),
        "local_shadows_ok": not any("unmanaged local" in e for e in errors),
        "errors": errors,
    }
    receipt_path = hermes_home / "state" / "ignite-skills-external-verify.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(verify_receipt, indent=2) + "\n", encoding="utf-8")

    if errors:
        print(f"FAIL: Ignite external wiring check ({len(errors)} issue(s))", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(f"receipt: {receipt_path}", file=sys.stderr)
        return 1

    print("OK: Ignite skills wired via external_dirs (no local ~/.hermes/skills copies)")
    print(f"  commit: {commit}")
    print(f"  receipt: {receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
