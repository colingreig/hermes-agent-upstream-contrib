#!/usr/bin/env python3
"""Tests for the lane filter in claim_next.py (ClickUp 86e2hw6fc).

The general clickup-executor cron has no lane awareness and was grabbing
content-dominant tasks (tagged lane:content) that are reserved for the
content-lane-executor cron — one such task ran 8 hours in the wrong
executor and produced nothing. Covers:

  - _is_content_lane(task): tag detection (dict and string tag shapes)
  - _lane_arg(argv): --lane {code,content,all} parsing, default 'code'
  - main() end-to-end: default/--lane code skips lane:content tasks,
    --lane content keeps only lane:content tasks, --lane all disables the
    filter, and tasks with no lane tag are always claimable.

No network calls, no real ~/.hermes/state writes — every scan test points
SNAP/claim state at a throwaway tempdir, matching test_claim_history.py's
pattern for this module.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MINI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
CLAIM_STORE_PATH = MINI_SCRIPTS_DIR / "claim_store.py"
CLAIM_NEXT_PATH = MINI_SCRIPTS_DIR / "clickup-queue-poller-claim_next.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class IsContentLaneTests(unittest.TestCase):
    """_is_content_lane(task) tag detection, both tag shapes."""

    def setUp(self):
        self.cn = _load_module(CLAIM_NEXT_PATH, f"claim_next_lane_ut_{id(self)}")

    def test_dict_tag_shape_matches(self):
        task = {"id": "t1", "tags": [{"name": "agent-ready"}, {"name": "lane:content"}]}
        self.assertTrue(self.cn._is_content_lane(task))

    def test_string_tag_shape_matches(self):
        task = {"id": "t1", "tags": ["agent-ready", "lane:content"]}
        self.assertTrue(self.cn._is_content_lane(task))

    def test_no_lane_tag_is_not_content(self):
        task = {"id": "t1", "tags": ["agent-ready"]}
        self.assertFalse(self.cn._is_content_lane(task))

    def test_no_tags_key_is_not_content(self):
        task = {"id": "t1"}
        self.assertFalse(self.cn._is_content_lane(task))

    def test_other_lane_tag_is_not_content(self):
        task = {"id": "t1", "tags": ["lane:code"]}
        self.assertFalse(self.cn._is_content_lane(task))


class LaneArgParsingTests(unittest.TestCase):
    """_lane_arg(argv) CLI parsing."""

    def setUp(self):
        self.cn = _load_module(CLAIM_NEXT_PATH, f"claim_next_lane_arg_ut_{id(self)}")

    def test_no_flag_defaults_to_code(self):
        self.assertEqual(self.cn._lane_arg([]), "code")

    def test_space_separated_flag(self):
        self.assertEqual(self.cn._lane_arg(["--lane", "content"]), "content")

    def test_equals_separated_flag(self):
        self.assertEqual(self.cn._lane_arg(["--lane=all"]), "all")

    def test_unrecognized_value_falls_back_to_code(self):
        self.assertEqual(self.cn._lane_arg(["--lane", "bogus"]), "code")

    def test_trailing_flag_with_no_value_falls_back_to_code(self):
        self.assertEqual(self.cn._lane_arg(["--lane"]), "code")


class ClaimNextLaneScanTests(unittest.TestCase):
    """main() end-to-end: lane filter wired into the candidate scan."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cs = _load_module(CLAIM_STORE_PATH, f"claim_store_for_lane_{id(self)}")
        self.cs.CLAIM_HISTORY_DIR = os.path.join(self.tmp.name, "claim_history")
        self.cs.CLAIMS_DIR = os.path.join(self.tmp.name, "claims")
        with mock.patch.dict(sys.modules, {"claim_store": self.cs}):
            self.cn = _load_module(CLAIM_NEXT_PATH, f"claim_next_lane_scan_ut_{id(self)}")
        self.cn.claim_store = self.cs

        self.snap_path = os.path.join(self.tmp.name, "queue_snapshot.json")
        self.candidate_path = os.path.join(self.tmp.name, "first_claim_candidate.json")
        self.run_path = os.path.join(self.tmp.name, "first_claim_run.txt")

    def _write_snapshot(self, tasks):
        with open(self.snap_path, "w", encoding="utf-8") as f:
            json.dump({"tasks": tasks}, f)

    def _open_redirect(self):
        real_open = open
        candidate_path, run_path = self.candidate_path, self.run_path

        def _opener(path, *args, **kwargs):
            if path == "/tmp/first_claim_candidate.json":
                path = candidate_path
            elif path == "/tmp/first_claim_run.txt":
                path = run_path
            return real_open(path, *args, **kwargs)

        return _opener

    def _run_main(self, argv):
        with (
            mock.patch.object(self.cn, "SNAP", self.snap_path),
            mock.patch.object(self.cn, "subprocess") as mock_subprocess,
            mock.patch.object(sys, "argv", ["claim_next.py"] + argv),
            mock.patch("builtins.open", side_effect=self._open_redirect()),
        ):
            mock_subprocess.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            self.cn.main()
        with open(self.candidate_path, encoding="utf-8") as f:
            return json.load(f)

    def test_default_skips_content_lane_task(self):
        # No --lane flag: content-lane task must be passed over in favor of
        # the plain code task, even though the content task is higher priority.
        self._write_snapshot([
            {"id": "content-task", "status": "to do",
             "tags": ["agent-ready", "lane:content"],
             "priority": "urgent", "date_created": "1"},
            {"id": "code-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "low", "date_created": "2"},
        ])
        chosen = self._run_main([])
        self.assertEqual(chosen["id"], "code-task")

    def test_lane_code_skips_content_lane_task(self):
        self._write_snapshot([
            {"id": "content-task", "status": "to do",
             "tags": ["agent-ready", "lane:content"],
             "priority": "urgent", "date_created": "1"},
            {"id": "code-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "low", "date_created": "2"},
        ])
        chosen = self._run_main(["--lane", "code"])
        self.assertEqual(chosen["id"], "code-task")

    def test_lane_content_keeps_only_content_lane_task(self):
        self._write_snapshot([
            {"id": "content-task", "status": "to do",
             "tags": ["agent-ready", "lane:content"],
             "priority": "low", "date_created": "1"},
            {"id": "code-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "urgent", "date_created": "2"},
        ])
        chosen = self._run_main(["--lane", "content"])
        self.assertEqual(chosen["id"], "content-task")

    def test_lane_all_disables_filter(self):
        # --lane all: no lane filter — highest priority wins regardless of tag.
        self._write_snapshot([
            {"id": "content-task", "status": "to do",
             "tags": ["agent-ready", "lane:content"],
             "priority": "urgent", "date_created": "1"},
            {"id": "code-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "low", "date_created": "2"},
        ])
        chosen = self._run_main(["--lane", "all"])
        self.assertEqual(chosen["id"], "content-task")

    def test_task_with_no_lane_tag_always_claimable(self):
        # A task with no lane tag at all should never be excluded by any
        # --lane mode except the (mirror) content-only restriction.
        self._write_snapshot([
            {"id": "plain-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "normal", "date_created": "1"},
        ])
        self.assertEqual(self._run_main([])["id"], "plain-task")

        self._write_snapshot([
            {"id": "plain-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "normal", "date_created": "1"},
        ])
        self.assertEqual(self._run_main(["--lane", "code"])["id"], "plain-task")

        self._write_snapshot([
            {"id": "plain-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "normal", "date_created": "1"},
        ])
        self.assertEqual(self._run_main(["--lane", "all"])["id"], "plain-task")

    def test_lane_content_excludes_no_lane_tag_task(self):
        # --lane content is a strict restriction: a task with no lane tag is
        # not a content task, so it must NOT be picked up in content-only mode.
        self._write_snapshot([
            {"id": "plain-task", "status": "to do", "tags": ["agent-ready"],
             "priority": "urgent", "date_created": "1"},
            {"id": "content-task", "status": "to do",
             "tags": ["agent-ready", "lane:content"],
             "priority": "low", "date_created": "2"},
        ])
        chosen = self._run_main(["--lane", "content"])
        self.assertEqual(chosen["id"], "content-task")


if __name__ == "__main__":
    unittest.main()
