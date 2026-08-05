"""Behavior coverage for verdict-less PR recovery in the review poll gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_pipeline import review_poll_gate as gate  # noqa: E402


def test_main_tags_linked_verdict_less_pr_through_packaged_write_path(
    monkeypatch, tmp_path
):
    """A qualifying existing orphan is repaired on the production gate tick."""
    state_path = tmp_path / "review-state.json"
    snapshot_path = tmp_path / "review-snapshot.json"
    add_tag_calls: list[tuple[str, str]] = []
    gh_calls: list[list[str]] = []

    monkeypatch.setattr(gate, "STATE_PATH", str(state_path))
    monkeypatch.setattr(gate, "SNAPSHOT_PATH", str(snapshot_path))
    monkeypatch.setattr(gate, "ORPHAN_SCAN_INTERVAL_S", 2 * 60 * 60)
    monkeypatch.setattr(gate.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(gate, "_token", lambda: "test-token")
    monkeypatch.setattr(gate, "_merge_sweep", lambda: 0)
    monkeypatch.setattr(gate, "_revalidation_sweep", lambda: 0)
    monkeypatch.setattr(gate, "_human_merge_sweep", lambda: 0)
    monkeypatch.setattr(gate, "_scan", lambda: [])
    monkeypatch.setattr(
        gate.pr_pipeline_improvements,
        "route_conflicting_prs_to_executor",
        lambda _allowlist, dry_run=False: 0,
    )
    monkeypatch.setattr(
        gate.pr_pipeline_event_driven,
        "wake_validator_if_needed",
        lambda _allowlist: (0, []),
    )
    monkeypatch.setattr(
        gate.autonomous_merge,
        "_load_allowlist",
        lambda: {"colingreig/hermes-agent-upstream-contrib"},
    )
    monkeypatch.setattr(gate.validator_verdict, "load_verdicts", lambda: {})
    monkeypatch.setattr(gate, "_task_has_needs_validation_tag", lambda _task_id: False)
    monkeypatch.setattr(gate, "_task_is_human_fenced", lambda _task_id: False)
    monkeypatch.setattr(
        gate.hermes_validate_ops,
        "add_tag",
        lambda task_id, tag: add_tag_calls.append((task_id, tag)) or True,
    )

    def run(args, **_kwargs):
        gh_calls.append(args)
        assert args[:3] == ["gh", "pr", "list"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 336,
                        "body": "content-qa report attached to ClickUp task 86e2kj1tr.",
                        "headRefName": "ignite-cycle-20260803-170826",
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(gate.subprocess, "run", run)

    assert gate.main() == 0
    assert len(gh_calls) == 1
    assert add_tag_calls == [("86e2kj1tr", gate.NEEDS_TAG)]
    assert json.loads(state_path.read_text())["last_orphan_scan_ts"] == 10_000.0


def test_failed_orphan_tag_write_does_not_advance_scan_timestamp(monkeypatch, tmp_path):
    state_path = tmp_path / "review-state.json"
    state_path.write_text(json.dumps({"last_orphan_scan_ts": 1.0}))
    monkeypatch.setattr(gate, "STATE_PATH", str(state_path))
    monkeypatch.setattr(gate.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(
        gate.autonomous_merge, "_load_allowlist", lambda: {"owner/repo"}
    )
    monkeypatch.setattr(gate.validator_verdict, "load_verdicts", lambda: {})
    monkeypatch.setattr(gate, "_task_has_needs_validation_tag", lambda _task_id: False)
    monkeypatch.setattr(gate, "_task_is_human_fenced", lambda _task_id: False)
    monkeypatch.setattr(gate.hermes_validate_ops, "add_tag", lambda *_args: False)
    monkeypatch.setattr(
        gate.pr_pipeline_event_driven,
        "wake_validator_if_needed",
        lambda _allowlist: (0, []),
    )
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"number": 7, "body": "ClickUp task 86abc", "headRefName": ""}]
            ),
            stderr="",
        ),
    )

    assert gate._orphan_pr_sweep() == 0
    assert json.loads(state_path.read_text())["last_orphan_scan_ts"] == 1.0


def test_orphan_sweep_preserves_human_approval_fence(monkeypatch, tmp_path):
    state_path = tmp_path / "review-state.json"
    add_tag_calls = []
    monkeypatch.setattr(gate, "STATE_PATH", str(state_path))
    monkeypatch.setattr(gate.time, "time", lambda: 10_000.0)
    monkeypatch.setattr(
        gate.autonomous_merge, "_load_allowlist", lambda: {"owner/repo"}
    )
    monkeypatch.setattr(gate.validator_verdict, "load_verdicts", lambda: {})
    monkeypatch.setattr(gate, "_task_has_needs_validation_tag", lambda _task_id: False)
    monkeypatch.setattr(gate, "_task_is_human_fenced", lambda _task_id: True)
    monkeypatch.setattr(
        gate.hermes_validate_ops,
        "add_tag",
        lambda *args: add_tag_calls.append(args) or True,
    )
    monkeypatch.setattr(
        gate.pr_pipeline_event_driven,
        "wake_validator_if_needed",
        lambda _allowlist: (0, []),
    )
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [{"number": 8, "body": "ClickUp task 86human", "headRefName": ""}]
            ),
            stderr="",
        ),
    )

    assert gate._orphan_pr_sweep() == 0
    assert add_tag_calls == []
    assert json.loads(state_path.read_text())["last_orphan_scan_ts"] == 10_000.0
