from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "fleet_outcome_probe.py"
CONTRACTS_PATH = SCRIPTS / "fleet_outcome_contracts.json"
JOBS_PATH = SCRIPTS.parent / "fleet-config" / "jobs.json"
NOW = datetime(2026, 7, 30, 16, 0, tzinfo=timezone.utc)
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"fleet_outcome_probe_ut_{_COUNTER}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["command"], returncode, stdout, stderr)


def _fixture(tmp_path):
    active_id = "active"
    disabled_id = "disabled"
    jobs = {
        "jobs": [
            {
                "id": active_id,
                "name": "active job",
                "enabled": True,
                "last_status": "ok",
                "last_run_at": (NOW - timedelta(minutes=2)).isoformat(),
            },
            {
                "id": disabled_id,
                "name": "disabled job",
                "enabled": False,
                "last_status": None,
                "last_run_at": None,
            },
        ]
    }
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    output_root = tmp_path / "output"
    output_dir = output_root / active_id
    output_dir.mkdir(parents=True)
    output = output_dir / "latest.md"
    output.write_text("# Cron Job\nsemantic-success\n", encoding="utf-8")
    timestamp = NOW.timestamp()
    import os

    os.utime(output, (timestamp, timestamp))

    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    with (launch_agents / "com.colingreig.hermes.fixture.plist").open("wb") as handle:
        plistlib.dump({"Label": "com.colingreig.hermes.fixture"}, handle)

    contracts = {
        "schema_version": 1,
        "cron_jobs": [
            {
                "id": active_id,
                "name": "active job",
                "enabled": True,
                "max_age_seconds": 600,
                "outcome": {
                    "kind": "cron_output",
                    "max_age_seconds": 600,
                    "success_patterns": ["semantic-success"],
                    "failure_patterns": ["semantic-failure"],
                },
            },
            {
                "id": disabled_id,
                "name": "disabled job",
                "enabled": False,
                "max_age_seconds": 0,
                "outcome": {
                    "kind": "cron_output",
                    "max_age_seconds": 1,
                    "success_patterns": [],
                    "failure_patterns": [],
                },
            },
        ],
        "launch_agents": [
            {
                "label": "com.colingreig.hermes.fixture",
                "expected": "loaded",
                "outcome": {"kind": "self"},
            },
            {
                "label": "com.colingreig.hermes.retired",
                "expected": "retired",
                "outcome": {"kind": "self"},
            },
        ],
    }
    return contracts, jobs_path, output_root, launch_agents, output


def test_evaluate_requires_fresh_semantic_outcome_not_only_scheduler_ok(tmp_path):
    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, output = _fixture(tmp_path)

    def launchctl(label):
        return _completed(0 if label.endswith(".fixture") else 113)

    findings, evidence = module.evaluate(
        contracts,
        jobs_path=jobs_path,
        output_root=output_root,
        launch_agents_dir=launch_agents,
        home=tmp_path,
        now=NOW,
        launchctl=launchctl,
        launch_inventory=lambda: {"com.colingreig.hermes.fixture"},
    )
    assert findings == []
    assert {item["id"] for item in evidence} == {
        "active",
        "disabled",
        "com.colingreig.hermes.fixture",
        "com.colingreig.hermes.retired",
    }

    output.write_text("# Cron Job\nsemantic-failure\n", encoding="utf-8")
    findings, _ = module.evaluate(
        contracts,
        jobs_path=jobs_path,
        output_root=output_root,
        launch_agents_dir=launch_agents,
        home=tmp_path,
        now=NOW,
        launchctl=launchctl,
        launch_inventory=lambda: {"com.colingreig.hermes.fixture"},
    )
    codes = {item["code"] for item in findings}
    assert "failure_marker" in codes
    assert "success_marker_missing" in codes


def test_unknown_enabled_cron_and_monitored_plist_fail_closed(tmp_path):
    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, _output = _fixture(tmp_path)
    payload = json.loads(jobs_path.read_text())
    payload["jobs"].append({"id": "new", "name": "new", "enabled": True})
    jobs_path.write_text(json.dumps(payload), encoding="utf-8")
    with (launch_agents / "com.colingreig.hermes.unknown.plist").open("wb") as handle:
        plistlib.dump({"Label": "com.colingreig.hermes.unknown"}, handle)

    findings, _ = module.evaluate(
        contracts,
        jobs_path=jobs_path,
        output_root=output_root,
        launch_agents_dir=launch_agents,
        home=tmp_path,
        now=NOW,
        launchctl=lambda label: _completed(0 if label.endswith(".fixture") else 113),
        launch_inventory=lambda: {
            "com.colingreig.hermes.fixture",
            "com.colingreig.hermes.unknown",
        },
    )
    assert ("cron", "new", "uncovered_enabled_job") in {
        (item["surface"], item["id"], item["code"]) for item in findings
    }
    assert (
        "launchd",
        "com.colingreig.hermes.unknown",
        "uncovered_plist",
    ) in {(item["surface"], item["id"], item["code"]) for item in findings}


