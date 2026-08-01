"""Tests for cleanup_hermes_local_ignite_shadows.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

FLEET_ROOT = Path(__file__).resolve().parents[2] / "fleet-config"
POLICY_PATH = FLEET_ROOT / "skills-policy.json"
CLEANUP_SCRIPT = Path(__file__).resolve().parents[1] / "cleanup_hermes_local_ignite_shadows.py"
INSTALL_SCRIPT = FLEET_ROOT / "install_fleet_config.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


install_mod = _load_module(INSTALL_SCRIPT, "install_fleet_config_cleanup_test")
cleanup_mod = _load_module(CLEANUP_SCRIPT, "cleanup_hermes_local_ignite_shadows_test")


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _seed_minimal_fleet_tree(home: Path, policy: dict) -> None:
    skills_dir = home / ".hermes" / "skills"
    skills_dir.mkdir(parents=True)
    for name, rel in policy["required_local_keep"].items():
        _write_skill(skills_dir / rel, name)


@pytest.fixture
def policy():
    return install_mod.load_skill_policy(POLICY_PATH, bundle_root=FLEET_ROOT)


@pytest.fixture(autouse=True)
def _patch_fleet_root(monkeypatch):
    monkeypatch.setattr(cleanup_mod, "_fleet_config_root", lambda: FLEET_ROOT)


def test_cleanup_dry_run_reports_shadow_without_mutation(tmp_path, policy, capsys):
    home = tmp_path / "home"
    _seed_minimal_fleet_tree(home, policy)
    _write_skill(home / ".hermes" / "skills" / "ignite-execute", "ignite-execute")

    rc = cleanup_mod.run_cleanup(home=home, apply=False)

    assert rc == 1
    assert "ignite-execute" in capsys.readouterr().out
    assert (home / ".hermes" / "skills" / "ignite-execute").exists()


def test_cleanup_apply_archives_shadow(tmp_path, policy):
    home = tmp_path / "home"
    _seed_minimal_fleet_tree(home, policy)
    shadow = home / ".hermes" / "skills" / "ignite-execute"
    _write_skill(shadow, "ignite-execute")

    rc = cleanup_mod.run_cleanup(home=home, apply=True)

    assert rc == 0
    assert not shadow.exists()
    archive_dirs = list((home / ".hermes" / "archives" / "ignite-local-shadow").iterdir())
    assert len(archive_dirs) == 1
    assert (archive_dirs[0] / "ignite-execute" / "SKILL.md").is_file()
    remaining = install_mod._find_unmanaged_skill_manifests(home / ".hermes" / "skills", policy)
    assert remaining == []


def test_cleanup_ok_when_no_shadows(tmp_path, policy, capsys):
    home = tmp_path / "home"
    _seed_minimal_fleet_tree(home, policy)

    rc = cleanup_mod.run_cleanup(home=home, apply=False)

    assert rc == 0
    assert "OK:" in capsys.readouterr().out
