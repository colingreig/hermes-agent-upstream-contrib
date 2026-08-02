"""Fail-closed contracts for the Hermes Mac mini continuity adapter."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

MINI_SCRIPTS = Path(__file__).resolve().parents[1]
JOURNAL_PATH = MINI_SCRIPTS / "report_activity_journal.py"
ADAPTER_PATH = MINI_SCRIPTS / "report_activity_continuity.py"
NOW = dt.datetime(2026, 8, 2, 13, 23, tzinfo=dt.timezone.utc)
HEALTH = {
    "status": "OK",
    "schema": "hermes-mini-health-attestation/v1",
    "generated_at": "2026-08-02T13:22:00Z",
}
PROVENANCE = {
    "status": "OK",
    "coverage_started_at": "2026-08-02T00:00:00Z",
    "source_commit": "a" * 40,
    "manifest_sha256": "b" * 64,
}
WRITER_COVERAGE = {
    "status": "OK",
    "verified_jobs": [
        "clickup-executor",
        "hermes-pr-validate",
        "clickup-lifecycle",
    ],
}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    journal = _load(
        JOURNAL_PATH, f"continuity_journal_{os.getpid()}_{id(object())}"
    )
    monkeypatch.setitem(sys.modules, "report_activity_journal", journal)
    adapter = _load(
        ADAPTER_PATH, f"continuity_adapter_{os.getpid()}_{id(object())}"
    )
    return journal, adapter


def _task_for(event):
    status = {
        "claim": "in progress",
        "review_handoff": "in review",
        "validator_complete": "complete",
    }[event["kind"]]
    return {
        "id": event["task_id"],
        "status": {"status": status},
        "date_updated": "1785686400000",
        "date_closed": (
            "1785686400000"
            if event["kind"] == "validator_complete"
            else None
        ),
    }


def _emit(journal, root, *, kind="claim", task="task-1", when):
    event = journal.build_event(
        kind=kind,
        task_id=task,
        source="continuity-test",
        clickup_updated_at=(
            journal._iso(when) if kind != "claim" else None
        ),
        now=when,
    )
    journal.append_event(event, state_dir=root)
    return event


def _evaluate(adapter, root, **overrides):
    events_by_task = overrides.pop("events_by_task", {})
    kwargs = {
        "now": NOW,
        "state_dir": root,
        "strict_validator_completed": 0,
        "provenance": PROVENANCE,
        "health_attestation": HEALTH,
        "writer_coverage": WRITER_COVERAGE,
        "fetch_task": lambda task_id: _task_for(events_by_task[task_id]),
    }
    kwargs.update(overrides)
    return adapter.evaluate_continuity(**kwargs)


def test_stable_slot_identity_for_scheduled_late_and_manual_invocations(
    modules,
):
    _journal, adapter = modules
    expected = "2026-08-02T12:00:00Z"
    for value in (
        dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 8, 2, 12, 49, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 8, 2, 17, 59, tzinfo=dt.timezone.utc),
    ):
        assert adapter.iso(adapter.scheduled_slot(value)) == expected


def test_two_empty_complete_covered_windows_emit_one_stable_concern(
    modules, tmp_path
):
    _journal, adapter = modules
    first = _evaluate(adapter, tmp_path)
    second = adapter.evaluate_continuity(
        now=dt.datetime(2026, 8, 2, 17, 59, tzinfo=dt.timezone.utc),
        state_dir=tmp_path,
        strict_validator_completed=0,
        provenance=PROVENANCE,
        health_attestation=HEALTH,
        writer_coverage=WRITER_COVERAGE,
        fetch_task=lambda _task_id: pytest.fail("empty sample"),
    )
    assert first["state"] == second["state"] == "INACTIVE"
    assert first["slot_id"] == second["slot_id"] == "2026-08-02T12:00:00Z"
    assert first["concern_id"] == second["concern_id"]
    assert first["windows"]["previous"]["total"] == 0
    assert first["windows"]["current"]["total"] == 0


@pytest.mark.parametrize(
    "when",
    [
        dt.datetime(2026, 8, 2, 2, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 8, 2, 8, 0, tzinfo=dt.timezone.utc),
    ],
)
def test_one_event_in_either_window_suppresses_inactivity(
    modules, tmp_path, when
):
    journal, adapter = modules
    event = _emit(journal, tmp_path, when=when)
    result = _evaluate(
        adapter, tmp_path, events_by_task={event["task_id"]: event}
    )
    assert result["state"] == "ACTIVE"
    assert result["concern_id"] is None


def test_duplicate_records_are_deduplicated_without_inflating_counts(
    modules, tmp_path
):
    journal, adapter = modules
    event = _emit(
        journal,
        tmp_path,
        when=dt.datetime(2026, 8, 2, 8, 0, tzinfo=dt.timezone.utc),
    )
    path = tmp_path / "2026-08-02.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    result = _evaluate(
        adapter, tmp_path, events_by_task={event["task_id"]: event}
    )
    assert result["state"] == "ACTIVE"
    assert result["windows"]["current"]["total"] == 1
    assert result["windows"]["duplicates_deduped"] == 1


@pytest.mark.parametrize("failure", ["corrupt", "partial", "degraded"])
def test_unreadable_partial_corrupt_or_degraded_journal_is_unknown(
    modules, tmp_path, failure
):
    journal, adapter = modules
    tmp_path.mkdir(exist_ok=True)
    if failure == "corrupt":
        (tmp_path / "2026-08-02.jsonl").write_text("{nope}\n")
    elif failure == "partial":
        (tmp_path / "2026-08-02.jsonl").write_text("{}")
    else:
        assert journal.mark_degraded(
            "append failure retained", source="test", state_dir=tmp_path, now=NOW
        )
    result = _evaluate(adapter, tmp_path)
    assert result["state"] == "UNKNOWN"
    assert "INACTIVE" not in result["detail"]


def test_missing_or_uninstrumented_producer_is_unknown(modules, tmp_path):
    _journal, adapter = modules
    inventory = [
        {"id": "enabled-writer", "enabled": True, "kind": "claim", "emitter": ""}
    ]
    result = _evaluate(adapter, tmp_path, inventory=inventory)
    assert result["state"] == "UNKNOWN"
    assert any("lacks emitter" in reason for reason in result["reasons"])


def test_enabled_but_uninstrumented_live_writer_is_unknown(
    modules, tmp_path
):
    _journal, adapter = modules
    result = _evaluate(
        adapter,
        tmp_path,
        writer_coverage={
            "status": "UNKNOWN",
            "reason": "enabled lifecycle writer clickup-executor is uninstrumented",
        },
    )
    assert result["state"] == "UNKNOWN"
    assert any("uninstrumented" in reason for reason in result["reasons"])


def test_live_writer_inventory_requires_all_markers(modules, tmp_path):
    _journal, adapter = modules
    jobs = {
        "jobs": [
            {
                "name": name,
                "enabled": True,
                "prompt": (
                    "report_activity_journal.py confirm-transition successor "
                    "86e2gnz71"
                ),
            }
            for name in adapter.REQUIRED_WRITER_JOBS
        ]
    }
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps(jobs))
    assert adapter.verify_writer_coverage(path)["status"] == "OK"
    jobs["jobs"][0]["prompt"] = "missing instrumentation"
    path.write_text(json.dumps(jobs))
    with pytest.raises(adapter.ContinuityError):
        adapter.verify_writer_coverage(path)


@pytest.mark.parametrize(
    "health",
    [
        {"status": "UNKNOWN", "schema": "hermes-mini-health-attestation/v1"},
        {"status": "OK", "schema": "wrong"},
    ],
)
def test_missing_unhealthy_or_unbound_execution_health_is_unknown(
    modules, tmp_path, health
):
    _journal, adapter = modules
    result = _evaluate(adapter, tmp_path, health_attestation=health)
    assert result["state"] == "UNKNOWN"


def test_coverage_that_does_not_span_both_windows_is_provisional(
    modules, tmp_path
):
    _journal, adapter = modules
    provenance = dict(
        PROVENANCE, coverage_started_at="2026-08-02T01:00:00Z"
    )
    result = _evaluate(adapter, tmp_path, provenance=provenance)
    assert result["state"] == "PROVISIONAL"
    assert result["concern_id"] is None


def test_missing_provenance_is_unknown(modules, tmp_path):
    _journal, adapter = modules
    result = _evaluate(
        adapter,
        tmp_path,
        provenance={"status": "UNKNOWN", "reason": "receipt unavailable"},
    )
    assert result["state"] == "UNKNOWN"


def test_clickup_disagreement_or_insufficient_timestamp_is_unknown(
    modules, tmp_path
):
    journal, adapter = modules
    event = _emit(
        journal,
        tmp_path,
        kind="review_handoff",
        when=dt.datetime(2026, 8, 2, 8, 0, tzinfo=dt.timezone.utc),
    )
    result = _evaluate(
        adapter,
        tmp_path,
        events_by_task={event["task_id"]: event},
        fetch_task=lambda task_id: {
            "id": task_id,
            "status": {"status": "to do"},
            "date_updated": None,
        },
    )
    assert result["state"] == "UNKNOWN"
    assert result["parity"]["status"] == "UNKNOWN"


def test_strict_validator_completion_is_independent_and_disagreement_visible(
    modules, tmp_path
):
    journal, adapter = modules
    event = _emit(
        journal,
        tmp_path,
        kind="validator_complete",
        when=dt.datetime(2026, 8, 2, 12, 30, tzinfo=dt.timezone.utc),
    )
    result = _evaluate(
        adapter,
        tmp_path,
        events_by_task={event["task_id"]: event},
        strict_validator_completed=0,
    )
    strict = result["parity"]["strict_completion"]
    assert result["state"] == "UNKNOWN"
    assert strict["authoritative_validator_completed"] == 0
    assert strict["outbox_validator_complete"] == 1
    assert strict["outbox_role"] == "parity evidence only"


def test_bounded_sample_is_deterministic(modules, tmp_path):
    journal, adapter = modules
    events = {}
    for index in range(20):
        event = _emit(
            journal,
            tmp_path,
            task=f"task-{index:02d}",
            when=dt.datetime(
                2026, 8, 2, 8, index, tzinfo=dt.timezone.utc
            ),
        )
        events[event["task_id"]] = event
    calls = []
    result = _evaluate(
        adapter,
        tmp_path,
        events_by_task=events,
        sample_size=12,
        fetch_task=lambda task_id: (
            calls.append(task_id) or _task_for(events[task_id])
        ),
    )
    expected = [
        event["task_id"]
        for event in sorted(
            events.values(),
            key=lambda item: __import__("hashlib").sha256(
                item["event_id"].encode()
            ).hexdigest(),
        )[:12]
    ]
    assert result["state"] == "ACTIVE"
    assert calls == expected
    assert result["parity"]["population"] == 20
    assert result["parity"]["sampled"] == 12


def test_health_attestation_subprocess_failures_fail_closed(modules):
    _journal, adapter = modules
    good = {
        "schema": adapter.HEALTH_SCHEMA,
        "healthy": True,
        "exit_code": 0,
        "generated_at": "now",
        "checks": [
            {"id": check_id, "state": "pass"}
            for check_id in (
                "runtime.commit",
                "runtime.source-binding",
                "execution.inflight-classification",
            )
        ],
    }
    ok = subprocess.CompletedProcess([], 0, json.dumps(good), "")
    assert adapter.run_health_attestation(runner=lambda *a, **k: ok)["status"] == "OK"
    for bad in (
        subprocess.CompletedProcess([], 2, json.dumps(good), ""),
        subprocess.CompletedProcess([], 0, "not-json", ""),
    ):
        with pytest.raises(adapter.ContinuityError):
            adapter.run_health_attestation(runner=lambda *a, _bad=bad, **k: _bad)


def test_provenance_requires_success_hash_binding_and_jobs_install(modules):
    _journal, adapter = modules
    receipt = {
        "result": "success",
        "timestamp": "20260802T000000Z",
        "manifest_path": f"/release/v0.18.2-{'a' * 12}/fleet.json",
        "manifest_sha256": "b" * 64,
        "production_write_lease": {"commit_sha": "a" * 40},
        "steps": [{"step": "jobs_json", "status": "installed"}],
    }
    assert adapter.verify_provenance(receipt)["status"] == "OK"
    for key in ("manifest_sha256", "steps"):
        broken = dict(receipt)
        broken[key] = "" if key == "manifest_sha256" else []
        with pytest.raises(adapter.ContinuityError):
            adapter.verify_provenance(broken)
