"""Tests for the governed Mini clickup_workspace_refresh.py source."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "machine-setup"
    / "mini-scripts"
    / "clickup_workspace_refresh.py"
)
_SPEC = importlib.util.spec_from_file_location("clickup_workspace_refresh", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
refresh_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = refresh_mod
_SPEC.loader.exec_module(refresh_mod)


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
    assert workspace_map["assignment_patterns"] == []
    assert workspace_map["write_boundaries"][0]["permission_level"] == "edit"
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
        write_brain_note=False,
    )

    assert result == cached
    assert not markdown_path.exists()
    receipt = json.loads(refresh_mod._receipt_path_for(json_path).read_text(encoding="utf-8"))
    assert receipt["outcome"] == "success"
    assert receipt["cache_used"] is True
    assert receipt["request_count"] == 0


def test_ensure_workspace_map_fresh_cache_still_updates_brain_note(tmp_path, monkeypatch):
    json_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    brain_path = tmp_path / "brain" / "clickup-workspace-map.md"
    cached = {
        "schema_version": refresh_mod.SCHEMA_VERSION,
        "generated_at": 10_000,
        "generated_at_iso": "1970-01-01T00:00:10+00:00",
        "team_id": "team-1",
        "refresh_cadence_hours": 6,
        "refresh_cadence_cron_expr": "0 */6 * * *",
        "refresh_cadence_source": "cron_registration",
        "spaces": [],
        "folders": [],
        "lists": [],
        "task_tags": {"sampled_task_count": 0, "lookback_days": 7, "tags": []},
        "clients_aliases": {},
    }
    json_path.write_text(json.dumps(cached), encoding="utf-8")
    monkeypatch.setattr(refresh_mod, "_now_ms", lambda: 10_000)
    monkeypatch.setattr(
        refresh_mod,
        "build_workspace_map",
        lambda _: pytest.fail("fresh cache must not call ClickUp"),
    )

    result = refresh_mod.ensure_workspace_map(
        team_id="team-1",
        force=False,
        max_age_seconds=60,
        output_path=json_path,
        markdown_path=markdown_path,
        write_brain_note=True,
        brain_note_path=brain_path,
    )

    assert result == cached
    assert "# ClickUp workspace map" in brain_path.read_text(encoding="utf-8")
    assert "every 6 hours" in brain_path.read_text(encoding="utf-8")
    assert not markdown_path.exists()


def test_ensure_workspace_map_force_refresh_writes_json_and_markdown(tmp_path, monkeypatch):
    json_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    brain_path = tmp_path / "brain" / "clickup-workspace-map.md"
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

    result = refresh_mod.ensure_workspace_map(
        team_id="team-1",
        force=True,
        output_path=json_path,
        markdown_path=markdown_path,
        write_brain_note=True,
        brain_note_path=brain_path,
    )

    assert result == workspace_map
    assert json.loads(json_path.read_text(encoding="utf-8"))["team_id"] == "team-1"
    assert "# ClickUp workspace map" in markdown_path.read_text(encoding="utf-8")
    brain_body = brain_path.read_text(encoding="utf-8")
    assert "# ClickUp workspace map" in brain_body
    assert "**Team ID:** `team-1`" in brain_body


def test_brain_note_write_does_not_require_basic_memory(tmp_path, monkeypatch):
    brain_path = tmp_path / "brain" / "clickup-workspace-map.md"
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

    refresh_mod.write_workspace_map_note("canonical body\n", brain_path)

    assert brain_path.read_text(encoding="utf-8") == "canonical body\n"


def test_brain_note_overwrites_one_canonical_file_without_dated_proliferation(tmp_path):
    brain_dir = tmp_path / "brain"
    brain_path = brain_dir / "clickup-workspace-map.md"

    refresh_mod.write_workspace_map_note("first\n", brain_path)
    refresh_mod.write_workspace_map_note("second\n", brain_path)

    assert brain_path.read_text(encoding="utf-8") == "second\n"
    assert [path.name for path in brain_dir.iterdir()] == ["clickup-workspace-map.md"]


def test_read_workspace_map_note_reads_the_canonical_file(tmp_path):
    brain_path = tmp_path / "brain" / "clickup-workspace-map.md"
    refresh_mod.write_workspace_map_note("readable map\n", brain_path)

    assert refresh_mod.read_workspace_map_note(brain_path) == "readable map\n"


def test_main_read_note_exposes_the_canonical_read_path(tmp_path, monkeypatch, capsys):
    brain_path = tmp_path / "brain" / "clickup-workspace-map.md"
    refresh_mod.write_workspace_map_note("cli-readable map\n", brain_path)
    monkeypatch.setattr(
        refresh_mod.sys,
        "argv",
        [
            refresh_mod.THIS_SCRIPT_NAME,
            "--read-note",
            "--brain-output",
            str(brain_path),
        ],
    )

    assert refresh_mod.main() == 0
    assert capsys.readouterr().out == "cli-readable map\n"


def test_main_reports_canonical_note_write_failure(monkeypatch, capsys):
    def _fail_note_write(**_kwargs):
        raise refresh_mod.WorkspaceMapNoteError(
            "could not write canonical workspace-map note at /blocked/note.md: denied"
        )

    monkeypatch.setattr(refresh_mod, "ensure_workspace_map", _fail_note_write)
    monkeypatch.setattr(refresh_mod.sys, "argv", [refresh_mod.THIS_SCRIPT_NAME])

    assert refresh_mod.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "ERROR: could not write canonical workspace-map note at "
        "/blocked/note.md: denied"
    ) in captured.err


def test_brain_note_failure_propagates_from_cache_hit(tmp_path, monkeypatch):
    json_path = tmp_path / "clickup-map.json"
    blocked_parent = tmp_path / "brain"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    brain_path = blocked_parent / "clickup-workspace-map.md"
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
    monkeypatch.setattr(refresh_mod, "_now_ms", lambda: 10_000)

    with pytest.raises(
        refresh_mod.WorkspaceMapNoteError,
        match="could not write canonical workspace-map note",
    ):
        refresh_mod.ensure_workspace_map(
            force=False,
            max_age_seconds=60,
            output_path=json_path,
            markdown_path=tmp_path / "clickup-workspace-map.md",
            write_brain_note=True,
            brain_note_path=brain_path,
        )


def test_detect_markdown_drift_ignores_generated_noise():
    prior = """# ClickUp workspace map (auto-generated 2026-07-06T00:00:00+00:00)\n\n_Sampled 10 task(s) updated in the last 7 days._\n\nreal line\n"""
    current = """# ClickUp workspace map (auto-generated 2026-07-06T06:00:00+00:00)\n\n_Sampled 12 task(s) updated in the last 7 days._\n\nreal line\n"""

    assert refresh_mod.detect_markdown_drift(prior, current) == []


