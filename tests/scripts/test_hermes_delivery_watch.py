from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
MODULE = SCRIPTS / "hermes_delivery_watch.py"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_delivery_watch_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


watch = _load_module()
UTC = timezone.utc
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _task(task_id: str = "TASK-1", run_id: str = "run-1", sha: str = "sha-1") -> dict:
    return {
        "task": {"id": task_id, "lane": "repo-only"},
        "sources": {"task": {"status": "OK"}, "github": {"status": "OK"}},
        "executor": {"job_id": f"job-{task_id}", "run_id": run_id, "fencing_token": run_id},
        "ledger": {
            "execution_id": f"execution-{task_id}",
            "job_id": f"job-{task_id}",
            "run_id": run_id,
            "fencing_token": run_id,
        },
        "repository": "owner/repo",
        "governing_workflows": ["governing-ci"],
        "pull_requests": [
            {
                "repository": "owner/repo",
                "number": int(task_id.rsplit("-", 1)[1]),
                "head_sha": sha,
            }
        ],
        "ci": {
            "runs": [
                {
                    "repository": "owner/repo",
                    "workflow": "governing-ci",
                    "run_id": f"ci-{run_id}",
                    "head_sha": sha,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        "handoff": {"id": f"handoff-{task_id}", "head_sha": sha},
        "validator": {"identity": f"validator-{task_id}", "verdict": "PASS", "head_sha": sha},
    }


def _snapshot() -> dict:
    return {
        "collection": {"feed": {"status": "OK"}},
        "tasks": [_task()],
        "watch": {
            "owners": [],
            "queue": [],
            "review_gate": {"status": "clean", "consecutive_clean_runs": 3},
            "lifecycle_events": [],
            "promotions": [],
        },
    }


def test_run_once_persists_atomic_state_and_deduplicates_transitions(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "_send_slack", lambda *_args, **_kwargs: True)
    config = {"collectors": [{"kind": "file", "path": "/unused"}], "slack_target": "slack:test"}

    first = watch.run_once(config, tmp_path, snapshot=_snapshot(), now=NOW, ping_deadman=False)
    second = watch.run_once(
        config,
        tmp_path,
        snapshot=_snapshot(),
        now=NOW + timedelta(minutes=5),
        ping_deadman=False,
    )

    assert first["status"] == "OK"
    assert first["correlations"][0]["delivery_status"] == "DELIVERED"
    assert first["transitions"] == second["transitions"] == 0
    assert len((tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))["status"] == "OK"


def test_alert_open_close_is_append_only_and_slack_is_transition_only(tmp_path, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        watch,
        "_send_slack",
        lambda _config, transition: sent.append(transition["event"]) is None or True,
    )
    config = {"collectors": [{"kind": "file", "path": "/unused"}], "slack_target": "slack:test"}
    bad = _snapshot()
    bad["watch"]["queue"] = [
        {"task_id": "TASK-Q", "eligible_at": "2026-07-29T10:00:00Z", "owner_run_id": None}
    ]

    watch.run_once(config, tmp_path, snapshot=bad, now=NOW, ping_deadman=False)
    watch.run_once(config, tmp_path, snapshot=bad, now=NOW + timedelta(minutes=5), ping_deadman=False)
    watch.run_once(config, tmp_path, snapshot=_snapshot(), now=NOW + timedelta(minutes=10), ping_deadman=False)

    incidents = [
        json.loads(line)
        for line in (tmp_path / "incidents.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [item["event"] for item in incidents] == ["opened", "closed"]
    assert sent == ["opened", "closed"]


def test_every_sla_and_integrity_class_is_reported():
    old = "2026-07-29T08:00:00Z"
    expired = "2026-07-29T11:00:00Z"
    snapshot = _snapshot()
    snapshot["watch"] = {
        "owners": [
            {
                "task_id": "TASK-1",
                "run_id": "run-a",
                "fencing_token": None,
                "claimed_at": old,
                "heartbeat_at": old,
                "lease_expires_at": expired,
                "execution_started_at": old,
                "budget_seconds": 60,
                "pr_opened_at": None,
            },
            {
                "task_id": "TASK-1",
                "run_id": "run-b",
                "fencing_token": "fence-b",
                "claimed_at": old,
                "heartbeat_at": old,
                "lease_expires_at": expired,
                "pr_opened_at": old,
                "ci_terminal_at": old,
                "in_review_at": old,
            },
            {
                "task_id": "TASK-PR",
                "run_id": "run-pr",
                "fencing_token": "fence-pr",
                "heartbeat_at": old,
                "lease_expires_at": expired,
                "execution_finished_at": old,
                "pr_opened_at": old,
                "ci_terminal_at": None,
            },
            {
                "task_id": "TASK-CI",
                "run_id": "run-ci",
                "fencing_token": "fence-ci",
                "heartbeat_at": old,
                "lease_expires_at": expired,
                "execution_finished_at": old,
                "ci_terminal_at": old,
                "in_review_at": None,
            },
        ],
        "queue": [{"task_id": "TASK-Q", "eligible_at": old, "owner_run_id": None}],
        "review_gate": {"status": "failed", "consecutive_clean_runs": 0},
        "lifecycle_events": [{"id": "life-1", "valid": True, "false_alert": True}],
        "promotions": [
            {
                "id": "promotion-1",
                "prod_sha": "prod",
                "certified": False,
                "receipt_id": "receipt",
                "receipt_sha": "wrong",
            }
        ],
    }

    alerts = watch.evaluate_alerts(snapshot, [], now=NOW)
    kinds = {item["kind"] for item in alerts}

    assert {
        "unfenced_owner",
        "lease_heartbeat_missing",
        "execution_over_budget",
        "claim_to_pr_sla",
        "pr_to_ci_sla",
        "ci_to_review_sla",
        "review_to_validator_sla",
        "duplicate_ownership",
        "eligible_unowned_sla",
        "review_gate_failure",
        "invalid_lifecycle_event",
        "uncertified_promotion",
        "promotion_receipt_mismatch",
    } <= kinds


def test_failed_collector_makes_delivery_unknown(tmp_path):
    snapshot = _snapshot()
    snapshot["collection"]["github"] = {"status": "UNKNOWN", "error": "timeout"}
    config = {"collectors": [{"kind": "file", "path": "/unused"}]}

    result = watch.run_once(
        config,
        tmp_path,
        snapshot=snapshot,
        now=NOW,
        alert=False,
        ping_deadman=False,
    )

    assert result["status"] == "UNKNOWN"
    assert result["correlations"][0]["delivery_status"] == "UNKNOWN"
    checkpoint = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert {item["kind"] for item in checkpoint["open_incidents"].values()} == {
        "delivery_unknown",
        "source_unknown",
    }


def test_collector_payload_propagates_underlying_source_unknown(tmp_path):
    payload = _snapshot()
    payload["collection"]["mini:ledger"] = {
        "status": "UNKNOWN",
        "error": "read failed",
    }
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    merged = watch.collect_snapshot(
        {"collectors": [{"name": "producer", "kind": "file", "path": str(source)}]}
    )

    assert merged["collection"]["producer"] == {"status": "OK"}
    assert merged["collection"]["mini:ledger"] == {
        "status": "UNKNOWN",
        "error": "read failed",
    }
    result = watch.run_once(
        {"collectors": [{"name": "producer", "kind": "file", "path": str(source)}]},
        tmp_path / "state",
        snapshot=merged,
        now=NOW,
        alert=False,
        ping_deadman=False,
    )
    assert result["status"] == "UNKNOWN"
    assert result["correlations"][0]["delivery_status"] == "UNKNOWN"


def test_executor_overlap_is_global_across_different_tasks():
    snapshot = _snapshot()
    snapshot["watch"]["owners"] = [
        {
            "task_id": "TASK-1",
            "run_id": "run-a",
            "fencing_token": "fence-a",
            "execution_started_at": "2026-07-29T10:00:00Z",
            "execution_finished_at": "2026-07-29T11:00:00Z",
            "heartbeat_at": "2026-07-29T10:59:00Z",
            "lease_expires_at": "2026-07-29T13:00:00Z",
        },
        {
            "task_id": "TASK-2",
            "run_id": "run-b",
            "fencing_token": "fence-b",
            "execution_started_at": "2026-07-29T10:30:00Z",
            "execution_finished_at": "2026-07-29T11:30:00Z",
            "heartbeat_at": "2026-07-29T11:29:00Z",
            "lease_expires_at": "2026-07-29T13:00:00Z",
        },
    ]

    alerts = watch.evaluate_alerts(snapshot, [], now=NOW)

    assert "duplicate_ownership" in {item["kind"] for item in alerts}


def test_final_evidence_accepts_three_distinct_chains_after_24_hours(tmp_path):
    config = {"collectors": [{"kind": "file", "path": "/unused"}]}
    snapshot = _snapshot()
    snapshot["tasks"] = [
        _task("TASK-1", "run-1", "sha-1"),
        _task("TASK-2", "run-2", "sha-2"),
        _task("TASK-3", "run-3", "sha-3"),
    ]

    watch.run_once(
        config,
        tmp_path,
        snapshot=snapshot,
        now=NOW - timedelta(hours=25),
        alert=False,
        ping_deadman=False,
    )
    first_event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    for offset in range(5, 25 * 60, 5):
        event = dict(first_event)
        event["timestamp"] = (
            NOW - timedelta(hours=25) + timedelta(minutes=offset)
        ).isoformat()
        watch._append_jsonl(tmp_path / "events.jsonl", event)
    watch.run_once(
        config,
        tmp_path,
        snapshot=snapshot,
        now=NOW,
        alert=False,
        ping_deadman=False,
    )
    result = watch.final_evidence(tmp_path, now=NOW)

    assert result["accepted"] is True
    assert result["delivered_task_ids"] == ["TASK-1", "TASK-2", "TASK-3"]


def test_final_twelve_hours_rejects_even_a_closed_alert(tmp_path):
    config = {"collectors": [{"kind": "file", "path": "/unused"}]}
    snapshot = _snapshot()
    snapshot["tasks"] = [
        _task("TASK-1", "run-1", "sha-1"),
        _task("TASK-2", "run-2", "sha-2"),
        _task("TASK-3", "run-3", "sha-3"),
    ]
    watch.run_once(
        config,
        tmp_path,
        snapshot=snapshot,
        now=NOW - timedelta(hours=25),
        alert=False,
        ping_deadman=False,
    )
    first_event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    for offset in range(5, 25 * 60, 5):
        event = dict(first_event)
        event["timestamp"] = (
            NOW - timedelta(hours=25) + timedelta(minutes=offset)
        ).isoformat()
        if offset == 20 * 60:
            event["alerts"] = [
                {"kind": "eligible_unowned_sla", "signature": "transient"}
            ]
        watch._append_jsonl(tmp_path / "events.jsonl", event)
    watch.run_once(
        config,
        tmp_path,
        snapshot=snapshot,
        now=NOW,
        alert=False,
        ping_deadman=False,
    )

    result = watch.final_evidence(tmp_path, now=NOW)

    assert result["checks"]["final_12h_no_unknown_or_open_alert"] is False
    assert result["accepted"] is False


def test_collectors_are_limited_to_read_only_transports(tmp_path):
    payload = tmp_path / "snapshot.json"
    payload.write_text(json.dumps(_snapshot()), encoding="utf-8")
    collected = watch.collect_snapshot(
        {"collectors": [{"name": "local", "kind": "file", "path": str(payload)}]}
    )

    assert collected["collection"]["local"]["status"] == "OK"
    assert len(collected["tasks"]) == 1

    unsupported = watch.collect_snapshot(
        {"collectors": [{"name": "bad", "kind": "command", "argv": ["touch", "/tmp/no"]}]}
    )
    assert unsupported["collection"]["bad"]["status"] == "UNKNOWN"
