# ~/.hermes/scripts/tests/test_review_poll_gate.py
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "rpg", ROOT / "review_poll_gate.py")
rpg = importlib.util.module_from_spec(spec); spec.loader.exec_module(rpg)

def _task(status, tags):
    return {"id": "t1", "name": "x", "status": {"status": status},
            "tags": [{"name": n} for n in tags], "list": {"name": "L", "id": "1"},
            "url": "http://x"}

def test_pending_true_when_ready_for_review_and_needs_validation():
    assert rpg.is_pending(_task("ready for review", ["needs-validation"])) is True

def test_pending_false_without_needs_validation():
    assert rpg.is_pending(_task("ready for review", ["agent-ready"])) is False

def test_pending_is_tag_only_for_three_status_lists():
    assert rpg.is_pending(_task("in progress", ["needs-validation"])) is True

def test_pending_false_for_db_publish_lane(monkeypatch):
    monkeypatch.setattr(rpg, "_is_db_publish_task", lambda task_id: task_id == "t1")
    assert rpg.is_pending(_task("in progress", ["needs-validation"])) is False

def test_pending_false_for_needs_human_no_measurement_task():
    """86e29q8qd/86e2eu8a4: a task ESCALATEd to Needs Human for the
    no-measurement/external-blocked failure class must never be re-woken as a
    PR-validation handoff even if needs-validation is still (or gets
    re-)tagged — mirrors the DB-publish exclusion above."""
    assert rpg.is_pending(_task("in progress", ["needs-validation", "needs-human"])) is False


def test_pending_false_for_legacy_no_measurement_only_task():
    assert rpg.is_pending(_task("in progress", ["needs-validation", "no-measurement"])) is False


def test_revalidation_sweep_does_not_mutate_human_fenced_task(monkeypatch):
    monkeypatch.setattr(
        rpg.autonomous_merge,
        "sweep_gaps",
        lambda: [{"action": "needs-revalidation", "task_id": "fenced", "repo": "o/r", "pr": 1, "detail": "head moved"}],
    )
    monkeypatch.setattr(
        rpg,
        "_get",
        lambda _url: {"tags": [{"name": "needs-human"}, {"name": "no-measurement"}]},
    )
    monkeypatch.setattr(rpg.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not re-tag a fenced task"))

    assert rpg._revalidation_sweep() == 0


def test_revalidation_sweep_preserves_normal_fixable_revalidation(monkeypatch):
    monkeypatch.setattr(
        rpg.autonomous_merge,
        "sweep_gaps",
        lambda: [{"action": "needs-revalidation", "task_id": "fixable", "repo": "o/r", "pr": 2, "detail": "head moved"}],
    )
    monkeypatch.setattr(rpg, "_get", lambda _url: {"tags": [{"name": "validate-failed"}]})
    calls = []
    monkeypatch.setattr(
        rpg.subprocess,
        "run",
        lambda args, **_kwargs: calls.append(args) or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )

    assert rpg._revalidation_sweep() == 1
    assert calls[0][-2:] == ["fixable", "needs-validation"]

def test_entry_shape():
    e = rpg.entry(_task("ready for review", ["needs-validation"]))
    assert e["id"] == "t1" and e["list_id"] == "1"

def test_wake_cooldown_blocks_recent_wake():
    assert rpg.wake_allowed({"last_wake_ts": 1000}, now=1000 + 60) is False     # <20m
    assert rpg.wake_allowed({"last_wake_ts": 1000}, now=1000 + 20*60 + 1) is True

def test_extract_clickup_task_id_from_executor_branch_when_body_is_empty():
    assert rpg._extract_clickup_task_id("", "agent/86e29q8pg") == "86e29q8pg"

def test_extract_clickup_task_id_from_nested_executor_branch():
    assert rpg._extract_clickup_task_id(None, "prefix/agent/86e1yjkmp-fix") == "86e1yjkmp"

def test_body_task_id_takes_precedence_over_branch_fallback():
    body = "ClickUp task 86ebody123"
    assert rpg._extract_clickup_task_id(body, "agent/86ebranch9") == "86ebody123"

def test_non_executor_branch_is_not_treated_as_clickup_task():
    assert rpg._extract_clickup_task_id("", "feature/86e29q8pg") == ""
