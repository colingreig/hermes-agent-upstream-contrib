"""Behavior tests for the platform=manual executor handoff policy.

The policy under test: on a repo `ignite-ship` classifies PLATFORM=manual there
is no deploy the executor can drive, so a CI-green PR is the complete executor
deliverable and the task belongs in `in review` WITH a review packet — never
parked in `in progress` behind an undeployable-platform block.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MINI_SCRIPTS = ROOT / "machine-setup" / "mini-scripts"
MODULE_PATH = MINI_SCRIPTS / "manual_platform_handoff.py"

_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    name = f"manual_platform_handoff_ut_{_COUNTER}"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


@pytest.fixture()
def mod():
    return _load_module()


NOW = dt.datetime(2026, 8, 3, 12, 0, 0, tzinfo=dt.timezone.utc)


def _green_rollup(minutes_ago=60, count=2):
    completed = (NOW - dt.timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return [
        {"__typename": "CheckRun", "name": f"check-{i}", "status": "COMPLETED",
         "conclusion": "SUCCESS", "completedAt": completed}
        for i in range(count)
    ]


def _task(status="in progress", task_id="86e2ky2dk"):
    return {"id": task_id, "status": {"status": status}}


def _pr(**overrides):
    pr = {"number": 304, "title": "ignite- 86e2kmud4: add fleet contention controls",
          "body": "Closes https://app.clickup.com/t/86e2kmud4",
          "headRefName": "agent/86e2kmud4", "state": "OPEN",
          "statusCheckRollup": _green_rollup()}
    pr.update(overrides)
    return pr


# --------------------------------------------------------------------------
# Platform classification
# --------------------------------------------------------------------------

def test_baseline_manual_repos_hold_when_hints_file_is_absent(mod, tmp_path):
    names = mod.load_manual_platform_repos(hints_path=str(tmp_path / "nope.json"))
    assert "hermes-agent" in names
    assert "hermes-agent-upstream-contrib" in names


def test_platform_hints_extend_the_manual_set(mod, tmp_path):
    hints = tmp_path / "platform-hints.json"
    hints.write_text(json.dumps({
        "some-legacy-site": {"platform": "manual", "deploy_on_push": False},
        "a-vercel-app": {"platform": "vercel"},
    }), encoding="utf-8")
    names = mod.load_manual_platform_repos(hints_path=str(hints))
    assert "some-legacy-site" in names
    assert "a-vercel-app" not in names
    # the pinned floor is never dropped by a hints file
    assert "hermes-agent" in names


def test_hints_cannot_declassify_a_baseline_repo(mod, tmp_path):
    hints = tmp_path / "platform-hints.json"
    hints.write_text(json.dumps({"hermes-agent": {"platform": "vercel"}}), encoding="utf-8")
    assert "hermes-agent" in mod.load_manual_platform_repos(hints_path=str(hints))


def test_targets_are_the_intersection_with_the_allowlist(mod):
    targets = mod.manual_platform_targets(
        allowlist={"colingreig/hermes-agent-upstream-contrib", "ignite/some-web-app"},
        manual_names={"hermes-agent-upstream-contrib"},
    )
    assert targets == ["colingreig/hermes-agent-upstream-contrib"]


def test_missing_allowlist_fails_closed(mod, tmp_path):
    assert mod.load_allowlist(path=str(tmp_path / "absent.txt")) == set()
    assert mod.manual_platform_targets(
        allowlist=mod.load_allowlist(path=str(tmp_path / "absent.txt")),
        manual_names={"hermes-agent"},
    ) == []


# --------------------------------------------------------------------------
# Task-id extraction
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body,head,expected", [
    ("Closes https://app.clickup.com/t/86e2kmud4", "", "86e2kmud4"),
    ("ClickUp task: 86e2ky2dk", "", "86e2ky2dk"),
    ("", "agent/86e2kmud4", "86e2kmud4"),
    ("", "ignite-86e2kmud4", "86e2kmud4"),
    ("no reference at all", "main", ""),
])
def test_extract_task_id(mod, body, head, expected):
    assert mod.extract_task_id(body, head) == expected


def test_ambiguous_task_reference_is_refused(mod):
    body = ("clickup.com/t/86e2kmud4 and also clickup.com/t/86e2kxh59")
    assert mod.extract_task_id(body, "") == ""


# --------------------------------------------------------------------------
# CI summary
# --------------------------------------------------------------------------

def test_summarize_ci_green(mod):
    ci = mod.summarize_ci(_green_rollup(minutes_ago=30))
    assert ci["state"] == "green"
    assert ci["total"] == 2
    assert ci["settled_at"] == NOW - dt.timedelta(minutes=30)


def test_summarize_ci_failing_and_pending(mod):
    rollup = _green_rollup(count=1) + [
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
         "conclusion": "FAILURE", "completedAt": NOW.isoformat()},
        {"__typename": "CheckRun", "name": "lint", "status": "IN_PROGRESS",
         "conclusion": None},
    ]
    ci = mod.summarize_ci(rollup)
    assert ci["state"] == "failing"
    assert ci["failing"] == ["tests"]
    assert ci["pending"] == ["lint"]


def test_no_checks_is_not_green(mod):
    assert mod.summarize_ci([])["state"] == "none"
    assert mod.summarize_ci(None)["state"] == "none"


def test_neutral_and_skipped_do_not_block(mod):
    rollup = [
        {"__typename": "CheckRun", "name": "a", "status": "COMPLETED",
         "conclusion": "NEUTRAL", "completedAt": NOW.isoformat()},
        {"__typename": "CheckRun", "name": "b", "status": "COMPLETED",
         "conclusion": "SKIPPED", "completedAt": NOW.isoformat()},
    ]
    assert mod.summarize_ci(rollup)["state"] == "green"


def test_legacy_status_context_entries_are_understood(mod):
    rollup = [{"__typename": "StatusContext", "context": "ci/legacy",
               "state": "SUCCESS", "createdAt": NOW.isoformat()}]
    assert mod.summarize_ci(rollup)["state"] == "green"


# --------------------------------------------------------------------------
# The policy decision
# --------------------------------------------------------------------------

def test_ci_green_open_pr_hands_off_to_review(mod):
    action, detail = mod.evaluate(_pr(), _task(), ci=mod.summarize_ci(_green_rollup()),
                                  claim_live=False, now=NOW)
    assert action == "handoff"
    assert "in review" in detail


def test_merged_pr_is_left_to_the_closeout_actor(mod):
    action, detail = mod.evaluate(_pr(state="MERGED"), _task(),
                                  ci=mod.summarize_ci(_green_rollup()),
                                  claim_live=False, now=NOW)
    assert action == "skip"
    assert "closeout_actor" in detail


def test_failing_ci_never_hands_off(mod):
    rollup = [{"__typename": "CheckRun", "name": "tests", "status": "COMPLETED",
               "conclusion": "FAILURE", "completedAt": NOW.isoformat()}]
    action, _ = mod.evaluate(_pr(), _task(), ci=mod.summarize_ci(rollup),
                             claim_live=False, now=NOW)
    assert action == "skip"


def test_pending_ci_never_hands_off(mod):
    rollup = [{"__typename": "CheckRun", "name": "tests", "status": "IN_PROGRESS",
               "conclusion": None}]
    action, _ = mod.evaluate(_pr(), _task(), ci=mod.summarize_ci(rollup),
                             claim_live=False, now=NOW)
    assert action == "skip"


def test_live_claim_protects_a_running_executor(mod):
    action, detail = mod.evaluate(_pr(), _task(), ci=mod.summarize_ci(_green_rollup()),
                                  claim_live=True, now=NOW)
    assert action == "skip"
    assert "live executor claim" in detail


def test_recently_settled_ci_waits_for_the_idle_floor(mod):
    ci = mod.summarize_ci(_green_rollup(minutes_ago=1))
    action, detail = mod.evaluate(_pr(), _task(), ci=ci, claim_live=False, now=NOW,
                                  min_idle_seconds=600)
    assert action == "skip"
    assert "idle floor" in detail


def test_already_in_review_is_an_idempotent_noop(mod):
    action, _ = mod.evaluate(_pr(), _task(status="in review"),
                             ci=mod.summarize_ci(_green_rollup()),
                             claim_live=False, now=NOW)
    assert action == "skip"


def test_complete_task_is_never_touched(mod):
    action, _ = mod.evaluate(_pr(), _task(status="complete"),
                             ci=mod.summarize_ci(_green_rollup()),
                             claim_live=False, now=NOW)
    assert action == "skip"


def test_standing_validator_fail_blocks_the_handoff(mod):
    action, detail = mod.evaluate(_pr(), _task(), ci=mod.summarize_ci(_green_rollup()),
                                  claim_live=False, now=NOW,
                                  latest_validate_verdict="FAIL")
    assert action == "blocked"
    assert "FAIL" in detail


# --------------------------------------------------------------------------
# The review packet
# --------------------------------------------------------------------------

def test_review_packet_states_the_deploy_gate_and_what_to_validate(mod):
    packet = mod.build_review_packet(
        "colingreig/hermes-agent-upstream-contrib", 304,
        ci=mod.summarize_ci(_green_rollup()), task_id="86e2kmud4",
        pr_title="add fleet contention controls", now=NOW)
    assert "PLATFORM=manual" in packet
    assert "operator/poller gated" in packet
    assert "https://github.com/colingreig/hermes-agent-upstream-contrib/pull/304" in packet
    assert "What to validate now:" in packet
    assert "What to validate after the next release cut:" in packet
    assert "86e2kmud4" in packet


def test_review_packet_supersedes_a_prior_blocked_handoff(mod):
    packet = mod.build_review_packet(
        "colingreig/hermes-agent-upstream-contrib", 304,
        ci=mod.summarize_ci(_green_rollup()), task_id="86e2kmud4",
        blocked_marker=True, now=NOW)
    assert "supersedes it" in packet


def test_review_packet_claims_no_delegated_human_authority(mod):
    packet = mod.build_review_packet(
        "colingreig/hermes-agent-upstream-contrib", 304,
        ci=mod.summarize_ci(_green_rollup()), task_id="86e2kmud4", now=NOW)
    lowered = packet.lower()
    for banned in ("per colin", "approved by colin", "colin approved", "colin said"):
        assert banned not in lowered


def test_blocked_handoff_marker_detection(mod):
    assert mod._has_blocked_handoff_marker(
        [{"comment_text": "ignite- BLOCKED HANDOFF: mini unreachable"}])
    assert not mod._has_blocked_handoff_marker([{"comment_text": "ignite- claiming"}])
    assert not mod._has_blocked_handoff_marker([])


# --------------------------------------------------------------------------
# Sweep wiring
# --------------------------------------------------------------------------

def _stub_io(mod, monkeypatch, *, prs, task, comments=(), claim_live=False):
    posted, flipped = [], []
    monkeypatch.setattr(mod, "_gh_pr_list", lambda repo, limit=50: list(prs))
    monkeypatch.setattr(mod, "_fetch_task", lambda tid: task)
    monkeypatch.setattr(mod, "_fetch_comments", lambda tid: list(comments))
    monkeypatch.setattr(mod, "_claim_is_live", lambda tid: claim_live)
    monkeypatch.setattr(mod, "_negative_validate_verdict", lambda c: "")
    monkeypatch.setattr(mod, "_post_comment",
                        lambda tid, body: (posted.append((tid, body)), (True, "ok"))[1])
    monkeypatch.setattr(mod, "_set_status",
                        lambda tid, status: (flipped.append((tid, status)), (True, "ok"))[1])
    monkeypatch.setattr(mod, "_confirm", lambda tid: {"status": "ok", "confirmed": True})
    return posted, flipped


def test_sweep_posts_the_packet_before_flipping(mod, monkeypatch):
    order = []
    posted, flipped = _stub_io(mod, monkeypatch, prs=[_pr()], task=_task())
    monkeypatch.setattr(mod, "_post_comment",
                        lambda tid, body: (order.append("packet"), (True, "ok"))[1])
    monkeypatch.setattr(mod, "_set_status",
                        lambda tid, status: (order.append("status"), (True, "ok"))[1])
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"], now=NOW)
    assert [r["action"] for r in results] == ["handoff"]
    assert results[0]["handoff_ok"] is True
    assert order == ["packet", "status"]


def test_sweep_leaves_status_untouched_when_the_packet_fails(mod, monkeypatch):
    posted, flipped = _stub_io(mod, monkeypatch, prs=[_pr()], task=_task())
    monkeypatch.setattr(mod, "_post_comment", lambda tid, body: (False, "clickup 500"))
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"], now=NOW)
    assert results[0]["handoff_ok"] is False
    assert flipped == []
    assert "status left untouched" in results[0]["handoff_out"]


def test_sweep_reports_failure_when_read_after_write_cannot_confirm(mod, monkeypatch):
    _stub_io(mod, monkeypatch, prs=[_pr()], task=_task())
    monkeypatch.setattr(mod, "_confirm",
                        lambda tid: {"status": "UNKNOWN", "confirmed": False,
                                     "error": "clickup read failed"})
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"], now=NOW)
    assert results[0]["handoff_ok"] is False
    assert "read-after-write" in results[0]["handoff_out"]


def test_dry_run_writes_nothing(mod, monkeypatch):
    posted, flipped = _stub_io(mod, monkeypatch, prs=[_pr()], task=_task())
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"],
                        dry_run=True, now=NOW)
    assert results[0]["action"] == "handoff"
    assert posted == [] and flipped == []


def test_sweep_skips_a_repo_that_is_not_manual_platform(mod):
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"],
                        only_repo="ignite/some-web-app", now=NOW)
    assert results[0]["action"] == "skip"
    assert "not an allowlisted manual-platform repo" in results[0]["detail"]


def test_sweep_survives_a_gh_failure(mod, monkeypatch):
    def boom(repo, limit=50):
        raise mod.HandoffError("gh pr list rc=1: boom")
    monkeypatch.setattr(mod, "_gh_pr_list", boom)
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"], now=NOW)
    assert results[0]["action"] == "error"


def test_sweep_survives_an_unreadable_task(mod, monkeypatch):
    _stub_io(mod, monkeypatch, prs=[_pr()], task=_task())

    def boom(tid):
        raise mod.HandoffError("task fetch rc=1")
    monkeypatch.setattr(mod, "_fetch_task", boom)
    results = mod.sweep(targets=["colingreig/hermes-agent-upstream-contrib"], now=NOW)
    assert results[0]["action"] == "error"
    assert "could not read task" in results[0]["detail"]


def test_claim_liveness_is_unknown_safe(mod, monkeypatch):
    """No claim_store on this host must mean 'assume owned', never 'assume free'."""
    monkeypatch.setattr(mod, "_optional", lambda name: None)
    assert mod._claim_is_live("86e2kmud4") is True


def test_closeout_actor_drives_this_sweep(mod):
    """The policy is inert unless something runs it on a cadence."""
    source = (MINI_SCRIPTS / "closeout_actor.py").read_text(encoding="utf-8")
    assert "import manual_platform_handoff" in source
    assert "mph.sweep(" in source


def test_module_is_registered_for_deploy(mod):
    """A mini-script that no manifest governs never reaches ~/.hermes/scripts."""
    manifest = json.loads(
        (MINI_SCRIPTS / "self_report_manifest.json").read_text(encoding="utf-8"))
    srcs = {entry.get("src_rel") for entry in manifest.get("files", [])}
    assert "manual_platform_handoff.py" in srcs
