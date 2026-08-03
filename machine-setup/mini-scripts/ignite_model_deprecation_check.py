#!/usr/bin/env python3
"""Read-only provider model-deprecation collector for the fleet digest."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> int:
    root = Path(os.environ.get("IGNITE_SKILLS_ROOT") or Path.home() / "dev" / "ignite-skills")
    checker = root / "ignite-state" / "scripts" / "model-deprecation-check.mjs"
    if not checker.is_file():
        # Legacy checkout layout.
        checker = root / "skills" / "ignite-state" / "scripts" / "model-deprecation-check.mjs"
    node = shutil.which("node")
    if node is None or not checker.is_file():
        print(json.dumps({"status": "unavailable", "detail": f"node={node!r} checker={checker}"}))
        return 2
    try:
        result = subprocess.run(
            [node, str(checker), "--json"], capture_output=True, text=True,
            timeout=90, check=False, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "detail": str(exc)}))
        return 2
    output = (result.stdout or "").strip()
    try:
        parsed = json.loads(output)
    except ValueError:
        print(json.dumps({"status": "error", "detail": "checker returned invalid JSON",
                          "stderr": (result.stderr or "")[-1000:]}))
        return 2
    print(json.dumps(parsed, sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
