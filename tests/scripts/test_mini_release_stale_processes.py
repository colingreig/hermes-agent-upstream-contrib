from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CUT_SCRIPT = REPO_ROOT / "scripts" / "mini-release-cut.sh"


def _install_hermes_console_script(release: Path) -> Path:
    hermes = release / "venv" / "bin" / "hermes"
    hermes.parent.mkdir(parents=True, exist_ok=True)
    hermes.write_text(
        f"""#!{sys.executable}
import os
from pathlib import Path
import signal
import subprocess
import time

if os.environ.get("HERMES_TEST_IGNORE_SIGTERM") == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
command = os.environ.get("HERMES_TEST_REAPER_COMMAND")
if command:
    result = subprocess.run(["bash", "-c", command], text=True, capture_output=True, check=False)
    Path(os.environ["HERMES_TEST_REAPER_RESULT"]).write_text(
        str(result.returncode) + "\\n" + result.stdout + result.stderr,
        encoding="utf-8",
    )
time.sleep(60)
""",
        encoding="utf-8",
    )
    hermes.chmod(0o755)
    return hermes


def _hermes_process(
    console_script: Path,
    cwd: Path,
    *,
    ignore_sigterm: bool = False,
    env_extra: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    if ignore_sigterm:
        env["HERMES_TEST_IGNORE_SIGTERM"] = "1"
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [str(console_script), "mcp", "serve"],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sleeping_unrelated_process(cwd: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=cwd,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sleeping_unrelated_old_release_executable(old_release: Path) -> subprocess.Popen[str]:
    executable = old_release / "bin" / "unrelated-sleep"
    executable.parent.mkdir(parents=True, exist_ok=True)
    compiler = shutil.which("cc")
    assert compiler is not None, "C compiler required for executable-provenance process test"
    subprocess.run(
        [compiler, "-x", "c", "-o", str(executable), "-"],
        input="#include <unistd.h>\nint main(void) { sleep(60); return 0; }\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return subprocess.Popen(
        [str(executable), "60"],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _install_target_python(target_release: Path) -> None:
    python = target_release / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(f"#!/bin/sh\nexec {str(Path(sys.executable))!r} \"$@\"\n", encoding="utf-8")
    python.chmod(0o755)


def _reaper_command(hermes_home: Path, releases: Path, target_release: Path) -> str:
    return f"""
set -euo pipefail
export HERMES_HOME={str(hermes_home)!r}
MINI_RELEASE_CUT_TEST_LIB=1 source {str(CUT_SCRIPT)!r}
RELEASES_DIR={str(releases)!r}
DRY_RUN=0
reap_stale_release_processes {str(target_release)!r}
"""


def _run_reaper(hermes_home: Path, releases: Path, target_release: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _reaper_command(hermes_home, releases, target_release)],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_cut_reaps_console_script_mcp_serve_but_preserves_unrelated_old_release_cwd(tmp_path: Path):
    hermes_home = tmp_path / ".hermes"
    releases = hermes_home / "releases"
    old_release = releases / "v0.18.1-old"
    target_release = releases / "v0.18.2-target"
    unrelated = tmp_path / "unrelated"
    for directory in (old_release, target_release, unrelated):
        directory.mkdir(parents=True)
    _install_target_python(target_release)
    old_hermes = _install_hermes_console_script(old_release)
    target_hermes = _install_hermes_console_script(target_release)
    external_hermes = _install_hermes_console_script(unrelated)

    # Real venv console scripts expose the old-release path in cmdline while
    # psutil may report the base Python as exe and an unrelated cwd.
    stale = _hermes_process(old_hermes, unrelated)
    unrelated_in_old_cwd = _sleeping_unrelated_process(old_release)
    unrelated_old_executable = _sleeping_unrelated_old_release_executable(old_release)
    external_hermes_in_old_cwd = _hermes_process(external_hermes, old_release)
    target = _hermes_process(target_hermes, old_release)
    try:
        result = _run_reaper(hermes_home, releases, target_release)

        assert result.returncode == 0, result.stderr
        stale.wait(timeout=5)
        assert unrelated_in_old_cwd.poll() is None
        assert unrelated_old_executable.poll() is None
        assert external_hermes_in_old_cwd.poll() is None
        assert target.poll() is None
        output = result.stdout + result.stderr
        assert f"pid={stale.pid}" in output
        assert f"cmdline={old_hermes}" in output
        assert "mcp serve" in output
        assert "reaped 1 stale old-release Hermes process(es)" in output
    finally:
        for process in (
            stale,
            unrelated_in_old_cwd,
            unrelated_old_executable,
            external_hermes_in_old_cwd,
            target,
        ):
            _stop(process)


def test_cut_stale_process_reaper_protects_hermes_ancestor(tmp_path: Path):
    hermes_home = tmp_path / ".hermes"
    releases = hermes_home / "releases"
    old_release = releases / "v0.18.1-old"
    target_release = releases / "v0.18.2-target"
    unrelated = tmp_path / "unrelated"
    result_file = tmp_path / "reaper-result.txt"
    for directory in (old_release, target_release, unrelated):
        directory.mkdir(parents=True)
    _install_target_python(target_release)
    old_hermes = _install_hermes_console_script(old_release)

    ancestor = _hermes_process(
        old_hermes,
        unrelated,
        env_extra={
            "HERMES_TEST_REAPER_COMMAND": _reaper_command(hermes_home, releases, target_release),
            "HERMES_TEST_REAPER_RESULT": str(result_file),
        },
    )
    try:
        deadline = time.monotonic() + 10
        while not result_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert result_file.exists(), "ancestor's reaper did not finish"
        result = result_file.read_text(encoding="utf-8")
        assert result.startswith("0\n"), result
        assert ancestor.poll() is None
        assert "no stale old-release Hermes processes found" in result
    finally:
        _stop(ancestor)


def test_cut_stale_process_reaper_escalates_sigterm_to_sigkill(tmp_path: Path):
    hermes_home = tmp_path / ".hermes"
    releases = hermes_home / "releases"
    old_release = releases / "v0.18.1-old"
    target_release = releases / "v0.18.2-target"
    unrelated = tmp_path / "unrelated"
    for directory in (old_release, target_release, unrelated):
        directory.mkdir(parents=True)
    _install_target_python(target_release)
    old_hermes = _install_hermes_console_script(old_release)

    stubborn = _hermes_process(old_hermes, unrelated, ignore_sigterm=True)
    try:
        result = _run_reaper(hermes_home, releases, target_release)

        assert result.returncode == 0, result.stderr
        stubborn.wait(timeout=5)
        output = result.stdout + result.stderr
        assert f"sending SIGKILL pid={stubborn.pid}" in output
        assert "reaped 1 stale old-release Hermes process(es)" in output
    finally:
        _stop(stubborn)


def test_cut_stale_process_reaper_is_idempotent_when_no_old_release_processes(tmp_path: Path):
    hermes_home = tmp_path / ".hermes"
    releases = hermes_home / "releases"
    target_release = releases / "v0.18.2-target"
    target_release.mkdir(parents=True)
    _install_target_python(target_release)

    result = _run_reaper(hermes_home, releases, target_release)

    assert result.returncode == 0, result.stderr
    assert "no stale old-release Hermes processes found" in result.stdout + result.stderr