def test_cron_expr_to_hours_handles_hourly_and_minute_steps():
    assert refresh_mod._cron_expr_to_hours("0 */6 * * *") == 6.0
    assert refresh_mod._cron_expr_to_hours("*/30 * * * *") == 0.5
    assert refresh_mod._cron_expr_to_hours("0 2 * * *") is None
    assert refresh_mod._cron_expr_to_hours("not a cron expr") is None


def test_derive_refresh_cadence_reads_the_live_cron_registration(monkeypatch):
    """86e1vw79j: cadence must come from the registered job, never a literal."""
    import cron.jobs as cron_jobs_mod

    monkeypatch.setattr(
        cron_jobs_mod,
        "load_jobs",
        lambda: [
            {"script": "some_other_script.py", "schedule": {"expr": "0 2 * * *"}},
            {"script": refresh_mod.THIS_SCRIPT_NAME, "schedule": {"expr": "0 */3 * * *"}},
        ],
    )

    cadence = refresh_mod._derive_refresh_cadence()

    assert cadence == {
        "hours": 3.0,
        "cron_expr": "0 */3 * * *",
        "source": "cron_registration",
    }


def test_derive_refresh_cadence_falls_back_when_job_not_registered(monkeypatch):
    import cron.jobs as cron_jobs_mod

    monkeypatch.setattr(cron_jobs_mod, "load_jobs", lambda: [])

    cadence = refresh_mod._derive_refresh_cadence()

    assert cadence["source"] == "ttl_fallback"
    assert cadence["cron_expr"] is None
    assert cadence["hours"] == refresh_mod.REFRESH_TTL_SECONDS / 3600


def test_derive_refresh_cadence_survives_cron_subsystem_errors(monkeypatch):
    import cron.jobs as cron_jobs_mod

    def _boom():
        raise RuntimeError("jobs.json corrupted")

    monkeypatch.setattr(cron_jobs_mod, "load_jobs", _boom)

    cadence = refresh_mod._derive_refresh_cadence()

    assert cadence["source"] == "ttl_fallback"


