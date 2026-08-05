"""The release poller must diagnose a corrupt runtime-current pointer.

Forensics for ClickUp 86e2kt3yr: on 2026-08-02 ``~/.hermes/runtime-current``
was left as a bare-name relative symlink by an out-of-band write. Every cron
job on the mini dereferences that pointer, but nothing on the box asserted it
between release cuts, so the defect was found by a human eyeball roughly three
minutes into an active cron agent run.

The poller runs every 15 minutes and is the fleet's most frequent toucher of
the pointer, which makes it the cheapest place to get detection cadence
without adding a LaunchAgent. These tests pin that it (a) refuses to poll on a
corrupt pointer, (b) still emits its liveness heartbeat first so the
fleet-outcome liveness contract keeps working, and (c) emits a stable,
greppable reason line an alarm can match distinguishably.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POLLER = REPO_ROOT / "scripts" / "mini-release-poll.sh"
CORRUPT_PREFIX = "mini-release-poll: runtime-current pointer corrupt: "

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "releases").mkdir(parents=True)
    return home


def _active_release(home: Path, name: str = "v9.9.9-abcdef123456") -> Path:
    release = home / "releases" / name
    (release / "venv" / "bin").mkdir(parents=True)
    python = release / "venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return release


def _run(home: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    return subprocess.run(
        ["bash", str(POLLER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_heartbeat_precedes_the_pointer_verdict(tmp_path):
    """Liveness must not be starved by a fail-closed pointer check."""
    home = _hermes_home(tmp_path)
    (home / "runtime-current").symlink_to("v9.9.9-abcdef123456")

    result = _run(home)

    lines = result.stdout.splitlines()
    assert lines, result.stderr
    assert lines[0].startswith("mini-release-poll: heartbeat "), result.stdout


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("bare_name", "relative symlink"),
        ("relative_resolvable", "relative symlink"),
        ("dangling", "dangling target"),
        ("escapes", "escapes releases/"),
        ("nested", "not a direct child"),
        ("no_venv", "no usable runtime Python"),
        ("missing", "missing"),
        ("not_a_symlink", "not a symlink"),
    ],
)
def test_poller_refuses_to_run_on_a_corrupt_pointer(tmp_path, shape, expected):
    home = _hermes_home(tmp_path)
    release = _active_release(home)
    pointer = home / "runtime-current"

    if shape == "bare_name":
        pointer.symlink_to(release.name)
    elif shape == "relative_resolvable":
        pointer.symlink_to(Path("releases") / release.name)
    elif shape == "dangling":
        pointer.symlink_to(home / "releases" / "v0.0.0-deadbeefcafe")
    elif shape == "escapes":
        outside = tmp_path / "outside"
        outside.mkdir()
        pointer.symlink_to(outside)
    elif shape == "nested":
        nested = release / "nested"
        nested.mkdir()
        pointer.symlink_to(nested)
    elif shape == "no_venv":
        bare = home / "releases" / "v9.9.9-000000000000"
        bare.mkdir()
        pointer.symlink_to(bare)
    elif shape == "not_a_symlink":
        pointer.mkdir()
    elif shape != "missing":  # pragma: no cover - guards the parametrization
        raise AssertionError(f"unhandled shape: {shape}")

    result = _run(home)

    assert result.returncode == 1, result.stdout + result.stderr
    corrupt = [line for line in result.stdout.splitlines() if line.startswith(CORRUPT_PREFIX)]
    assert corrupt, result.stdout + result.stderr
    assert expected in corrupt[0], corrupt[0]
    assert "--repair-pointer" in result.stderr, result.stderr


def test_healthy_pointer_passes_the_guard(tmp_path):
    """A healthy pointer must fall through to the poller's normal checks."""
    home = _hermes_home(tmp_path)
    release = _active_release(home)
    (home / "runtime-current").symlink_to(release)

    result = _run(home)

    assert CORRUPT_PREFIX not in result.stdout, result.stdout
    # The cutter is absent from this fixture, so the poller stops at the next
    # check — proving the pointer guard admitted a healthy pointer.
    assert "release cutter missing or not executable" in result.stderr, result.stderr
