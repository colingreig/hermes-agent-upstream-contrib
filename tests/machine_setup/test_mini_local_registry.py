"""Integrity checks for the mini deployment-coverage registry.

mini_local_registry.json is the machine-readable declaration that classifies
every live file on the mini (bundle-governed, direct-deploy, or mini-local).
These tests keep the registry honest against the repository it ships from:
every referenced source file must exist, the fleet-outcome manifest must carry
the registry with a current sha256 (the silent-rollback checksum trap), and the
contract wiring for the probe's deployment_coverage check must be present.
"""
from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest import SkipTest

REPO_ROOT = Path(__file__).resolve().parents[2]
MINI_SCRIPTS = REPO_ROOT / "machine-setup" / "mini-scripts"
REGISTRY_PATH = MINI_SCRIPTS / "mini_local_registry.json"
MANIFEST_PATH = MINI_SCRIPTS / "fleet_outcome_manifest.json"
CONTRACTS_PATH = MINI_SCRIPTS / "fleet_outcome_contracts.json"
RETENTION_POLICY_PATH = REPO_ROOT / "machine-setup" / "fleet-config" / "state_db_retention.json"

VALID_DEST_PREFIXES = ("scripts/", "launch_agents/")
VALID_SCHEMAS = {"dest_map", "fleet_outcome", "pr_pipeline"}


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _probe_module():
    module_path = MINI_SCRIPTS / "fleet_outcome_probe.py"
    spec = importlib.util.spec_from_file_location("fleet_outcome_registry_ut", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_parses_with_expected_schema():
    registry = _registry()
    assert registry["schema_version"] == 1
    for section in ("bundles", "direct_deploy", "pinned_exceptions", "mini_local", "state"):
        assert section in registry, f"registry is missing {section}"


def test_every_declared_bundle_manifest_exists_with_known_schema():
    registry = _registry()
    for bundle in registry["bundles"]:
        manifest = MINI_SCRIPTS / bundle["manifest_rel"]
        assert manifest.is_file(), f"bundle manifest missing: {bundle['manifest_rel']}"
        json.loads(manifest.read_text(encoding="utf-8"))
        assert bundle["schema"] in VALID_SCHEMAS, bundle
        assert (MINI_SCRIPTS / bundle["installer"]).is_file() or (
            REPO_ROOT / "scripts" / bundle["installer"]
        ).is_file(), f"bundle installer missing: {bundle['installer']}"


def test_every_direct_deploy_source_exists_in_repo_with_valid_dest():
    registry = _registry()
    seen: set[str] = set()
    for entry in registry["direct_deploy"]:
        source = MINI_SCRIPTS / entry["src_rel"]
        assert source.is_file(), f"direct_deploy source missing from repo: {entry['src_rel']}"
        assert entry["dest"].startswith(VALID_DEST_PREFIXES), entry
        assert entry["dest"] not in seen, f"duplicate direct_deploy dest: {entry['dest']}"
        seen.add(entry["dest"])


def test_pinned_exceptions_are_well_formed_and_reference_tracked_work():
    registry = _registry()
    declared = {entry["dest"] for entry in registry["direct_deploy"]}
    for key, pin in registry["pinned_exceptions"].items():
        assert key.startswith(VALID_DEST_PREFIXES), key
        assert isinstance(pin.get("sha256"), str) and len(pin["sha256"]) == 64, key
        assert pin.get("reason"), f"pin {key} has no reason"
        assert pin.get("reconcile_task"), f"pin {key} has no reconcile task"
        assert "TBD" not in pin["reconcile_task"], f"pin {key} has a placeholder task id"
        # A pin must attach to something the registry actually governs: a
        # direct-deploy dest or a bundle-governed dest (spot-check dest_map).
        if key not in declared:
            governed = set()
            for bundle in registry["bundles"]:
                if bundle["schema"] != "dest_map":
                    continue
                manifest = json.loads((MINI_SCRIPTS / bundle["manifest_rel"]).read_text(encoding="utf-8"))
                for item in manifest.get("files", []):
                    dest = str(item.get("dest_abs", ""))
                    if dest.startswith("~/.hermes/scripts/"):
                        governed.add("scripts/" + dest[len("~/.hermes/scripts/") :])
                    elif dest.startswith("~/Library/LaunchAgents/"):
                        governed.add("launch_agents/" + dest[len("~/Library/LaunchAgents/") :])
            assert key in governed, f"pin {key} attaches to nothing the registry governs"


def test_mini_local_entries_have_paths_categories_and_reasons():
    registry = _registry()
    seen: set[str] = set()
    for entry in registry["mini_local"]:
        assert entry["path"].startswith(VALID_DEST_PREFIXES), entry
        assert entry.get("category"), f"mini_local {entry['path']} has no category"
        assert entry.get("reason"), f"mini_local {entry['path']} has no reason"
        assert entry["path"] not in seen, f"duplicate mini_local path: {entry['path']}"
        seen.add(entry["path"])
        if "*" in entry["path"]:
            assert entry.get("glob") is True, f"wildcard path must set glob: {entry['path']}"


def test_deployment_classes_are_mutually_exclusive():
    registry = _registry()
    probe = _probe_module()
    classification, findings = probe._coverage_bundle_classification(
        registry, mirror_root=MINI_SCRIPTS, identifier="deployment-coverage"
    )
    assert findings == []

    direct = {entry["dest"] for entry in registry["direct_deploy"]}
    local = registry["mini_local"]
    governed = set(classification["exact"])
    governed_direct = sorted(
        key for key in direct if probe._coverage_key_is_bundle_governed(key, classification)
    )
    assert not governed_direct, f"governed/direct overlap: {governed_direct}"

    # Exercise every dynamic PR-pipeline rule as well as exact bundle paths.
    # Literal pattern strings are valid witnesses for fnmatch, and package
    # sentinels cover the prefix-based classification used by the live probe.
    dynamic_witnesses = {
        *(f"scripts/{pattern}" for pattern in classification["pr_root_patterns"]),
        *(f"{prefix}__coverage_witness__.py" for prefix in classification["pr_package_prefixes"]),
    }
    assert all(
        probe._coverage_key_is_bundle_governed(key, classification) for key in dynamic_witnesses
    )
    for key in sorted(governed | direct | dynamic_witnesses):
        overlaps = [entry["path"] for entry in local if fnmatch.fnmatch(key, entry["path"])]
        assert not overlaps, f"{key} overlaps mini_local classification(s): {overlaps}"

    local_governed = sorted(
        entry["path"]
        for entry in local
        if probe._coverage_key_is_bundle_governed(entry["path"], classification)
    )
    assert not local_governed, f"governed/mini_local overlap: {local_governed}"


def test_fleet_outcome_manifest_ships_registry_with_current_sha():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = [item for item in manifest["files"] if item["source"] == "mini_local_registry.json"]
    assert len(entries) == 1, "fleet_outcome_manifest must ship the coverage registry exactly once"
    entry = entries[0]
    assert entry["destination_root"] == "scripts"
    assert entry["destination"] == "mini_local_registry.json"
    actual = hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest()
    assert entry["sha256"] == actual, (
        "fleet_outcome_manifest sha256 for mini_local_registry.json is stale; "
        "regenerate it or the release cut silently rolls back (checksum trap)"
    )


def test_probe_and_contracts_shas_in_manifest_are_current():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for name in ("fleet_outcome_probe.py", "fleet_outcome_contracts.json"):
        entry = next(item for item in manifest["files"] if item["source"] == name)
        actual = hashlib.sha256((MINI_SCRIPTS / name).read_bytes()).hexdigest()
        assert entry["sha256"] == actual, f"fleet_outcome_manifest sha256 stale for {name}"


def test_contracts_wire_the_deployment_coverage_check():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    checks = [c for c in contracts["operational_checks"] if c.get("kind") == "deployment_coverage"]
    assert len(checks) == 1, "exactly one deployment_coverage operational check must be wired"
    assert checks[0]["path"] == "~/.hermes/scripts/mini_local_registry.json"


def test_session_db_fleet_alarm_matches_the_retention_profile_budget():
    """The outcome plane must not alarm on bytes retention cannot prune yet."""
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    retention = json.loads(RETENTION_POLICY_PATH.read_text(encoding="utf-8"))
    checks = [c for c in contracts["operational_checks"] if c.get("id") == "session-db-health"]

    assert len(checks) == 1
    assert checks[0]["path"] == "~/.hermes/state.db"
    assert checks[0]["kind"] == "session_db_health"
    assert checks[0]["max_size_bytes"] == retention["profile"]["max_bytes"]
    assert checks[0]["profile_max_size_bytes"] == retention["profile"]["max_bytes"]
    assert checks[0]["root_max_size_bytes"] == retention["root"]["max_bytes"]


def test_live_mini_inventory_is_complete_when_available():
    """Prove the production declaration against the live Mini inventory.

    Other hosts do not own this machine-specific inventory and skip. On the
    Mini, every regular file must classify cleanly and every symlink must be
    surfaced explicitly rather than disappearing from coverage.
    """
    home = Path.home()
    scripts = home / ".hermes" / "scripts"
    agents = home / "Library" / "LaunchAgents"
    runtime = home / ".hermes" / "runtime-current"
    if home != Path("/Users/colingreig") or not all(path.is_dir() for path in (scripts, agents, runtime)):
        raise SkipTest("live Hermes Mini inventory is unavailable on this host")

    module = _probe_module()
    findings, evidence = module._check_deployment_coverage(
        {
            "id": "deployment-coverage",
            "path": str(REGISTRY_PATH),
            "release_root": str(REPO_ROOT),
            "scripts_dir": str(scripts),
            "launch_agents_dir": str(agents),
        },
        home=home,
    )

    symlinks = {
        f"{prefix}/{path.relative_to(base).as_posix()}"
        for base, prefix in ((scripts, "scripts"), (agents, "launch_agents"))
        for path in base.rglob("*")
        if path.is_symlink()
    }
    assert symlinks, "live proof must exercise fail-closed symlink inventory"
    codes = {finding["code"] for finding in findings}
    assert "undeclared_file" not in codes, findings
    assert not codes.intersection(
        {"coverage_registry_missing", "coverage_registry_invalid", "coverage_root_missing"}
    ), findings
    symlink_finding = next(finding for finding in findings if finding["code"] == "symlink_entry")
    assert all(key in symlink_finding["detail"] for key in symlinks)
    assert evidence[0]["symlink_entries"] == len(symlinks)