def test_format_cadence_text_reflects_derived_cadence_not_a_literal():
    assert refresh_mod._format_cadence_text(
        {"refresh_cadence_hours": 3, "refresh_cadence_cron_expr": "0 */3 * * *", "refresh_cadence_source": "cron_registration"}
    ) == "every 3 hours (`0 */3 * * *`)"
    assert refresh_mod._format_cadence_text(
        {"refresh_cadence_hours": 0.5, "refresh_cadence_cron_expr": "*/30 * * * *", "refresh_cadence_source": "cron_registration"}
    ) == "every 30 minutes (`*/30 * * * *`)"
    fallback_text = refresh_mod._format_cadence_text(
        {"refresh_cadence_hours": 6.0, "refresh_cadence_cron_expr": None, "refresh_cadence_source": "ttl_fallback"}
    )
    assert "fallback default" in fallback_text


def test_render_markdown_mirror_reflects_a_changed_cadence_and_keeps_root_cause_note():
    """The literal this task exists to kill: cadence text must track a schedule
    change (not stay pinned at 'every 6 hours'), and the root-cause note must
    survive every regeneration, not just the day it was written."""
    workspace_map = {
        "schema_version": refresh_mod.SCHEMA_VERSION,
        "generated_at": 123000,
        "generated_at_iso": "1970-01-01T00:02:03+00:00",
        "team_id": "team-1",
        "refresh_cadence_hours": 3,
        "refresh_cadence_cron_expr": "0 */3 * * *",
        "refresh_cadence_source": "cron_registration",
        "root_cause_note_2026_07_09": refresh_mod.ROOT_CAUSE_NOTE_2026_07_09,
        "spaces": [],
        "folders": [],
        "lists": [],
        "task_tags": {"sampled_task_count": 0, "lookback_days": 7, "tags": []},
        "clients_aliases": {},
    }

    markdown = refresh_mod.render_markdown_mirror(workspace_map)

    assert "every 3 hours" in markdown
    assert "every 6 hours" not in markdown
    assert "2026-07-09" in markdown
    assert refresh_mod.ROOT_CAUSE_NOTE_2026_07_09 in markdown
    for section in (
        "Header",
        "Spaces",
        "Folders",
        "Lists",
        "Statuses per list",
        "Custom fields per list",
        "Tag taxonomy",
        "Assignment patterns",
        "Last-known write boundaries",
        "Refresh notes",
    ):
        assert f"## {section}" in markdown


def test_recent_task_sample_includes_assignment_patterns(monkeypatch):
    monkeypatch.setattr(refresh_mod, "_now_utc", lambda: refresh_mod.dt.datetime.fromtimestamp(123, tz=refresh_mod.dt.timezone.utc))
    monkeypatch.setattr(
        refresh_mod,
        "_get",
        lambda _path: {
            "tasks": [
                {
                    "list": {"id": "list-1", "name": "Product Build"},
                    "tags": [{"name": "agent-ready"}],
                    "assignees": [{"username": "Colin"}],
                },
                {
                    "list": {"id": "list-1", "name": "Product Build"},
                    "tags": [],
                    "assignees": [],
                },
            ],
            "last_page": True,
        },
    )

    tags, task_count, patterns = refresh_mod.fetch_recent_task_tags("team-1")

    assert tags == {"agent-ready": 1}
    assert task_count == 2
    assert patterns == [
        {
            "list_id": "list-1",
            "list_name": "Product Build",
            "sampled_task_count": 2,
            "assignees": [
                {"name": "(unassigned)", "count": 1},
                {"name": "Colin", "count": 1},
            ],
        }
    ]


