"""Verifies the real fleet-config bundle's lifecycle contracts across all 5 profiles."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

FLEET_CONFIG_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = FLEET_CONFIG_ROOT / "fleet_config_manifest.json"
PROFILES = ("coder", "content", "design", "research", "ops")


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _soul_text(profile: str) -> str:
    return (FLEET_CONFIG_ROOT / "profiles" / profile / "SOUL.md").read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_all_profiles_present_in_manifest():
    manifest = _load_manifest()
    src_rels = {entry["src_rel"] for entry in manifest["files"]}
    for profile in PROFILES:
        assert f"profiles/{profile}/config.yaml" in src_rels
        assert f"profiles/{profile}/SOUL.md" in src_rels


def test_manifest_source_hashes_match():
    manifest = _load_manifest()
    for entry in manifest["files"]:
        src_path = FLEET_CONFIG_ROOT / entry["src_rel"]
        actual_sha256 = hashlib.sha256(src_path.read_bytes()).hexdigest()
        assert actual_sha256 == entry["sha256"], f"hash mismatch for {entry['src_rel']}"


def test_all_souls_contain_lifecycle_contract():
    for profile in PROFILES:
        text = _soul_text(profile)
        norm = _normalized(text)
        assert "Kanban card handoff vs ClickUp" in text
        assert "kanban complete" in text
        assert "kanban block" in text
        assert "does **not** mark the ClickUp task Complete" in norm
        assert "remain short of Complete" in norm


def test_ops_soul_contains_verifier_contract():
    norm = _normalized(_soul_text("ops"))
    assert "verifier" in norm
    assert "verification evidence" in norm
    assert '"gate":"pass"' in norm
    assert "Only a session explicitly acting as the" in norm
    assert "`ignite-validate` pass may move ClickUp to Complete" in norm


def test_content_soul_fail_closed_model_policy():
    norm = _normalized(_soul_text("content"))
    assert "claude-sonnet-5 and nothing else" in norm
    assert "no fallback providers, on purpose" in norm
    assert "fail the task closed" in norm
    assert "is a defect" in norm
