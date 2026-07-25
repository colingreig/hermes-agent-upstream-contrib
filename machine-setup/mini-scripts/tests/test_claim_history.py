#!/usr/bin/env python3
"""Tests for the claim retry cap (ClickUp 86e2ddcpb, 2026-07-24).

Covers the two halves of the fix:
  - claim_store.py: record_attempt / count_attempts / last_failure_note
  - clickup-queue-poller-claim_next.py: _over_attempt_cap / _handle_attempt_cap_exceeded

Every claim_store.py entry point is documented as FAIL-OPEN (a bug here must
never block a legitimate claim), so the dominant theme of these tests is
proving that corrupt/missing/unreadable state resolves to "under cap" /
"no history" rather than raising or wrongly refusing a claim.

No network calls, no real ~/.hermes/state writes — every test points
CLAIM_HISTORY_DIR at a throwaway tempdir.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MINI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLAIM_STORE_PATH = MINI_SCRIPTS_DIR / "claim_store.py"
CLAIM_NEXT_PATH = MINI_SCRIPTS_DIR / "clickup-queue-poller-claim_next.py"

DAY = 24 * 60 * 60


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class ClaimStoreHistoryTests(unittest.TestCase):
    """record_attempt / count_attempts / last_failure_note in isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Fresh module instance per test so class-level state never leaks.
        self.cs = _load_module(CLAIM_STORE_PATH, f"claim_store_ut_{id(self)}")
        self.cs.CLAIM_HISTORY_DIR = os.path.join(self.tmp.name, "claim_history")

    def test_record_and_count_under_cap(self):
        self.cs.record_attempt("taskA", "fail", note="boom")
        self.cs.record_attempt("taskA", "fail", note="boom again")
        self.assertEqual(self.cs.count_attempts("taskA", DAY), 2)

    def test_count_is_scoped_per_task_id(self):
        self.cs.record_attempt("taskA", "fail")
        self.cs.record_attempt("taskB", "fail")
        self.cs.record_attempt("taskB", "fail")
        self.assertEqual(self.cs.count_attempts("taskA", DAY), 1)
        self.assertEqual(self.cs.count_attempts("taskB", DAY), 2)

    def test_window_rolls_off_stale_entries(self):
        path = self.cs._history_path("taskA")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([
                {"ts": time.time() - (DAY + 3600), "run": "old", "outcome": "fail", "note": ""},
                {"ts": time.time() - 60, "run": "recent", "outcome": "fail", "note": ""},
            ], f)
        # Only the entry inside the trailing 24h window counts.
        self.assertEqual(self.cs.count_attempts("taskA", DAY), 1)

    def test_missing_history_file_counts_zero(self):
        self.assertEqual(self.cs.count_attempts("never-seen", DAY), 0)

    def test_corrupt_history_file_fails_open_to_zero(self):
        path = self.cs._history_path("taskA")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json,,,")
        self.assertEqual(self.cs.count_attempts("taskA", DAY), 0)
        # record_attempt must also survive a corrupt file rather than raising.
        self.cs.record_attempt("taskA", "fail")
        self.assertEqual(self.cs.count_attempts("taskA", DAY), 1)  # self-healed

    def test_unwritable_history_dir_fails_open(self):
        # Point CLAIM_HISTORY_DIR at a plain FILE so os.makedirs(..., exist_ok=True)
        # raises — proves record_attempt/count_attempts/last_failure_note never
        # propagate that error to the caller.
        bad_dir = os.path.join(self.tmp.name, "not_a_directory")
        with open(bad_dir, "w", encoding="utf-8") as f:
            f.write("x")
        self.cs.CLAIM_HISTORY_DIR = bad_dir
        self.assertEqual(self.cs.count_attempts("taskA", DAY), 0)
        self.assertEqual(self.cs.last_failure_note("taskA"), "")
        self.cs.record_attempt("taskA", "fail")  # must not raise

    def test_history_capped_at_max_entries(self):
        self.cs.MAX_HISTORY_ENTRIES = 3
        for i in range(5):
            self.cs.record_attempt("taskA", "fail", note=str(i))
        history = self.cs._load_history("taskA")
        self.assertEqual([h["note"] for h in history], ["2", "3", "4"])

    def test_last_failure_note_skips_success_and_returns_most_recent(self):
        self.cs.record_attempt("taskA", "fail", note="first failure")
        self.cs.record_attempt("taskA", "success", note="ignored")
        self.assertEqual(self.cs.last_failure_note("taskA"), "first failure")
        self.cs.record_attempt("taskA", "crash", note="second failure")
        self.assertEqual(self.cs.last_failure_note("taskA"), "second failure")


