from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPT_DIR / "hermes_report_build.py"
sys.path.insert(0, str(SCRIPT_DIR))
_spec = importlib.util.spec_from_file_location("hermes_report_build_under_test", SCRIPT)
report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = report
_spec.loader.exec_module(report)


def _task(task_id: str, status: str, *, list_name: str = "List", folder_name: str = "Folder") -> dict:
    return {
        "id": task_id,
        "name": f"Task {task_id}",
        "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": status, "type": "custom"},
        "list": {"id": f"list-{list_name}", "name": list_name},
        "folder": {"id": f"folder-{folder_name}", "name": folder_name},
        "parent": None,
        "date_updated": "123",
    }


def _empty_scoreboard(**overrides):
    base = {
        "shipped": 0,
        "ready": 0,
        "in_progress": 0,
        "in_review": 0,
        "blocked": 0,
        "lane_code": 0,
        "lane_content": 0,
        "validator_completed_window": 0,
        "daily": {
            "claimed": {"today": 0, "yesterday": 0},
            "shipped": {"today": 0, "yesterday": 0},
            "blocked": {"today": 0, "yesterday": 0},
        },
    }
    base.update(overrides)
    return base


def _empty_spend(**overrides):
    base = {
        "empty": True,
        "error": None,
        "total_cost": 0.0,
        "writer_total_cost": 0.0,
        "cost_delta": 0.0,
        "today_cost": 0.0,
        "provider_rows": [],
        "drift_n": 0,
        "top_drift_model": None,
        "guard_total_cost": 12.34,
        "guard_error": None,
    }
    base.update(overrides)
    return base


def test_workspace_review_query_paginates_dedupes_both_statuses_and_unmapped_board(monkeypatch):
    calls: list[str] = []

    pages = {
        ("in review", 0): {"tasks": [_task("86e2aaaaa", "in review", list_name="Mapped")], "last_page": False},
        ("in review", 1): {"tasks": [_task("86e2dupe1", "in review", list_name="Unmapped", folder_name="Other")], "last_page": True},
        ("ready for review", 0): {"tasks": [_task("86e2dupe1", "ready for review", list_name="Unmapped", folder_name="Other")], "last_page": False},
        ("ready for review", 1): {"tasks": [_task("86e2bbbbb", "ready for review", list_name="Subtasks")], "last_page": True},
    }

    def fake_get(url, token):
        calls.append(url)
        status = "ready for review" if "ready+for+review" in url else "in review"
        page = 1 if "page=1" in url else 0
        return pages[(status, page)]

    rows, meta = report.fetch_workspace_review_queue(token="token", get_json=fake_get)

    assert meta["error"] is None
    assert len(rows) == 3
    assert {r["status"] for r in rows} == {"in review", "ready for review"}
    assert any(r["list"] == "Unmapped" and r["project"] == "Other" for r in rows)
    assert sum(1 for r in rows if r["id"] == "86e2dupe1") == 1
    assert all("subtasks=true" in url for url in calls)
    assert any("statuses%5B%5D=in+review" in url for url in calls)
    assert any("statuses%5B%5D=ready+for+review" in url for url in calls)
    assert len(calls) == 4


def test_review_query_failure_reports_degraded_not_false_zero():
    def fake_get(url, token):
        raise OSError("network down")

    rows, meta = report.fetch_workspace_review_queue(token="token", get_json=fake_get)

    assert rows == []
    assert "degraded" in meta["error"]


def test_review_backlog_threshold_alert_renders_prominently_and_flags_subject():
    rows = [_task(f"86e2x{i:04d}", "in review") for i in range(report.DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD)]
    row_cards = [report._task_row_from_clickup(t) for t in rows]
    meta = {"error": None}
    alert = report.build_review_backlog_alert(row_cards, meta, report.DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD)
    alerts = [alert]
    scoreboard = _empty_scoreboard(in_review=len(row_cards))
    spend = _empty_spend()

    subject = report.build_subject(scoreboard, spend, report.summarize_alerts(alerts))
    text = report.build_text({}, scoreboard, spend, alerts, [], {"ready": 0}, [], row_cards, meta,
                             report.DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD, 360)

    assert "review backlog" in subject.lower()
    assert "25 waiting" in subject
    assert "REVIEW BACKLOG ALERT" in text
    assert "25 tasks waiting" in text


