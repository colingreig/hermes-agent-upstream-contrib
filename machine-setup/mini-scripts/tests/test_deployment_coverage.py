"""Unit tests for the fleet-outcome probe deployment_coverage operational check.

The check enforces mini_local_registry.json: every live file under the scripts
and LaunchAgents roots must be bundle-governed, direct-deployed (byte-matching
its release mirror), or explicitly declared mini-local.  These tests build a
miniature home tree with a runtime-current mirror and drive each alarm code.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "fleet_outcome_probe.py"
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(
        f"fleet_outcome_probe_coverage_ut_{_COUNTER}", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path):
    """A fully declared tree: one dest_map bundle, one direct file, one local file."""
    scripts = tmp_path / ".hermes" / "scripts"
    agents = tmp_path / "Library" / "LaunchAgents"
    mirror = tmp_path / ".hermes" / "runtime-current" / "machine-setup" / "mini-scripts"
    for directory in (scripts, agents, mirror):
        directory.mkdir(parents=True)

    bundle_bytes = b"print('governed bundle file')\n"
    (scripts / "spendlike.py").write_bytes(bundle_bytes)
    (mirror / "bundle_manifest.json").write_text(
        json.dumps(
            {
                "files": [
                    {"dest_abs": "~/.hermes/scripts/spendlike.py", "sha256": _sha(bundle_bytes)},
                    {"dest_abs": "/outside/of/scope.py", "sha256": "ignored"},
                ]
            }
        ),
        encoding="utf-8",
    )

    direct_bytes = b"echo direct\n"
    (mirror / "direct.sh").write_bytes(direct_bytes)
    (scripts / "direct.sh").write_bytes(direct_bytes)

    plist_bytes = b"<plist/>\n"
    (mirror / "launchd").mkdir()
    (mirror / "launchd" / "com.example.agent.plist").write_bytes(plist_bytes)
    (agents / "com.example.agent.plist").write_bytes(plist_bytes)

    (scripts / "local_only.py").write_bytes(b"# mini-local\n")
    (agents / "com.google.keystone.agent.plist").write_bytes(b"<plist/>\n")

    # State/noise that must never alarm.
    (scripts / ".some_state.json").write_bytes(b"{}")
    (scripts / "__pycache__").mkdir()
    (scripts / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\x00")
    (scripts / "direct.sh.bak-20260101").write_bytes(b"old")

    registry = {
        "schema_version": 1,
        "release_mirror_rel": "machine-setup/mini-scripts",
        "bundles": [
            {"manifest_rel": "bundle_manifest.json", "schema": "dest_map", "installer": "x"}
        ],
        "direct_deploy": [
            {"src_rel": "direct.sh", "dest": "scripts/direct.sh"},
            {"src_rel": "launchd/com.example.agent.plist", "dest": "launch_agents/com.example.agent.plist"},
        ],
        "pinned_exceptions": {},
        "mini_local": [
            {"path": "scripts/local_only.py", "category": "test", "reason": "declared"},
            {"path": "launch_agents/com.google.*", "category": "third-party", "reason": "google", "glob": True},
        ],
        "state": {
            "dir_parts": ["__pycache__"],
            "dir_part_prefixes": [".bak-"],
            "basename_patterns": ["*.pyc", "*.bak-*"],
            "dot_state_suffixes": [".json", ".sqlite3", ".lock"],
            "files": [],
        },
    }
    registry_path = scripts / "mini_local_registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    contract = {
        "id": "deployment-coverage",
        "kind": "deployment_coverage",
        "path": "~/.hermes/scripts/mini_local_registry.json",
        "launch_agents_dir": "~/Library/LaunchAgents",
    }
    return contract, registry, registry_path, scripts, agents, mirror


def _codes(findings):
    return sorted(item["code"] for item in findings)


def test_fully_declared_tree_is_clean(tmp_path):
    module = _load_module()
    contract, _registry, _registry_path, _scripts, _agents, _mirror = _fixture(tmp_path)
    findings, evidence = module._check_deployment_coverage(contract, home=tmp_path)
    assert findings == []
    assert evidence[0]["live_files"] >= 5
    assert evidence[0]["pinned_accepted"] == []


def test_undeclared_live_file_alarms_with_aggregated_paths(tmp_path):
    module = _load_module()
    contract, _r, _rp, scripts, agents, _m = _fixture(tmp_path)
    (scripts / "forgotten.py").write_bytes(b"# nobody declared me\n")
    (agents / "com.mystery.tool.plist").write_bytes(b"<plist/>\n")
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["undeclared_file"]
    detail = findings[0]["detail"]
    assert "scripts/forgotten.py" in detail
    assert "launch_agents/com.mystery.tool.plist" in detail
    assert detail.startswith("2 file(s):")


def test_direct_deploy_drift_alarms_and_pin_accepts_known_sha(tmp_path):
    module = _load_module()
    contract, registry, registry_path, scripts, _a, _m = _fixture(tmp_path)
    drifted = b"echo drifted\n"
    (scripts / "direct.sh").write_bytes(drifted)
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["direct_deploy_drift"]
    assert "scripts/direct.sh" in findings[0]["detail"]

    registry["pinned_exceptions"] = {
        "scripts/direct.sh": {"sha256": _sha(drifted), "reason": "known", "reconcile_task": "t"}
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    findings, evidence = module._check_deployment_coverage(contract, home=tmp_path)
    assert findings == []
    assert evidence[0]["pinned_accepted"] == ["scripts/direct.sh"]


def test_bundle_sha_drift_alarms_and_pin_accepts_known_sha(tmp_path):
    module = _load_module()
    contract, registry, registry_path, scripts, _a, _m = _fixture(tmp_path)
    drifted = b"print('hand edited')\n"
    (scripts / "spendlike.py").write_bytes(drifted)
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["bundle_sha_drift"]

    registry["pinned_exceptions"] = {
        "scripts/spendlike.py": {"sha256": _sha(drifted), "reason": "known", "reconcile_task": "t"}
    }
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    findings, evidence = module._check_deployment_coverage(contract, home=tmp_path)
    assert findings == []
    assert evidence[0]["pinned_accepted"] == ["scripts/spendlike.py"]


def test_declared_direct_file_missing_live_alarms_when_mirror_has_it(tmp_path):
    module = _load_module()
    contract, _r, _rp, scripts, _a, _m = _fixture(tmp_path)
    (scripts / "direct.sh").unlink()
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["direct_deploy_missing"]
    assert "scripts/direct.sh" in findings[0]["detail"]


def test_declared_direct_file_absent_everywhere_is_pending_release_not_alarm(tmp_path):
    module = _load_module()
    contract, registry, registry_path, _s, _a, _m = _fixture(tmp_path)
    registry["direct_deploy"].append({"src_rel": "not_released_yet.py", "dest": "scripts/not_released_yet.py"})
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    findings, evidence = module._check_deployment_coverage(contract, home=tmp_path)
    assert findings == []
    assert evidence[0]["pending_release"] == ["scripts/not_released_yet.py"]


def test_live_direct_file_with_missing_mirror_source_alarms(tmp_path):
    module = _load_module()
    contract, _r, _rp, _s, _a, mirror = _fixture(tmp_path)
    (mirror / "direct.sh").unlink()
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["mirror_source_missing"]


def test_stale_mini_local_entry_alarms_so_the_registry_stays_honest(tmp_path):
    module = _load_module()
    contract, _r, _rp, scripts, _a, _m = _fixture(tmp_path)
    (scripts / "local_only.py").unlink()
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["registry_stale_entry"]
    assert "scripts/local_only.py" in findings[0]["detail"]


def test_missing_registry_is_its_own_distinguishable_alarm(tmp_path):
    module = _load_module()
    contract, _r, registry_path, _s, _a, _m = _fixture(tmp_path)
    registry_path.unlink()
    findings, evidence = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["coverage_registry_missing"]
    assert evidence == []


def test_invalid_registry_json_alarms_instead_of_crashing(tmp_path):
    module = _load_module()
    contract, _r, registry_path, _s, _a, _m = _fixture(tmp_path)
    registry_path.write_text("{not json", encoding="utf-8")
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["coverage_registry_invalid"]


def test_unreadable_bundle_manifest_alarms_without_masking_other_checks(tmp_path):
    module = _load_module()
    contract, _r, _rp, _s, _a, mirror = _fixture(tmp_path)
    (mirror / "bundle_manifest.json").write_text("{broken", encoding="utf-8")
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    # The governed file also becomes undeclared because its manifest is gone.
    assert "coverage_manifest_unreadable" in _codes(findings)
    assert "undeclared_file" in _codes(findings)


def test_pr_pipeline_schema_classifies_entrypoints_patterns_and_package(tmp_path):
    module = _load_module()
    contract, registry, registry_path, scripts, _a, mirror = _fixture(tmp_path)
    (mirror / "pr_pipeline").mkdir()
    (mirror / "pr_pipeline" / "manifest.json").write_text(
        json.dumps(
            {
                "source_root_entrypoints": ["review_poll_gate.py"],
                "legacy_flat_entrypoints": ["merge_guard.py"],
                "expected_local_patches": {"verify-hermes-patches.sh": {"deployed_sha256": "x"}},
                "managed_root_patterns": ["validator_*.py"],
                "unmanaged_root_exclusions": ["validator_autonomy.py"],
                "package_glob": "*.py",
                "package_destination": "pr_pipeline",
            }
        ),
        encoding="utf-8",
    )
    registry["bundles"].append(
        {"manifest_rel": "pr_pipeline/manifest.json", "schema": "pr_pipeline", "installer": "x"}
    )
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    (scripts / "review_poll_gate.py").write_bytes(b"1")
    (scripts / "merge_guard.py").write_bytes(b"2")
    (scripts / "verify-hermes-patches.sh").write_bytes(b"3")
    (scripts / "validator_panel.py").write_bytes(b"4")
    (scripts / "pr_pipeline").mkdir()
    (scripts / "pr_pipeline" / "anything.py").write_bytes(b"5")
    # Excluded from managed patterns: must be explicitly declared or alarm.
    (scripts / "validator_autonomy.py").write_bytes(b"6")

    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert _codes(findings) == ["undeclared_file"]
    assert findings[0]["detail"] == "1 file(s): scripts/validator_autonomy.py"


def test_state_and_backup_debris_never_alarm(tmp_path):
    module = _load_module()
    contract, _r, _rp, scripts, _a, _m = _fixture(tmp_path)
    (scripts / ".other_state.sqlite3").write_bytes(b"\x00")
    (scripts / "anything.py.bak-precutover").write_bytes(b"old")
    findings, _ = module._check_deployment_coverage(contract, home=tmp_path)
    assert findings == []


def test_operational_dispatch_routes_deployment_coverage_kind(tmp_path):
    module = _load_module()
    contract, _r, registry_path, _s, _a, _m = _fixture(tmp_path)
    registry_path.unlink()
    findings, _ = module._check_operational_contracts(
        [contract], home=tmp_path, now=module._now()
    )
    assert _codes(findings) == ["coverage_registry_missing"]
