"""End-to-end storage and tool API coverage for cron skill_scope."""
from __future__ import annotations

import json

import pytest

from cron.jobs import VALID_SKILL_SCOPES


def test_cron_scope_contract_matches_prompt_builder_roles():
    from agent.prompt_builder import _SKILL_ROLE_GROUPS

    assert set(VALID_SKILL_SCOPES) == set(_SKILL_ROLE_GROUPS)


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    cron_dir = hermes_home / "cron"
    (cron_dir / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs

    monkeypatch.setattr(jobs, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    return hermes_home


@pytest.mark.parametrize("scope", VALID_SKILL_SCOPES)
def test_create_and_update_store_every_supported_scope(cron_env, scope):
    from cron.jobs import create_job, get_job, update_job

    job = create_job(
        prompt="run scoped",
        schedule="every 1h",
        skill_scope=scope,
    )
    assert job["skill_scope"] == scope
    assert get_job(job["id"])["skill_scope"] == scope

    replacement = next(candidate for candidate in VALID_SKILL_SCOPES if candidate != scope)
    updated = update_job(job["id"], {"skill_scope": replacement})
    assert updated["skill_scope"] == replacement


def test_omitted_scope_is_backward_compatible_and_empty_update_clears(cron_env):
    from cron.jobs import create_job, update_job

    job = create_job(prompt="legacy", schedule="every 1h")
    assert "skill_scope" not in job

    update_job(job["id"], {"skill_scope": "validator"})
    cleared = update_job(job["id"], {"skill_scope": ""})
    assert cleared["skill_scope"] is None


@pytest.mark.parametrize("invalid", ["dev", "unknown", 123, ["validator"]])
def test_core_create_and_update_reject_invalid_scope(cron_env, invalid):
    from cron.jobs import create_job, update_job

    with pytest.raises(ValueError, match="skill_scope"):
        create_job(prompt="bad", schedule="every 1h", skill_scope=invalid)

    job = create_job(prompt="good", schedule="every 1h")
    with pytest.raises(ValueError, match="skill_scope"):
        update_job(job["id"], {"skill_scope": invalid})


def test_cronjob_tool_create_update_list_and_schema(cron_env):
    from tools.cronjob_tools import CRONJOB_SCHEMA, cronjob

    created = json.loads(
        cronjob(
            action="create",
            prompt="tool scoped",
            schedule="every 1h",
            skill_scope="content-executor",
        )
    )
    assert created["success"] is True
    job_id = created["job_id"]

    updated = json.loads(
        cronjob(
            action="update",
            job_id=job_id,
            skill_scope="seo-ppc-executor",
        )
    )
    assert updated["job"]["skill_scope"] == "seo-ppc-executor"
    listed = json.loads(cronjob(action="list"))
    row = next(job for job in listed["jobs"] if job["job_id"] == job_id)
    assert row["skill_scope"] == "seo-ppc-executor"

    schema = CRONJOB_SCHEMA["parameters"]["properties"]["skill_scope"]
    assert set(VALID_SKILL_SCOPES).issubset(schema["enum"])


def test_cronjob_tool_rejects_invalid_scope(cron_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(
            action="create",
            prompt="bad scope",
            schedule="every 1h",
            skill_scope="typo-executor",
        )
    )
    assert result["success"] is False
    assert "skill_scope" in result["error"]
