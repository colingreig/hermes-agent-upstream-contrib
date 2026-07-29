from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "install-hermes-delivery-watcher.sh"
FIXTURE = ROOT / "tests" / "fixtures" / "hermes_delivery_snapshot" / "live-sources.json"
APPLE_PYTHON = "/usr/bin/python3"
pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="LaunchAgent installer is macOS-only"
)


def _paths(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
    }
    return home, hermes_home, env


def _install(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALLER), "--install"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_installed_copy_runs_end_to_end_under_apple_python(tmp_path):
    home, hermes_home, env = _paths(tmp_path)

    result = _install(env)

    assert result.returncode == 0, result.stderr
    installed_dir = hermes_home / "libexec" / "delivery-watch"
    for name in (
        "hermes_delivery_watch.py",
        "task_delivery.py",
        "hermes_delivery_snapshot.py",
        "delivery_watch_safety.py",
    ):
        assert (installed_dir / name).read_bytes() == (
            ROOT / "scripts" / name
        ).read_bytes()

    config_path = hermes_home / "config.delivery-watch.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    snapshot = hermes_home / "state" / "delivery-input" / "macbook.json"
    assert config["delivery_snapshot"]["clickup_list_id"] == "901714465284"
    assert config["delivery_watch"]["collectors"] == [
        {
            "kind": "file",
            "name": "live-delivery-snapshot",
            "path": str(snapshot),
        }
    ]
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    plist_path = (
        home
        / "Library"
        / "LaunchAgents"
        / "com.colingreig.hermes.delivery-watch.plist"
    )
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload["ProgramArguments"] == [
        APPLE_PYTHON,
        str(installed_dir / "hermes_delivery_snapshot.py"),
        "--once",
        "--config",
        str(config_path),
        "--output",
        str(snapshot),
        "--run-watcher",
    ]
    assert "runtime-current" not in plist_path.read_text(encoding="utf-8")

    signature = subprocess.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--strict",
            "-R=anchor apple",
            APPLE_PYTHON,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert signature.returncode == 0, signature.stderr

    smoke = subprocess.run(
        [
            APPLE_PYTHON,
            str(installed_dir / "hermes_delivery_snapshot.py"),
            "--once",
            "--fixture",
            str(FIXTURE),
            "--config",
            str(config_path),
            "--output",
            str(snapshot),
            "--run-watcher",
        ],
        cwd=installed_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert json.loads(snapshot.read_text(encoding="utf-8"))["schema"] == (
        "hermes_delivery_snapshot/v1"
    )
    heartbeat = json.loads(
        (
            hermes_home / "state" / "task-delivery-watch" / "heartbeat.json"
        ).read_text(encoding="utf-8")
    )
    assert heartbeat["schema"] == "hermes_delivery_watch/v1"


def test_installer_rejects_non_apple_interpreter_override(tmp_path):
    home, _hermes_home, env = _paths(tmp_path)
    env["HERMES_DELIVERY_WATCH_PYTHON"] = sys.executable
    if sys.executable == APPLE_PYTHON:
        env["HERMES_DELIVERY_WATCH_PYTHON"] = "/opt/homebrew/bin/python3"

    result = _install(env)

    assert result.returncode == 1
    assert "interpreter override is not trusted" in result.stderr
    assert not (home / "Library" / "LaunchAgents").exists()


def test_installer_preserves_existing_json_config_byte_for_byte(tmp_path):
    _home, hermes_home, env = _paths(tmp_path)
    hermes_home.mkdir()
    config_path = hermes_home / "config.delivery-watch.json"
    original = (
        b'{\n  "delivery_snapshot": {"clickup_list_id": "123", '
        b'"mini_host": "mini"},\n'
        b'  "delivery_watch": {"collectors": [{"kind": "file", "name": '
        b'"custom", "path": "/tmp/custom.json"}]}\n}\n'
    )
    config_path.write_bytes(original)
    config_path.chmod(0o644)

    first = _install(env)
    second = _install(env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert config_path.read_bytes() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_installer_migrates_legacy_json_without_modifying_legacy_file(tmp_path):
    _home, hermes_home, env = _paths(tmp_path)
    hermes_home.mkdir()
    legacy = hermes_home / "config.delivery-watch.yaml"
    original = (
        b'{"delivery_snapshot":{"clickup_list_id":"456","mini_host":"mini"},'
        b'"delivery_watch":{"collectors":[{"kind":"file","name":"legacy",'
        b'"path":"/tmp/legacy.json"}]}}\n'
    )
    legacy.write_bytes(original)
    legacy.chmod(0o640)

    result = _install(env)

    assert result.returncode == 0, result.stderr
    migrated = hermes_home / "config.delivery-watch.json"
    assert migrated.read_bytes() == original
    assert stat.S_IMODE(migrated.stat().st_mode) == 0o600
    assert legacy.read_bytes() == original
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o640
