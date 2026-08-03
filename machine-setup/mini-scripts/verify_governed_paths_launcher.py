#!/usr/bin/env python3
"""Stdlib launcher for the governed-path verifier."""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from governed_interpreter import InterpreterSelectionError, select_governed_interpreter


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        interpreter = select_governed_interpreter(Path.home() / ".hermes" / "releases")
    except InterpreterSelectionError as exc:
        print(f"GOVERNED_VERIFIER_INTERPRETER_UNAVAILABLE: {exc}", file=sys.stderr)
        return 1
    verifier = Path(__file__).absolute().with_name("verify_governed_paths.py")
    os.execv(str(interpreter), [str(interpreter), str(verifier), "--quiet", *args])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
