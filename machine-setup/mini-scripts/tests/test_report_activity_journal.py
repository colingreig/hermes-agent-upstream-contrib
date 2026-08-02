"""Real-path contracts for the Mini report-activity shadow outbox."""
from __future__ import annotations

import datetime as dt
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

MINI_SCRIPTS = Path(__file__).resolve().parents[1]
JOURNAL_PATH = MINI_SCRIPTS / "report_activity_journal.py"
CLAIM_PATH = MINI_SCRIPTS / "claim_store.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def journal():
    return _load(JOURNAL_PATH, f"report_activity_journal_test_{os.getpid()}_{id(object())}")


def _records(root: Path) -> list[dict]:
    result = []
    for path in sorted(root.glob("????-??-??.jsonl")):
        result.extend(json.loads(line) for line in path.read_text().splitlines() if line)
    return result


def test_concurrent_append_is_locked_complete_and_deduped(tmp_path):
    root = tmp_path / "activity"
    procs = []
    for index in range(18):
        # Six logical identities, each raced three times.  O_APPEND alone is not
        # enough for dedupe; the read/check/write sequence must share the flock.
        task_id = f"task-{index % 6}"
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(JOURNAL_PATH),
                    "--state-dir",
                    str(root),
                    "emit",
                    "--kind",
                    "claim",
                    "--task-id",
                    task_id,
                    "--source",
                    "concurrency-test",
                    "--run-id",
                    f"run-{index % 6}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    results = [proc.communicate(timeout=20) + (proc.returncode,) for proc in procs]
    assert all(rc == 0 for _out, _err, rc in results), results
    records = _records(root)
    assert len(records) == 6
    assert len({record["event_id"] for record in records}) == 6
    assert all(record["v"] == 1 for record in records)


def test_stable_event_id_same_day_and_utc_boundary(journal, tmp_path):
    root = tmp_path / "activity"
    first = journal.build_event(
        kind="claim",
        task_id="86x",
        source="queue",
        run_id="run-1",
        now=dt.datetime(2026, 8, 2, 23, 59, tzinfo=dt.timezone.utc),
    )
    later = journal.build_event(
        kind="claim",
        task_id="86x",
        source="queue",
        run_id="run-1",
        now=dt.datetime(2026, 8, 2, 19, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7))),
    )
    assert first["event_id"] != later["event_id"]  # second instant is 2026-08-03 UTC
    assert first["ts"].startswith("2026-08-02")
    assert later["ts"].startswith("2026-08-03")

    duplicate = journal.build_event(
        kind="claim",
        task_id="86x",
        source="queue",
        run_id="run-1",
        now=dt.datetime(2026, 8, 2, 23, 59, 30, tzinfo=dt.timezone.utc),
    )
    assert duplicate["event_id"] == first["event_id"]
    assert journal.append_event(first, state_dir=root)["appended"] is True
    result = journal.append_event(duplicate, state_dir=root)
    assert result["deduped"] is True
    assert len(_records(root)) == 1


def test_append_rejects_non_utc_partition_input_and_tampered_identity(journal, tmp_path):
    root = tmp_path / "activity"
    offset_event = {
        "v": 1,
        "event_id": "placeholder",
        "ts": "2026-08-02T23:30:00.000-07:00",
        "kind": "claim",
        "task_id": "86offset",
        "source": "utc-contract-test",
    }
    offset_event["event_id"] = journal._event_id(
        {
            "v": 1,
            "utc_day": "2026-08-02",
            "kind": "claim",
            "task_id": "86offset",
            "source": "utc-contract-test",
            "run_id": None,
            "execution_id": None,
            "clickup_updated_at": None,
            "clickup_transition_at": None,
        }
    )
    with pytest.raises(journal.JournalError, match="ts is not UTC"):
        journal.append_event(offset_event, state_dir=root)
    assert list(root.glob("*.jsonl")) == []

    valid = journal.build_event(
        kind="claim", task_id="86stable", source="identity-test"
    )
    valid["event_id"] = "ra1-tampered"
    with pytest.raises(journal.JournalError, match="stable event identity"):
        journal.append_event(valid, state_dir=root)
    assert list(root.glob("*.jsonl")) == []


def test_retention_keeps_exactly_trailing_21_utc_days(journal, tmp_path):
    root = tmp_path / "activity"
    root.mkdir()
    today = dt.date(2026, 8, 21)
    for age in range(1, 26):
        day = today - dt.timedelta(days=age)
        (root / f"{day.isoformat()}.jsonl").write_text("\n")
    event = journal.build_event(
        kind="claim",
        task_id="new",
        source="retention-test",
        now=dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc),
    )
    result = journal.append_event(event, state_dir=root)
    days = sorted(path.stem for path in root.glob("????-??-??.jsonl"))
    assert days[0] == "2026-08-01"
    assert days[-1] == "2026-08-21"
    assert len(days) == 21
    assert set(result["pruned"]) == {
        f"{(today - dt.timedelta(days=age)).isoformat()}.jsonl"
        for age in range(21, 26)
    }


