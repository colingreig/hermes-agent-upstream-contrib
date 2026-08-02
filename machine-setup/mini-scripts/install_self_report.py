#!/usr/bin/env python3
"""Manifest-verified installer for the hermes-self-report deploy bundle.

This script is the SOLE writer of the eleven hermes-self-report and lifecycle
continuity artifacts into
``~/.hermes/scripts/`` (and ``~/.hermes/skills/hermes-self-report/SKILL.md``).
It reads ``self_report_manifest.json`` (the declared bundle), verifies every
source's sha256 against the manifest, snapshots each existing destination, then
atomically installs and re-verifies the deployed bytes. It NEVER rsyncs the
scripts dir and NEVER touches any file that is not a manifest destination — in
particular ``queue_snapshot.json`` and the release venv, which must co-exist
alongside the bundle (the installer only warns if required co-exist files are
missing). ``claim_store.py`` and ``closeout_actor.py`` are manifest-governed
because they are load-bearing producers for the bundled shadow outbox.

stdlib only, ``no_agent``-safe. All destination roots derive from ``--home``
(default ``~``), so the whole flow is sandbox-testable against a tmp dir with
no ability to touch a real ``~/.hermes`` or the mini.

Usage:
    install_self_report.py --dry-run
    install_self_report.py
    install_self_report.py --include-skill --brain-path /path/to/brain-checkout
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

# Runtime-state files that must co-exist alongside the bundle but are NEVER
# written by the installer (belt-and-suspenders: refuse a manifest that tries
# to include them).
BLOCKED_DEST_BASENAMES = {"queue_snapshot.json"}

# Every destination must resolve under <home>/.hermes/ — a manifest that points
# anywhere else is rejected before any write happens.
ALLOWED_DEST_SUBPATH = ".hermes"


class InstallError(RuntimeError):
    """Fail-closed installer error (drift, out-of-bounds dest, verify failure)."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _expand(path_str: str, home: Path) -> Path:
    """Expand a manifest dest_abs against an explicit home root.

    ``~`` / ``~/x`` map onto ``home`` so tests can pin everything to a tmp dir.
    Any other (already-absolute) path is returned verbatim.
    """
    if path_str == "~":
        return home
    if path_str.startswith("~/"):
        return home / path_str[2:]
    return Path(path_str)


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_src(entry: dict, mirror_root: Path, brain_path: Path | None) -> Path:
    base = entry.get("src_base")
    rel = entry["src_rel"]
    if base == "mirror":
        return mirror_root / rel
    if base == "brain":
        if brain_path is None:
            raise InstallError(
                f"entry {rel!r} has src_base 'brain' but --brain-path was not given"
            )
        return brain_path / rel
    raise InstallError(f"entry {rel!r} has unknown src_base {base!r}")


def load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _check_dest_in_bounds(dest: Path, dest_abs: str, allowed_root: Path) -> None:
    """Refuse any dest that could resolve outside ``allowed_root``.

    ``Path.relative_to`` is purely lexical and does not collapse ``..``
    components, so a manifest entry like ``~/.hermes/scripts/../../.ssh/x``
    would otherwise pass the bounds check while landing outside
    ``allowed_root``. Two independent guards close that:

    1. Refuse a literal ``..`` path component outright, before any
       normalization — the manifest is not allowed to even spell traversal.
    2. Normalize the path and resolve the closest existing ancestor
       directory, then re-check the bound against the resolved root, so a
       symlinked intermediate directory can't redirect the write outside
       ``allowed_root`` either.
    """
    if ".." in dest.parts:
        raise InstallError(
            f"manifest destination {dest_abs!r} contains a '..' path "
            "component — refusing"
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
            f"manifest destination {dest_abs!r} resolves (via ancestor "
            f"{ancestor}) to {resolved_ancestor} which is outside "
            f"{resolved_root} — refusing"
        ) from exc


def build_plan(
    manifest: dict,
    *,
    home: Path,
    mirror_root: Path,
    brain_path: Path | None,
    include_skill: bool,
) -> list[dict]:
    """Resolve + verify sources. Returns the ordered install plan.

    Raises InstallError on any source-hash drift, out-of-bounds dest, or a
    blocked destination basename. Nothing is written here.
    """
    allowed_root = home / ALLOWED_DEST_SUBPATH
    plan: list[dict] = []
    for entry in manifest["files"]:
        deploy_mode = entry.get("deploy_mode")
        if deploy_mode == "skill" and not include_skill:
            continue

        dest = _expand(entry["dest_abs"], home)
        if dest.name in BLOCKED_DEST_BASENAMES:
            raise InstallError(
                f"manifest destination {entry['dest_abs']!r} targets protected "
                f"file {dest.name!r} — refusing"
            )
        _check_dest_in_bounds(dest, entry["dest_abs"], allowed_root)

        src = _resolve_src(entry, mirror_root, brain_path)
        if not src.is_file():
            raise InstallError(f"source missing for {entry['src_rel']!r}: {src}")

        actual = _sha256(src)
        expected = entry["sha256"]
        if actual != expected:
            raise InstallError(
                f"source hash drift for {entry['src_rel']!r}: manifest "
                f"{expected} != source {actual} — refusing to install "
                "unverified bytes"
            )

        plan.append(
            {
                "src": src,
                "dest": dest,
                "expected_sha256": expected,
                "role": entry.get("role", ""),
                "deploy_mode": deploy_mode,
            }
        )
    return plan


def check_coexist(manifest: dict, home: Path) -> list[str]:
    """Return a warning for each required co-exist file that is missing."""
    warnings: list[str] = []
    for req in manifest.get("coexist_required", []):
        target = _expand(req["dest_abs"], home)
        if not target.exists():
            warnings.append(
                f"required co-exist file missing: {req['dest_abs']} "
                f"({req.get('reason', '')})"
            )
    return warnings


