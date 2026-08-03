"""Run the mini-release-cut bash safety suite under pytest.

``tests/scripts/test_mini_release_cut_safety.sh`` is the only coverage the
release cutter has for its path/lock/rollback/pointer invariants, but as a
standalone ``.sh`` it was never collected by pytest and no workflow invoked it,
so it could rot silently while CI stayed green (ClickUp 86e2kt3yr). Collecting
it here puts it on the same gate as everything else.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE = Path(__file__).with_name("test_mini_release_cut_safety.sh")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_mini_release_cut_safety_suite_passes():
    assert SUITE.is_file(), f"missing bash safety suite: {SUITE}"
    result = subprocess.run(
        ["bash", str(SUITE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    detail = f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    assert result.returncode == 0, f"bash safety suite failed\n{detail}"
    assert "mini-release-cut safety checks passed" in result.stdout, detail
