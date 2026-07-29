#!/usr/bin/env python3
"""
test_staleness_sweep.py — pure-logic unit tests for staleness_sweep.

Skips network; mocks `subprocess.run` for the gh/curl paths. Run with:
  $HOME/.hermes/hermes-agent/venv/bin/python3.11 -m unittest scripts/test_staleness_sweep.py -v

(When run from ~/.hermes/, the script imports from scripts/staleness_sweep.py.)
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Make sure scripts/ is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import staleness_sweep as ss  # noqa: E402


class TestClassifyPrFailure(unittest.TestCase):
    def test_green(self):
        checks = [
            {"conclusion": "SUCCESS", "name": "CI"},
            {"conclusion": "SUCCESS", "name": "Lint"},
        ]
        self.assertEqual(ss._classify_pr_failure(checks), "green")

    def test_no_checks_unknown(self):
        self.assertEqual(ss._classify_pr_failure([]), "unknown")

    def test_landable_only_identity_failure(self):
        # PR #14's exact failure shape: lint passes, smoke fails. Not landable (real test
        # failure). But a PR with ONLY Vercel identity failure IS landable.
        checks = [
            {"conclusion": "SUCCESS", "name": "Lint, typecheck, test"},
            {"state": "FAILURE", "context": "Vercel", "name": "No GitHub account was found matching the commit author email address"},
        ]
        self.assertEqual(ss._classify_pr_failure(checks), "landable")

    def test_genuinely_red_smoke_test_fail(self):
        checks = [
            {"conclusion": "SUCCESS", "name": "Lint, typecheck, test"},
            {"conclusion": "FAILURE", "name": "Report-outage smoke test"},
        ]
        self.assertEqual(ss._classify_pr_failure(checks), "genuinely_red")

    def test_mixed_red_and_flaky_is_genuinely_red(self):
        # Mixed: one flaky, one real failure -> not landable (conservative).
        checks = [
            {"conclusion": "FAILURE", "name": "Real test"},
            {"state": "FAILURE", "context": "Vercel"},
        ]
        self.assertEqual(ss._classify_pr_failure(checks), "genuinely_red")


class TestStableId(unittest.TestCase):
    def test_task_id(self):
        self.assertEqual(ss._stable_id({"task_id": "abc"}, "task"), "task:abc")

    def test_pr_id(self):
        self.assertEqual(ss._stable_id({"repo": "x/y", "number": 12}, "pr"), "pr:x/y#12")


class TestAgeHours(unittest.TestCase):
    def test_recent(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        age = ss._age_hours(ts)
        self.assertAlmostEqual(age, 2.0, places=1)

    def test_none(self):
        self.assertIsNone(ss._age_hours(None))

    def test_z_suffix(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        age = ss._age_hours(ts)
        self.assertAlmostEqual(age, 3.0, places=1)


class TestEmitDigest(unittest.TestCase):
    def test_empty_all_clear(self):
        d = ss._emit_digest([], [], {"first_seen": {}}, set())
        self.assertIn("all clear", d["text"])
        self.assertEqual(d["new"], 0)
        self.assertEqual(d["stale"], 0)
        self.assertEqual(d["total"], 0)

    def test_new_vs_persistent_split(self):
        # First run with 2 items -> both NEW, 0 PERSISTENT.
        items = [
            {"task_id": "t1", "name": "n1", "status": "ready for review", "url": "u1", "list": "L", "date_updated": "2026-01-01T00:00:00Z"},
            {"task_id": "t2", "name": "n2", "status": "in review", "url": "u2", "list": "L", "date_updated": "2026-01-01T00:00:00Z"},
        ]
        d = ss._emit_digest(items, [], {"first_seen": {}}, set())
        self.assertEqual(d["new"], 2)
        self.assertEqual(d["stale"], 0)
        self.assertIn("NEW (2)", d["text"])

        # Second run with same 2 items -> 0 NEW, 2 PERSISTENT.
        seen = {ss._stable_id(t, "task") for t in items}
        d2 = ss._emit_digest(items, [], {"first_seen": {sid: "2026-01-01" for sid in seen}}, seen)
        self.assertEqual(d2["new"], 0)
        self.assertEqual(d2["stale"], 2)
        self.assertIn("PERSISTENT (2)", d2["text"])

    def test_one_new_one_persistent(self):
        items = [
            {"task_id": "t1", "name": "n1", "status": "ready for review", "url": "u1", "list": "L", "date_updated": "2026-01-01T00:00:00Z"},
            {"task_id": "t2", "name": "n2", "status": "in review", "url": "u2", "list": "L", "date_updated": "2026-01-01T00:00:00Z"},
        ]
        # seen_before has t1 only.
        seen = {"task:t1"}
        d = ss._emit_digest(items, [], {"first_seen": {"task:t1": "2026-01-01"}}, seen)
        self.assertEqual(d["new"], 1)
        self.assertEqual(d["stale"], 1)

    def test_action_lines_present(self):
        items = [{"task_id": "t1", "name": "n1", "status": "ready for review", "url": "u1", "list": "L", "date_updated": "2026-01-01T00:00:00Z"}]
        prs = [
            {"repo": "x/y", "number": 1, "title": "title-landable", "url": "u", "mergeable": "MERGEABLE", "verdict": "landable", "age_h": 30.0, "created": "2026-01-01", "author": "me"},
            {"repo": "x/y", "number": 2, "title": "title-green", "url": "u", "mergeable": "MERGEABLE", "verdict": "green", "age_h": 30.0, "created": "2026-01-01", "author": "me"},
        ]
        d = ss._emit_digest(items, prs, {"first_seen": {}}, set())
        # Landable PR triggers the flaky/identity action line.
        self.assertIn("flaky/identity", d["text"])
        # Green PR triggers the "green but stale" action line.
        self.assertIn("green but stale", d["text"])
        # No genuinely_red items here, so the red-diagnose line should NOT appear.
        self.assertNotIn("GENUINELY RED", d["text"])


class TestAgeHoursMsEpoch(unittest.TestCase):
    """Verify _age_hours handles ClickUp's millisecond-epoch date_updated strings."""

    def test_ms_epoch_recent(self):
        import time
        ms_now = int(time.time() * 1000)
        ms_2h_ago = str(ms_now - 2 * 3600 * 1000)
        age = ss._age_hours(ms_2h_ago)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 2.0, places=1)

    def test_ms_epoch_old(self):
        import time
        ms_10h_ago = str(int(time.time() * 1000) - 10 * 3600 * 1000)
        age = ss._age_hours(ms_10h_ago)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 10.0, places=1)