def test_spend_labels_keep_guard_and_writer_served_sources_separate():
    rows = [{"cost_usd": 3.50, "served_provider": "writer", "ts": "2026-07-28T12:00:00+00:00"}]
    spend = report.summarize_spend(rows, rows, [])
    spend.update(report.load_guard_tracked_spend("20260728", spend_guard_module=_SpendGuard))
    spend["empty"] = False
    spend["error"] = None
    text = report.build_text(
        {},
        _empty_scoreboard(),
        spend,
        [],
        [],
        {"ready": 0},
        [],
        [],
        {"error": None},
        report.DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD,
        360,
    )

    assert "Writer (6h): $3.50" in text
    assert "Daily total (all sources): $42.12" in text
    assert "separate sources" in text
    assert "$45.62" not in text


class _SpendGuard:
    @staticmethod
    def _daily_spend_usd_strict(today_str=None):
        return 42.125


def test_verdict_subject_is_human_not_metric_dump():
    scoreboard = _empty_scoreboard(ready=5, validator_completed_window=3, in_progress=1)
    spend = _empty_spend(empty=False, writer_total_cost=1.25, total_cost=1.25)
    alert_summary = {
        "action_required": 0,
        "watch_list": 0,
        "system_signals": 0,
        "review_backlog": 0,
        "alerts_n": 0,
    }
    subject = report.build_subject(scoreboard, spend, alert_summary)
    assert subject == "Hermes: all clear — 3 completed · $1.25"
    assert "validator-completed" not in subject
    assert "action required" not in subject


def test_stalled_verdict_outranks_other_signals():
    scoreboard = _empty_scoreboard(ready=5, in_progress=2, blocked=1)
    spend = _empty_spend()
    header = {"work_stoppage": "ready=5 completed=0 in_progress=2 live_claims=0 -> STALLED"}
    alerts = [
        {
            "kind": "blocked",
            "name": "Blocked task",
            "detail": "Awaiting you",
            "url": "https://example.com/t/1",
        },
        {
            "kind": "health",
            "name": "Work stoppage signal",
            "detail": header["work_stoppage"],
            "sub": "Health scan",
        },
    ]
    model = report.build_report_view_model(
        header, scoreboard, spend, alerts, [], {"ready": 5}, [], 360,
    )
    assert model["verdict"]["level"] == "stalled"
    assert model["subject"].startswith("Hermes: stalled")
    assert "ready=5 completed=0" not in model["headline"]
    assert "Ready work isn't moving" in model["verdict"]["summary"]
    text = report.build_text_view(model)
    assert "NEEDS YOU" in text
    assert "SYSTEM" in text
    assert "ACTION REQUIRED" not in text
    assert "validator-completed" not in text
    assert "WORTH WATCHING" not in text
    html = report.render_html_view(model)
    assert "Ready work isn" in html
    assert "At a glance" in html
    assert "Needs you" in html


def test_humanize_work_stoppage_sentences():
    stalled = report.humanize_work_stoppage(
        "ready=5 completed=0 in_progress=2 live_claims=0 -> STALLED"
    )
    assert "Ready work isn't moving" in stalled
    assert "5 ready" in stalled
    assert "->" not in stalled

    ok = report.humanize_work_stoppage("ready=1 completed=2 in_progress=1 live_claims=1 -> OK")
    assert ok == "Work is flowing."


def test_queue_roster_not_dumped_in_digest():
    hermes_rows = [
        {"name": f"Task {i}", "status": "to do", "url": f"https://x/{i}", "project": "P", "list": "L"}
        for i in range(8)
    ]
    model = report.build_report_view_model(
        {},
        _empty_scoreboard(ready=8, validator_completed_window=1),
        _empty_spend(empty=False, writer_total_cost=0.5, total_cost=0.5),
        [],
        hermes_rows,
        {"ready": 8, "generated": "2026-08-01T13:00:00Z"},
        [],
        360,
    )
    text = report.build_text_view(model)
    html = report.render_html_view(model)
    assert "8 tasks on the Hermes board · 8 ready" in text
    assert "Task 0" not in text
    assert "Task 0" not in html
    assert "8 tasks on the Hermes board" in html
