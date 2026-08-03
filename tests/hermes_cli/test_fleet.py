from __future__ import annotations

import json
import sqlite3


def test_incident_report_joins_execution_ledger_and_deployed_sha(monkeypatch, tmp_path):
    import hermes_cli.fleet as fleet

    home = tmp_path / "profile"
    root = tmp_path / "repo"
    (home / "cron").mkdir(parents=True)
    (home / "releases").mkdir()
    (root / "machine-setup/mini-scripts").mkdir(parents=True)
    (root / "machine-setup/mini-scripts/fleet_outcome_manifest.json").write_text(
        json.dumps({"files": []}), encoding="utf-8"
    )
    (home / "releases/.mini-release-last-receipt.json").write_text(
        json.dumps({"to_commit": "a" * 40, "runtime_target": "/release/a"}),
        encoding="utf-8",
    )
    ledger = home / "cron/executions.db"
    with sqlite3.connect(ledger) as conn:
        conn.execute(
            "CREATE TABLE executions (id TEXT, job_id TEXT, status TEXT, claimed_at TEXT, terminal_at TEXT, finished_at TEXT)"
        )
        conn.execute(
            "INSERT INTO executions VALUES ('execution','job','running','2026-08-03T00:00:00+00:00',NULL,NULL)"
        )

    monkeypatch.setattr(fleet, "_process_evidence", lambda: [])
    monkeypatch.setattr(fleet, "_gateway_evidence", lambda: {"reachable": True})
    report = fleet.collect_incident_report(home=home, root=root)

    assert report["execution_ledger"]["active"][0]["id"] == "execution"
    assert report["deployment"]["deployed_sha"] == "a" * 40
    assert report["safe_to_cutover"] is False


def test_incident_report_reads_requested_profile_admission_store(monkeypatch, tmp_path):
    import hermes_cli.fleet as fleet

    home = tmp_path / "profile"
    root = tmp_path / "repo"
    (root / "machine-setup/mini-scripts").mkdir(parents=True)
    (root / "machine-setup/mini-scripts/fleet_outcome_manifest.json").write_text(
        json.dumps({"files": []}), encoding="utf-8"
    )
    observed = []
    monkeypatch.setattr(
        fleet,
        "executor_drain_status",
        lambda *, database_path=None: observed.append(database_path) or {
            "safe_to_cutover": True,
            "generic_leases": [],
            "generic_recovery_receipts": [],
        },
    )
    monkeypatch.setattr(fleet, "_process_evidence", lambda: [])
    monkeypatch.setattr(fleet, "_gateway_evidence", lambda: {"reachable": True})

    fleet.collect_incident_report(home=home, root=root)
    assert observed == [home.resolve() / "state/executor-admission.db"]


def test_incident_report_surfaces_fleet_outcome_history_not_only_latest_receipt(
    monkeypatch, tmp_path
):
    """The probe receipt is overwritten per run; triage needs the archive."""
    import hermes_cli.fleet as fleet

    home = tmp_path / "profile"
    root = tmp_path / "repo"
    state = home / "state"
    state.mkdir(parents=True)
    (root / "machine-setup/mini-scripts").mkdir(parents=True)
    (root / "machine-setup/mini-scripts/fleet_outcome_manifest.json").write_text(
        json.dumps({"files": []}), encoding="utf-8"
    )
    (state / "fleet-outcome-probe.json").write_text(
        json.dumps(
            {
                "checked_at": "2026-08-03T02:00:00+00:00",
                "status": "clean",
                "finding_count": 0,
                "alarm": {"action": "recovery-sent", "reason": "stayed clean"},
            }
        ),
        encoding="utf-8",
    )
    (state / "fleet-outcome-alert-state.json").write_text(
        json.dumps({"active": False}), encoding="utf-8"
    )
    (state / "fleet-outcome-probe-history.jsonl").write_text(
        "\n".join(
            json.dumps(
                {
                    "checked_at": f"2026-08-03T01:{minute:02d}:00+00:00",
                    "status": "alert",
                    "finding_count": 3,
                    "alarm_action": "deduped",
                    "incident_id": "deadbeef",
                }
            )
            for minute in (10, 15, 20)
        )
        + "\n",
        encoding="utf-8",
    )
    snapshots = state / "fleet-outcome-probe-history"
    snapshots.mkdir()
    (snapshots / "20260803T011000-sent.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(fleet, "_process_evidence", lambda: [])
    monkeypatch.setattr(fleet, "_gateway_evidence", lambda: {"reachable": True})
    report = fleet.collect_incident_report(home=home, root=root)

    outcome = report["fleet_outcome"]
    assert outcome["latest"]["status"] == "clean"
    assert outcome["latest"]["alarm_reason"] == "stayed clean"
    assert outcome["incident_open"] is False
    # The storm the current receipt no longer mentions is still reconstructable.
    assert outcome["history_available"] is True
    assert [item["finding_count"] for item in outcome["history"]] == [3, 3, 3]
    assert outcome["recent_snapshots"] == ["20260803T011000-sent.json"]


def test_incident_report_reports_a_missing_fleet_outcome_archive(monkeypatch, tmp_path):
    import hermes_cli.fleet as fleet

    home = tmp_path / "profile"
    root = tmp_path / "repo"
    (root / "machine-setup/mini-scripts").mkdir(parents=True)
    (root / "machine-setup/mini-scripts/fleet_outcome_manifest.json").write_text(
        json.dumps({"files": []}), encoding="utf-8"
    )
    monkeypatch.setattr(fleet, "_process_evidence", lambda: [])
    monkeypatch.setattr(fleet, "_gateway_evidence", lambda: {"reachable": True})

    outcome = fleet.collect_incident_report(home=home, root=root)["fleet_outcome"]
    assert outcome["latest"] is None
    assert outcome["history"] == []
    assert outcome["history_available"] is False