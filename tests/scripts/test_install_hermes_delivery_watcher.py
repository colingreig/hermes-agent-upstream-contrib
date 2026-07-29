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
    assert (installed_dir / "hermes_delivery_snapshot.py").read_bytes() == (
        ROOT / "scripts" / "hermes_delivery_snapshot.py"
    ).read_bytes()
    assert (installed_dir / "delivery_watch_safety.py").read_bytes() == (
        ROOT / "scripts" / "delivery_watch_safety.py"
    ).read_bytes()
    config_path = hermes_home / "config.delivery-watch.yaml"
    config = __import__("json").loads(config_path.read_text(encoding="utf-8"))
    assert config["delivery_snapshot"]["clickup_list_id"] == "901714465284"
    assert config["delivery_watch"]["collectors"] == [
        {
            "kind": "file",
            "name": "live-delivery-snapshot",
            "path": str(hermes_home / "state" / "delivery-input" / "macbook.json"),
        }
    ]
    assert config_path.stat().st_mode & 0o777 == 0o600
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
        installed_dir / "hermes_delivery_snapshot.py"
    )
    assert payload["ProgramArguments"][2:] == [
        "--once",
        "--config",
        str(config_path),
        "--output",
        str(hermes_home / "state" / "delivery-input" / "macbook.json"),
        "--run-watcher",
    ]
    assert "runtime-current" not in plist_path.read_text(encoding="utf-8")
