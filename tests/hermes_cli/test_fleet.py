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