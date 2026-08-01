"""Contracts for the checked-in Hermes Mini production writer inventory."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE_SETUP = REPO_ROOT / "machine-setup"
REGISTRY_PATH = MACHINE_SETUP / "production_mutation_registry.json"
SCHEMA_PATH = MACHINE_SETUP / "production_mutation_registry.schema.json"
RENDERER_PATH = MACHINE_SETUP / "render_production_writers.py"
INDEX_PATH = MACHINE_SETUP / "fleet-config" / "PRODUCTION_WRITERS.md"
JOBS_PATH = MACHINE_SETUP / "fleet-config" / "jobs.json"


def _registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def _actors_by_id(registry: dict) -> dict[str, dict]:
    return {actor["id"]: actor for actor in registry["actors"]}


def _all_entry_points(registry: dict) -> set[str]:
    return {point for actor in registry["actors"] for point in actor["entry_points"]}


def _all_cron_job_ids(registry: dict) -> set[str]:
    return {job_id for actor in registry["actors"] for job_id in actor.get("cron_job_ids", [])}


def _load_renderer():
    spec = importlib.util.spec_from_file_location("production_writers_renderer", RENDERER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_registry_validates_and_schema_declares_the_same_core_contract():
    registry = _registry()
    _load_renderer().validate_registry(registry)

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["$schema"] == {
        "const": "./production_mutation_registry.schema.json"
    }
    assert set(schema["required"]) == {
        "schema_version",
        "registry_kind",
        "unknown_path_policy",
        "resources",
        "actors",
    }
    actor_required = set(schema["$defs"]["actor"]["required"])
    assert {
        "actor_class",
        "owning_repository",
        "mutability",
        "resources_touched",
        "current_lock_or_transaction_boundary",
        "rollback_surface",
    } <= actor_required
    assert schema["$defs"]["unknownPathPolicy"]["properties"]["default"] == {
        "const": "fail-closed"
    }


def test_every_governed_installer_and_deploy_entry_point_is_registered():
    registry = _registry()
    entry_points = _all_entry_points(registry)
    expected_live_entry_points = {
        "machine-setup/fleet-config/install_fleet_config.py",
        "scripts/mini-release-cut.sh",
        "machine-setup/mini-scripts/ignite-skills-pull.sh",
    }
    assert expected_live_entry_points <= entry_points
    for entry_point in expected_live_entry_points:
        assert (REPO_ROOT / entry_point).is_file(), entry_point

    # The execution brief's former name is retained as an explicit alias, but
    # coverage is bound to the script that is currently deployed by launchd.
    sync_actor = _actors_by_id(registry)["ignite-skills-sync"]
    assert sync_actor["legacy_entry_point_names"] == ["sync_ignite_skills_to_hermes.sh"]
    assert sync_actor["entry_points"] == ["machine-setup/mini-scripts/ignite-skills-pull.sh"]


def test_all_six_llm_cron_jobs_are_covered_and_executors_share_one_lease_identity():
    registry = _registry()
    llm_jobs = {
        job["id"]: job["name"]
        for job in json.loads(JOBS_PATH.read_text(encoding="utf-8"))["jobs"]
        if job["no_agent"] is False
    }
    assert len(llm_jobs) == 6
    assert set(llm_jobs) <= _all_cron_job_ids(registry)
    assert set(llm_jobs.values()) == {
        "clickup-executor",
        "content-lane-executor",
        "hermes-pr-validate",
        "clickup-lifecycle",
        "fleet-health-digest",
        "repo-maintenance",
    }

    executor_ids = {
        job_id
        for job_id, name in llm_jobs.items()
        if name in {"clickup-executor", "content-lane-executor"}
    }
    matching_actors = [
        actor
        for actor in registry["actors"]
        if executor_ids.intersection(actor.get("cron_job_ids", []))
    ]
    assert len(matching_actors) == 1
    assert set(matching_actors[0]["cron_job_ids"]) == executor_ids
    assert matching_actors[0]["logical_dispatch_surface"] == "production-clickup-executor"
    assert matching_actors[0]["current_lock_or_transaction_boundary"]["path_or_identity"].endswith(
        "production-clickup-executor"
    )


def test_every_current_fleet_job_is_registered_including_no_agent_writers():
    registry = _registry()
    jobs = json.loads(JOBS_PATH.read_text(encoding="utf-8"))["jobs"]
    assert len(jobs) == 15  # Filesystem-authoritative count: six LLM plus nine no-agent jobs.
    assert {job["id"] for job in jobs} == _all_cron_job_ids(registry)

    no_agent_ids = {job["id"] for job in jobs if job["no_agent"] is True}
    registered_no_agent_ids = {
        job_id
        for actor in registry["actors"]
        for job_id in actor.get("cron_job_ids", [])
        if actor["id"]
        not in {
            "production-clickup-executor",
            "hermes-pr-validator",
            "clickup-lifecycle-cron",
            "fleet-health-digest-cron",
            "repo-maintenance-cron",
        }
    }
    assert no_agent_ids == registered_no_agent_ids


def test_each_actor_has_a_complete_resource_and_recovery_model():
    registry = _registry()
    resource_ids = {resource["id"] for resource in registry["resources"]}
    required = {
        "actor_class",
        "owning_repository",
        "mutability",
        "resources_touched",
        "current_lock_or_transaction_boundary",
        "rollback_surface",
    }
    for actor in registry["actors"]:
        assert required <= actor.keys(), actor["id"]
        assert set(actor["resources_touched"]) <= resource_ids
        assert actor["current_lock_or_transaction_boundary"].keys() == {
            "kind",
            "path_or_identity",
            "behavior",
        }
        assert actor["rollback_surface"].keys() == {"surface", "procedure"}

    actors = _actors_by_id(registry)
    assert actors["ignite-email-infra-poller-deploy"]["owning_repository"] == "ignite-email-infra"
    assert "~/.hermes/deploy/purelymail-poller/.lock" in actors[
        "ignite-email-infra-poller-deploy"
    ]["current_lock_or_transaction_boundary"]["path_or_identity"]
    assert actors["dashboard-session-writer"]["actor_class"] == "dashboard"
    assert actors["gateway-kanban-dispatcher"]["actor_class"] == "gateway"


def test_unknown_path_guard_and_generated_human_index_are_current():
    registry = _registry()
    policy = registry["unknown_path_policy"]
    assert policy["default"] == "fail-closed"
    assert {hook["task"] for hook in policy["follow_on_hook_points"]} == {
        "86e2kmucr",
        "86e2kmuct",
    }

    result = subprocess.run(
        [sys.executable, str(RENDERER_PATH), "--check"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    generated = INDEX_PATH.read_text(encoding="utf-8")
    assert "Generated by machine-setup/render_production_writers.py" in generated
    assert "DO NOT EDIT" in generated
    assert "production_mutation_registry.json" in generated
