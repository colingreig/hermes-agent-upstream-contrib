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
    scoreboard = {"shipped": 0, "ready": 0, "in_progress": 0, "in_review": len(row_cards), "blocked": 0,
                  "lane_code": 0, "lane_content": 0,
                  "daily": {"claimed": {"today": 0, "yesterday": 0}, "shipped": {"today": 0, "yesterday": 0}, "blocked": {"today": 0, "yesterday": 0}}}
    spend = {"empty": True, "error": None, "total_cost": 0.0, "writer_total_cost": 0.0,
             "cost_delta": 0.0, "provider_rows": [], "drift_n": 0, "top_drift_model": None,
             "guard_total_cost": 12.34, "guard_error": None}

    subject = report.build_subject(scoreboard, spend, report.summarize_alerts(alerts))
    text = report.build_text({}, scoreboard, spend, alerts, [], {"ready": 0}, [], row_cards, meta,
                             report.DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD, 360)

    assert subject.startswith("🚨 REVIEW BACKLOG")
    assert "REVIEW BACKLOG ALERT" in text
    assert "25 tasks at/above threshold 25" in text


class _SpendGuard:
    @staticmethod
    def _daily_spend_usd_strict(today_str=None):
        return 42.125


def test_spend_labels_keep_guard_and_writer_served_sources_separate():
    rows = [{"cost_usd": 3.50, "served_provider": "writer", "ts": "2026-07-28T12:00:00+00:00"}]
    spend = report.summarize_spend(rows, rows, [])
    spend.update(report.load_guard_tracked_spend("20260728", spend_guard_module=_SpendGuard))
    spend["empty"] = False
    spend["error"] = None
    text = report.build_text(
        {},
        {"shipped": 0, "ready": 0, "in_progress": 0, "in_review": 0, "blocked": 0,
         "lane_code": 0, "lane_content": 0,
         "daily": {"claimed": {"today": 0, "yesterday": 0}, "shipped": {"today": 0, "yesterday": 0}, "blocked": {"today": 0, "yesterday": 0}}},
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

    assert "Writer-served receipts this window: $3.50" in text
    assert "Guard-tracked daily spend (spend_guard: state.db + opencode logs): $42.12" in text
    assert "separate sources; no combined total is reported" in text
    assert "$45.62" not in text
