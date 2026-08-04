from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parent.parent
INSTALLER_PATH = SCRIPTS / "install_spend.py"
MANIFEST_PATH = SCRIPTS / "spend_manifest.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("install_spend_ut", INSTALLER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_real_manifest_installs_only_declared_files_with_verified_receipt(tmp_path):
    module = _load_module()
    manifest = _manifest()
    scripts = tmp_path / ".hermes" / "scripts"
    scripts.mkdir(parents=True)
    protected = {
        "claim_store.py": b"claim-store-live\n",
        "hermes_report_build.py": b"report-builder-live\n",
        "unrelated.py": b"unrelated-live\n",
    }
    for name, data in protected.items():
        (scripts / name).write_bytes(data)

    result = module.install(
        manifest,
        home=tmp_path,
        mirror_root=SCRIPTS,
        manifest_path=MANIFEST_PATH,
        dry_run=False,
    )

    assert result == 0
    declared = {Path(entry["dest_abs"]).name: entry for entry in manifest["files"]}
    assert set(declared) == {
        "spend_opencode.py",
        "opencode_exec.py",
        "spend_guard.py",
        "spend_meter.py",
    }
    for name, entry in declared.items():
        deployed = scripts / name
        assert hashlib.sha256(deployed.read_bytes()).hexdigest() == entry["sha256"]
    assert (scripts / "spend_manifest.json").read_bytes() == MANIFEST_PATH.read_bytes()
    for name, data in protected.items():
        assert (scripts / name).read_bytes() == data

    receipts = list((tmp_path / ".hermes/logs/spend-guard-installs").glob("*/install-receipt.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["result"] == "success"
    assert len(receipt["files"]) == 5
    assert {item["status"] for item in receipt["files"]} == {"installed"}


def test_source_hash_drift_refuses_before_any_destination_write(tmp_path):
    module = _load_module()
    manifest = _manifest()
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for entry in manifest["files"]:
        (mirror / entry["src_rel"]).write_bytes((SCRIPTS / entry["src_rel"]).read_bytes())
    (mirror / manifest["files"][0]["src_rel"]).write_bytes(b"tampered\n")

    with pytest.raises(module.InstallError, match="source hash drift"):
        module.install(
            manifest,
            home=tmp_path,
            mirror_root=mirror,
            manifest_path=MANIFEST_PATH,
            dry_run=False,
        )

    assert not (tmp_path / ".hermes/scripts").exists()
    assert not (tmp_path / ".hermes/logs/spend-guard-installs").exists()


def test_manifest_cannot_claim_protected_coexist_file(tmp_path):
    module = _load_module()
    manifest = _manifest()
    manifest["files"][0] = {
        **manifest["files"][0],
        "dest_abs": "~/.hermes/scripts/claim_store.py",
    }

    with pytest.raises(module.InstallError, match="protected file"):
        module.build_plan(manifest, home=tmp_path, mirror_root=SCRIPTS)


def test_existing_symlinked_scripts_directory_cannot_escape_home(tmp_path):
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    (hermes / "scripts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.InstallError, match="outside"):
        module.build_plan(_manifest(), home=tmp_path, mirror_root=SCRIPTS)


def test_allowed_hermes_root_itself_cannot_be_a_symlink(tmp_path):
    module = _load_module()
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".hermes").symlink_to(outside, target_is_directory=True)

    with pytest.raises(module.InstallError, match="allowed destination root.*symlink"):
        module.build_plan(_manifest(), home=tmp_path, mirror_root=SCRIPTS)