@pytest.mark.parametrize(
    "content, reason",
    [
        (b'{"v":1', "incomplete trailing line"),
        (b"not-json\n", "corrupt JSON"),
        (json.dumps({"v": 1, "event_id": "x"}).encode() + b"\n", "missing fields"),
    ],
)
def test_partial_corrupt_and_invalid_records_make_health_unknown(
    journal, tmp_path, content, reason
):
    root = tmp_path / "activity"
    root.mkdir()
    (root / "2026-08-02.jsonl").write_bytes(content)
    result = journal.health(
        state_dir=root, now=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc)
    )
    assert result["status"] == "UNKNOWN"
    assert result["degraded"] is True
    assert any(reason in item for item in result["reasons"])


def test_enabled_producer_without_emitter_makes_coverage_unknown(journal, tmp_path):
    inventory = [
        {"id": "wired", "enabled": True, "kind": "claim", "emitter": "callsite"},
        {"id": "missing", "enabled": True, "kind": "review_handoff"},
        {"id": "disabled", "enabled": False, "kind": "validator_complete"},
    ]
    result = journal.health(state_dir=tmp_path / "empty", inventory=inventory)
    assert result["status"] == "UNKNOWN"
    assert result["continuity_consumers_enabled"] is False
    assert any("missing" in item and "lacks emitter" in item for item in result["reasons"])


def test_enabled_producer_inventory_covers_every_lifecycle_writer(journal):
    assert {producer["id"] for producer in journal.PRODUCER_INVENTORY} == {
        "queue-poller-claim",
        "merged-before-claim-reconciliation",
        "ordinary-publish-closeout",
        "db-publish-closeout",
        "closeout-actor",
        "clickup-lifecycle-reconciliation",
        "validator",
    }
    assert all(producer["enabled"] is True for producer in journal.PRODUCER_INVENTORY)
    assert all(producer["emitter"].strip() for producer in journal.PRODUCER_INVENTORY)


def test_append_failure_is_visible_as_unknown_health(journal, tmp_path, monkeypatch):
    root = tmp_path / "activity"

    def fail_append(_event, *, state_dir=None):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(journal, "append_event", fail_append)
    result = journal.safe_emit(
        kind="claim", task_id="86disk", source="queue", state_dir=root
    )
    assert result["status"] == "UNKNOWN"
    health = journal.health(state_dir=root)
    assert health["status"] == "UNKNOWN"
    assert any("synthetic disk full" in reason for reason in health["reasons"])


def test_first_health_issue_fsyncs_file_before_parent_directory(journal, tmp_path, monkeypatch):
    root = tmp_path / "activity"
    calls = []
    real_fsync = os.fsync

    def record_fsync(fd):
        calls.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(journal.os, "fsync", record_fsync)
    assert journal.mark_degraded("first", source="durability-test", state_dir=root)
    assert calls == ["file", "dir"]

    calls.clear()
    assert journal.mark_degraded("second", source="durability-test", state_dir=root)
    assert calls == ["file"]


def test_read_failure_is_visible_as_unknown_health(journal, tmp_path, monkeypatch):
    root = tmp_path / "activity"
    root.mkdir()
    journal_file = root / "2026-08-02.jsonl"
    journal_file.write_text("\n")
    real_read_bytes = Path.read_bytes

    def fail_journal_read(path):
        if path == journal_file:
            raise OSError("synthetic read denial")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_journal_read)
    result = journal.health(state_dir=root)
    assert result["status"] == "UNKNOWN"
    assert any("synthetic read denial" in reason for reason in result["reasons"])


def test_confirm_transition_rejects_mismatch_then_dedupes_verified_success(journal, tmp_path):
    root = tmp_path / "activity"
    rejected = journal.confirm_transition(
        kind="review_handoff",
        task_id="86review",
        source="ordinary-publish-closeout",
        expected_status="in review",
        state_dir=root,
        fetch_task=lambda _tid: {
            "id": "86review",
            "status": {"status": "in progress"},
            "date_updated": "1785630000000",
        },
    )
    assert rejected["confirmed"] is False
    assert _records(root) == []
    assert journal.health(state_dir=root)["status"] == "UNKNOWN"

    # Use a fresh health directory for the successful postcondition so the
    # deliberate rejection marker above cannot mask the event assertion.
    success_root = tmp_path / "success"
    fetch = lambda _tid: {
        "id": "86review",
        "status": {"status": "in review"},
        "date_updated": "1785630000123",
    }
    first = journal.confirm_transition(
        kind="review_handoff",
        task_id="86review",
        source="ordinary-publish-closeout",
        expected_status="in review",
        state_dir=success_root,
        fetch_task=fetch,
        now=dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc),
    )
    second = journal.confirm_transition(
        kind="review_handoff",
        task_id="86review",
        source="ordinary-publish-closeout",
        expected_status="in review",
        state_dir=success_root,
        fetch_task=fetch,
        now=dt.datetime(2026, 8, 2, 13, tzinfo=dt.timezone.utc),
    )
    assert first["confirmed"] is second["confirmed"] is True
    assert first["appended"] is True
    assert second["deduped"] is True
    records = _records(success_root)
    assert len(records) == 1
    assert records[0]["clickup_updated_at"] == "1785630000123"


