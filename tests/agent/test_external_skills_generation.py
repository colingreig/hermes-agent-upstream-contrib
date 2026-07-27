from __future__ import annotations

import json
import pytest


@pytest.fixture(autouse=True)
def _clear_generation_caches():
    from agent import prompt_builder as pb
    from tools import skills_tool

    pb._SKILLS_PROMPT_CACHE.clear()
    skills_tool._SKILLS_CACHE.clear()
    yield
    pb._SKILLS_PROMPT_CACHE.clear()
    skills_tool._SKILLS_CACHE.clear()


def _write_generation(home, generation: str, source: str = "fixture"):
    marker = home / "state" / "skill-pulls" / "catalog-generation.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generation": generation,
                "source": source,
                "published_at": "2026-07-27T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    return marker


def test_generation_change_clears_only_future_build_caches(monkeypatch, tmp_path):
    from agent import prompt_builder as pb
    from tools import skills_tool

    home = tmp_path / ".hermes"
    monkeypatch.setattr(pb, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pb, "_SKILLS_CATALOG_GENERATION", None)
    pb._SKILLS_PROMPT_CACHE.clear()
    skills_tool._SKILLS_CACHE.clear()

    _write_generation(home, "a" * 64)
    pb._SKILLS_PROMPT_CACHE[("existing",)] = "existing prompt bytes"
    skills_tool._SKILLS_CACHE["filtered"] = ("signature", 0.0, [])
    existing_agent_prompt = pb._SKILLS_PROMPT_CACHE[("existing",)]

    # Establishing the process baseline does not invalidate anything.
    assert pb.observe_external_skills_generation() is False
    assert pb._SKILLS_PROMPT_CACHE[("existing",)] == existing_agent_prompt

    _write_generation(home, "b" * 64)
    assert pb.observe_external_skills_generation() is True

    # Existing agents own their already-built string; only future lookup
    # caches are invalidated.
    assert existing_agent_prompt == "existing prompt bytes"
    assert pb._SKILLS_PROMPT_CACHE == {}
    assert skills_tool._SKILLS_CACHE == {}
    ack = json.loads(
        (
            home
            / "state"
            / "skill-pulls"
            / "catalog-observed"
            / f"{pb.os.getpid()}.json"
        ).read_text()
    )
    assert ack["generation"] == "b" * 64
    assert ack["pid"] == pb.os.getpid()


def test_invalid_generation_fails_closed_and_clears_future_cache(
    monkeypatch, tmp_path
):
    from agent import prompt_builder as pb

    home = tmp_path / ".hermes"
    monkeypatch.setattr(pb, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pb, "_SKILLS_CATALOG_GENERATION", "a" * 64)
    pb._SKILLS_PROMPT_CACHE.clear()
    pb._SKILLS_PROMPT_CACHE[("keep",)] = "stable"
    _write_generation(home, "not-a-valid-generation")

    assert pb.observe_external_skills_generation() is True
    assert pb._SKILLS_PROMPT_CACHE == {}


def test_prompt_build_retries_when_generation_changes_before_cache_store(
    monkeypatch, tmp_path
):
    """A pull racing a cold scan cannot publish old bytes into the new cache."""
    from agent import prompt_builder as pb

    home = tmp_path / ".hermes"
    local = home / "skills"
    external = tmp_path / "external-skills"
    local.mkdir(parents=True)
    skill_file = external / "writing" / "release-notes" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: release-notes\ndescription: stale description\n---\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(pb, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pb, "get_skills_dir", lambda: local)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [local, external])
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda _platform=None: set())

    generations = []

    def generation_during_pull():
        generations.append(None)
        if len(generations) == 1:
            return "a" * 64
        if len(generations) == 2:
            # Deterministically publish the pulled content at the exact
            # before-store generation check.
            skill_file.write_text(
                "---\nname: release-notes\ndescription: fresh description\n---\n",
                encoding="utf-8",
            )
        return "b" * 64

    monkeypatch.setattr(
        pb, "_read_external_skills_generation", generation_during_pull
    )

    result = pb.build_skills_system_prompt()

    assert "fresh description" in result
    assert "stale description" not in result
    assert len(generations) == 4
    assert pb._SKILLS_PROMPT_CACHE
    assert all(cache_key[2] == "b" * 64 for cache_key in pb._SKILLS_PROMPT_CACHE)


def test_updating_catalog_times_out_without_scanning_or_returning_cache(
    monkeypatch, tmp_path
):
    from agent import prompt_builder as pb

    home = tmp_path / ".hermes"
    local = home / "skills"
    external = tmp_path / "external-skills"
    local.mkdir(parents=True)
    external.mkdir()
    marker = _write_generation(home, "a" * 64)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "updating",
                "source": "fixture",
                "operation_id": "operation-a",
                "updating_at": "2026-07-27T00:00:00Z",
                "previous_generation": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pb, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pb, "get_skills_dir", lambda: local)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [local, external])
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda _platform=None: set())
    monkeypatch.setattr(pb, "_SKILLS_CATALOG_UPDATE_WAIT_SECONDS", 0.0)
    pb._SKILLS_PROMPT_CACHE[("old",)] = "stale cached bytes"
    monkeypatch.setattr(
        pb,
        "iter_skill_index_files",
        lambda *_args, **_kwargs: pytest.fail("scanner ran during catalog update"),
    )

    assert pb.build_skills_system_prompt() == ""
    assert pb._SKILLS_PROMPT_CACHE == {("old",): "stale cached bytes"}


def test_prompt_waits_through_mutation_interval_then_scans_stable_bytes(
    monkeypatch, tmp_path
):
    from agent import prompt_builder as pb

    home = tmp_path / ".hermes"
    local = home / "skills"
    external = tmp_path / "external-skills"
    local.mkdir(parents=True)
    skill_file = external / "writing" / "release-notes" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: release-notes\ndescription: mutating bytes\n---\n",
        encoding="utf-8",
    )
    marker = _write_generation(home, "a" * 64)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "updating",
                "source": "fixture",
                "operation_id": "operation-b",
                "updating_at": "2026-07-27T00:00:00Z",
                "previous_generation": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(pb, "get_hermes_home", lambda: home)
    monkeypatch.setattr(pb, "get_skills_dir", lambda: local)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [local, external])
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda _platform=None: set())
    monkeypatch.setattr(pb, "_resolve_skill_dir_scope", lambda _role: None)
    monkeypatch.setattr(pb, "_SKILLS_CATALOG_UPDATE_WAIT_SECONDS", 1.0)

    original_parse = pb._parse_skill_file
    parse_states = []

    def parse_only_when_stable(path):
        parse_states.append(json.loads(marker.read_text()).get("state", "stable"))
        return original_parse(path)

    monkeypatch.setattr(pb, "_parse_skill_file", parse_only_when_stable)
    generation_reads = []

    def wait_through_pull():
        generation_reads.append(None)
        if len(generation_reads) == 1:
            assert json.loads(marker.read_text())["state"] == "updating"
            skill_file.write_text(
                "---\nname: release-notes\ndescription: stable fresh bytes\n---\n",
                encoding="utf-8",
            )
            _write_generation(home, "b" * 64)
        return "b" * 64

    monkeypatch.setattr(
        pb, "_read_external_skills_generation", wait_through_pull
    )

    result = pb.build_skills_system_prompt()

    assert "stable fresh bytes" in result
    assert "mutating bytes" not in result
    assert parse_states == ["stable"]