def test_alarm_dedup_state_advances_only_after_confirmed_send(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "one", "failed", "synthetic")]

    failed = module.route_alarm(
        findings,
        state_path=state_path,
        now=NOW,
        drill=True,
        real_alert=True,
        sender=lambda _message: _completed(1, stderr="offline"),
    )
    assert failed["action"] == "delivery-failed"
    assert not state_path.exists()

    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    sent = module.route_alarm(
        findings,
        state_path=state_path,
        now=NOW,
        drill=True,
        real_alert=True,
        sender=sender,
    )
    deduped = module.route_alarm(
        findings,
        state_path=state_path,
        now=NOW + timedelta(minutes=1),
        drill=True,
        real_alert=True,
        sender=sender,
    )
    assert sent["action"] == "sent"
    assert deduped["action"] == "deduped"
    assert len(messages) == 1
    assert "SYNTHETIC DRILL" in messages[0]

    recovered = module.route_alarm(
        [],
        state_path=state_path,
        now=NOW + timedelta(minutes=2),
        drill=False,
        real_alert=True,
        sender=sender,
    )
    repeated = module.route_alarm(
        findings,
        state_path=state_path,
        now=NOW + timedelta(minutes=3),
        drill=False,
        real_alert=True,
        sender=sender,
    )
    assert recovered["action"] == "recovery-sent"
    assert repeated["action"] == "sent"
    assert len(messages) == 3


def test_canonical_contract_inventory_covers_exact_jobs_and_semantic_outcomes():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    jobs = json.loads(JOBS_PATH.read_text(encoding="utf-8"))["jobs"]
    assert len(jobs) == 16
    assert {item["id"] for item in contracts["cron_jobs"]} == {
        item["id"] for item in jobs
    }
    assert len(contracts["launch_agents"]) == 19
    assert sum(item["expected"] == "loaded" for item in contracts["launch_agents"]) == 18
    assert sum(item["expected"] == "retired" for item in contracts["launch_agents"]) == 1

    for item in contracts["cron_jobs"]:
        if not item["enabled"]:
            continue
        outcome = item["outcome"]
        assert outcome["kind"] in {"cron_output", "json_artifact", "text_artifact"}
        assert outcome.get("success_patterns") or outcome.get("required_patterns")
    for item in contracts["launch_agents"]:
        outcome = item["outcome"]
        if item["expected"] == "retired" or outcome["kind"] in {"self", "tcp"}:
            continue
        assert outcome.get("success_patterns") or outcome.get("required_patterns")


def test_launchctl_inventory_ignores_enabled_overrides_outside_services():
    module = _load_module()
    text = """
gui/501 = {
\tservices = {
\t       0      0 \tcom.colingreig.hermes.real-monitor
\t       0      - \tcom.apple.Finder
\t}
\tdisabled services = {
\t\t"com.ignite.retired-override" => enabled
\t\t"com.colingreig.hermes.old-cleaner" => enabled
\t}
}
"""
    assert module._labels_from_launchctl_domain(text) == {
        "com.colingreig.hermes.real-monitor"
    }


def test_run_end_boundary_rejects_current_partial_failure_not_old_failure(tmp_path):
    module = _load_module()
    artifact = tmp_path / "runner.log"
    outcome = {
        "kind": "text_artifact",
        "max_age_seconds": 60,
        "tail_lines": 100,
        "run_end_pattern": "RUN_COMPLETE",
        "success_patterns": ["RUN_COMPLETE"],
        "failure_patterns": ["PARTIAL_FAILURE"],
    }
    artifact.write_text(
        "PARTIAL_FAILURE old\nRUN_COMPLETE\ncurrent work\nRUN_COMPLETE\n",
        encoding="utf-8",
    )
    import os

    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    assert (
        module._check_text_evidence(
            surface="launchd",
            identifier="fixture",
            path=artifact,
            outcome=outcome,
            now=NOW,
        )
        == []
    )

    artifact.write_text(
        "RUN_COMPLETE\ncurrent work\nPARTIAL_FAILURE current\nRUN_COMPLETE\n",
        encoding="utf-8",
    )
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="launchd",
        identifier="fixture",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"failure_marker"}


def test_drill_trips_one_real_predicate_per_contract():
    module = _load_module()
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    findings = module._inject_contract_failures(contracts, now=NOW)
    assert len(findings) == 35
    assert len({(item["surface"], item["id"]) for item in findings}) == 35
    assert all(item["code"] != "synthetic_outcome_failure" for item in findings)
    assert all(item["detail"].startswith("SYNTHETIC DRILL:") for item in findings)
    assert module.DEFAULT_DRILL_STATE != module.DEFAULT_STATE
    assert module.DEFAULT_DRILL_RECEIPT != module.DEFAULT_RECEIPT
