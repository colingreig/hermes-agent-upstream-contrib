from __future__ import annotations

import importlib.util
import json
import plistlib
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest


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


def _canonical_contract(*, cron_name=None, launch_label=None):
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    if cron_name is not None:
        return next(item for item in contracts["cron_jobs"] if item["name"] == cron_name)
    return next(item for item in contracts["launch_agents"] if item["label"] == launch_label)


def _fresh_artifact(tmp_path, text):
    artifact = tmp_path / "evidence.log"
    artifact.write_text(text, encoding="utf-8")
    import os

    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    return artifact


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

    def launchctl(domain, label):
        _, user_domain = module._launchd_domains()
        if label.endswith(".fixture"):
            return _completed(0 if domain != user_domain else 113)
        return _completed(113)

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
        launchctl=lambda domain, label: _completed(
            0 if label.endswith(".fixture") and domain != module._launchd_domains()[1] else 113
        ),
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


def test_clickup_executor_accepts_canonical_admission_no_claim_receipt(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="clickup-executor")["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """# Cron Job: clickup-executor
## Response
Executor outcome: success-no-claim
Zero ClickUp claims were started because another fenced owner is active.
""",
    )

    assert module._check_text_evidence(
        surface="cron",
        identifier="62714b869845",
        path=artifact,
        outcome=outcome,
        now=NOW,
    ) == []

    artifact.write_text(
        """# Cron Job: clickup-executor
## Response
Zero ClickUp claims and zero swarms were started because another owner is active.
""",
        encoding="utf-8",
    )
    findings = module._check_text_evidence(
        surface="cron",
        identifier="62714b869845",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"success_marker_missing"}

    for negated in (
        "not one swarm was started",
        "one swarm failed to start",
        "a swarm was not started",
    ):
        artifact.write_text(
            f"# Cron Job: clickup-executor\n## Response\n{negated}.\n",
            encoding="utf-8",
        )
        findings = module._check_text_evidence(
            surface="cron",
            identifier="62714b869845",
            path=artifact,
            outcome=outcome,
            now=NOW,
        )
        assert {item["code"] for item in findings} == {"success_marker_missing"}

    artifact.write_text(
        """# Cron Job: clickup-executor
## Response
0 swarms were started.
""",
        encoding="utf-8",
    )
    findings = module._check_text_evidence(
        surface="cron",
        identifier="62714b869845",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"success_marker_missing"}


def test_malformed_plist_is_skipped_with_warning_not_crash(tmp_path):
    """A third-party LaunchAgent plist with invalid XML (e.g. a malformed
    comment) must never crash the probe -- it is skipped and surfaced as a
    distinguishable, non-fatal evidence entry instead of an
    xml.parsers.expat.ExpatError propagating out of evaluate()."""
    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, _output = _fixture(tmp_path)
    malformed = launch_agents / "com.hermes.opendesign.plist"
    malformed.write_bytes(
        b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        b"<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        b"\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        b"<plist version=\"1.0\">\n<dict>\n<!-- broken comment -- >\n"
        b"<key>Label</key>\n<string>com.hermes.opendesign</string>\n</dict>\n</plist>\n"
    )

    findings, evidence = module.evaluate(
        contracts,
        jobs_path=jobs_path,
        output_root=output_root,
        launch_agents_dir=launch_agents,
        home=tmp_path,
        now=NOW,
        launchctl=lambda domain, label: _completed(
            0 if label.endswith(".fixture") and domain != module._launchd_domains()[1] else 113
        ),
        launch_inventory=lambda: {"com.colingreig.hermes.fixture"},
    )

    # The run completes normally (no ExpatError raised) and the fixture's
    # declared contracts are still unaffected by the unrelated bad plist.
    assert findings == []
    warning = next(item for item in evidence if item.get("code") == "plist_unreadable")
    assert warning["surface"] == "launchd"
    assert warning["id"] == "com.hermes.opendesign.plist"
    assert "com.hermes.opendesign.plist" in warning["detail"]
    # A skipped-plist warning must never masquerade as a checked contract
    # (those use "checked"/"disabled-as-declared"/"retired-as-declared").
    assert warning.get("status") is None


def test_route_alarm_timeout_expired_maps_to_delivery_failed_not_crash(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "one", "failed", "synthetic")]

    def hanging_sender(_message):
        raise subprocess.TimeoutExpired(cmd=["hermes", "send"], timeout=30)

    result = module.route_alarm(
        findings,
        state_path=state_path,
        now=NOW,
        drill=True,
        real_alert=True,
        sender=hanging_sender,
    )
    assert result["action"] == "delivery-failed"
    assert "timed out" in result["error"].lower()
    state = json.loads(state_path.read_text())
    assert state["pending_incident_id"] == result["incident_id"]
    assert state.get("active") is not True
    assert "delivered_signature" not in state


def test_route_alarm_recovery_timeout_expired_maps_to_recovery_delivery_failed(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "active": True,
                "delivered_signature": "abc123",
                "clean_since": (NOW - timedelta(minutes=5)).timestamp(),
            }
        ),
        encoding="utf-8",
    )

    def hanging_sender(_message):
        raise subprocess.TimeoutExpired(cmd=["hermes", "send"], timeout=30)

    result = module.route_alarm(
        [],
        state_path=state_path,
        now=NOW,
        drill=False,
        real_alert=True,
        sender=hanging_sender,
    )
    assert result["action"] == "recovery-delivery-failed"
    assert "timed out" in result["error"].lower()


def _stub_launchctl_print(cmd, *, loaded_label):
    """Deterministic stand-in for real `launchctl print`, matching _fixture()'s
    LaunchAgent contracts (``<loaded_label>`` loaded only in the gui domain,
    everything else absent) so main()-level tests never touch the real
    launchd session."""
    target = cmd[2]
    parts = target.split("/")
    if len(parts) == 2:  # domain-only inventory call, e.g. "gui/501"
        domain_kind = parts[0]
        if domain_kind == "gui":
            stdout = f"\n\tservices = {{\n\t\t1234\t0\t{loaded_label}\n\t}}\n"
        else:
            stdout = "\n\tservices = {\n\n\t}\n"
        return _completed(0, stdout=stdout)
    domain_kind, _uid, label = parts  # per-label registration check
    if label == loaded_label and domain_kind == "gui":
        return _completed(0)
    return _completed(113, stderr="Could not find service")


def test_main_writes_receipt_and_holds_exit_code_when_alarm_send_times_out(tmp_path, monkeypatch):
    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, output = _fixture(tmp_path)
    output.write_text("# Cron Job\nsemantic-failure\n", encoding="utf-8")
    import os

    os.utime(output, (NOW.timestamp(), NOW.timestamp()))

    contracts_path = tmp_path / "contracts.json"
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")
    state_path = tmp_path / "state.json"
    receipt_path = tmp_path / "receipt.json"

    def flaky_run(cmd, *args, **kwargs):
        if cmd and str(cmd[0]) == str(module.HERMES_BIN):
            raise module.subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        if cmd and cmd[0] == "launchctl":
            return _stub_launchctl_print(cmd, loaded_label="com.colingreig.hermes.fixture")
        raise AssertionError(f"unexpected subprocess call in test: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", flaky_run)
    monkeypatch.setattr(module, "_now", lambda: NOW)

    exit_code = module.main(
        [
            "--contracts",
            str(contracts_path),
            "--jobs",
            str(jobs_path),
            "--output-root",
            str(output_root),
            "--launch-agents-dir",
            str(launch_agents),
            "--state",
            str(state_path),
            "--receipt",
            str(receipt_path),
        ]
    )

    assert exit_code == 1
    state = json.loads(state_path.read_text())
    assert state.get("pending_incident_id")
    assert state.get("active") is not True
    assert "delivered_signature" not in state
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["finding_count"] >= 1
    assert receipt["alarm"]["action"] == "delivery-failed"
    assert "timed out" in receipt["alarm"]["error"].lower()


def test_main_clean_run_writes_clean_receipt_with_no_delivery_attempt(tmp_path, monkeypatch):
    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, _output = _fixture(tmp_path)

    contracts_path = tmp_path / "contracts.json"
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")
    state_path = tmp_path / "state.json"
    receipt_path = tmp_path / "receipt.json"

    def launchctl_only_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "launchctl":
            return _stub_launchctl_print(cmd, loaded_label="com.colingreig.hermes.fixture")
        raise AssertionError(f"unexpected subprocess call in test: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", launchctl_only_run)
    monkeypatch.setattr(module, "_now", lambda: NOW)

    exit_code = module.main(
        [
            "--contracts",
            str(contracts_path),
            "--jobs",
            str(jobs_path),
            "--output-root",
            str(output_root),
            "--launch-agents-dir",
            str(launch_agents),
            "--state",
            str(state_path),
            "--receipt",
            str(receipt_path),
        ]
    )

    assert exit_code == 0
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "clean"
    assert receipt["finding_count"] == 0
    assert receipt["alarm"]["action"] == "clean"


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
    failed_state = json.loads(state_path.read_text())
    assert failed_state.get("active") is not True
    assert "delivered_signature" not in failed_state

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

    pending_recovery = module.route_alarm(
        [],
        state_path=state_path,
        now=NOW + timedelta(minutes=2),
        drill=False,
        real_alert=True,
        sender=sender,
    )
    recovered = module.route_alarm(
        [],
        state_path=state_path,
        now=NOW + timedelta(minutes=7),
        drill=False,
        real_alert=True,
        sender=sender,
    )
    repeated = module.route_alarm(
        findings,
        state_path=state_path,
        now=NOW + timedelta(minutes=8),
        drill=False,
        real_alert=True,
        sender=sender,
    )
    assert pending_recovery["action"] == "recovery-pending"
    assert recovered["action"] == "recovery-sent"
    assert repeated["action"] == "sent"
    assert len(messages) == 3


def test_release_cut_boundary_requires_service_affecting_valid_receipt(tmp_path):
    module = _load_module()
    receipt_path = tmp_path / "receipt.json"
    valid = {
        "schema_version": 2,
        "event": "advanced",
        "to_commit": "a" * 40,
        "runtime_target": "/Users/example/.hermes/releases/v1",
    }
    receipt_path.write_text(json.dumps(valid), encoding="utf-8")
    observed = module._release_cut_observed_at(receipt_path)
    assert observed is not None

    receipt_path.write_text(json.dumps({**valid, "event": "cut"}), encoding="utf-8")
    assert module._release_cut_observed_at(receipt_path) is not None

    for event in ("noop", "rejected"):
        receipt_path.write_text(json.dumps({**valid, "event": event}), encoding="utf-8")
        assert module._release_cut_observed_at(receipt_path) is None
    receipt_path.write_text("{not-json", encoding="utf-8")
    assert module._release_cut_observed_at(receipt_path) is None
    receipt_path.unlink()
    assert module._release_cut_observed_at(receipt_path) is None

    receipt_path.write_text(json.dumps({**valid, "event": "rollback"}), encoding="utf-8")
    assert module._release_cut_observed_at(receipt_path) is not None


def test_initial_delivery_retry_keeps_pending_incident_identity(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "one", "failed", "synthetic")]

    failed = module.route_alarm(
        findings, state_path=state_path, now=NOW, drill=False,
        real_alert=True, sender=lambda _message: _completed(1, stderr="offline"),
    )
    sent = module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(minutes=1),
        drill=False, real_alert=True, sender=lambda _message: _completed(),
    )
    assert failed["action"] == "delivery-failed"
    assert sent["action"] == "sent"
    assert failed["incident_id"] == sent["incident_id"]
    assert sent["incident_id"] == json.loads(state_path.read_text())["incident_id"]


def test_alarm_identity_dedupes_code_churn_but_pages_persistent_new_contract(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    initial = [module._finding("cron", "one", "stale", "old")]
    changed_code = [module._finding("cron", "one", "failure_marker", "failed")]
    with_new_contract = [
        *changed_code,
        module._finding("launchd", "two", "http_failed", "offline"),
    ]

    sent = module.route_alarm(
        initial, state_path=state_path, now=NOW, drill=False,
        real_alert=True, sender=sender,
    )
    deduped = module.route_alarm(
        changed_code, state_path=state_path, now=NOW + timedelta(minutes=1),
        drill=False, real_alert=True, sender=sender,
    )
    pending = module.route_alarm(
        with_new_contract, state_path=state_path, now=NOW + timedelta(minutes=2),
        drill=False, real_alert=True, sender=sender,
    )
    persistent = module.route_alarm(
        with_new_contract, state_path=state_path, now=NOW + timedelta(minutes=7),
        drill=False, real_alert=True, sender=sender,
    )

    assert sent["action"] == "sent"
    assert deduped["action"] == "deduped"
    assert pending["action"] == "new-finding-pending"
    assert persistent["action"] == "sent"
    assert len({item["incident_id"] for item in (sent, deduped, pending, persistent)}) == 1
    assert len(messages) == 2


def test_staggered_new_findings_are_confirmed_individually(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    base = module._finding("cron", "base", "failed", "base failed")
    finding_a = module._finding("launchd", "two", "http_failed", "A failed")
    finding_b = module._finding("launchd", "three", "http_failed", "B transient")
    module.route_alarm(
        [base], state_path=state_path, now=NOW, drill=False,
        real_alert=True, sender=sender,
    )
    module.route_alarm(
        [base, finding_a], state_path=state_path, now=NOW + timedelta(minutes=1),
        drill=False, real_alert=True, sender=sender,
    )
    first_update = module.route_alarm(
        [base, finding_a, finding_b], state_path=state_path,
        now=NOW + timedelta(minutes=6), drill=False,
        real_alert=True, sender=sender,
    )
    state = json.loads(state_path.read_text())
    assert first_update["action"] == "sent"
    assert "launchd three" not in messages[-1]
    assert "launchd:three" not in state["delivered_finding_identities"]
    assert "launchd:three" in state["pending_new_findings"]

    second_update = module.route_alarm(
        [base, finding_a, finding_b], state_path=state_path,
        now=NOW + timedelta(minutes=11), drill=False,
        real_alert=True, sender=sender,
    )
    assert second_update["action"] == "sent"
    assert "launchd three" in messages[-1]


def test_old_active_state_migrates_without_upgrade_page(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "active": True,
                "delivered_signature": "legacy-signature",
                "last_alert_at": NOW.timestamp(),
            }
        ),
        encoding="utf-8",
    )
    result = module.route_alarm(
        [module._finding("cron", "one", "failed", "still failed")],
        state_path=state_path, now=NOW + timedelta(minutes=1), drill=False,
        real_alert=True, sender=lambda _message: pytest.fail("must not page on migration"),
    )
    state = json.loads(state_path.read_text())
    assert result["action"] == "deduped"
    assert result["incident_id"] == "legacy-signature"
    assert state["delivered_finding_identities"] == ["cron:one"]


def test_dry_run_does_not_mutate_alarm_state(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    result = module.route_alarm(
        [module._finding("cron", "one", "failed", "failed")],
        state_path=state_path, now=NOW, drill=False, real_alert=False,
        emit_dry_run=False,
    )
    assert result["action"] == "dry-run"
    assert not state_path.exists()


def test_recovery_delivery_retries_once_then_stays_clean(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "one", "failed", "failed")]
    module.route_alarm(
        findings, state_path=state_path, now=NOW, drill=False,
        real_alert=True, sender=lambda _message: _completed(),
    )
    assert module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=1), drill=False,
        real_alert=True, sender=lambda _message: _completed(),
    )["action"] == "recovery-pending"
    failed = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=6), drill=False,
        real_alert=True, sender=lambda _message: _completed(1, stderr="offline"),
    )
    recovered = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=7), drill=False,
        real_alert=True, sender=lambda _message: _completed(),
    )
    repeated = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=8), drill=False,
        real_alert=True, sender=lambda _message: pytest.fail("must not send twice"),
    )
    assert failed["action"] == "recovery-delivery-failed"
    assert recovered["action"] == "recovery-sent"
    assert repeated["action"] == "clean"


def test_reappearing_finding_resets_clean_confirmation(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "one", "failed", "failed")]
    sender = lambda _message: _completed()
    module.route_alarm(
        findings, state_path=state_path, now=NOW, drill=False,
        real_alert=True, sender=sender,
    )
    assert module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=1), drill=False,
        real_alert=True, sender=sender,
    )["action"] == "recovery-pending"
    assert module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(minutes=2),
        drill=False, real_alert=True, sender=sender,
    )["action"] == "deduped"
    assert module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=5), drill=False,
        real_alert=True, sender=sender,
    )["action"] == "recovery-pending"


def test_cutover_grace_suppresses_transient_alarm_and_recovery(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("launchd", "gateway", "http_failed", "offline")]
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    suppressed = module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(minutes=1),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )
    cleared = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=6),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )
    assert suppressed["action"] == "cutover-suppressed"
    assert cleared["action"] == "clean"
    assert messages == []
    assert json.loads(state_path.read_text()).get("pending_incident_id") is None

    sent = module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(minutes=11),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )
    recovery_suppressed = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=12),
        cutover_at=NOW + timedelta(minutes=11), drill=False,
        real_alert=True, sender=sender,
    )
    assert sent["action"] == "sent"
    assert recovery_suppressed["action"] == "recovery-suppressed"
    assert len(messages) == 1


def test_finding_first_seen_near_cutover_end_still_requires_confirmation(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    finding_a = module._finding("launchd", "a", "http_failed", "A failed")
    finding_b = module._finding("launchd", "b", "http_failed", "B failed")
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    assert module.route_alarm(
        [finding_a], state_path=state_path, now=NOW + timedelta(minutes=1),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )["action"] == "cutover-suppressed"
    assert module.route_alarm(
        [finding_b], state_path=state_path, now=NOW + timedelta(minutes=9),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )["action"] == "cutover-suppressed"
    assert module.route_alarm(
        [finding_b], state_path=state_path, now=NOW + timedelta(minutes=11),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )["action"] == "new-finding-pending"
    assert module.route_alarm(
        [finding_b], state_path=state_path, now=NOW + timedelta(minutes=14),
        cutover_at=NOW, drill=False, real_alert=True, sender=sender,
    )["action"] == "sent"
    assert len(messages) == 1


def test_canonical_contract_inventory_covers_exact_jobs_and_semantic_outcomes():
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    jobs = json.loads(JOBS_PATH.read_text(encoding="utf-8"))["jobs"]
    assert {item["id"] for item in contracts["cron_jobs"]} == {
        item["id"] for item in jobs
    }
    declared_launch_labels = {item["label"] for item in contracts["launch_agents"]}
    loaded_labels = {
        item["label"] for item in contracts["launch_agents"]
        if item["expected"] == "loaded"
    }
    retired_labels = {
        item["label"] for item in contracts["launch_agents"]
        if item["expected"] == "retired"
    }
    assert loaded_labels | retired_labels == declared_launch_labels
    assert loaded_labels.isdisjoint(retired_labels)
    required_states = {
        "com.colingreig.hermes.usage-alert": "loaded",
        "com.colingreig.hermes.fleet-outcome-probe": "loaded",
        "com.colingreig.hermes.disk-space-alert": "loaded",
        "com.colingreig.hermes.kanban-workspace-sweep": "loaded",
        "com.colingreig.hermes.release-poll": "loaded",
    }
    declared_states = {
        item["label"]: item["expected"] for item in contracts["launch_agents"]
    }
    assert {label: declared_states[label] for label in required_states} == required_states
    assert retired_labels == set()

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


def test_clickup_refresh_receipt_requires_success_and_matching_map(tmp_path):
    module = _load_module()
    map_path = tmp_path / "clickup-map.json"
    map_path.write_text('{"lists": []}\n', encoding="utf-8")
    import hashlib
    import os

    receipt_path = tmp_path / "clickup-map.receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "outcome": "success",
                "written_at": NOW.isoformat(),
                "map_sha256": hashlib.sha256(map_path.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    os.utime(receipt_path, (NOW.timestamp(), NOW.timestamp()))
    outcome = {
        "kind": "json_artifact",
        "max_age_seconds": 60,
        "timestamp_key": "written_at",
        "required_values": {"schema_version": 3, "outcome": "success"},
        "linked_artifact": {"path": str(map_path), "sha256_key": "map_sha256"},
        "success_patterns": ['"outcome"\\s*:\\s*"success"'],
        "failure_patterns": ['"outcome"\\s*:\\s*"failed"'],
    }

    assert module._check_artifact(
        surface="cron", identifier="clickup", outcome={**outcome, "path": str(receipt_path)}, home=tmp_path, now=NOW
    ) == []

    receipt_path.write_text(
        json.dumps({**json.loads(receipt_path.read_text()), "outcome": "failed"}),
        encoding="utf-8",
    )
    os.utime(receipt_path, (NOW.timestamp(), NOW.timestamp()))
    codes = {
        item["code"]
        for item in module._check_artifact(
            surface="cron", identifier="clickup", outcome={**outcome, "path": str(receipt_path)}, home=tmp_path, now=NOW
        )
    }
    assert {"failure_marker", "success_marker_missing"} <= codes


def test_disk_space_contract_requires_fresh_ok_receipt(tmp_path):
    module = _load_module()
    label = "com.colingreig.hermes.disk-space-alert"
    outcome = _canonical_contract(launch_label=label)["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        json.dumps(
            {"checked_at": NOW.isoformat(), "status": "ok", "delivery": "n/a"}
        ),
    )
    checked_outcome = {**outcome, "path": str(artifact)}

    assert module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    ) == []

    import os

    for status in ("low", "check_error"):
        artifact.write_text(
            json.dumps(
                {
                    "checked_at": NOW.isoformat(),
                    "status": status,
                    "delivery": "n/a",
                }
            ),
            encoding="utf-8",
        )
        os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
        findings = module._check_artifact(
            surface="launchd",
            identifier=label,
            outcome=checked_outcome,
            home=tmp_path,
            now=NOW,
        )
        assert {(item["id"], item["code"]) for item in findings} == {
            (label, "failure_marker"),
            (label, "success_marker_missing"),
        }

    artifact.write_text(
        json.dumps(
            {
                "checked_at": NOW.isoformat(),
                "status": "ok",
                "delivery": "failed",
            }
        ),
        encoding="utf-8",
    )
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    )
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "failure_marker")
    }

    stale_timestamp = NOW.timestamp() - outcome["max_age_seconds"] - 1
    os.utime(artifact, (stale_timestamp, stale_timestamp))
    findings = module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    )
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "evidence_stale")
    }


def test_release_poll_contract_requires_fresh_heartbeat(tmp_path):
    """ClickUp 86e2ky37p: the release-poll liveness contract must alarm on a
    genuinely unloaded or non-executing poller, but stay silent on the
    common, healthy no-op case of "ran, found nothing new to cut" (which
    produces no distinguishing text beyond the unconditional heartbeat
    line)."""
    module = _load_module()
    label = "com.colingreig.hermes.release-poll"
    contract = _canonical_contract(launch_label=label)
    assert contract["expected"] == "loaded"
    outcome = contract["outcome"]

    import os

    heartbeat = _fresh_artifact(
        tmp_path, "mini-release-poll: heartbeat ts=2026-08-03T12:00:00Z pid=123\n"
    )
    checked_outcome = {**outcome, "path": str(heartbeat)}

    # Loaded, and heartbeat present and fresh: healthy, no findings.
    assert (
        module._check_artifact(
            surface="launchd",
            identifier=label,
            outcome=checked_outcome,
            home=tmp_path,
            now=NOW,
        )
        == []
    )

    # A no-op poll (nothing to cut) writes nothing beyond the heartbeat.
    # Still healthy -- the contract never requires a cut to have happened.
    assert (
        module._check_artifact(
            surface="launchd",
            identifier=label,
            outcome=checked_outcome,
            home=tmp_path,
            now=NOW,
        )
        == []
    )

    # Log exists but has gone stale (poller loaded yet not actually executing,
    # or a heartbeat older than 2x StartInterval): alarm distinguishably.
    stale_timestamp = NOW.timestamp() - outcome["max_age_seconds"] - 1
    os.utime(heartbeat, (stale_timestamp, stale_timestamp))
    findings = module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    )
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "evidence_stale")
    }

    # Log is fresh but missing the heartbeat marker entirely (unexpected
    # content): alarm rather than silently pass. The heartbeat doubles as this
    # contract's run-start marker (ClickUp 86e2kt3yr), so its absence is
    # reported as a missing run boundary -- equally distinguishable, and one
    # code instead of two for the same underlying condition.
    os.utime(heartbeat, (NOW.timestamp(), NOW.timestamp()))
    heartbeat.write_text("some unrelated line\n", encoding="utf-8")
    os.utime(heartbeat, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"run_boundary_missing"}

    # Structurally absent (unloaded): the launchd-registration check itself
    # must alarm, independent of any log content.
    launch_findings, _ = module._check_launch_contracts(
        [contract],
        launch_agents_dir=tmp_path / "LaunchAgents",
        home=tmp_path,
        now=NOW,
        launchctl=lambda _domain, _label: _completed(returncode=113, stderr="not found"),
        loaded_inventory=set(),
    )
    assert {(item["id"], item["code"]) for item in launch_findings} == {
        (label, "agent_not_loaded")
    }


def test_release_poll_corrupt_pointer_alarms_distinguishably_and_clears(tmp_path):
    """ClickUp 86e2kt3yr: a corrupt runtime-current pointer must alarm as
    itself, and the alarm must be satisfiable.

    The poller fails closed on a corrupt pointer with a stable, greppable
    prefix. Two things have to hold together:

    * the corrupt line must produce a ``failure_marker``, not the generic
      liveness silence a broken poller would otherwise look like; and
    * once the pointer is repaired, the very next poll must clear it. The log
      is append-only and the contract keeps a 4000-line tail, so without a run
      boundary a single one-minute incident would hold this alarm red for
      weeks -- the unsatisfiable-alarm failure mode. The heartbeat, printed
      unconditionally as the first line of every invocation, is therefore the
      run-start marker, which scopes evaluation to the latest poll.
    """
    module = _load_module()
    label = "com.colingreig.hermes.release-poll"
    outcome = _canonical_contract(launch_label=label)["outcome"]
    assert outcome["run_start_pattern"] == "^mini-release-poll: heartbeat "

    healthy = "mini-release-poll: heartbeat ts=2026-08-03T12:00:00Z pid=123\n"
    corrupt = (
        "mini-release-poll: heartbeat ts=2026-08-03T12:15:00Z pid=124\n"
        "mini-release-poll: runtime-current pointer corrupt: "
        "relative symlink (must be absolute): -> v0.18.2-aae777c146da\n"
    )

    log = _fresh_artifact(tmp_path, healthy + corrupt)
    checked_outcome = {**outcome, "path": str(log)}
    findings = module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    )
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "failure_marker")
    }

    # Pointer repaired: the next poll appends a clean run and the alarm clears
    # even though the corrupt line is still in the tail.
    log.write_text(healthy + corrupt + healthy, encoding="utf-8")
    import os

    os.utime(log, (NOW.timestamp(), NOW.timestamp()))
    assert (
        module._check_artifact(
            surface="launchd",
            identifier=label,
            outcome=checked_outcome,
            home=tmp_path,
            now=NOW,
        )
        == []
    )


def test_kanban_sweep_contract_requires_complete_clean_summary(tmp_path):
    module = _load_module()
    label = "com.colingreig.hermes.kanban-workspace-sweep"
    outcome = _canonical_contract(launch_label=label)["outcome"]
    full_summary = (
        "sweep-finish root=/tmp/hermes boards_swept=2 removed=0 "
        "orphan_removed=0 removed_bytes=0 removed_size=0B skipped_active=0 "
        "skipped_non_scratch=0 skipped_path_mismatch=0 skipped_recent=0 "
        "errors=0 days=14 dry_run=False\n"
    )
    artifact = _fresh_artifact(tmp_path, full_summary)

    def check():
        return module._check_text_evidence(
            surface="launchd",
            identifier=label,
            path=artifact,
            outcome=outcome,
            now=NOW,
        )

    def rewrite(text):
        artifact.write_text(text, encoding="utf-8")
        import os

        os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))

    assert check() == []

    rewrite(full_summary.replace("root=/tmp/hermes", "root=/tmp/errors=7/Error:"))
    assert check() == []

    rewrite(full_summary.replace("dry_run=False", "dry_run=True"))
    findings = check()
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "required_marker_missing"),
        (label, "success_marker_missing"),
    }

    rewrite(full_summary.replace("errors=0", "errors=1"))
    findings = check()
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "failure_marker"),
        (label, "required_marker_missing"),
        (label, "success_marker_missing"),
    }

    rewrite(full_summary + "next sweep still running\n")
    findings = check()
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "run_incomplete")
    }

    rewrite(full_summary + "Traceback (most recent call last):\ncrash\n")
    findings = check()
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "failure_marker"),
        (label, "run_incomplete"),
    }

    rewrite("sweep-start root=/tmp/hermes\n")
    findings = check()
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "run_boundary_missing")
    }

    for marker in (
        "BOARD_SKIP_NO_DB",
        "BOARD_SKIP_UNLISTABLE",
        "BOARD_SKIP_TASK_LOOKUP_ERROR",
        "BOARD_DISCOVERY_UNLISTABLE",
    ):
        rewrite(f"{marker}: content\n{full_summary}")
        findings = check()
        assert {(item["id"], item["code"]) for item in findings} == {
            (label, "failure_marker")
        }


def test_sentinel_contract_uses_structured_terminal_errors_not_issue_titles(tmp_path):
    module = _load_module()
    label = "com.colingreig.hermes.ignite-sentinel"
    outcome = _canonical_contract(launch_label=label)["outcome"]
    start = "2026-08-01T15:06:07Z sentinel_run: secrets resolved, running monitor.py\n"
    artifact = _fresh_artifact(
        tmp_path,
        start
        + json.dumps(
            {
                "mode": "live",
                "verdicts": [
                    {
                        "title": "Error: embedded application issue title",
                        "reason": "Sentry GET diagnostic failed inside issue prose",
                    }
                ],
                "errors": [],
            },
            indent=2,
        )
        + "\n",
    )

    def check():
        return module._check_text_evidence(
            surface="launchd",
            identifier=label,
            path=artifact,
            outcome=outcome,
            now=NOW,
        )

    assert check() == []

    payload = {
        "mode": "live",
        "verdicts": [],
        "errors": [{"stage": "handle", "message": "ClickUp list deleted"}],
    }
    artifact.write_text(start + json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    import os

    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = check()
    assert {(item["id"], item["code"]) for item in findings} == {
        (label, "failure_marker"),
        (label, "required_marker_missing"),
    }


def test_response_only_canonical_failed_artifact_reports_failure_marker(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="content-lane-executor")["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """# Cron Job: content-lane-executor (FAILED)

**Job ID:** dcab830aa41c
**Run Time:** 2026-08-01 11:06:46

## Prompt

/ignite-execute --lane content

## Error

```
RuntimeError: out of extra usage
```
""",
    )
    findings = module._check_text_evidence(
        surface="cron",
        identifier="content-lane-executor",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    codes = {item["code"] for item in findings}
    assert "failure_marker" in codes
    assert "response_section_missing" not in codes


def test_response_only_success_ignores_failure_text_in_prompt(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="content-lane-executor")["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """# Cron Job: content-lane-executor

## Prompt

(FAILED) from an old skill example — not this run's outcome

## Response

No actionable tasks. swarm complete.
""",
    )
    assert (
        module._check_text_evidence(
            surface="cron",
            identifier="content-lane-executor",
            path=artifact,
            outcome=outcome,
            now=NOW,
        )
        == []
    )


def test_response_only_malformed_artifact_reports_response_section_missing(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="content-lane-executor")["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """# Cron Job: content-lane-executor

## Prompt

Some prompt with no response or scheduler error section.
""",
    )
    findings = module._check_text_evidence(
        surface="cron",
        identifier="content-lane-executor",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"response_section_missing"}


def test_response_only_failed_header_without_error_reports_response_section_missing(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="content-lane-executor")["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """# Cron Job: content-lane-executor (FAILED)

**Job ID:** dcab830aa41c
**Run Time:** 2026-08-01 11:06:46

## Prompt

/ignite-execute --lane content
""",
    )
    findings = module._check_text_evidence(
        surface="cron",
        identifier="content-lane-executor",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"response_section_missing"}


def test_launchctl_inventory_returns_partial_results_when_one_domain_fails():
    module = _load_module()
    gui_domain, user_domain = module._launchd_domains()

    def fake_inventory(domain: str) -> set[str]:
        if domain == gui_domain:
            raise module.ProbeError("gui inventory unavailable")
        return {"com.colingreig.hermes.gateway"}

    with mock.patch.object(module, "_launch_domain_inventory", side_effect=fake_inventory):
        assert module._launchctl_inventory() == {"com.colingreig.hermes.gateway"}


def test_launchctl_inventory_raises_when_both_domains_fail():
    module = _load_module()

    def fake_inventory(_domain: str) -> set[str]:
        raise module.ProbeError("inventory unavailable")

    with mock.patch.object(module, "_launch_domain_inventory", side_effect=fake_inventory):
        with pytest.raises(module.ProbeError, match="inventory unavailable"):
            module._launchctl_inventory()


def test_launch_agent_registration_accepts_gui_or_user_domain():
    module = _load_module()
    gui_domain, user_domain = module._launchd_domains()
    label = "ai.hermes.gateway"

    def launchctl(domain, _label):
        return _completed(0 if domain == user_domain else 113)

    loaded_domain, _, _ = module._launch_agent_registration(label, launchctl=launchctl)
    assert loaded_domain == user_domain

    def launchctl_gui(domain, _label):
        return _completed(0 if domain == gui_domain else 113)

    loaded_domain, _, _ = module._launch_agent_registration(label, launchctl=launchctl_gui)
    assert loaded_domain == gui_domain


def test_launch_agent_registration_reports_duplicate_domains():
    module = _load_module()

    def launchctl(_domain, _label):
        return _completed(0)

    loaded_domain, gui_result, user_result = module._launch_agent_registration(
        "ai.hermes.gateway",
        launchctl=launchctl,
    )
    assert loaded_domain is None
    assert gui_result.returncode == 0
    assert user_result.returncode == 0


def test_check_launch_contracts_flags_duplicate_domain_registration(tmp_path):
    module = _load_module()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    contract = {
        "label": "ai.hermes.gateway",
        "expected": "loaded",
        "outcome": {"kind": "self"},
    }

    def launchctl(_domain, _label):
        return _completed(0)

    findings, evidence = module._check_launch_contracts(
        [contract],
        launch_agents_dir=launch_agents,
        home=tmp_path,
        now=NOW,
        launchctl=launchctl,
        loaded_inventory=set(),
    )
    assert {(item["id"], item["code"]) for item in findings} == {
        ("ai.hermes.gateway", "duplicate_domain_registration"),
    }
    assert evidence == []


def test_check_launch_contracts_accepts_user_domain_only_registration(tmp_path):
    module = _load_module()
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    with (launch_agents / "ai.hermes.gateway.plist").open("wb") as handle:
        plistlib.dump({"Label": "ai.hermes.gateway"}, handle)
    contract = {
        "label": "ai.hermes.gateway",
        "expected": "loaded",
        "outcome": {"kind": "self"},
    }
    user_domain = module._launchd_domains()[1]

    def launchctl(domain, _label):
        return _completed(0 if domain == user_domain else 113)

    findings, evidence = module._check_launch_contracts(
        [contract],
        launch_agents_dir=launch_agents,
        home=tmp_path,
        now=NOW,
        launchctl=launchctl,
        loaded_inventory=set(),
    )
    assert findings == []
    assert evidence == [
        {
            "surface": "launchd",
            "id": "ai.hermes.gateway",
            "status": "checked",
            "outcome_kind": "self",
            "domain": user_domain,
        }
    ]


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

    artifact.write_text(
        "RUN_COMPLETE\ncurrent work\nRUN_COMPLETE\nPARTIAL_FAILURE next run\n",
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
    assert {item["code"] for item in findings} == {"failure_marker", "run_incomplete"}

    artifact.write_text(
        "RUN_COMPLETE\ncurrent work\nRUN_COMPLETE\nnext run still working\n",
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
    assert {item["code"] for item in findings} == {"run_incomplete"}


def test_board_sync_distinguishes_empty_and_nonempty_structured_failures(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="ignite-board-sync nightly scheduled run")[
        "outcome"
    ]

    green = _fresh_artifact(
        tmp_path,
        'sync complete\n{"failures": []}\nThe failure summary is intentionally empty.\n',
    )
    assert module._check_text_evidence(
        surface="cron",
        identifier="board-sync",
        path=green,
        outcome=outcome,
        now=NOW,
    ) == []

    green.write_text(
        'sync complete\n{"failures": [{"task": "86e", "reason": "missing heading"}]}\n',
        encoding="utf-8",
    )
    import os

    os.utime(green, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="cron",
        identifier="board-sync",
        path=green,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"failure_marker"}

    green.write_text("sync complete\nFAILED: board refresh aborted\n", encoding="utf-8")
    os.utime(green, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="cron",
        identifier="board-sync",
        path=green,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"failure_marker"}


def test_gateway_contract_requires_bounded_healthy_http_response(monkeypatch):
    module = _load_module()
    outcome = _canonical_contract(launch_label="ai.hermes.gateway")["outcome"]

    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self, _limit):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda url, timeout: Response(b'{"status":"ok","platform":"hermes-agent"}'),
    )
    assert module._check_endpoint(
        surface="launchd",
        identifier="ai.hermes.gateway",
        outcome=outcome,
    ) == []

    for body in (
        b'{"status":"unhealthy","platform":"hermes-agent"}',
        b'{"status":"ok"}',
        b"not-json",
        b'{"status":"ok","platform":"hermes-agent" trailing garbage',
    ):
        monkeypatch.setattr(
            module.urllib.request,
            "urlopen",
            lambda url, timeout, body=body: Response(body),
        )
        findings = module._check_endpoint(
            surface="launchd",
            identifier="ai.hermes.gateway",
            outcome=outcome,
        )
        assert findings

    def closed_listener(_url, timeout):
        raise module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(module.urllib.request, "urlopen", closed_listener)
    findings = module._check_endpoint(
        surface="launchd",
        identifier="ai.hermes.gateway",
        outcome=outcome,
    )
    assert {item["code"] for item in findings} == {"endpoint_failed"}


def test_runtime_cleanup_uses_full_completion_boundary_and_rejects_trailing_run(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(
        launch_label="com.colingreig.hermes.runtime-artifact-cleanup"
    )["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """remove-failed historical
[2026-07-29] complete dry_run=False
current cleanup
[2026-07-30] complete dry_run=False
""",
    )
    assert module._check_text_evidence(
        surface="launchd",
        identifier="runtime-cleanup",
        path=artifact,
        outcome=outcome,
        now=NOW,
    ) == []

    artifact.write_text(
        """[2026-07-29] complete dry_run=False
[2026-07-30] complete dry_run=False
remove-failed current run
""",
        encoding="utf-8",
    )
    import os

    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="launchd",
        identifier="runtime-cleanup",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"failure_marker", "run_incomplete"}

    artifact.write_text("[2026-07-30] complete dry_run=True\n", encoding="utf-8")
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="launchd",
        identifier="runtime-cleanup",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"success_marker_missing"}

def test_clickup_lifecycle_accepts_human_headings_but_requires_all_sections(tmp_path):
    module = _load_module()
    outcome = _canonical_contract(cron_name="clickup-lifecycle")["outcome"]
    artifact = _fresh_artifact(
        tmp_path,
        """# Cron Job
## Response
## Closeout Audit
Reviewed 0 tasks.
## Stalled Task Reconciler
Reviewed 0 tasks.
## Staleness Sweep
Reviewed 0 tasks.
## Drift Reconciliation
Reviewed 0 tasks.
""",
    )
    assert module._check_text_evidence(
        surface="cron",
        identifier="clickup-lifecycle",
        path=artifact,
        outcome=outcome,
        now=NOW,
    ) == []

    artifact.write_text(
        """# Cron Job
## Response
Complete and clean.
## Closeout Audit
## Stalled Task Reconciler
## Staleness Sweep
""",
        encoding="utf-8",
    )
    import os

    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="cron",
        identifier="clickup-lifecycle",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {"required_marker_missing"}

    artifact.write_text(
        """# Cron Job
## Response
(FAILED)
## Closeout Audit
## Stalled Task Reconciler
## Staleness Sweep
## Drift Reconciliation
""",
        encoding="utf-8",
    )
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="cron",
        identifier="clickup-lifecycle",
        path=artifact,
        outcome=outcome,
        now=NOW,
    )
    assert "failure_marker" in {item["code"] for item in findings}


def test_restic_and_mcp_contracts_require_clean_completed_runs(tmp_path):
    module = _load_module()
    restic = _canonical_contract(launch_label="com.hermes.offbox-restic-backup")[
        "outcome"
    ]
    artifact = _fresh_artifact(
        tmp_path,
        """running restic backup for 2 target(s)
running retention (keep-daily 7 / keep-weekly 4 / keep-monthly 6)
backup and retention complete
""",
    )
    assert module._check_text_evidence(
        surface="launchd",
        identifier="restic",
        path=artifact,
        outcome=restic,
        now=NOW,
    ) == []

    artifact.write_text(
        """running restic backup for 2 target(s)
restic backup failed: exit 1
""",
        encoding="utf-8",
    )
    import os

    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    assert module._check_text_evidence(
        surface="launchd",
        identifier="restic",
        path=artifact,
        outcome=restic,
        now=NOW,
    )

    mcp = _canonical_contract(
        launch_label="com.colingreig.hermes.mcp-serve-reaper"
    )["outcome"]
    artifact.write_text(
        "sweep-start\nsweep-finish candidates=1 reaped=0 failed=0 dry_run=False\n",
        encoding="utf-8",
    )
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    assert module._check_text_evidence(
        surface="launchd",
        identifier="mcp",
        path=artifact,
        outcome=mcp,
        now=NOW,
    ) == []

    artifact.write_text(
        "sweep-start\nsweep-finish candidates=1 reaped=0 failed=1 dry_run=False\n",
        encoding="utf-8",
    )
    os.utime(artifact, (NOW.timestamp(), NOW.timestamp()))
    findings = module._check_text_evidence(
        surface="launchd",
        identifier="mcp",
        path=artifact,
        outcome=mcp,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {
        "failure_marker",
        "required_marker_missing",
        "success_marker_missing",
    }


def test_drill_trips_one_real_predicate_per_contract():
    module = _load_module()
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    findings = module._inject_contract_failures(contracts, now=NOW)
    expected = {
        *(("cron", item["id"]) for item in contracts["cron_jobs"]),
        *(("launchd", item["label"]) for item in contracts["launch_agents"]),
    }
    observed = {(item["surface"], item["id"]) for item in findings}
    assert observed == expected
    assert len(findings) == len(expected)
    assert all(item["code"] != "synthetic_outcome_failure" for item in findings)
    assert all(item["detail"].startswith("SYNTHETIC DRILL:") for item in findings)
    assert module.DEFAULT_DRILL_STATE != module.DEFAULT_STATE
    assert module.DEFAULT_DRILL_RECEIPT != module.DEFAULT_RECEIPT


def test_kanban_sweep_stderr_shares_canonical_semantic_evidence_log():
    contract = _canonical_contract(
        launch_label="com.colingreig.hermes.kanban-workspace-sweep"
    )
    plist_path = (
        SCRIPTS
        / "launchd"
        / "com.colingreig.hermes.kanban-workspace-sweep.plist"
    )
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)

    stdout_path = plist["StandardOutPath"]
    assert plist["StandardErrorPath"] == stdout_path
    # The plist is a real launchd config deployed to one fixed machine (the
    # mini, home /Users/colingreig) — it hardcodes that absolute path rather
    # than "~". Resolve the contract's "~"-relative path against that same
    # fixed, known deployment home instead of `Path.expanduser()`, which
    # resolves against whatever $HOME the test happens to run under (e.g.
    # /home/runner in CI) and would otherwise make this assertion pass or
    # fail based on the test runner's environment rather than the actual
    # deployed configuration.
    deployment_home = "/Users/colingreig"
    contract_path = contract["outcome"]["path"]
    if contract_path.startswith("~"):
        contract_path = deployment_home + contract_path[1:]
    assert Path(stdout_path) == Path(contract_path)
    assert any(
        pattern.startswith("^Traceback")
        for pattern in contract["outcome"]["failure_patterns"]
    )


def test_admission_health_covers_legacy_stale_owner_and_recovery(tmp_path):
    module = _load_module()
    database = tmp_path / "executor-admission.db"
    expired = (NOW - timedelta(minutes=1)).isoformat()
    recovered = (NOW - timedelta(minutes=5)).isoformat()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TABLE executor_lease (singleton INTEGER, ledger_execution_id TEXT, heartbeat_at TEXT, expires_at TEXT, state TEXT)"
        )
        conn.execute(
            "INSERT INTO executor_lease VALUES (1, 'legacy-ledger', ?, ?, 'active')",
            (expired, expired),
        )
        conn.execute(
            "CREATE TABLE recovery_receipts (receipt_id TEXT, recovered_at TEXT)"
        )
        conn.execute(
            "INSERT INTO recovery_receipts VALUES ('legacy-recovery', ?)",
            (recovered,),
        )

    findings, _evidence = module._check_operational_contracts(
        [{
            "id": "admission",
            "kind": "admission_health",
            "path": str(database),
            "recovery_alarm_seconds": 3600,
        }],
        home=tmp_path,
        now=NOW,
    )
    assert {item["code"] for item in findings} == {
        "stale_heartbeat",
        "owner_dead_recovery",
    }


def test_sqlite_health_reports_corruption_instead_of_crashing(tmp_path):
    module = _load_module()
    database = tmp_path / "corrupt.db"
    database.write_bytes(b"not a sqlite database")

    findings, _evidence = module._check_operational_contracts(
        [{"id": "db", "kind": "sqlite_health", "path": str(database)}],
        home=tmp_path,
        now=NOW,
    )
    assert [item["code"] for item in findings] == ["database_unreadable"]


def _canonical_operational_contract(op_id):
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    return next(item for item in contracts["operational_checks"] if item["id"] == op_id)


def test_release_poll_drift_pages_on_persistent_drift_not_transient(tmp_path):
    """86e2m61xw: a single 'remote prod SHA does not match governed control'
    line is a benign race (poller ticked mid-cutover); three across ~45 min
    of 15-min poller ticks is a genuinely stuck governed control that must
    not sit silent for hours."""
    module = _load_module()
    contract = dict(_canonical_operational_contract("release-poll-drift"))
    assert contract["threshold"] == 3
    log = tmp_path / "mini-release-poll.log"
    contract["path"] = str(log)

    drift_line = "mini-release-poll: remote prod SHA does not match governed control\n"
    heartbeat_line = "mini-release-poll: heartbeat ts=2026-08-03T12:00:00Z pid=1\n"

    # Two drift ticks: below threshold, stay quiet.
    log.write_text((heartbeat_line + drift_line) * 2, encoding="utf-8")
    findings, _evidence = module._check_operational_contracts(
        [contract], home=tmp_path, now=NOW
    )
    assert findings == []

    # Three drift ticks (~45 min of 15-min polls): alarm distinguishably.
    log.write_text((heartbeat_line + drift_line) * 3, encoding="utf-8")
    findings, _evidence = module._check_operational_contracts(
        [contract], home=tmp_path, now=NOW
    )
    assert [item["code"] for item in findings] == ["repeated_release_drift"]
    assert findings[0]["id"] == "release-poll-drift"


def test_release_poll_drift_recency_window_ignores_resolved_historical_incident(tmp_path):
    """86e2m2c1g finding 4: repeated_release_drift must not alarm forever on

    a resolved incident just because the matching lines are still inside the
    tail_bytes byte window (a low-volume poller log can hold days of history
    in 256KB). Old drift ticks from well outside window_minutes must not
    count, but a fresh sustained run must still alarm distinguishably."""
    module = _load_module()
    contract = dict(_canonical_operational_contract("release-poll-drift"))
    assert contract["window_minutes"] == 90
    assert contract["timestamp_pattern"]
    log = tmp_path / "mini-release-poll.log"
    contract["path"] = str(log)

    drift_line = "mini-release-poll: remote prod SHA does not match governed control\n"

    def heartbeat(ts):
        return f"mini-release-poll: heartbeat ts={ts} pid=1\n"

    # This morning's incident: 5 drift ticks, all well outside the 90-minute
    # recency window (and already resolved) -- must NOT alarm even though
    # they are still within the tail_bytes byte window.
    historical = "".join(
        heartbeat(f"2026-07-30T0{h}:00:00Z") + drift_line for h in range(3, 8)
    )
    # Recovery: subsequent healthy ticks with no drift line.
    recovered = "".join(
        heartbeat(f"2026-07-30T1{h}:00:00Z") for h in range(0, 5)
    )
    log.write_text(historical + recovered, encoding="utf-8")
    findings, evidence = module._check_operational_contracts(
        [contract], home=tmp_path, now=NOW
    )
    assert findings == []
    assert evidence[0]["matches"] == 0

    # Fresh sustained drift near "now" (NOW = 2026-07-30T16:00Z) must still
    # alarm distinguishably -- the recency window doesn't silence real
    # incidents, only stale ones.
    fresh = "".join(
        heartbeat(f"2026-07-30T15:{m:02d}:00Z") + drift_line for m in (30, 40, 50)
    )
    log.write_text(historical + recovered + fresh, encoding="utf-8")
    findings, evidence = module._check_operational_contracts(
        [contract], home=tmp_path, now=NOW
    )
    assert [item["code"] for item in findings] == ["repeated_release_drift"]
    assert findings[0]["id"] == "release-poll-drift"
    assert evidence[0]["matches"] == 3


def test_governed_manifest_resolves_launch_agents_from_live_plist_dir(tmp_path):
    """86e2m61xw (2a): governed-path-integrity resolved *every* manifest entry
    as path.parent/source, but LaunchAgents entries deploy from a staging
    source (launchd/foo.plist) to a live path under ~/Library/LaunchAgents
    named by "destination" -- those two are never siblings on disk. Scripts
    entries have no such split and must keep the staging-source check."""
    module = _load_module()
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()

    script_source = scripts_dir / "reconcile_fleet_outcomes.py"
    script_source.write_bytes(b"print('governed script')\n")
    script_sha = module.hashlib.sha256(script_source.read_bytes()).hexdigest()

    live_plist = launch_agents_dir / "com.colingreig.hermes.fleet-outcome-probe.plist"
    live_plist.write_bytes(b"<plist>live</plist>")
    plist_sha = module.hashlib.sha256(live_plist.read_bytes()).hexdigest()
    # The staging source (launchd/*.plist) legitimately differs from the
    # live deployed plist's bytes/path -- it must never be consulted for a
    # launch_agents entry.
    staging_plist_dir = scripts_dir / "launchd"
    staging_plist_dir.mkdir()
    (staging_plist_dir / "com.colingreig.hermes.fleet-outcome-probe.plist").write_bytes(
        b"<plist>staged, not live</plist>"
    )

    manifest_path = scripts_dir / "fleet_outcome_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "source": "reconcile_fleet_outcomes.py",
                        "destination_root": "scripts",
                        "destination": "reconcile_fleet_outcomes.py",
                        "sha256": script_sha,
                    },
                    {
                        "source": "launchd/com.colingreig.hermes.fleet-outcome-probe.plist",
                        "destination_root": "launch_agents",
                        "destination": "com.colingreig.hermes.fleet-outcome-probe.plist",
                        "sha256": plist_sha,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    contract = {
        "id": "governed-path-integrity",
        "kind": "governed_manifest",
        "path": str(manifest_path),
        "launch_agents_dir": str(launch_agents_dir),
    }
    findings, _evidence = module._check_operational_contracts(
        [contract], home=tmp_path, now=NOW
    )
    assert findings == []

    # Corrupt the live plist only: must alarm against the live LaunchAgents
    # copy, not the (unrelated) staging source.
    live_plist.write_bytes(b"<plist>tampered</plist>")
    findings, _evidence = module._check_operational_contracts(
        [contract], home=tmp_path, now=NOW
    )
    assert [item["code"] for item in findings] == ["governed_path_drift"]
    assert str(live_plist) in findings[0]["detail"]


def test_degraded_secrets_monitor_success_marker_survives_trailing_diagnostics(tmp_path):
    """86e2m61xw (2c): the probe keeps only the LAST tail line matching
    latest_record_pattern, and degraded_secrets_monitor.py prints its
    reduced-redundancy diagnostic line(s) AFTER the terminal status line on
    a healthy run. A loose latest_record_pattern let the diagnostic line win
    "last match", losing the real status line and false-paging
    success_marker_missing on an actually-healthy tick."""
    module = _load_module()
    label = "com.colingreig.hermes.degraded-secrets-monitor"
    outcome = _canonical_contract(launch_label=label)["outcome"]

    real_shaped_tail = (
        "[degraded-secrets-monitor] healthy\n"
        "[degraded-secrets-monitor] credential pool reduced redundancy: "
        "provider=anthropic usable=2/3 unavailable=backup:invalid\n"
    )
    artifact = _fresh_artifact(tmp_path, real_shaped_tail)
    checked_outcome = {**outcome, "path": str(artifact)}

    findings = module._check_artifact(
        surface="launchd",
        identifier=label,
        outcome=checked_outcome,
        home=tmp_path,
        now=NOW,
    )
    assert findings == []


def _run_main(
    module,
    monkeypatch,
    *,
    contracts_path,
    jobs_path,
    output_root,
    launch_agents,
    state_path,
    receipt_path,
    now,
    sent_messages=None,
):
    """Drive main() against deterministic launchd and Slack surfaces."""

    def stub_run(cmd, *args, **kwargs):
        if cmd and str(cmd[0]) == str(module.HERMES_BIN):
            if sent_messages is not None:
                sent_messages.append(cmd[-1])
            return _completed(0)
        if cmd and cmd[0] == "launchctl":
            return _stub_launchctl_print(cmd, loaded_label="com.colingreig.hermes.fixture")
        raise AssertionError(f"unexpected subprocess call in test: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", stub_run)
    monkeypatch.setattr(module, "_now", lambda: now)
    return module.main(
        [
            "--contracts", str(contracts_path),
            "--jobs", str(jobs_path),
            "--output-root", str(output_root),
            "--launch-agents-dir", str(launch_agents),
            "--state", str(state_path),
            "--receipt", str(receipt_path),
        ]
    )


def test_every_probe_run_is_archived_even_though_the_receipt_is_overwritten(
    tmp_path, monkeypatch
):
    """The overwritten-in-place receipt is why past storms were untriageable."""
    import os

    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, output = _fixture(tmp_path)
    contracts_path = tmp_path / "contracts.json"
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    ledger_path = tmp_path / "receipt-history.jsonl"
    snapshot_dir = tmp_path / "receipt-history"
    kwargs = dict(
        contracts_path=contracts_path,
        jobs_path=jobs_path,
        output_root=output_root,
        launch_agents=launch_agents,
        state_path=tmp_path / "state.json",
        receipt_path=receipt_path,
    )

    output.write_text("# Cron Job\nsemantic-failure\n", encoding="utf-8")
    os.utime(output, (NOW.timestamp(), NOW.timestamp()))
    assert _run_main(module, monkeypatch, now=NOW, **kwargs) == 1

    output.write_text("# Cron Job\nsemantic-success\n", encoding="utf-8")
    later = NOW + timedelta(minutes=5)
    os.utime(output, (later.timestamp(), later.timestamp()))
    assert _run_main(module, monkeypatch, now=later, **kwargs) == 0

    # The live receipt only ever describes the newest run ...
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "clean"
    assert receipt["history"]["ledger_path"] == str(ledger_path)

    # ... but the archive still proves the earlier alarm happened.
    archived = module._history_tail(ledger_path, 10)
    assert [item["status"] for item in archived] == ["alert", "clean"]
    assert archived[0]["finding_count"] >= 1
    assert archived[0]["alarm_reason"]
    assert any(item["code"] == "failure_marker" for item in archived[0]["findings"])

    # Both transitions kept a full receipt snapshot for deep triage.
    snapshots = sorted(snapshot_dir.glob("*.json"))
    assert len(snapshots) == 2
    first = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert first["status"] == "alert"
    assert first["evidence"]


def test_history_snapshot_captures_signature_churn_inside_one_deduped_incident(tmp_path):
    module = _load_module()
    receipt_path = tmp_path / "receipt.json"

    def archive(code, action, now):
        finding = module._finding("cron", "job", code, "detail")
        receipt = {
            "checked_at": now.isoformat(),
            "mode": "production",
            "status": "alert",
            "finding_count": 1,
            "findings": [finding],
            "evidence": [],
            "alarm": {
                "action": action,
                "incident_id": "abc123",
                "signature": module._signature([finding]),
                "reason": "reason",
            },
        }
        return module.archive_probe_run(receipt, receipt_path=receipt_path, now=now)

    first = archive("scheduler_not_ok", "sent", NOW)
    churned = archive("evidence_stale", "deduped", NOW + timedelta(minutes=5))
    repeat = archive("evidence_stale", "deduped", NOW + timedelta(minutes=10))

    assert first["transition"] is True
    # Code churn inside one incident is deduped for Slack but must not be
    # erased -- it is exactly the evidence a post-hoc triage pass needs.
    assert churned["transition"] is True
    assert churned["snapshot"]
    # A genuinely unchanged repeat does not burn a snapshot.
    assert repeat["transition"] is False
    assert repeat["snapshot"] is None
    assert len(list((tmp_path / "receipt-history").glob("*.json"))) == 2
    assert len(module._history_tail(tmp_path / "receipt-history.jsonl", 10)) == 3


def test_history_ledger_rotates_one_generation_and_stays_bounded(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "HISTORY_MAX_BYTES", 2048)
    ledger = tmp_path / "history.jsonl"
    for index in range(200):
        module._append_history(ledger, {"checked_at": index, "detail": "x" * 100})

    assert ledger.stat().st_size <= 2048 + 512
    assert (tmp_path / "history.jsonl.1").is_file()
    assert not list(tmp_path.glob("history.jsonl.2"))
    assert [item["checked_at"] for item in module._history_tail(ledger, 3)] == [
        197,
        198,
        199,
    ]


def test_history_snapshots_are_pruned_to_a_bounded_count(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "HISTORY_SNAPSHOT_LIMIT", 5)
    receipt_path = tmp_path / "receipt.json"
    for index in range(12):
        module.archive_probe_run(
            {
                "checked_at": index,
                "status": "alert",
                "finding_count": index,
                "findings": [],
                "alarm": {"action": "sent", "incident_id": f"i{index}"},
            },
            receipt_path=receipt_path,
            now=NOW + timedelta(seconds=index),
        )
    snapshots = sorted((tmp_path / "receipt-history").glob("*.json"))
    assert len(snapshots) == 5
    assert json.loads(snapshots[-1].read_text(encoding="utf-8"))["checked_at"] == 11


def test_archive_failure_is_recorded_in_the_receipt_not_swallowed(tmp_path, monkeypatch):
    module = _load_module()
    contracts, jobs_path, output_root, launch_agents, _output = _fixture(tmp_path)
    contracts_path = tmp_path / "contracts.json"
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"

    def stub_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "launchctl":
            return _stub_launchctl_print(cmd, loaded_label="com.colingreig.hermes.fixture")
        raise AssertionError(f"unexpected subprocess call in test: {cmd}")

    monkeypatch.setattr(module.subprocess, "run", stub_run)
    monkeypatch.setattr(module, "_now", lambda: NOW)
    exit_code = module.main(
        [
            "--contracts", str(contracts_path),
            "--jobs", str(jobs_path),
            "--output-root", str(output_root),
            "--launch-agents-dir", str(launch_agents),
            "--state", str(tmp_path / "state.json"),
            "--receipt", str(receipt_path),
            "--history-dir", str(blocker / "nested"),
        ]
    )

    assert exit_code == 0
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "clean"
    # A persisted failure reason, not a silent boolean.
    assert receipt["history"]["error"]
    assert receipt["history"]["snapshot"] is None


def test_alarm_outcomes_persist_a_human_reason_for_every_routing_decision(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "job", "scheduler_not_ok", "last_status=error")]
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    sent = module.route_alarm(
        findings, state_path=state_path, now=NOW,
        drill=False, real_alert=True, sender=sender,
    )
    deduped = module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(minutes=5),
        drill=False, real_alert=True, sender=sender,
    )
    recovery_pending = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=6),
        drill=False, real_alert=True, sender=sender,
    )
    recovered = module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=15),
        drill=False, real_alert=True, sender=sender,
    )

    assert sent["action"] == "sent"
    assert "New incident" in sent["reason"]
    assert deduped["action"] == "deduped"
    assert "re-alert interval has not elapsed" in deduped["reason"]
    assert deduped["next_alert_at"] == NOW.timestamp() + module.REALERT_SECONDS
    assert recovery_pending["action"] == "recovery-pending"
    assert recovery_pending["reason"]
    assert recovered["action"] == "recovery-sent"
    assert recovered["alert_count"] == 1
    assert len(messages) == 2


def test_alert_message_explains_the_incident_and_points_at_the_archive(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("cron", "job", "scheduler_not_ok", "last_status=error")]
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    module.route_alarm(
        findings, state_path=state_path, now=NOW,
        drill=False, real_alert=True, sender=sender,
    )
    alert = messages[0]
    incident_id = json.loads(state_path.read_text())["incident_id"]
    assert f"Incident {incident_id[:12]}" in alert
    assert "Why this alert fired now:" in alert
    assert str(module.DEFAULT_HISTORY_LEDGER) in alert
    assert "hermes fleet incident-report" in alert

    module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=1),
        drill=False, real_alert=True, sender=sender,
    )
    module.route_alarm(
        [], state_path=state_path, now=NOW + timedelta(minutes=30),
        drill=False, real_alert=True, sender=sender,
    )
    recovery = messages[-1]
    assert "recovered" in recovery
    assert "1 alert(s) delivered" in recovery
    assert str(module.DEFAULT_HISTORY_LEDGER) in recovery


def test_long_open_unchanged_incident_is_flagged_as_probable_contract_drift(tmp_path):
    """The poller expected:retired class: a stale contract, not a live outage."""
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("launchd", "poller", "agent_not_loaded", "absent")]
    messages = []

    def sender(message):
        messages.append(message)
        return _completed()

    module.route_alarm(
        findings, state_path=state_path, now=NOW,
        drill=False, real_alert=True, sender=sender,
    )
    stale = module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(hours=30),
        drill=False, real_alert=True, sender=sender,
    )

    assert stale["action"] == "sent"
    assert "re-alert" in stale["reason"]
    assert "contract drift" in messages[-1]
    assert "contract drift" not in messages[0]


def test_cutover_suppression_publishes_its_bounded_expiry(tmp_path):
    module = _load_module()
    state_path = tmp_path / "state.json"
    findings = [module._finding("launchd", "gateway", "endpoint_failed", "offline")]

    suppressed = module.route_alarm(
        findings, state_path=state_path, now=NOW + timedelta(minutes=1),
        cutover_at=NOW, drill=False, real_alert=True,
        sender=lambda _message: _completed(),
    )

    assert suppressed["action"] == "cutover-suppressed"
    assert (
        suppressed["suppressed_until"]
        == NOW.timestamp() + module.CUTOVER_GRACE_SECONDS
    )
    assert suppressed["pending_identities"] == ["launchd:gateway"]
    assert "grace window" in suppressed["reason"]