def test_closeout_writer_rejection_has_no_event_and_verified_success_has_one(
    journal, tmp_path, monkeypatch
):
    validator_verdict = types.ModuleType("validator_verdict")
    validator_verdict.load_verdicts = lambda: {}
    guard = types.ModuleType("clickup_status_guard")
    guard.is_review_class = lambda status: status == "in review"
    guard.is_complete_class = lambda status: status == "complete"
    guard.latest_validate_verdict = lambda comments: None
    guard.NEGATIVE_VERDICTS = re.compile(r"FAIL|BLOCK", re.I)
    task_lock = types.ModuleType("task_action_lock")
    task_lock.task_lock = contextlib.nullcontext
    monkeypatch.setitem(sys.modules, "validator_verdict", validator_verdict)
    monkeypatch.setitem(sys.modules, "clickup_status_guard", guard)
    monkeypatch.setitem(sys.modules, "task_action_lock", task_lock)
    monkeypatch.setitem(sys.modules, "report_activity_journal", journal)

    actor = _load(MINI_SCRIPTS / "closeout_actor.py", f"closeout_actor_{id(object())}")
    journal.DEFAULT_STATE_DIR = tmp_path / "activity"
    monkeypatch.setattr(actor, "_post_clickup_comment", lambda *_args: (True, "ok"))
    monkeypatch.setattr(actor, "_cu_node", lambda _args: (False, "guard rejected"))
    ok, _message = actor._do_flip("86close", "owner/repo", 42)
    assert ok is False
    assert _records(journal.DEFAULT_STATE_DIR) == []

    monkeypatch.setattr(actor, "_cu_node", lambda _args: (True, "ok"))
    monkeypatch.setattr(
        actor,
        "_task_json",
        lambda _tid: (
            {
                "id": "86close",
                "status": {"status": "in review"},
                "date_updated": "1785630000456",
            },
            None,
        ),
    )
    first, _message = actor._do_flip("86close", "owner/repo", 42)
    second, _message = actor._do_flip("86close", "owner/repo", 42)
    assert first is second is True
    records = _records(journal.DEFAULT_STATE_DIR)
    assert len(records) == 1
    assert records[0]["kind"] == "review_handoff"
    assert records[0]["source"] == "closeout-actor"


def test_claim_fail_open_emits_no_claim_and_degrades_health(tmp_path, monkeypatch):
    claim = _load(CLAIM_PATH, f"claim_store_fail_open_{id(object())}")
    root = tmp_path / "activity"
    claim._activity_journal.DEFAULT_STATE_DIR = root

    def fail_path(_task_id):
        raise PermissionError("synthetic claims-dir denial")

    monkeypatch.setattr(claim, "_claim_path", fail_path)
    assert claim.acquire("86claim", "run-fail-open") is True
    assert _records(root) == []
    result = claim._activity_journal.health(state_dir=root)
    assert result["status"] == "UNKNOWN"
    assert any("fail-open" in reason for reason in result["reasons"])


def test_claim_event_follows_durable_claim_create(tmp_path):
    claim = _load(CLAIM_PATH, f"claim_store_durable_{id(object())}")
    claims = tmp_path / "claims"
    activity = tmp_path / "activity"
    claim.CLAIMS_DIR = str(claims)
    claim._activity_journal.DEFAULT_STATE_DIR = activity
    assert claim.acquire("86claim", "run-durable") is True
    assert (claims / "86claim.claim").is_file()
    records = _records(activity)
    assert [(row["kind"], row["run_id"]) for row in records] == [
        ("claim", "run-durable")
    ]


def test_governed_manifest_pins_all_shadow_outbox_producer_bytes():
    manifest = json.loads((MINI_SCRIPTS / "self_report_manifest.json").read_text())
    expected = {
        "report_activity_journal.py": JOURNAL_PATH,
        "claim_store.py": CLAIM_PATH,
        "closeout_actor.py": MINI_SCRIPTS / "closeout_actor.py",
    }
    entries = {item["src_rel"]: item for item in manifest["files"]}
    for name, path in expected.items():
        entry = entries[name]
        assert entry["dest_abs"] == f"~/.hermes/scripts/{name}"
        assert entry["deploy_mode"] == "script"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
