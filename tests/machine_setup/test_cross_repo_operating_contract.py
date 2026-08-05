"""Contracts for the hermes-agent ↔ ignite-email-infra operating boundary."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_SETUP = REPO_ROOT / "machine-setup"
CONTRACT_PATH = MACHINE_SETUP / "cross-repo-operating-contract.md"
MANIFEST_PATH = MACHINE_SETUP / "ignite-email-infra.resource-manifest.json"
MANIFEST_SCHEMA_PATH = MACHINE_SETUP / "ignite-email-infra.resource-manifest.schema.json"
REGISTRY_PATH = MACHINE_SETUP / "production_mutation_registry.json"
INSTALLER_PATH = MACHINE_SETUP / "fleet-config" / "install_fleet_config.py"
MINI_RELEASE_PATH = REPO_ROOT / "scripts" / "mini-release-cut.sh"

_guard_spec = importlib.util.spec_from_file_location(
    "cross_repo_poller_guard_under_test",
    MACHINE_SETUP / "cross_repo_poller_guard.py",
)
guard_mod = importlib.util.module_from_spec(_guard_spec)
sys.modules[_guard_spec.name] = guard_mod
_guard_spec.loader.exec_module(guard_mod)


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_contract_doc_names_schedule_authority_and_one_owner_rules():
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    assert "one owner per shared resource" in text.casefold()
    assert "6e25865a22a4" in text
    assert "*/15 * * * *" in text
    assert "6139465f559f" in text
    assert "deploy-poller.sh" in text
    assert "Conductor workspace" in text
    assert "cross_repo_poller_guard.py" in text


def test_resource_manifest_matches_schema_and_registry_resource():
    manifest = _manifest()
    schema = json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["manifest_kind"]["const"] == "hermes-cross-repo-resource-manifest"
    assert manifest["registry_resource_id"] == "purelymail-poller-deploy"
    assert manifest["partner_repository"] == "ignite-email-infra"
    assert manifest["cron_schedule_authority"]["hermes_cron_job_id"] == "6e25865a22a4"
    assert manifest["cron_schedule_authority"]["live_schedule_expr"] == "*/15 * * * *"

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    resource_ids = {row["id"] for row in registry["resources"]}
    assert manifest["registry_resource_id"] in resource_ids
    path_ids = {entry["path"] for entry in manifest["paths"]}
    assert "~/.hermes/deploy/purelymail-poller/" in path_ids
    assert "~/.hermes/state/purelymail-poller/.lock" in path_ids


def test_guard_rejects_poller_deploy_path_without_lease_resource(tmp_path):
    dest = tmp_path / ".hermes" / "deploy" / "purelymail-poller" / "incoming" / "rel"
    dest.parent.mkdir(parents=True)
    with pytest.raises(guard_mod.CrossRepoPollerGuardError, match="purelymail-poller-deploy"):
        guard_mod.assert_destination_allowed(
            dest,
            lease_resources=("fleet-config", "cron-jobs"),
            home=tmp_path,
        )


def test_guard_allows_poller_deploy_path_with_matching_lease_resource(tmp_path):
    dest = tmp_path / ".hermes" / "deploy" / "purelymail-poller" / "incoming" / "rel"
    dest.parent.mkdir(parents=True)
    guard_mod.assert_destination_allowed(
        dest,
        lease_resources=("purelymail-poller-deploy",),
        home=tmp_path,
    )


def test_guard_allows_unrelated_hermes_paths(tmp_path):
    dest = tmp_path / ".hermes" / "cron" / "jobs.json"
    dest.parent.mkdir(parents=True)
    guard_mod.assert_destination_allowed(dest, lease_resources=("cron-jobs",), home=tmp_path)


def test_hermes_governed_entry_points_do_not_reference_poller_staging_paths():
    sources = [
        ("install_fleet_config.py", INSTALLER_PATH.read_text(encoding="utf-8")),
        ("mini-release-cut.sh", MINI_RELEASE_PATH.read_text(encoding="utf-8")),
    ]
    violations = guard_mod.scan_sources_for_forbidden_poller_writes(sources)
    assert violations == []


def test_production_write_lease_skill_points_at_cross_repo_contract():
    skill = (REPO_ROOT / "skills" / "production-write-lease" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "cross-repo-operating-contract.md" in skill
    assert "ignite-email-infra" in skill