class TestResolveClickupToken:
    """Token resolution order: plain env first, lazy 1Password resolver only
    as a restart-race fallback.

    _CLICKUP_TOKEN_CACHE is reset before/after each test so a resolved
    token from one case can't leak into the next.
    """

    @pytest.fixture(autouse=True)
    def _reset_token_cache(self):
        refresh_mod._CLICKUP_TOKEN_CACHE = None
        yield
        refresh_mod._CLICKUP_TOKEN_CACHE = None

    def test_lazy_resolver_token_used_when_env_unset(self, monkeypatch):
        import agent.lazy_secret_resolver as lsr

        monkeypatch.setattr(lsr, "get", lambda name: "lazy-clickup-token")
        monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

        token = refresh_mod._resolve_clickup_token()

        assert token == "lazy-clickup-token"

    def test_env_token_skips_lazy_resolver(self, monkeypatch):
        import agent.lazy_secret_resolver as lsr

        resolver_calls = []

        def _unexpected_resolver(name):
            resolver_calls.append(name)
            raise AssertionError("env-present path must not call the lazy resolver")

        monkeypatch.setattr(lsr, "get", _unexpected_resolver)
        monkeypatch.setenv("CLICKUP_API_TOKEN", "env-clickup-token")

        token = refresh_mod._resolve_clickup_token()

        assert token == "env-clickup-token"
        assert resolver_calls == []

    def test_empty_env_uses_lazy_resolver(self, monkeypatch):
        import agent.lazy_secret_resolver as lsr

        monkeypatch.setattr(lsr, "get", lambda name: "lazy-clickup-token")
        monkeypatch.setenv("CLICKUP_API_TOKEN", "   ")

        assert refresh_mod._resolve_clickup_token() == "lazy-clickup-token"

    def test_both_empty_raises_sanitized_system_exit_via_fallback_req(self, monkeypatch, capsys):
        import agent.lazy_secret_resolver as lsr

        monkeypatch.setattr(lsr, "get", lambda name: None)
        monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

        assert refresh_mod._resolve_clickup_token() == ""

        with pytest.raises(SystemExit) as exc_info:
            refresh_mod._fallback_req("GET", "/team")

        assert exc_info.value.code == 2
        stderr = capsys.readouterr().err
        assert "could not be resolved via 1Password" in stderr


def test_get_retries_on_429_and_succeeds(monkeypatch):
    """_get() retries on transient 429s and returns data on eventual success."""
    responses = iter([(429, None), (429, None), (200, {"ok": True})])
    calls = []

    def fake_req(method, path, body=None):
        calls.append((method, path))
        return next(responses)

    slept = []
    monkeypatch.setattr(refresh_mod, "_req", fake_req)
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda s: slept.append(s))

    result = refresh_mod._get("/test/path")

    assert result == {"ok": True}
    assert len(calls) == 3  # 2 x 429 retried + 1 success
    assert slept == [15, 30]  # 15 * 2^0, 15 * 2^1


def test_get_honors_retry_after_and_reset_headers(monkeypatch):
    responses = iter(
        [
            (429, None, {"Retry-After": "7"}),
            (429, None, {"X-RateLimit-Reset": "11"}),
            (200, {"ok": True}, {}),
        ]
    )
    slept = []
    monkeypatch.setattr(refresh_mod, "_req", lambda *_: next(responses))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda seconds: slept.append(seconds))

    assert refresh_mod._get("/test/path") == {"ok": True}
    assert slept == [7, 11]


def test_get_never_retries_earlier_than_long_provider_retry_after(monkeypatch):
    responses = iter([(429, None, {"Retry-After": "120"}), (200, {"ok": True}, {})])
    slept = []
    monkeypatch.setattr(refresh_mod, "_req", lambda *_: next(responses))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda seconds: slept.append(seconds))

    assert refresh_mod._get("/test/path") == {"ok": True}
    assert slept == [120]


def test_get_raises_and_logs_after_exhausting_retries(monkeypatch):
    """_get() raises ClickUpApiError and logs a drift entry after max retries on 429."""
    monkeypatch.setattr(refresh_mod, "_req", lambda *_: (429, None))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda _: None)

    logged = []
    monkeypatch.setattr(refresh_mod, "_log_failure", lambda r: logged.append(r))

    with pytest.raises(refresh_mod.ClickUpApiError):
        refresh_mod._get("/test/path")

    assert any("rate_limit_exhausted" in entry for entry in logged)


def _complete_workspace_map(team_id="team-1", generated_at=123000):
    return {
        "schema_version": refresh_mod.SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_at_iso": "1970-01-01T00:02:03+00:00",
        "team_id": team_id,
        "refresh_cadence_hours": 6,
        "refresh_cadence_cron_expr": "0 */6 * * *",
        "refresh_cadence_source": "cron_registration",
        "spaces": [],
        "folders": [],
        "lists": [],
        "task_tags": {"sampled_task_count": 0, "lookback_days": 7, "tags": []},
        "assignment_patterns": [],
        "write_boundaries": [],
        "clients_aliases": {},
    }


