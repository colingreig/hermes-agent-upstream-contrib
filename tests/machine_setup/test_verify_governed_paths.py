"""Read-only fixture coverage for the governed Mini path verifier."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "machine-setup" / "mini-scripts" / "verify_governed_paths.py"
MINI_SCRIPTS = SCRIPT.parent
FLEET_ROOT = REPO_ROOT / "machine-setup" / "fleet-config"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_governed_paths", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


module = _load_module()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _install_fixture(home: Path) -> Path:
    hermes = home / ".hermes"
    releases = hermes / "releases"
    active = releases / "v1-active"
    previous = releases / "v0-previous"
    active.mkdir(parents=True)
    previous.mkdir()
    (hermes / "runtime-current").symlink_to(active)
    (releases / ".previous").write_text(f"{previous}\n", encoding="utf-8")

    receipt = {
        "schema_version": 2,
        "event": "cut",
        "from_commit": "a" * 40,
        "to_commit": "b" * 40,
        "runtime_target": str(active),
    }
    receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = _sha256(receipt_bytes)
    (releases / f".mini-release-receipt-{digest}.json").write_bytes(receipt_bytes)
    (releases / ".mini-release-last-receipt.json").write_bytes(receipt_bytes)

    outcome_manifest = json.loads((MINI_SCRIPTS / "fleet_outcome_manifest.json").read_text())
    scripts = hermes / "scripts"
    launch_agents = home / "Library" / "LaunchAgents"
    scripts.mkdir(parents=True)
    launch_agents.mkdir(parents=True)
    shutil.copy2(MINI_SCRIPTS / "fleet_outcome_manifest.json", scripts / "fleet_outcome_manifest.json")
    for entry in outcome_manifest["files"]:
        source = MINI_SCRIPTS / entry["source"]
        destination = (scripts if entry["destination_root"] == "scripts" else launch_agents) / entry["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(int(entry["mode"], 8))

    source_jobs = json.loads((FLEET_ROOT / "jobs.json").read_text())
    source_jobs["jobs"][0]["last_run_at"] = "runtime-owned"
    cron = hermes / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text(json.dumps(source_jobs), encoding="utf-8")

    overlay = yaml.safe_load((FLEET_ROOT / "config-overlay.yaml").read_text())
    overlay["unmanaged"] = {"kept": True}
    (hermes / "config.yaml").write_text(yaml.safe_dump(overlay), encoding="utf-8")
    fleet_manifest = json.loads((FLEET_ROOT / "fleet_config_manifest.json").read_text())
    for entry in fleet_manifest["files"]:
        if entry.get("deploy_mode") != "profile_file":
            continue
        source = FLEET_ROOT / entry["src_rel"]
        destination = hermes / "profiles" / Path(entry["src_rel"]).relative_to("profiles")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return active


def _copied_outcome_source(tmp_path: Path) -> Path:
    source = tmp_path / "outcome-source"
    shutil.copytree(MINI_SCRIPTS, source)
    return source


def _replace_deployed_manifest(home: Path, source_root: Path) -> None:
    shutil.copy2(
        source_root / "fleet_outcome_manifest.json",
        home / ".hermes" / "scripts" / "fleet_outcome_manifest.json",
    )


def test_fixture_safe_verifier_accepts_runtime_metadata_and_unmanaged_config(tmp_path):
    _install_fixture(tmp_path)

    findings = module.GovernedPathsVerifier(home=tmp_path, fixture_safe=True).verify()

    assert [finding.check for finding in findings] == ["release", "fleet-outcomes", "fleet-config"]


def test_verifier_detects_direct_managed_config_write(tmp_path):
    _install_fixture(tmp_path)
    config_path = tmp_path / ".hermes" / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["model"]["provider"] = "tampered"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    with pytest.raises(module.VerificationError, match="managed config value drifted"):
        module.GovernedPathsVerifier(home=tmp_path, fixture_safe=True).verify()


def test_verifier_ignores_runtime_claims_but_enforces_repeat_limit(tmp_path):
    _install_fixture(tmp_path)
    jobs_path = tmp_path / ".hermes" / "cron" / "jobs.json"
    jobs = json.loads(jobs_path.read_text())
    job = jobs["jobs"][0]
    job["fire_claim"] = {"owner": "scheduler"}
    job["run_claim"] = {"run": "runtime-only"}
    job["repeat"]["completed"] = 7
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")

    module.GovernedPathsVerifier(home=tmp_path, fixture_safe=True).verify()

    job["repeat"]["times"] = 3
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    with pytest.raises(module.VerificationError, match="managed cron job drifted"):
        module.GovernedPathsVerifier(home=tmp_path, fixture_safe=True).verify()


def test_deployed_default_fleet_root_uses_active_release(tmp_path):
    active = _install_fixture(tmp_path)
    deployed_machine_setup = active / "machine-setup"
    deployed_fleet = deployed_machine_setup / "fleet-config"
    deployed_machine_setup.mkdir(parents=True)
    shutil.copytree(FLEET_ROOT, deployed_fleet)
    shutil.copytree(MINI_SCRIPTS, deployed_machine_setup / "mini-scripts")

    result = subprocess.run(
        [
            sys.executable,
            str(tmp_path / ".hermes" / "scripts" / "verify_governed_paths.py"),
            "--home",
            str(tmp_path),
            "--fixture-safe",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "OK fleet-config" in result.stdout


def test_rejects_traversal_in_outcome_manifest_source(tmp_path):
    _install_fixture(tmp_path)
    source = _copied_outcome_source(tmp_path)
    manifest_path = source / "fleet_outcome_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["source"] = "../outside.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _replace_deployed_manifest(tmp_path, source)

    with pytest.raises(module.VerificationError, match="must not be absolute or contain '..'"):
        module.GovernedPathsVerifier(home=tmp_path, source_root=source, fixture_safe=True).verify()


def test_rejects_deployed_parent_symlink(tmp_path):
    _install_fixture(tmp_path)
    scripts = tmp_path / ".hermes" / "scripts"
    actual = tmp_path / "outside-scripts"
    scripts.rename(actual)
    scripts.symlink_to(actual, target_is_directory=True)

    with pytest.raises(module.VerificationError, match="symlinked"):
        module.GovernedPathsVerifier(home=tmp_path, fixture_safe=True).verify()


def test_rejects_null_or_duplicate_outcome_cron_updates(tmp_path):
    _install_fixture(tmp_path)
    source = _copied_outcome_source(tmp_path)
    manifest_path = source / "fleet_outcome_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["cron_updates"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _replace_deployed_manifest(tmp_path, source)
    with pytest.raises(module.VerificationError, match="cron_updates must be a list"):
        module.GovernedPathsVerifier(home=tmp_path, source_root=source, fixture_safe=True).verify()

    manifest["cron_updates"] = [
        {
            "id": "e835c614cfb2",
            "name": "ci-health-watch",
            "fields": {"script": "ci-health-watch-cron.py"},
        },
        {
            "id": "e835c614cfb2",
            "name": "ci-health-watch",
            "fields": {"script": "ci-health-watch-cron.py"},
        },
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _replace_deployed_manifest(tmp_path, source)
    with pytest.raises(module.VerificationError, match="duplicate job target"):
        module.GovernedPathsVerifier(home=tmp_path, source_root=source, fixture_safe=True).verify()
