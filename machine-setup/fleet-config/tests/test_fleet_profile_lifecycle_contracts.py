"""Contracts for the direct fleet ClickUp executors and profile bundle."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

FLEET_CONFIG_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = FLEET_CONFIG_ROOT / "fleet_config_manifest.json"
JOBS_PATH = FLEET_CONFIG_ROOT / "jobs.json"
OUTCOME_CONTRACTS_PATH = (
    FLEET_CONFIG_ROOT.parent / "mini-scripts" / "fleet_outcome_contracts.json"
)
COVERAGE_PATH = FLEET_CONFIG_ROOT / "MONITOR_COVERAGE.md"
PROFILES = ("coder", "content", "design", "research", "ops")
RETIRED_POLLER_MARKERS = (
    "6139465f559f",
    "Purelymail notify-me poller",
    "purelymail-notify-poller.py",
)


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _jobs_by_name() -> dict[str, dict]:
    jobs = json.loads(JOBS_PATH.read_text(encoding="utf-8"))["jobs"]
    return {job["name"]: job for job in jobs}


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


def test_manifest_pins_governed_installer_as_source_only():
    manifest = _load_manifest()
    assert manifest["fleet_contract"] == "direct-clickup-v1"
    installer = next(entry for entry in manifest["files"] if entry["src_rel"] == "install_fleet_config.py")
    assert installer["deploy_mode"] == "installer_source"
    assert "dest_abs" not in installer


def test_clickup_executor_jobs_use_direct_paths_with_stable_scheduling():
    jobs = _jobs_by_name()
    expected = {
        "clickup-executor": {
            "id": "62714b869845",
            "prompt": "Work the ClickUp queue now — follow the clickup-queue-poller skill.",
        },
        "content-lane-executor": {
            "id": "dcab830aa41c",
            "prompt": "/ignite-execute --lane content",
        },
    }

    for name, contract in expected.items():
        job = jobs[name]
        assert job["id"] == contract["id"]
        assert job["prompt"] == contract["prompt"]
        assert job["enabled"] is True
        assert job["schedule"] == {
            "display": "*/30 * * * *",
            "expr": "*/30 * * * *",
            "kind": "cron",
        }
        assert job["schedule_display"] == "*/30 * * * *"


def test_clickup_executor_contract_uses_direct_skills_without_kanban_routing():
    jobs = _jobs_by_name()
    for name in ("clickup-executor", "content-lane-executor"):
        prompt = jobs[name]["prompt"].casefold()
        assert "kanban" not in prompt
        assert "swarm" not in prompt
        assert "synthesizer" not in prompt


def test_clickup_executor_jobs_have_no_fleet_swarm_handoff_contract():
    jobs = _jobs_by_name()
    for name in ("clickup-executor", "content-lane-executor"):
        prompt = jobs[name]["prompt"]
        assert "hermes kanban swarm" not in prompt.casefold()
        assert "swarm_synthesis" not in prompt.casefold()
        assert "synthesizer" not in prompt.casefold()


def test_content_executor_is_sonnet_only_and_loads_approved_skill():
    job = _jobs_by_name()["content-lane-executor"]

    assert job["provider"] == "anthropic"
    assert job["model"] == "claude-sonnet-5"
    assert job["no_fallback"] is True
    assert job["skill"] == "ignite-execute"
    assert job["skills"] == ["ignite-execute"]
    assert job["skill_scope"] == "content-executor"
    assert job["max_turns"] == 200


def test_pr_validator_uses_only_the_canonical_ignite_root():
    job = _jobs_by_name()["hermes-pr-validate"]
    prompt = job["prompt"]
    expected_modules = (
        "$IGNITE_SKILLS_ROOT/clickup/clickup.mjs",
        "$IGNITE_SKILLS_ROOT/ignite-state/scripts/blocklist-sync.mjs",
        "$IGNITE_SKILLS_ROOT/ignite-state/scripts/sync-project-plugins.mjs",
        "$IGNITE_SKILLS_ROOT/ignite-state/scripts/target-repo.mjs",
    )

    assert "IGNITE_SKILLS_ROOT" in job["required_environment_variables"]
    assert job["skill"] == "ignite-validate"
    assert job["skills"] == ["ignite-validate"]
    for module_path in expected_modules:
        assert module_path in prompt
    for home_local_root in (
        "~/.hermes/skills",
        "~/.claude/skills",
        "~/.codex/skills",
    ):
        assert home_local_root not in prompt
    assert "Do not probe, copy, symlink, or fall back" in prompt


def test_retired_purelymail_poller_is_absent_from_fleet_surfaces():
    surfaces = {
        "fleet jobs": json.dumps(json.loads(JOBS_PATH.read_text(encoding="utf-8"))),
        "outcome contracts": json.dumps(
            json.loads(OUTCOME_CONTRACTS_PATH.read_text(encoding="utf-8"))
        ),
        "monitor coverage": COVERAGE_PATH.read_text(encoding="utf-8"),
    }

    for surface, content in surfaces.items():
        normalized = content.casefold()
        for marker in RETIRED_POLLER_MARKERS:
            assert marker.casefold() not in normalized, f"{marker!r} returned in {surface}"


def test_all_souls_are_direct_profile_personas():
    for profile in PROFILES:
        text = _soul_text(profile)
        lowered = text.casefold()
        assert "direct hermes" in _normalized(lowered)
        assert "kanban" not in lowered
        assert "synthesizer" not in lowered
        assert "--worker" not in lowered
        assert "--verifier" not in lowered


def test_ops_soul_preserves_truthful_validation_boundary():
    norm = _normalized(_soul_text("ops"))
    assert "Actually exercise the claim" in norm
    assert "exact evidence" in norm
    assert "Only a session explicitly acting as the `ignite-validate` pass may move" in norm
    assert "ClickUp task to Complete" in norm


def test_content_soul_fail_closed_model_policy():
    norm = _normalized(_soul_text("content"))
    assert "claude-sonnet-5 and nothing else" in norm
    assert "no fallback providers, on purpose" in norm
    assert "fail the task closed" in norm
    assert "is a defect" in norm


def test_design_profile_uses_only_sanctioned_fallbacks():
    config = (FLEET_CONFIG_ROOT / "profiles" / "design" / "config.yaml").read_text(
        encoding="utf-8"
    ).casefold()
    assert "google" not in config
    assert "gemini" not in config
