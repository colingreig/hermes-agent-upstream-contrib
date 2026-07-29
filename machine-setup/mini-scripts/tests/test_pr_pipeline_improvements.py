"""Regression tests for bounded recounts and durable conflict-route dedupe."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS / "pr_pipeline" / "pr_pipeline_improvements.py"
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    autonomous_merge = types.ModuleType("autonomous_merge")
    sys.modules["autonomous_merge"] = autonomous_merge
    slack = types.ModuleType("slack_msg_builder")
    sys.modules["slack_msg_builder"] = slack
    wake = types.ModuleType("pr_wake_and_sweep")
    wake.ALERT_LOG_PATH = Path("/tmp/alerts")
    wake.DEFAULT_ALLOWLIST = Path("/tmp/allowlist")
    wake.DEFAULT_MERGE_SCRIPT = Path("/tmp/merge")
    wake.DEFAULT_REPORT_PATH = Path("/tmp/report")
    wake.GitHubClient = lambda: object()
    wake.PullRequestState = object
    wake.build_rework_body = lambda state, task: f"rework {task}"
    wake.lacks_fresh_verdict = lambda verdict, now: verdict is None
    wake.load_allowlist = lambda _: ["org/repo"]
    wake.notify = mock.Mock()
    wake.run_sweep = mock.Mock()
    wake.scan_repos = lambda repos, now, gh, errors: ([], 0)
    wake.stale_without_verdict = lambda *args: False
    wake.utcnow = lambda: datetime(2026, 7, 25, tzinfo=timezone.utc)
    sys.modules["pr_wake_and_sweep"] = wake
    spec = importlib.util.spec_from_file_location(f"ppi_test_{_COUNTER}", MODULE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _state(repo="org/repo", number=7, mergeable=True, verdict=None, merge_state="clean"):
    return types.SimpleNamespace(repo=repo, number=number, url=f"https://example/{number}", mergeable=mergeable,
                                 latest_verdict_at=verdict, mergeable_state=merge_state)


class BacklogStatusTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_recount_emits_incremental_progress_and_factual_final_count(self):
        progress = []
        def scan(repos, now, gh, errors):
            return ([_state(number=1), _state(number=2, mergeable=False)], 1)
        report = self.mod.count_unverdicted_mergeable_backlog(
            ["one", "two"], phase="before", progress=progress.append, scan=scan,
        )
        self.assertEqual(report["count"], 2)
        self.assertEqual(report["repos_scanned"], 2)
        self.assertTrue(report["complete"])
        self.assertIn("progress 1/2 repo=one unverdicted_mergeable=1", progress[0])
        self.assertIn("progress 2/2 repo=two unverdicted_mergeable=1", progress[1])
        self.assertIn("backlog before final count=2", progress[-1])

    def test_timeout_is_reported_and_later_repos_still_count(self):
        progress = []
        def bounded(operation, timeout):
            if len(progress) == 0:
                raise TimeoutError("timed out after 1s")
            return operation()
        with mock.patch.object(self.mod, "_run_bounded", side_effect=bounded):
            report = self.mod.count_unverdicted_mergeable_backlog(
                ["slow", "fast"], repo_timeout_s=1, progress=progress.append,
                scan=lambda repos, now, gh, errors: ([_state(repo=repos[0])], 1),
            )
        self.assertEqual(report["count"], 1)
        self.assertEqual(len(report["errors"]), 1)
        self.assertEqual(report["repos_attempted"], 2)
        self.assertEqual(report["repos_completed"], 1)
        self.assertEqual(report["repos_timed_out"], 1)
        self.assertFalse(report["complete"])
        self.assertIn("repo=slow timed_out", progress[0])
        self.assertIn("repo=fast unverdicted_mergeable=1", progress[1])

    def test_cap_is_explicit_and_does_not_scan_more_repositories(self):
        seen = []
        report = self.mod.count_unverdicted_mergeable_backlog(
            ["one", "two"], max_repos=1, progress=lambda _: None,
            scan=lambda repos, now, gh, errors: (seen.append(repos[0]) or [], 0),
        )
        self.assertEqual(seen, ["one"])
        self.assertTrue(report["repos_capped"])


class ConflictDurableDedupeTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.json"

    def test_lost_local_state_rebuilds_from_durable_comment_without_reposting(self):
        pr = _state(mergeable=False, merge_state="CONFLICTING")
        details = {"body": "", "title": "Fix task 86e25qqf9", "head": {"ref": "agent/86e25qqf9", "sha": "abc"}}
        gh = types.SimpleNamespace(pr_details=lambda repo, number: details)
        marker = self.mod._conflict_route_marker("org/repo", 7, "abc")
        with mock.patch.object(self.mod, "STATE_PATH", self.state_path), \
             mock.patch.object(self.mod, "GitHubClient", return_value=gh), \
             mock.patch.object(self.mod, "scan_repos", return_value=([pr], 0)), \
             mock.patch.object(self.mod, "_task_comment_page", return_value={"comments": [{"comment_text": marker}], "last_page": True}), \
             mock.patch.object(self.mod, "_task_comment") as post_comment, \
             mock.patch.object(self.mod, "_task_tag") as tag, \
             mock.patch.object(self.mod, "_set_status") as set_status:
            self.assertEqual(self.mod.route_conflicting_prs_to_executor(["org/repo"]), 0)
        post_comment.assert_not_called()
        tag.assert_not_called()
        set_status.assert_not_called()
        saved = __import__("json").loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["routed_conflicts"]["org/repo#7"]["head_sha"], "abc")

    def test_marker_on_later_page_suppresses_repost(self):
        marker = self.mod._conflict_route_marker("org/repo", 7, "abc")
        pages = [
            {"comments": [{"id": "new", "date": 200, "comment_text": "newer"}], "last_page": False},
            {"comments": [{"id": "old", "date": 100, "comment_text": marker}], "last_page": True},
        ]
        with mock.patch.object(self.mod, "_task_comment_page", side_effect=pages) as page:
            self.assertTrue(self.mod._has_durable_conflict_route("86e25qqf9", "org/repo", 7, "abc"))
        self.assertEqual(page.call_count, 2)
        self.assertEqual(page.call_args_list[1].args[1:], ("200", "new"))

    def test_comment_pagination_omits_initial_cursor_and_uses_later_cursor(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"comments": [], "last_page": true}'
        with mock.patch.object(self.mod, "_clickup_token", return_value="token"), \
             mock.patch("urllib.request.urlopen", return_value=response), \
             mock.patch("urllib.request.Request", side_effect=lambda url, **kwargs: url) as request:
            self.mod._task_comment_page("86e25qqf9")
            self.mod._task_comment_page("86e25qqf9", "200", "new")
        first_url = request.call_args_list[0].args[0]
        later_url = request.call_args_list[1].args[0]
        self.assertNotIn("?", first_url)
        self.assertTrue(later_url.endswith("?start=200&start_id=new"))

    def test_partial_route_failure_does_not_write_marker_or_suppress_retry(self):
        pr = _state(mergeable=False, merge_state="CONFLICTING")
        details = {"body": "", "title": "Fix task 86e25qqf9", "head": {"ref": "agent/86e25qqf9", "sha": "abc"}}
        gh = types.SimpleNamespace(pr_details=lambda repo, number: details)
        with mock.patch.object(self.mod, "STATE_PATH", self.state_path), \
             mock.patch.object(self.mod, "GitHubClient", return_value=gh), \
             mock.patch.object(self.mod, "scan_repos", return_value=([pr], 0)), \
             mock.patch.object(self.mod, "_has_durable_conflict_route", return_value=False), \
             mock.patch.object(self.mod, "_task_tag", side_effect=[True, False, True, True]) as tag, \
             mock.patch.object(self.mod, "_set_status", return_value=True) as set_status, \
             mock.patch.object(self.mod, "_task_comment", return_value=True) as post_comment:
            self.assertEqual(self.mod.route_conflicting_prs_to_executor(["org/repo"]), 0)
            self.assertEqual(self.mod.route_conflicting_prs_to_executor(["org/repo"]), 1)
        self.assertEqual(tag.call_count, 4)
        set_status.assert_called_once()
        post_comment.assert_called_once()


if __name__ == "__main__":
    unittest.main()