class ClaimNextAttemptCapTests(unittest.TestCase):
    """_over_attempt_cap / _handle_attempt_cap_exceeded as wired into claim_next.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cs = _load_module(CLAIM_STORE_PATH, f"claim_store_for_claim_next_{id(self)}")
        self.cs.CLAIM_HISTORY_DIR = os.path.join(self.tmp.name, "claim_history")
        # claim_next.py does `import claim_store` at module load time, resolved
        # against ~/.hermes/scripts on the real mini. Pre-seed sys.modules so
        # that import resolves to our isolated fixture instead of touching (or
        # requiring) any real path on the test machine.
        with mock.patch.dict(sys.modules, {"claim_store": self.cs}):
            self.cn = _load_module(CLAIM_NEXT_PATH, f"claim_next_ut_{id(self)}")
        self.cn.claim_store = self.cs  # belt-and-suspenders

    def test_under_cap_is_not_excluded(self):
        self.cs.record_attempt("taskA", "fail")
        self.assertFalse(self.cn._over_attempt_cap("taskA", cap=5))

    def test_over_cap_excludes(self):
        for _ in range(6):
            self.cs.record_attempt("taskA", "fail")
        self.assertTrue(self.cn._over_attempt_cap("taskA", cap=5))

    def test_exactly_at_cap_is_not_excluded(self):
        # Design brief: "if count > cap: exclude" — equal to cap must still grant.
        for _ in range(5):
            self.cs.record_attempt("taskA", "fail")
        self.assertFalse(self.cn._over_attempt_cap("taskA", cap=5))

    def test_over_cap_is_scoped_per_task_id(self):
        for _ in range(6):
            self.cs.record_attempt("taskA", "fail")
        self.assertFalse(self.cn._over_attempt_cap("taskB", cap=5))

    def test_corrupt_history_fails_open_under_cap(self):
        path = self.cs._history_path("taskA")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json at all")
        self.assertFalse(self.cn._over_attempt_cap("taskA", cap=5))

    def test_claim_store_unavailable_fails_open(self):
        self.cn.claim_store = None
        self.assertFalse(self.cn._over_attempt_cap("taskA", cap=0))

    def test_count_attempts_raising_fails_open(self):
        with mock.patch.object(self.cs, "count_attempts", side_effect=RuntimeError("boom")):
            self.assertFalse(self.cn._over_attempt_cap("taskA", cap=0))

    def test_handle_attempt_cap_exceeded_untags_tags_and_comments(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        self.cs.record_attempt("taskA", "fail", note="opencode finished but made no file changes")
        with mock.patch.object(self.cn.subprocess, "run", side_effect=fake_run):
            self.cn._handle_attempt_cap_exceeded("taskA", cap=5)

        self.assertEqual(len(calls), 3)
        untag_call, tag_call, comment_call = calls
        self.assertEqual(untag_call[2:5], ["untag", "taskA", "agent-ready"])
        self.assertEqual(tag_call[2:4], ["tag", "taskA"])
        self.assertTrue(tag_call[4].startswith("attempt-cap-exceeded-"))
        self.assertEqual(comment_call[2:4], ["comment", "taskA"])
        self.assertIn("exceeded the attempt cap", comment_call[4])
        self.assertIn("opencode finished but made no file changes", comment_call[4])

    def test_handle_attempt_cap_exceeded_never_raises_on_subprocess_error(self):
        with mock.patch.object(self.cn.subprocess, "run", side_effect=OSError("clickup unreachable")):
            self.cn._handle_attempt_cap_exceeded("taskA", cap=5)  # must not raise

    def test_scan_excludes_capped_task_from_claimable_candidates(self):
        # End-to-end through main(): a capped task must never reach `claimable`
        # (and therefore is never acquired), while an under-cap task still is.
        for _ in range(6):
            self.cs.record_attempt("capped-task", "fail")

        snap = {
            "tasks": [
                {"id": "capped-task", "status": "to do", "tags": ["agent-ready"],
                 "priority": "normal", "date_created": "1"},
                {"id": "fresh-task", "status": "to do", "tags": ["agent-ready"],
                 "priority": "normal", "date_created": "2"},
            ]
        }
        snap_path = os.path.join(self.tmp.name, "queue_snapshot.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snap, f)

        candidate_path = os.path.join(self.tmp.name, "first_claim_candidate.json")
        run_path = os.path.join(self.tmp.name, "first_claim_run.txt")

        with (
            mock.patch.object(self.cn, "SNAP", snap_path),
            mock.patch.object(self.cn, "subprocess") as mock_subprocess,
            mock.patch.object(self.cs, "CLAIMS_DIR", os.path.join(self.tmp.name, "claims")),
            mock.patch("builtins.open", side_effect=self._open_redirect(candidate_path, run_path)),
        ):
            mock_subprocess.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            self.cn.main()

        with open(candidate_path, encoding="utf-8") as f:
            chosen = json.load(f)
        self.assertEqual(chosen["id"], "fresh-task")

    @staticmethod
    def _open_redirect(candidate_path, run_path):
        real_open = open

        def _opener(path, *args, **kwargs):
            if path == "/tmp/first_claim_candidate.json":
                path = candidate_path
            elif path == "/tmp/first_claim_run.txt":
                path = run_path
            return real_open(path, *args, **kwargs)

        return _opener


if __name__ == "__main__":
    unittest.main()
