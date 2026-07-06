"""Tests for scripts/clickup_workspace_refresh.py."""

from __future__ import annotations

import json
from pathlib import Path

import scripts.clickup_workspace_refresh as refresh_mod


def test_build_workspace_map_captures_schema_and_aliases(monkeypatch):
    responses = {
        "/team/team-1/space?archived=false": {
            "spaces": [{"id": "space-1", "name": "Growth", "archived": False, "private": False}],
        },
        "/space/space-1/folder?archived=false": {
            "folders": [{"id": "folder-1", "name": "Advantive.com SEO", "archived": False, "hidden": False}],
        },
        "/space/space-1/list?archived=false": {
            "lists": [{"id": "list-2", "name": "Advantive.com Reporting", "archived": False}],
        },
        "/folder/folder-1/list?archived=false": {
            "lists": [{"id": "list-1", "name": "Advantive.com Content", "archived": False}],
        },
        "/list/list-1": {
            "id": "list-1",
            "name": "Advantive.com Content",
            "archived": False,
            "permission_level": "create",
            "statuses": [
                {"id": "st-1", "status": "to do", "type": "open", "orderindex": 1, "color": "#fff"},
            ],
        },
        "/list/list-1/field": {
            "fields": [
                {
                    "id": "field-1",
                    "name": "Service Line",
                    "type": "drop_down",
                    "required": True,
                    "type_config": {"options": [{"id": "opt-1", "name": "Content", "color": "blue"}]},
                }
            ]
        },
        "/list/list-2": {
            "id": "list-2",
            "name": "Advantive.com Reporting",
            "archived": False,
            "permission_level": "edit",
            "statuses": [
                {"id": "st-2", "status": "in progress", "type": "custom", "orderindex": 2, "color": "#000"},
            ],
        },
        "/list/list-2/field": {"fields": []},
    }

    monkeypatch.setattr(refresh_mod, "_now_utc", lambda: refresh_mod.dt.datetime.fromtimestamp(123, tz=refresh_mod.dt.timezone.utc))
    monkeypatch.setattr(refresh_mod, "_LIST_DETAIL_CACHE", {})
    monkeypatch.setattr(refresh_mod, "_LIST_FIELDS_CACHE", {})
    monkeypatch.setattr(refresh_mod, "_get", lambda path: responses[path])
    monkeypatch.setattr(
        refresh_mod,
        "fetch_recent_task_tags",
        lambda team_id, lookback_days=refresh_mod.TASK_LOOKBACK_DAYS: (
            refresh_mod.Counter({"agent-ready": 1, "seo-agent": 1}),
            1,
        ),
    )

    workspace_map = refresh_mod.build_workspace_map("team-1")
    list_by_id = {item["id"]: item for item in workspace_map["lists"]}

    assert workspace_map["schema_version"] == refresh_mod.SCHEMA_VERSION
    assert workspace_map["generated_at"] == 123000
    assert workspace_map["team_id"] == "team-1"
    assert workspace_map["folders"][0]["fetch_ok"] is True
    assert list_by_id["list-1"]["statuses"][0]["type"] == "open"
    assert list_by_id["list-1"]["custom_fields"][0]["metadata"]["options"][0]["name"] == "Content"
    assert workspace_map["task_tags"]["tags"][0] == {"name": "agent-ready", "count": 1}
    assert "advantive-com" in workspace_map["clients_aliases"]


def test_cache_is_fresh_respects_schema_and_ttl():
    fresh = {"schema_version": refresh_mod.SCHEMA_VERSION, "generated_at": 10_000}
    stale = {"schema_version": refresh_mod.SCHEMA_VERSION, "generated_at": 1}
    wrong_schema = {"schema_version": 999, "generated_at": 10_000}

    assert refresh_mod.cache_is_fresh(fresh, now_ms=10_000, max_age_seconds=1) is True
    assert refresh_mod.cache_is_fresh(stale, now_ms=5_000, max_age_seconds=1) is False
    assert refresh_mod.cache_is_fresh(wrong_schema, now_ms=10_000, max_age_seconds=60) is False


def test_ensure_workspace_map_uses_fresh_cache_without_refresh(tmp_path, monkeypatch):
    json_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    cached = {
        "schema_version": refresh_mod.SCHEMA_VERSION,
        "generated_at": 10_000,
        "team_id": "team-1",
        "spaces": [],
        "folders": [],
        "lists": [],
        "task_tags": {"sampled_task_count": 0, "lookback_days": 7, "tags": []},
        "clients_aliases": {},
    }
    json_path.write_text(json.dumps(cached), encoding="utf-8")

    def _unexpected(_: str) -> None:
        raise AssertionError("build_workspace_map should not be called for a fresh cache")

    monkeypatch.setattr(refresh_mod, "build_workspace_map", _unexpected)
    monkeypatch.setattr(refresh_mod, "_now_ms", lambda: 10_000)

    result = refresh_mod.ensure_workspace_map(
        team_id="team-1",
        force=False,
        max_age_seconds=60,
        output_path=json_path,
        markdown_path=markdown_path,
    )

    assert result == cached
    assert not markdown_path.exists()


def test_ensure_workspace_map_force_refresh_writes_json_and_markdown(tmp_path, monkeypatch):
    json_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    workspace_map = {
        "schema_version": refresh_mod.SCHEMA_VERSION,
        "generated_at": 123000,
        "generated_at_iso": "1970-01-01T00:02:03+00:00",
        "team_id": "team-1",
        "refresh_cadence_hours": 6,
        "spaces": [],
        "folders": [],
        "lists": [],
        "task_tags": {"sampled_task_count": 0, "lookback_days": 7, "tags": []},
        "clients_aliases": {},
    }
    monkeypatch.setattr(refresh_mod, "build_workspace_map", lambda team_id: workspace_map)
    monkeypatch.setattr(refresh_mod, "_write_brain_note", lambda body: None)

    result = refresh_mod.ensure_workspace_map(
        team_id="team-1",
        force=True,
        output_path=json_path,
        markdown_path=markdown_path,
        write_brain_note=True,
    )

    assert result == workspace_map
    assert json.loads(json_path.read_text(encoding="utf-8"))["team_id"] == "team-1"
    assert "# ClickUp workspace map" in markdown_path.read_text(encoding="utf-8")


def test_detect_markdown_drift_ignores_generated_noise():
    prior = """# ClickUp workspace map (auto-generated 2026-07-06T00:00:00+00:00)\n\n_Sampled 10 task(s) updated in the last 7 days._\n\nreal line\n"""
    current = """# ClickUp workspace map (auto-generated 2026-07-06T06:00:00+00:00)\n\n_Sampled 12 task(s) updated in the last 7 days._\n\nreal line\n"""

    assert refresh_mod.detect_markdown_drift(prior, current) == []