def _print_plan(plan: list[dict], warnings: list[str], home: Path) -> None:
    print(f"hermes-self-report install plan (home={home}):")
    for item in plan:
        print(
            f"  [{item['deploy_mode']}] {item['src']}\n"
            f"      -> {item['dest']}\n"
            f"      sha256={item['expected_sha256']}"
        )
    for warn in warnings:
        print(f"  WARNING: {warn}")


def _restore(installed: list[dict]) -> None:
    """Roll back already-written destinations from their snapshots."""
    for rec in reversed(installed):
        dest: Path = rec["dest"]
        snapshot: Path | None = rec["snapshot"]
        try:
            if snapshot is not None:
                shutil.copy2(snapshot, dest)
            elif rec["pre_existed"] is False and dest.exists():
                dest.unlink()
        except OSError as exc:  # pragma: no cover - best-effort rollback
            print(f"  ROLLBACK WARNING: could not restore {dest}: {exc}", file=sys.stderr)


def install(
    manifest: dict,
    *,
    home: Path,
    mirror_root: Path,
    manifest_path: Path,
    brain_path: Path | None,
    include_skill: bool,
    dry_run: bool,
) -> int:
    plan = build_plan(
        manifest,
        home=home,
        mirror_root=mirror_root,
        brain_path=brain_path,
        include_skill=include_skill,
    )
    warnings = check_coexist(manifest, home)

    if dry_run:
        _print_plan(plan, warnings, home)
        print("dry-run: verified all source hashes; wrote nothing.")
        return 0

    stamp = _utc_stamp()
    snapshot_dir = home / ".hermes" / "logs" / "self-report-installs" / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(manifest_path, snapshot_dir / manifest_path.name)

    installed: list[dict] = []
    receipt_files: list[dict] = []
    result = "success"
    failure_detail = None

    try:
        for item in plan:
            dest: Path = item["dest"]
            src: Path = item["src"]
            pre_existed = dest.exists()
            snapshot: Path | None = None
            if pre_existed:
                snapshot = snapshot_dir / dest.name
                shutil.copy2(dest, snapshot)
                shutil.copy2(dest, dest.with_name(dest.name + f".bak-self-report-install-{stamp}"))

            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dest)

            deployed = _sha256(dest)
            installed.append({"dest": dest, "snapshot": snapshot, "pre_existed": pre_existed})
            status = "installed"
            if deployed != item["expected_sha256"]:
                status = "verify-failed"
                receipt_files.append(
                    {
                        "src": str(src),
                        "dest": str(dest),
                        "expected_sha256": item["expected_sha256"],
                        "deployed_sha256": deployed,
                        "snapshot": str(snapshot) if snapshot else None,
                        "status": status,
                    }
                )
                raise InstallError(
                    f"deployed bytes for {dest} sha256 {deployed} != manifest "
                    f"{item['expected_sha256']} — restoring snapshot"
                )

            receipt_files.append(
                {
                    "src": str(src),
                    "dest": str(dest),
                    "expected_sha256": item["expected_sha256"],
                    "deployed_sha256": deployed,
                    "snapshot": str(snapshot) if snapshot else None,
                    "status": status,
                }
            )
    except InstallError as exc:
        result = "failed"
        failure_detail = str(exc)
        _restore(installed)
        for rec in receipt_files:
            if rec["status"] == "installed":
                rec["status"] = "rolled-back"

    receipt = {
        "bundle": manifest.get("bundle", "hermes-self-report"),
        "source_task": manifest.get("source_task"),
        "timestamp": stamp,
        "result": result,
        "failure_detail": failure_detail,
        "home": str(home),
        "include_skill": include_skill,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "snapshot_dir": str(snapshot_dir),
        "coexist_warnings": warnings,
        "files": receipt_files,
    }
    receipt_path = snapshot_dir / "install-receipt.json"
    with open(receipt_path, "w", encoding="utf-8") as fh:
        json.dump(receipt, fh, indent=2)
        fh.write("\n")

    _print_plan(plan, warnings, home)
    print(f"receipt: {receipt_path}")
    if result != "success":
        print(f"install FAILED and was rolled back: {failure_detail}", file=sys.stderr)
        return 1
    print("install complete; all deployed hashes match the manifest.")
    return 0


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=here / "self_report_manifest.json",
        help="path to self_report_manifest.json (default: alongside this script)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path(os.path.expanduser("~")),
        help="root that ~ in dest_abs expands to (default: current user home)",
    )
    parser.add_argument(
        "--mirror-root",
        type=Path,
        default=None,
        help="root for src_base=mirror files (default: the manifest's directory)",
    )
    parser.add_argument(
        "--brain-path",
        type=Path,
        default=None,
        help="root of a Brain checkout for src_base=brain files (SKILL.md)",
    )
    parser.add_argument(
        "--include-skill",
        action="store_true",
        help="also install SKILL.md from --brain-path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify source hashes and print the plan; write nothing",
    )
    args = parser.parse_args(argv)

    manifest_path = args.manifest.resolve()
    mirror_root = (args.mirror_root or manifest_path.parent).resolve()
    brain_path = args.brain_path.resolve() if args.brain_path else None

    try:
        manifest = load_manifest(manifest_path)
        return install(
            manifest,
            home=args.home.resolve(),
            mirror_root=mirror_root,
            manifest_path=manifest_path,
            brain_path=brain_path,
            include_skill=args.include_skill,
            dry_run=args.dry_run,
        )
    except InstallError as exc:
        print(f"install refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
