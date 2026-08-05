"""Run the mini-release-cut bash safety suite under pytest.

``tests/scripts/test_mini_release_cut_safety.sh`` is the only coverage the
release cutter has for its path/lock/rollback/pointer invariants, but as a
standalone ``.sh`` it was never collected by pytest and no workflow invoked it,
so it could rot silently while nothing noticed (ClickUp 86e2kt3yr). Collecting
it here puts it on the same gate as everything else.

**This only gates macOS runs.** The cutter is macOS-only production code and
the suite exercises BSD-specific primitives -- most importantly ``mv -fh`` in
``repoint_symlink``, a flag GNU coreutils does not accept (its equivalent is
``-T``). ``-h`` is load-bearing: without it BSD ``mv`` treats an existing
symlink-to-directory destination as "move INTO that directory" and the pointer
is never swapped, which is precisely the bug it was added to fix. Making the
primitive portable to satisfy an Ubuntu CI runner would put that regression one
bad platform detection away, so the suite stays BSD and this wrapper skips
elsewhere. On the Linux CI runner it therefore reports as skipped; it runs for
every developer on macOS and on the mini itself, which are the machines that
can actually invalidate the invariants it pins.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE = Path(__file__).with_name("test_mini_release_cut_safety.sh")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the suite exercises BSD-only cutter primitives (`mv -fh`); the mini is macOS",
)
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
