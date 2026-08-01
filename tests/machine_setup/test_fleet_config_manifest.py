"""CI-visible integrity contract for the governed fleet-config bundle."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_CONFIG_ROOT = REPO_ROOT / "machine-setup" / "fleet-config"
MANIFEST_PATH = FLEET_CONFIG_ROOT / "fleet_config_manifest.json"


def test_fleet_config_manifest_pins_current_source_bytes():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    for entry in manifest["files"]:
        source_path = FLEET_CONFIG_ROOT / entry["src_rel"]
        actual_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert actual_sha256 == entry["sha256"], (
            f"fleet-config manifest hash mismatch for {entry['src_rel']}"
        )


def test_fleet_config_installer_is_pinned_as_source_only():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    installer = next(
        entry
        for entry in manifest["files"]
        if entry["src_rel"] == "install_fleet_config.py"
    )

    assert installer["deploy_mode"] == "installer_source"
    assert "dest_abs" not in installer
