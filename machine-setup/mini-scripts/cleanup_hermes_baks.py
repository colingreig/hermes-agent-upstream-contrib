#!/usr/bin/env python3
"""Delete aged config-adjacent Hermes backup files.

Standing retention policy for runtime config backups:
- prune config.yaml backup copies older than 60 days
- prune cron/jobs.json backup copies older than 60 days
- prune script backup copies older than 60 days

This is intentionally narrow: it skips state-snapshot and worktree trees.
Run with --dry-run to preview deletions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from time import time


def _candidate_files(home: Path) -> list[Path]:
    patterns = [
        home / "config.yaml.bak*",
        home / "cron" / "jobs.json.bak*",
        home / "scripts" / "*.bak*",
        home / "scripts" / "**" / "*.bak*",
    ]
    seen: set[Path] = set()
    out: list[Path] = []
    for pat in patterns:
        for path in pat.parent.glob(pat.name) if "**" not in str(pat) else pat.parent.glob(str(pat.relative_to(pat.parent))):
            if not path.is_file():
                continue
            if "/state/" in f"/{path.as_posix()}/":
                continue
            if "/worktrees/" in f"/{path.as_posix()}/":
                continue
            if path in seen:
                continue
            seen.add(path)
            out.append(path)
    return sorted(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", default=os.path.expanduser("~/.hermes"))
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    home = Path(args.hermes_home).expanduser().resolve()
    cutoff = time() - (args.days * 24 * 60 * 60)
    deleted: list[str] = []
    kept: list[str] = []

    for path in _candidate_files(home):
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime > cutoff:
            kept.append(str(path))
            continue
        if args.dry_run:
            deleted.append(str(path))
            continue
        try:
            path.unlink()
            deleted.append(str(path))
        except FileNotFoundError:
            continue

    summary = {
        "hermes_home": str(home),
        "days": args.days,
        "dry_run": args.dry_run,
        "deleted_count": len(deleted),
        "kept_count": len(kept),
        "deleted": deleted[:200],
        "kept": kept[:50],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
