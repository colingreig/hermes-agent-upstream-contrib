"""Regression tests for first-fail human fences in the ClickUp poll gate.

The validator records ``class=no-measurement`` in its verdict comment but
writes the durable ClickUp tag ``needs-human``. The poll gate must consume the
tag that is actually produced, or a structurally-unmeasurable task can be
woken again despite its first-fail escalation.
"""

from __future__ import annotations

import pytest

import scripts.clickup_poll_gate as gate_mod


@pytest.fixture(autouse=True)
def _isolate_unrelated_hard_exclusions(monkeypatch):
    monkeypatch.setattr(
        gate_mod, "_is_oec_excluded_task", lambda task: (False, None)
    )
    monkeypatch.setattr(
        gate_mod, "_is_localization_task", lambda task: (False, None)
    )


def _task(*tags: str, status: str = "in progress", status_type: str = "custom"):
    return {
        "id": "task-1",
        "name": "Measure LCP without Lighthouse infrastructure",
        "status": {"status": status, "type": status_type},
        "tags": [{"name": tag} for tag in tags],
        "list": {"id": "list-1", "name": "Product Build"},
    }


@pytest.mark.parametrize(
    ("status", "status_type"),
    [
        ("to do", "open"),
        ("in progress", "custom"),
        ("in review", "custom"),
    ],
)
def test_needs_human_fence_excludes_task_from_every_gate_bucket(
    status, status_type
):
    task = _task(
        gate_mod.READY_TAG,
        gate_mod.NEEDS_HUMAN_TAG,
        "needs-validation",
        status=status,
        status_type=status_type,
    )

    assert gate_mod._classify(task) is None


def test_scan_queue_does_not_rearm_first_fail_needs_human_task(monkeypatch):
    task = _task(
        gate_mod.READY_TAG,
        gate_mod.NEEDS_HUMAN_TAG,
        "needs-validation",
    )
    monkeypatch.setattr(
        gate_mod.clickup_sync,
        "load_team_task_index",
        lambda: {"tasks": [task], "errors": []},
    )

    assert gate_mod._scan_queue() == ([], [], [])


def test_recovery_tick_does_not_reprobe_needs_human_task(monkeypatch):
    task = _task(
        gate_mod.READY_TAG,
        gate_mod.NEEDS_HUMAN_TAG,
        "needs-validation",
    )
    monkeypatch.setattr(gate_mod, "_fetch_task", lambda task_id: task)

    assert gate_mod._recover(["task-1"]) == ([], [], [], ["task-1"])


def test_legacy_no_measurement_tag_remains_a_hard_fence():
    task = _task(gate_mod.READY_TAG, gate_mod.NO_MEASUREMENT_TAG)

    assert gate_mod._classify(task) is None


def test_fixable_validator_fail_remains_a_continuation():
    task = _task(gate_mod.READY_TAG, "validate-failed")

    assert gate_mod._classify(task) == "continuation"
