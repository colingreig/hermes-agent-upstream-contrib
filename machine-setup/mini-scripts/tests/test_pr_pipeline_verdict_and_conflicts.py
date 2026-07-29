# ~/.hermes/scripts/tests/test_pr_pipeline_verdict_and_conflicts.py
"""Regression tests for the three 2026-07-22 pr-pipeline fixes
(task 86e25qqf9): the verdict_at lookup, the conflict-routing task-id
extraction (widened to titles/branch refs), and the staleness-alert wiring.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pws = _load("pws", "pr_wake_and_sweep.py")
ppi = _load("ppi", "pr_pipeline_improvements.py")


# ---------------------------------------------------------------------------
# Fix 1: verdict_timestamp_for_pr reads the real store, not GitHub comments.

def test_verdict_timestamp_reads_local_store(monkeypatch):
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    record = {"verdict": "PASS", "head_sha": "abc123", "ts": now.isoformat()}
    monkeypatch.setattr(pws.validator_verdict, "verdict_for", lambda repo, pr: record)
    got = pws.verdict_timestamp_for_pr("colingreig/x", 1, "abc123")
    assert got == now


def test_verdict_timestamp_none_when_store_empty(monkeypatch):
    monkeypatch.setattr(pws.validator_verdict, "verdict_for", lambda repo, pr: None)
    assert pws.verdict_timestamp_for_pr("colingreig/x", 1, "abc123") is None


def test_verdict_timestamp_stale_head_does_not_count_as_fresh(monkeypatch):
    """A verdict recorded against an OLD head SHA must not be treated as
    covering a PR that has since been pushed to a new head — otherwise a
    stale PASS could mask a real new defect."""
    now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    record = {"verdict": "PASS", "head_sha": "old-sha", "ts": now.isoformat()}
    monkeypatch.setattr(pws.validator_verdict, "verdict_for", lambda repo, pr: record)
    assert pws.verdict_timestamp_for_pr("colingreig/x", 1, "new-sha") is None


def test_lacks_fresh_verdict_true_when_no_record():
    now = pws.utcnow()
    assert pws.lacks_fresh_verdict(None, now) is True


def test_lacks_fresh_verdict_false_within_24h():
    now = pws.utcnow()
    recent = now - timedelta(hours=1)
    assert pws.lacks_fresh_verdict(recent, now) is False


# ---------------------------------------------------------------------------
# Fix 2: conflict-routing task-id extraction widened to titles/branch refs.
# Ground truth: islandwellservice.ca#5 (title "... (agent/86e1z3cdp)") and
# hvacservicebellevue.com#32 (title "... (task 86e20ftzu)") — neither PR body
# contains a clickup.com/t/ link or the literal words "ClickUp task", so the
# pre-fix body-only extraction silently skipped both.

def test_extract_task_id_from_body_link_unchanged():
    assert ppi._extract_clickup_task_id("see https://app.clickup.com/t/86e29q8mj") == "86e29q8mj"


def test_extract_task_id_from_title_agent_branch_paren():
    title = "[Humanizer] De-AI blog corpus — editorial pass (agent/86e1z3cdp)"
    assert ppi._extract_clickup_task_id(title) == "86e1z3cdp"


def test_extract_task_id_from_title_bare_task_word():
    title = "Fix: use literal comma in decoded redirect rows (task 86e20ftzu)"
    assert ppi._extract_clickup_task_id(title) == "86e20ftzu"


def test_extract_task_id_from_head_ref():
    assert ppi._extract_clickup_task_id("agent/86e1z3cdp") == "86e1z3cdp"


def test_extract_task_id_none_when_nothing_matches():
    assert ppi._extract_clickup_task_id("just a plain PR with no ids") == ""


# ---------------------------------------------------------------------------
# Fix 3: staleness alert — the check itself is unchanged behavior-wise; this
# guards that it stays silent with an empty allowlist and doesn't explode.

def test_check_staleness_and_alert_empty_allowlist_is_a_noop():
    assert ppi.check_staleness_and_alert([], dry_run=True) == []
