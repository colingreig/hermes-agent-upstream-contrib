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
        "com.colingreig.hermes.release-poll": "retired",
    }
    declared_states = {
        item["label"]: item["expected"] for item in contracts["launch_agents"]
    }
    assert {label: declared_states[label] for label in required_states} == required_states
    assert retired_labels == {"com.colingreig.hermes.release-poll"}

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
    assert Path(stdout_path).expanduser() == Path(contract["outcome"]["path"]).expanduser()
    assert "Traceback" in contract["outcome"]["failure_patterns"]
