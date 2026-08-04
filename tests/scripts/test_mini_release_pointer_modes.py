"""End-to-end coverage for ``--verify-pointer`` / ``--repair-pointer``.

ClickUp 86e2kt3yr. Before these modes existed there was no supported way to
either detect or repair a corrupt ``~/.hermes/runtime-current``: the normal
cut and ``--rollback`` paths both dereference the pointer (to bootstrap the
production-write lease from the active clone) and therefore fail with a
message that names neither the defect nor a fix. The 2026-08-02 incident was
consequently found by eyeball and repaired by an unregistered ``ln`` — the
same class of out-of-band write that caused it.

These run the real script, so they also pin that the modes need no lease, no
network, and no release venv.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CUT = REPO_ROOT / "scripts" / "mini-release-cut.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")

# The probe modes are portable, but any test that reaches ``repoint_symlink``
# is not: the swap is ``mv -fh``, a BSD-only flag with no GNU spelling
# (GNU's equivalent is ``-T``). That is deliberate, load-bearing macOS code --
# without ``-h``, BSD ``mv`` treats an existing symlink-to-directory
# destination as "move INTO that directory" and the pointer is never swapped,
# which is the bug the flag was added to fix. The mini is macOS, so the flag
# stays and these tests are macOS-only rather than the primitive being made
# portable for the benefit of an Ubuntu CI runner.
requires_bsd_mv = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="repoint_symlink uses BSD `mv -fh`; the release cutter is macOS-only",
)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    home = (tmp_path / ".hermes").resolve()
    releases = home / "releases"
    release = releases / "v9.9.9-abcdef123456"
    (release / "venv" / "bin").mkdir(parents=True)
    python = release / "venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)

    receipt = {
        "schema_version": 2,
        "event": "cut",
        "runtime_target": str(release),
    }
    payload = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (releases / ".mini-release-last-receipt.json").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (releases / f".mini-release-receipt-{digest}.json").write_bytes(payload)
    return home, release


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        ["bash", str(CUT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def test_verify_pointer_reports_a_healthy_pointer(tmp_path):
    home, release = _fixture(tmp_path)
    (home / "runtime-current").symlink_to(release)

    result = _run(home, "--verify-pointer")

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(release) in result.stdout


def test_verify_pointer_is_read_only_on_a_corrupt_pointer(tmp_path):
    home, release = _fixture(tmp_path)
    pointer = home / "runtime-current"
    pointer.symlink_to(release.name)

    result = _run(home, "--verify-pointer")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "CORRUPT" in result.stderr
    # The probe must never mutate: the corrupt link is still exactly as found.
    assert os.readlink(pointer) == release.name


@requires_bsd_mv
def test_repair_pointer_restores_the_receipt_verified_target(tmp_path):
    home, release = _fixture(tmp_path)
    pointer = home / "runtime-current"
    pointer.symlink_to(release.name)

    result = _run(home, "--repair-pointer")

    assert result.returncode == 0, result.stdout + result.stderr
    assert Path(os.readlink(pointer)).is_absolute()
    assert pointer.resolve() == release.resolve()
    assert _run(home, "--verify-pointer").returncode == 0


def test_repair_pointer_is_idempotent(tmp_path):
    home, release = _fixture(tmp_path)
    (home / "runtime-current").symlink_to(release)

    result = _run(home, "--repair-pointer")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "already healthy" in result.stdout


@requires_bsd_mv
def test_repair_pointer_releases_its_lock(tmp_path):
    home, release = _fixture(tmp_path)
    (home / "runtime-current").symlink_to(release.name)

    assert _run(home, "--repair-pointer").returncode == 0
    assert not (home / "releases" / ".mini-release-cut.lock").exists()


def test_repair_pointer_refuses_while_a_cutter_holds_the_lock(tmp_path):
    home, release = _fixture(tmp_path)
    (home / "runtime-current").symlink_to(release.name)
    lock = home / "releases" / ".mini-release-cut.lock"
    lock.write_text('{"schema_version":1,"lease":{"actor":"mini-release-cut"}}', encoding="utf-8")

    result = _run(home, "--repair-pointer")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "refusing pointer repair" in result.stderr
    # A repair must never evict a cutter's lock.
    assert lock.exists()


def test_repair_pointer_refuses_an_unverifiable_receipt(tmp_path):
    """A receipt with no content-addressed twin cannot be a repair source."""
    home, release = _fixture(tmp_path)
    (home / "runtime-current").symlink_to(release.name)
    for twin in (home / "releases").glob(".mini-release-receipt-*.json"):
        twin.unlink()

    result = _run(home, "--repair-pointer")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cannot repair" in result.stderr


def test_pointer_modes_are_mutually_exclusive_and_reject_mutating_flags(tmp_path):
    home, release = _fixture(tmp_path)
    (home / "runtime-current").symlink_to(release)

    both = _run(home, "--verify-pointer", "--repair-pointer")
    assert both.returncode == 1
    assert "mutually exclusive" in both.stderr

    combined = _run(home, "--verify-pointer", "--rollback")
    assert combined.returncode == 1
    assert "cannot be combined" in combined.stderr
