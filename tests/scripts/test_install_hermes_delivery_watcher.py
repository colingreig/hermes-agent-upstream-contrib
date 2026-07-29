from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install-hermes-delivery-watcher.sh"


@pytest.mark.skipif(sys.platform != "darwin", reason="LaunchAgent installer is macOS-only")
def test_installer_copies_repo_sources_and_pins_a_proven_python(tmp_path):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_DELIVERY_WATCH_PYTHON": sys.executable,
    }

    result = subprocess.run(
        [str(INSTALLER), "--install"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    installed_dir = hermes_home / "libexec" / "delivery-watch"
    assert (installed_dir / "hermes_delivery_watch.py").read_bytes() == (
        ROOT / "scripts" / "hermes_delivery_watch.py"
    ).read_bytes()
    assert (installed_dir / "task_delivery.py").read_bytes() == (
        ROOT / "scripts" / "task_delivery.py"
    ).read_bytes()
    plist_path = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.colingreig.hermes.delivery-watch.plist"
    )
    payload = plistlib.loads(plist_path.read_bytes())
    installed_python = payload["ProgramArguments"][0]
    assert installed_python != "/usr/bin/python3"
    subprocess.run(
        [installed_python, "-c", "import yaml"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert payload["ProgramArguments"][1] == str(
        installed_dir / "hermes_delivery_watch.py"
    )
    assert "runtime-current" not in plist_path.read_text(encoding="utf-8")
