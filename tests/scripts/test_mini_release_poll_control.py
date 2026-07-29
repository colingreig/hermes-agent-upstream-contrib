from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "scripts" / "mini-release-poll-control.py"
WRAPPER = ROOT / "scripts" / "mini-release-poll.sh"


def _load_control():
    spec = importlib.util.spec_from_file_location("mini_release_poll_control_test", CONTROL)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freeze_unfreeze_status_produce_verified_receipts(tmp_path: Path) -> None:
    module = _load_control()
    releases = tmp_path / "releases"
    releases.mkdir()
    certified = "a" * 40

    frozen = module.change_state(
        releases, state="frozen", actor="release-operator", reason="guarded rollout"
    )
    assert frozen["state"] == "frozen"
    assert frozen["certified_sha"] is None
    assert module.read_state(releases)["receipt_id"] == frozen["receipt_id"]

    unfrozen = module.change_state(
        releases,
        state="unfrozen",
        actor="release-operator",
        reason="exact main CI passed",
        certified_sha=certified,
    )
    assert unfrozen["sequence"] == 2
    assert unfrozen["previous_receipt_id"] == frozen["receipt_id"]
    assert module.read_state(releases)["certified_sha"] == certified
    receipts = list(releases.glob(".mini-release-poll-control-receipt-*.json"))
    assert len(receipts) == 2
    assert all((receipt.stat().st_mode & 0o077) == 0 for receipt in receipts)


def test_corrupt_or_unsafe_state_fails_closed_without_repair(tmp_path: Path) -> None:
    module = _load_control()
    releases = tmp_path / "releases"
    releases.mkdir()
    module.change_state(releases, state="frozen", actor="operator", reason="test")
    latest = releases / module.LATEST_NAME
    latest.write_text("{not-json", encoding="utf-8")
    before = latest.read_bytes()

    with pytest.raises(module.ControlError):
        module.read_state(releases)
    with pytest.raises(module.ControlError):
        module.change_state(
            releases,
            state="unfrozen",
            actor="operator",
            reason="must not overwrite uncertainty",
            certified_sha="b" * 40,
        )
    assert latest.read_bytes() == before


def _poll_fixture(tmp_path: Path) -> tuple[Path, Path]:
    hermes = tmp_path / ".hermes"
    runtime = hermes / "runtime-current"
    scripts = runtime / "scripts"
    python_dir = runtime / "venv" / "bin"
    releases = hermes / "releases"
    scripts.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    releases.mkdir()
    shutil.copy2(WRAPPER, scripts / WRAPPER.name)
    shutil.copy2(CONTROL, scripts / CONTROL.name)
    os.chmod(scripts / WRAPPER.name, 0o755)
    os.symlink(sys.executable, python_dir / "python")
    called = tmp_path / "cut-called.json"
    cutter = scripts / "mini-release-cut.sh"
    cutter.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"open({str(called)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    os.chmod(cutter, 0o755)
    return hermes, called


def test_poll_wrapper_never_calls_cutter_when_frozen_or_unknown(tmp_path: Path) -> None:
    module = _load_control()
    hermes, called = _poll_fixture(tmp_path)
    releases = hermes / "releases"
    env = {**os.environ, "HERMES_HOME": str(hermes)}

    unknown = subprocess.run(
        [str(hermes / "runtime-current" / "scripts" / WRAPPER.name)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unknown.returncode != 0
    assert not called.exists()

    module.change_state(releases, state="frozen", actor="operator", reason="hold")
    frozen = subprocess.run(
        [str(hermes / "runtime-current" / "scripts" / WRAPPER.name)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert frozen.returncode != 0
    assert not called.exists()


def test_poll_wrapper_binds_cutter_to_authorized_exact_sha(tmp_path: Path) -> None:
    module = _load_control()
    hermes, called = _poll_fixture(tmp_path)
    certified = "c" * 40
    module.change_state(
        hermes / "releases",
        state="unfrozen",
        actor="operator",
        reason="certified main",
        certified_sha=certified,
    )

    result = subprocess.run(
        [str(hermes / "runtime-current" / "scripts" / WRAPPER.name)],
        env={**os.environ, "HERMES_HOME": str(hermes)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    args = json.loads(called.read_text(encoding="utf-8"))
    assert args == [
        "--ref",
        "prod-live-patches",
        "--certified-sha",
        certified,
        "--if-advanced",
        "--prune",
    ]