def test_exhausted_quota_preserves_last_known_good_map(tmp_path, monkeypatch):
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    previous = _complete_workspace_map(generated_at=1)
    output_path.write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(refresh_mod, "_req", lambda *_: (429, None, {}))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda _: None)

    with pytest.raises(refresh_mod.ClickUpApiError):
        refresh_mod.ensure_workspace_map(
            team_id="team-1",
            force=True,
            output_path=output_path,
            markdown_path=markdown_path,
            write_brain_note=False,
        )

    assert json.loads(output_path.read_text(encoding="utf-8")) == previous
    receipt = json.loads(refresh_mod._receipt_path_for(output_path).read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed"
    assert receipt["cache_retained"] is True
    assert receipt["request_count"] == 4
    assert receipt["rate_limit"]["count"] == 4
    assert receipt["rate_limit"]["retry_delays_seconds"] == [15, 30, 60]


def test_successful_refresh_replaces_cache_atomically_with_matching_receipt(tmp_path, monkeypatch):
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    previous = _complete_workspace_map(generated_at=1)
    output_path.write_text(json.dumps(previous), encoding="utf-8")
    refreshed = _complete_workspace_map(generated_at=123000)
    monkeypatch.setattr(refresh_mod, "build_workspace_map", lambda _: refreshed)

    assert refresh_mod.ensure_workspace_map(
        team_id="team-1",
        force=True,
        output_path=output_path,
        markdown_path=markdown_path,
        write_brain_note=False,
    ) == refreshed

    assert json.loads(output_path.read_text(encoding="utf-8")) == refreshed
    assert refresh_mod._receipt_matches_workspace_map(
        refresh_mod._receipt_path_for(output_path), refreshed
    )
    receipt = json.loads(refresh_mod._receipt_path_for(output_path).read_text(encoding="utf-8"))
    assert receipt["outcome"] == "success"
    assert receipt["request_count"] == 0
    assert receipt["cache_used"] is False


def test_success_receipt_records_rate_limit_guidance_and_request_evidence(tmp_path, monkeypatch):
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    refreshed = _complete_workspace_map(generated_at=123000)
    responses = iter([(429, None, {"Retry-After": "120"}), (200, {"ok": True}, {})])
    slept = []
    monkeypatch.setattr(refresh_mod, "_req", lambda *_: next(responses))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda seconds: slept.append(seconds))

    def _build(_team_id):
        assert refresh_mod._get("/evidence") == {"ok": True}
        return refreshed

    monkeypatch.setattr(refresh_mod, "build_workspace_map", _build)
    refresh_mod.ensure_workspace_map(
        team_id="team-1",
        force=True,
        output_path=output_path,
        markdown_path=markdown_path,
        write_brain_note=False,
    )

    receipt = json.loads(refresh_mod._receipt_path_for(output_path).read_text(encoding="utf-8"))
    assert receipt["outcome"] == "success"
    assert receipt["request_count"] == 2
    assert receipt["cache_present"] is False
    assert receipt["cache_used"] is False
    assert receipt["rate_limit"] == {
        "count": 1,
        "guidance": [{"retry-after": "120"}],
        "retry_delays_seconds": [120],
    }
    assert slept == [120]


def test_mismatched_receipt_forces_non_clean_refresh_without_replacing_cache(tmp_path, monkeypatch):
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    previous = _complete_workspace_map(generated_at=1)
    output_path.write_text(json.dumps(previous), encoding="utf-8")
    refresh_mod._receipt_path_for(output_path).write_text(
        json.dumps({"map_sha256": "not-the-map"}), encoding="utf-8"
    )
    monkeypatch.setattr(refresh_mod, "_req", lambda *_: (429, None, {}))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda _: None)

    with pytest.raises(refresh_mod.ClickUpApiError):
        refresh_mod.ensure_workspace_map(
            team_id="team-1",
            output_path=output_path,
            markdown_path=markdown_path,
            write_brain_note=False,
        )

    assert json.loads(output_path.read_text(encoding="utf-8")) == previous


def test_partial_refresh_is_refused_without_replacing_last_known_good(tmp_path, monkeypatch):
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    previous = _complete_workspace_map(generated_at=1)
    output_path.write_text(json.dumps(previous), encoding="utf-8")
    partial = _complete_workspace_map(generated_at=123000)
    partial.pop("lists")
    monkeypatch.setattr(refresh_mod, "build_workspace_map", lambda _: partial)

    with pytest.raises(refresh_mod.WorkspaceMapValidationError, match="complete lists"):
        refresh_mod.ensure_workspace_map(
            team_id="team-1",
            force=True,
            output_path=output_path,
            markdown_path=markdown_path,
            write_brain_note=False,
        )

    assert json.loads(output_path.read_text(encoding="utf-8")) == previous
    receipt = json.loads(refresh_mod._receipt_path_for(output_path).read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed"
    assert receipt["cache_retained"] is True


def test_req_always_returns_headers_regardless_of_helper(monkeypatch):
    """_req always uses the header-aware fallback so Retry-After is never lost."""
    monkeypatch.setattr(refresh_mod, "_HELPER_MODULE_ATTEMPTED", False)
    monkeypatch.setattr(refresh_mod, "_HELPER_MODULE", None)
    responses = iter([(429, None, {"Retry-After": "5"}), (200, {"ok": True}, {})])
    slept = []
    monkeypatch.setattr(refresh_mod, "_fallback_req", lambda *_: next(responses))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda s: slept.append(s))

    assert refresh_mod._get("/test/path") == {"ok": True}
    assert slept == [5]


