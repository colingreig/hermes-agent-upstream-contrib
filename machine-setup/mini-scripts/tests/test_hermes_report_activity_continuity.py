from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys


SCRIPT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPT_DIR / "hermes_report_build.py"
sys.path.insert(0, str(SCRIPT_DIR))
_spec = importlib.util.spec_from_file_location(
    "hermes_report_continuity_under_test", SCRIPT
)
report = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = report
_spec.loader.exec_module(report)


def _payload(state="ACTIVE"):
    return {
        "schema": report.ACTIVITY_CONTINUITY_SCHEMA,
        "scope": "Hermes Mac mini",
        "state": state,
        "slot_id": "2026-08-02T12:00:00Z",
        "concern_id": (
            "hermes-mini-activity-continuity:stable"
            if state == "INACTIVE"
            else None
        ),
        "detail": f"Activity continuity {state} for the Hermes Mac mini",
    }


def test_consumer_gate_does_not_invoke_adapter_while_disabled():
    assert report.ACTIVITY_CONTINUITY_CONSUMER_ENABLED is True
    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled consumer invoked adapter")

    assert report.load_activity_continuity(
        0, enabled=False, runner=forbidden
    ) is None


def test_enabled_consumer_passes_strict_count_and_window_to_adapter():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, json.dumps(_payload()), ""
        )

    result = report.load_activity_continuity(
        3, enabled=True, report_window_min=720, runner=runner
    )
    assert result["state"] == "ACTIVE"
    argv = calls[0][0]
    assert argv[argv.index("--strict-validator-completed") + 1] == "3"
    assert argv[argv.index("--report-window-min") + 1] == "720"


def test_adapter_failure_or_malformed_contract_becomes_unknown():
    for completed in (
        subprocess.CompletedProcess([], 2, "{}", "failed"),
        subprocess.CompletedProcess([], 0, "not-json", ""),
        subprocess.CompletedProcess(
            [], 0, json.dumps({**_payload(), "scope": "fleet"}), ""
        ),
    ):
        result = report.load_activity_continuity(
            0,
            enabled=True,
            runner=lambda *_args, _result=completed, **_kwargs: _result,
        )
        assert result["state"] == "UNKNOWN"
        assert result["scope"] == "Hermes Mac mini"


def test_activity_continuity_is_system_signal_only_and_render_consistent():
    continuity = _payload("INACTIVE")
    signal = report.build_activity_continuity_signal(continuity)
    assert signal["kind"] == "health"
    assert "INACTIVE" in signal["name"]
    alerts = [signal]
    scoreboard = {
        "ready": 0,
        "in_progress": 0,
        "in_review": 0,
        "blocked": 0,
        "validator_completed_window": 7,
        "lane_code": 0,
        "lane_content": 0,
    }
    spend = {
        "empty": True,
        "error": None,
        "total_cost": 0.0,
        "writer_total_cost": 0.0,
        "today_cost": 0.0,
        "previous_window_cost": 0.0,
        "cost_delta": 0.0,
        "provider_rows": [],
        "providers_n": 0,
        "runs_n": 0,
        "drift_n": 0,
        "top_drift_model": None,
        "guard_total_cost": 0.0,
        "guard_error": None,
    }
    model = report.build_report_view_model(
        {},
        scoreboard,
        spend,
        alerts,
        [],
        {"ready": 0},
        [],
        360,
        [],
        {"error": None},
        report.DEFAULT_REVIEW_BACKLOG_ALERT_THRESHOLD,
        continuity,
    )
    html = report.render_html_view(model)
    text = report.build_text_view(model)
    assert model["counts"]["action_required"] == 0
    assert model["counts"]["system_signals"] == 1
    assert model["activity_continuity"] == continuity
    assert "1 system signals" in model["subject"]
    assert "Activity continuity" in html
    assert "Activity continuity" in text
    assert scoreboard["validator_completed_window"] == 7
