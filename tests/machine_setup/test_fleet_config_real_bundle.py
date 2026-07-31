"""Real-bundle integration contracts for the governed Mini fleet config.

Unlike machine-setup/fleet-config/tests/test_install_fleet_config.py, this file
uses the checked-in production bundle and manifest bytes. It proves the actual
Mini installer contract the ClickUp repair depends on: all five swarm profiles
install through the manifest-pinned path, each profile preserves the internal
kanban-card vs outer ClickUp lifecycle boundary, the ops verifier contract is
present, and the two live executor jobs bridge ClickUp only to In Review.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ROOT = ROOT / "machine-setup" / "fleet-config"
SCRIPT = BUNDLE_ROOT / "install_fleet_config.py"

_spec = importlib.util.spec_from_file_location("fleet_config_installer_real_bundle", SCRIPT)
assert _spec is not None and _spec.loader is not None
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)

EXPECTED_PROFILES = {"coder", "content", "design", "research", "ops"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest() -> dict:
    return json.loads((BUNDLE_ROOT / "fleet_config_manifest.json").read_text())


def _latest_receipt(home: Path) -> dict:
    receipts = sorted((home / ".hermes" / "logs" / "fleet-config-installs").glob("*/install-receipt.json"))
    assert len(receipts) == 1
    return json.loads(receipts[0].read_text())


def test_checked_in_real_bundle_installs_all_profiles_with_lifecycle_contracts(tmp_path):
    home = tmp_path / "mini-home"
    (home / ".hermes").mkdir(parents=True)
    # Seed live-only config to prove the governed overlay merges rather than
    # replacing unrelated Mini settings.
    (home / ".hermes" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "platforms": {"telegram": {"enabled": True}},
                "security": {"redact_secrets": True},
                "delegation": {"provider": "stale-provider", "base_url": "https://example.invalid/v1"},
            }
        ),
        encoding="utf-8",
    )

    manifest = _load_manifest()
    manifest_path = BUNDLE_ROOT / "fleet_config_manifest.json"

    rc = install_mod.install(
        manifest,
        home=home,
        bundle_root=BUNDLE_ROOT,
        manifest_path=manifest_path,
        dry_run=False,
    )

    assert rc == 0

    deployed_config = yaml.safe_load((home / ".hermes" / "config.yaml").read_text())
    assert deployed_config["platforms"] == {"telegram": {"enabled": True}}
    assert deployed_config["security"] == {"redact_secrets": True}
    assert deployed_config["delegation"] == {}

    manifest_entries = {entry["src_rel"]: entry for entry in manifest["files"]}
    installed_profiles: dict[str, str] = {}
    for profile in EXPECTED_PROFILES:
        profile_dir = home / ".hermes" / "profiles" / profile
        assert profile_dir.is_dir()
        for subdir in install_mod.PROFILE_BOOTSTRAP_DIRS:
            assert (profile_dir / subdir).is_dir()

        soul = (profile_dir / "SOUL.md").read_text()
        soul_words = " ".join(soul.split())
        installed_profiles[profile] = soul
        assert "These are two separate lifecycles" in soul_words
        assert "hermes kanban complete <card-id>" in soul_words
        assert "does **not** mark the ClickUp task Complete" in soul_words
        assert "Only `ignite-validate` moves ClickUp to Complete" in soul_words
        assert "Never block a successful card merely because ClickUp must remain short of Complete" in soul_words

        for rel in (f"profiles/{profile}/config.yaml", f"profiles/{profile}/SOUL.md"):
            deployed = home / ".hermes" / rel
            assert _sha256(deployed) == manifest_entries[rel]["sha256"]

    assert "As a kanban verifier" in installed_profiles["ops"]
    assert "--metadata '{\"gate\":\"pass\"}'" in installed_profiles["ops"]
    assert "If the evidence is insufficient" in installed_profiles["ops"]
    assert "Never block a successful card merely because ClickUp must remain short of Complete" in " ".join(installed_profiles["ops"].split())

    jobs = json.loads((home / ".hermes" / "cron" / "jobs.json").read_text())["jobs"]
    jobs_by_name = {job["name"]: job for job in jobs}
    for name, worker, synthesizer in (
        ("clickup-executor", "--worker coder:\"implement\"", "--synthesizer coder"),
        ("content-lane-executor", "--worker content:\"draft\"", "--synthesizer content"),
    ):
        prompt = jobs_by_name[name]["prompt"]
        assert "hermes kanban swarm" in prompt
        assert worker in prompt
        assert "--verifier ops" in prompt
        assert synthesizer in prompt
        assert "move the task to In Review" in prompt
        assert "never Complete" in prompt
        assert "Do NOT work the task in this session" in prompt or "Do NOT draft the piece in this session" in prompt

    receipt = _latest_receipt(home)
    assert receipt["result"] == "success"
    assert receipt["manifest_sha256"] == _sha256(manifest_path)
    profile_steps = [step for step in receipt["steps"] if step["step"] == "profile_file"]
    assert len(profile_steps) == 10
    assert {Path(step["dest"]).parents[0].name for step in profile_steps} == EXPECTED_PROFILES
    assert {step["sha256"] for step in profile_steps} == {
        entry["sha256"]
        for rel, entry in manifest_entries.items()
        if rel.startswith("profiles/")
    }


def test_real_bundle_manifest_covers_exactly_the_five_swarm_profiles():
    manifest = _load_manifest()
    profile_entries = [entry for entry in manifest["files"] if entry["deploy_mode"] == "profile_file"]

    assert len(profile_entries) == 10
    assert {Path(entry["src_rel"]).parts[1] for entry in profile_entries} == EXPECTED_PROFILES
    assert all(entry["dest_abs"].startswith(f"~/.hermes/profiles/{Path(entry['src_rel']).parts[1]}/") for entry in profile_entries)
    assert all(_sha256(BUNDLE_ROOT / entry["src_rel"]) == entry["sha256"] for entry in manifest["files"])