def test_brain_note_failure_does_not_publish_new_map(tmp_path, monkeypatch):
    """A brain-note write error must not leave a new map with a stale receipt."""
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    receipt_path = refresh_mod._receipt_path_for(output_path)
    previous = _complete_workspace_map(generated_at=1)
    output_path.write_text(json.dumps(previous), encoding="utf-8")
    refreshed = _complete_workspace_map(generated_at=2)
    monkeypatch.setattr(refresh_mod, "build_workspace_map", lambda _: refreshed)
    monkeypatch.setattr(
        refresh_mod,
        "write_workspace_map_note",
        _raise_workspace_map_note_error,
    )

    with pytest.raises(refresh_mod.WorkspaceMapNoteError):
        refresh_mod.ensure_workspace_map(
            team_id="team-1",
            force=True,
            output_path=output_path,
            markdown_path=markdown_path,
            write_brain_note=True,
            brain_note_path=tmp_path / "brain.md",
        )

    on_disk = json.loads(output_path.read_text(encoding="utf-8"))
    assert on_disk["generated_at"] == 1, "old map must survive a brain-note failure"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed"
    assert receipt["generated_at"] == 1, "failed receipt must reference the map on disk"


def test_post_map_write_failure_receipt_matches_published_map(tmp_path, monkeypatch):
    """If a write fails AFTER the map is published, the failed receipt must reference the new map."""
    output_path = tmp_path / "clickup-map.json"
    markdown_path = tmp_path / "clickup-workspace-map.md"
    receipt_path = refresh_mod._receipt_path_for(output_path)
    previous = _complete_workspace_map(generated_at=1)
    output_path.write_text(json.dumps(previous), encoding="utf-8")
    refreshed = _complete_workspace_map(generated_at=2)
    monkeypatch.setattr(refresh_mod, "build_workspace_map", lambda _: refreshed)

    orig_write_markdown = refresh_mod._write_markdown
    calls = []

    def exploding_markdown(path, body):
        calls.append(path.name)
        if path.name == "clickup-workspace-map.md":
            raise OSError("disk full")
        orig_write_markdown(path, body)

    monkeypatch.setattr(refresh_mod, "_write_markdown", exploding_markdown)

    with pytest.raises(OSError, match="disk full"):
        refresh_mod.ensure_workspace_map(
            team_id="team-1",
            force=True,
            output_path=output_path,
            markdown_path=markdown_path,
            write_brain_note=False,
        )

    on_disk = json.loads(output_path.read_text(encoding="utf-8"))
    assert on_disk["generated_at"] == 2, "new map is already published"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed"
    assert receipt["generated_at"] == 2, "failed receipt must match the published map"


def _raise_workspace_map_note_error(*_args, **_kwargs):
    raise refresh_mod.WorkspaceMapNoteError("simulated brain-note failure")


def test_field_discovery_is_paced_and_deduplicated(monkeypatch):
    refresh_mod._LIST_FIELDS_CACHE = {}
    refresh_mod._LAST_FIELD_DISCOVERY_AT = None
    requests = []
    clock = iter([0.0, 0.0, 0.0, 0.0, 0.25])
    slept = []
    monkeypatch.setattr(refresh_mod, "_get", lambda path: requests.append(path) or {"fields": []})
    monkeypatch.setattr(refresh_mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(refresh_mod.time, "sleep", lambda seconds: slept.append(seconds))

    refresh_mod.fetch_list_fields("list-1")
    refresh_mod.fetch_list_fields("list-1")
    refresh_mod.fetch_list_fields("list-2")

    assert requests == ["/list/list-1/field", "/list/list-2/field"]
    assert slept == [refresh_mod.FIELD_DISCOVERY_MIN_INTERVAL_SECONDS]