class TestOrphanedInProgressRequeue(unittest.TestCase):
    """Unit tests for the orphaned in-progress requeue helpers."""

    def _make_task(self, task_id: str, status: str, age_h: float, hermes_worked: bool = True,
                   tags: list | None = None) -> dict:
        import time
        ms_ts = str(int((time.time() - age_h * 3600) * 1000))
        fields = []
        if hermes_worked:
            fields = [{"id": ss.WORKED_BY_FIELD_ID, "value": [ss.HERMES_OPTION_ORDERINDEX]}]
        if tags is None:
            tags = ["agent-ready"]  # realistic default: a requeue-eligible strand
        return {
            "id": task_id,
            "name": f"Task {task_id}",
            "status": {"status": status},
            "list": {"name": "Test List"},
            "date_updated": ms_ts,
            "custom_fields": fields,
            "tags": [{"name": n} for n in tags],
        }

    def test_tag_eligible_requires_agent_ready(self):
        self.assertTrue(ss._requeue_tag_eligible(self._make_task("a", "in progress", 8, tags=["agent-ready"])))
        # No agent-ready → not eligible (poll-gate would not re-claim it)
        self.assertFalse(ss._requeue_tag_eligible(self._make_task("b", "in progress", 8, tags=["prepped"])))

    def test_tag_eligible_excludes_parked(self):
        # A deliberate park must never be requeued, even if agent-ready lingers
        self.assertFalse(ss._requeue_tag_eligible(
            self._make_task("c", "in progress", 8, tags=["park-blocked-external"])))
        self.assertFalse(ss._requeue_tag_eligible(
            self._make_task("d", "in progress", 8, tags=["agent-ready", "park-blocked-external"])))
        self.assertFalse(ss._requeue_tag_eligible(
            self._make_task("e", "in progress", 8, tags=["agent-ready", "agent-avoid"])))

    def test_is_in_progress_true(self):
        t = self._make_task("t1", "in progress", 10)
        self.assertTrue(ss._is_in_progress(t))

    def test_is_in_progress_false_for_review(self):
        t = self._make_task("t1", "ready for review", 10)
        self.assertFalse(ss._is_in_progress(t))

    def test_requeue_dry_run(self):
        """In dry-run mode, requeue produces WOULD lines without calling curl."""
        original_dry = ss.DRY_RUN
        ss.DRY_RUN = True
        try:
            orphans = [{"task_id": "t99", "name": "Orphan task", "list": "L", "age_h": 10.0,
                        "status": "in progress", "url": "https://app.clickup.com/t/t99"}]
            requeued, skipped = ss._requeue_orphaned_inprogress("fake-token", orphans)
            self.assertEqual(len(requeued), 1)
            self.assertIn("WOULD", requeued[0])
            self.assertIn("t99", requeued[0])
            self.assertEqual(skipped, [])
        finally:
            ss.DRY_RUN = original_dry

    def test_requeue_skips_fresh_tasks(self):
        """Tasks updated < STALENESS_INPROGRESS_HOURS ago must be absent from orphan list."""
        # _sweep_orphaned_inprogress filters by age; simulate fresh task (0.5h old)
        fresh = self._make_task("fresh1", "in progress", 0.5, hermes_worked=True)
        # We can't call _sweep_orphaned_inprogress without a token (network), so test
        # the filtering logic directly via the component parts.
        age = ss._age_hours(fresh["date_updated"])
        self.assertIsNotNone(age)
        self.assertLess(age, ss.STALENESS_INPROGRESS_HOURS)  # < 6h → would be skipped

    def test_requeue_includes_stale_unclaimed(self):
        """Tasks > STALENESS_INPROGRESS_HOURS and not live-claimed pass the filter."""
        stale = self._make_task("stale1", "in progress", 8.0, hermes_worked=True)
        age = ss._age_hours(stale["date_updated"])
        self.assertIsNotNone(age)
        self.assertGreater(age, ss.STALENESS_INPROGRESS_HOURS)  # > 6h → would pass age gate
        # is_claimed returns False for unknown task ids (no .claim file)
        self.assertFalse(ss._is_live_claimed("stale1"))


class TestRecordFirstSeen(unittest.TestCase):
    def test_adds_new_drops_resolved(self):
        state = {"first_seen": {"task:old": "2026-01-01", "pr:x/y#1": "2026-01-01"}}
        items = [{"task_id": "new", "name": "n", "status": "ready for review", "url": "u", "list": "L", "date_updated": "2026-01-01T00:00:00Z"}]
        ss._record_first_seen(state, items, "task")
        # new item added, old task gone (no longer present), pr preserved (kind mismatch)
        self.assertIn("task:new", state["first_seen"])
        self.assertNotIn("task:old", state["first_seen"])
        self.assertIn("pr:x/y#1", state["first_seen"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
