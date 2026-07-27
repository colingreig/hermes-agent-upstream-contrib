#!/usr/bin/env python3
"""Tests for the pr-staleness-alert dedupe fix (mini cron job 3043a00e6df8).

Root cause being fixed: the old check_staleness_and_alert() dedupe
fingerprinted each stale PR on round(age_hours, 2) — a value that changes on
almost every 15-minute tick — so its state comparison never matched two runs
in a row and Slack got an identical payload every 15 minutes (confirmed
byte-identical across six consecutive runs, 257 total runs since 2026-07-22,
for the same 6 stale PRs).

pr_staleness_alert.py now owns its own dedupe: fingerprint on
(repo, PR number) -> a COARSE age bucket, persisted at a JSON state path, and
post only on a fingerprint change or a digest-interval heartbeat. Every
dedupe entry point is documented FAIL-OPEN: a bug reading state must never
suppress a real alert, so the dominant theme here (mirroring
test_claim_history.py's contract for claim_store.py) is proving that
corrupt/missing/unreadable state resolves to "post" rather than "suppress".

No network calls, no `gh` subprocess, no real ~/.hermes/state writes —
autonomous_merge / pr_pipeline_improvements / slack_msg_builder (none of
which are vendored into this repo — they're mini-only and untouched by this
fix) are replaced with minimal stub modules before the file under test is
imported, then individual attributes are monkeypatched per test.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

MINI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = MINI_SCRIPTS_DIR / "pr_staleness_alert.py"

_COUNTER = 0


def _install_stub_deps() -> types.ModuleType:
    """Install stand-in autonomous_merge / pr_pipeline_improvements /
    slack_msg_builder modules and return the ppi stub so tests can
    monkeypatch its attributes (utcnow, scan_repos, notify, ...)."""
    autonomous_merge = types.ModuleType("autonomous_merge")
    autonomous_merge._load_allowlist = lambda: ["org/repo"]
    sys.modules["autonomous_merge"] = autonomous_merge

    ppi = types.ModuleType("pr_pipeline_improvements")
    ppi.utcnow = lambda: datetime.now(timezone.utc)
    ppi.GitHubClient = lambda: object()
    ppi.scan_repos = lambda repos, now, gh, errors: ([], 0)
    ppi.stale_without_verdict = lambda created_at, verdict_at, now: True
    ppi.notify = mock.Mock()
    ppi.ALERT_LOG_PATH = Path("/tmp/pr_wake_alerts.log")
    sys.modules["pr_pipeline_improvements"] = ppi

    pipeline = types.ModuleType("pr_pipeline")
    pipeline.__path__ = []
    verdicts = types.ModuleType("pr_pipeline.validator_verdict")
    verdicts.finalization_count = lambda: 0
    pipeline.validator_verdict = verdicts
    sys.modules["pr_pipeline"] = pipeline
    sys.modules["pr_pipeline.validator_verdict"] = verdicts

    smb = types.ModuleType("slack_msg_builder")
    smb.build_status_message = lambda emoji, headline, **kw: f"{emoji} {headline}"
    smb.build_alert_message = lambda emoji, headline, **kw: (
        f"{emoji} {headline}\n" + "\n".join(kw.get("facts") or [])
        + (f"\n{kw['footer']}" if kw.get("footer") else "")
    )
    sys.modules["slack_msg_builder"] = smb

    return ppi


def _load_module():
    global _COUNTER
    _COUNTER += 1
    ppi = _install_stub_deps()
    spec = importlib.util.spec_from_file_location(f"pr_staleness_alert_ut_{_COUNTER}", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod, ppi


def _pr_state(repo: str, number: int, age_hours: float, now: datetime):
    return types.SimpleNamespace(
        repo=repo,
        number=number,
        url=f"https://github.com/{repo}/pull/{number}",
        created_at=now - timedelta(hours=age_hours),
        mergeable=False,
        mergeable_state="dirty",
        latest_verdict_at=None,
    )


def _alert_pr(repo: str, number: int, age_hours: float) -> dict[str, object]:
    return {"repo": repo, "pr": number, "age_hours": age_hours}


class AgeBucketAndFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.mod, self.ppi = _load_module()

    def test_age_bucket_below_first_threshold_is_zero(self):
        self.assertEqual(self.mod.age_bucket(1.0, (24.0, 72.0, 168.0)), 0)

    def test_age_bucket_crosses_each_threshold(self):
        thresholds = (24.0, 72.0, 168.0)
        self.assertEqual(self.mod.age_bucket(25.0, thresholds), 1)
        self.assertEqual(self.mod.age_bucket(80.0, thresholds), 2)
        self.assertEqual(self.mod.age_bucket(200.0, thresholds), 3)

    def test_fingerprint_is_insensitive_to_raw_age(self):
        # This is the actual bug being fixed: two runs 15 minutes apart with
        # a slightly different raw age must fingerprint identically.
        thresholds = (24.0, 72.0, 168.0)
        run1 = [{"repo": "org/repo", "pr": 1, "age_hours": 30.00}]
        run2 = [{"repo": "org/repo", "pr": 1, "age_hours": 30.25}]
        self.assertEqual(
            self.mod.build_fingerprint(run1, thresholds),
            self.mod.build_fingerprint(run2, thresholds),
        )


class MinAgeHoursTests(unittest.TestCase):
    def setUp(self):
        self.mod, self.ppi = _load_module()

    def _set_min_age_env(self, raw: str):
        patcher = mock.patch.dict("os.environ", {"PR_STALENESS_MIN_AGE_HOURS": raw})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_is_48_hours_when_unset(self):
        self._set_min_age_env("")
        self.assertEqual(self.mod._min_age_hours(), 48.0)

    def test_valid_override_is_honoured(self):
        self._set_min_age_env("12")
        self.assertEqual(self.mod._min_age_hours(), 12.0)

    def test_invalid_overrides_fall_back_to_48_hours(self):
        for raw in ("garbage", "", "0", "-1"):
            with self.subTest(raw=raw):
                self._set_min_age_env(raw)
                self.assertEqual(self.mod._min_age_hours(), 48.0)


class DecideTests(unittest.TestCase):
    """decide() is the pure dedupe/heartbeat decision — no I/O."""

    def setUp(self):
        self.mod, self.ppi = _load_module()
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def test_unchanged_state_stays_silent(self):
        fp = {"org/repo#1": 0}
        should_post, _ = self.mod.decide(fp, dict(fp), self.now.isoformat(), self.now, digest_hours=24.0)
        self.assertFalse(should_post)

    def test_new_stale_pr_posts(self):
        should_post, reason = self.mod.decide({}, {"org/repo#1": 0}, None, self.now, digest_hours=24.0)
        self.assertTrue(should_post)
        self.assertEqual(reason, "stale set changed")

    def test_pr_dropping_out_posts(self):
        prev = {"org/repo#1": 0}
        should_post, reason = self.mod.decide(prev, {}, self.now.isoformat(), self.now, digest_hours=24.0)
        self.assertTrue(should_post)
        self.assertEqual(reason, "stale set changed")

    def test_age_bucket_crossing_posts(self):
        prev = {"org/repo#1": 0}
        curr = {"org/repo#1": 1}
        should_post, reason = self.mod.decide(prev, curr, self.now.isoformat(), self.now, digest_hours=24.0)
        self.assertTrue(should_post)
        self.assertEqual(reason, "stale set changed")

    def test_heartbeat_fires_after_digest_interval(self):
        fp = {"org/repo#1": 0}
        last_posted = (self.now - timedelta(hours=25)).isoformat()
        should_post, reason = self.mod.decide(fp, dict(fp), last_posted, self.now, digest_hours=24.0)
        self.assertTrue(should_post)
        self.assertEqual(reason, "heartbeat")

    def test_heartbeat_not_yet_due_stays_silent(self):
        fp = {"org/repo#1": 0}
        last_posted = (self.now - timedelta(hours=1)).isoformat()
        should_post, _ = self.mod.decide(fp, dict(fp), last_posted, self.now, digest_hours=24.0)
        self.assertFalse(should_post)

    def test_empty_stale_set_never_heartbeats(self):
        # Nothing stale and nothing changed => nothing to report, ever.
        last_posted = (self.now - timedelta(days=365)).isoformat()
        should_post, _ = self.mod.decide({}, {}, last_posted, self.now, digest_hours=24.0)
        self.assertFalse(should_post)


class LoadStateFailOpenTests(unittest.TestCase):
    """_load_state must never raise; any unreadable file degrades to {}."""

    def setUp(self):
        self.mod, self.ppi = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_returns_empty(self):
        missing = Path(self.tmp.name) / "does-not-exist.json"
        self.assertEqual(self.mod._load_state(missing), {})

    def test_corrupt_json_returns_empty(self):
        corrupt = Path(self.tmp.name) / "corrupt.json"
        corrupt.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(self.mod._load_state(corrupt), {})

    def test_wrong_top_level_shape_returns_empty(self):
        weird = Path(self.tmp.name) / "weird.json"
        weird.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        self.assertEqual(self.mod._load_state(weird), {})

    def test_valid_state_round_trips(self):
        path = Path(self.tmp.name) / "state.json"
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        self.mod._save_state(path, {"org/repo#1": 0}, now)
        loaded = self.mod._load_state(path)
        self.assertEqual(loaded["fingerprint"], {"org/repo#1": 0})
        self.assertEqual(loaded["last_posted_at"], now.isoformat())


class RunEndToEndTests(unittest.TestCase):
    """Drives run() through the stubbed ppi to prove the pieces are wired
    together correctly: notify() actually fires (or doesn't), and state is
    actually persisted (or, on a corrupt file, fails open and still posts)."""

    def setUp(self):
        self.mod, self.ppi = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "pr_staleness_last.json"
        self.now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        self.ppi.utcnow = lambda: self.now
        self.env_patch = mock.patch.dict(
            "os.environ",
            {
                "PR_STALENESS_STATE_PATH": str(self.state_path),
                "PR_STALENESS_DIGEST_HOURS": "24",
                "PR_STALENESS_AGE_BUCKET_HOURS": "24,72,168",
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def _set_states(self, states):
        self.ppi.scan_repos = lambda repos, now, gh, errors: (states, 0)

    def test_first_run_with_stale_pr_posts_and_saves_state(self):
        self._set_states([_pr_state("org/repo", 1, age_hours=49, now=self.now)])
        self.mod.run(["org/repo"])
        self.ppi.notify.assert_called_once()
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["fingerprint"], {"org/repo#1": 1})

    def test_unchanged_stale_set_stays_silent_on_second_run(self):
        self._set_states([_pr_state("org/repo", 1, age_hours=49, now=self.now)])
        self.mod.run(["org/repo"])
        self.ppi.notify.reset_mock()
        # Same PR, still same age bucket, run again a minute later.
        self.mod.ppi.utcnow = lambda: self.now + timedelta(minutes=1)
        self._set_states([_pr_state("org/repo", 1, age_hours=49.02, now=self.now)])
        self.mod.run(["org/repo"])
        self.ppi.notify.assert_not_called()

    def test_corrupt_state_file_fails_open_and_still_posts(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text("{not valid json", encoding="utf-8")
        self._set_states([_pr_state("org/repo", 1, age_hours=49, now=self.now)])
        self.mod.run(["org/repo"])
        self.ppi.notify.assert_called_once()

    def test_no_stale_prs_and_no_prior_state_stays_silent(self):
        self._set_states([])
        self.mod.run(["org/repo"])
        self.ppi.notify.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_pr_selection_is_age_only_at_the_48_hour_default(self):
        self.ppi.stale_without_verdict = mock.Mock(side_effect=AssertionError("must not be called"))
        self._set_states([_pr_state("org/repo", 1, age_hours=47, now=self.now)])
        self.mod.run(["org/repo"])
        self.ppi.notify.assert_not_called()

        self._set_states([_pr_state("org/repo", 1, age_hours=49, now=self.now)])
        self.mod.run(["org/repo"])
        self.ppi.notify.assert_called_once()

    def test_ambient_min_age_override_changes_pr_selection(self):
        with mock.patch.dict("os.environ", {"PR_STALENESS_MIN_AGE_HOURS": "12"}):
            self._set_states([_pr_state("org/repo", 1, age_hours=13, now=self.now)])
            self.mod.run(["org/repo"])
        self.ppi.notify.assert_called_once()


class DigestMessageTests(unittest.TestCase):
    def setUp(self):
        self.mod, self.ppi = _load_module()

    def test_zero_repos_keeps_the_resolved_message(self):
        self.assertIn("Previously stale PR", self.mod._build_message([], "stale set changed"))

    def test_one_repo_digest_and_zero_ledger_footer(self):
        self.mod.finalization_count = lambda: 0
        message = self.mod._build_message(
            [_alert_pr("org/repo", 7, age_hours=60)], "heartbeat"
        )
        self.assertIn("1 PR(s) at least 48h old", message)
        self.assertIn("org/repo — 1 PR(s), oldest 60.0h", message)
        self.assertIn("org/repo#7 — 60.0h", message)
        self.assertIn("Verdict ledger: 0 finalizations", message)

    def test_many_repos_cap_individual_prs_at_three(self):
        self.mod.finalization_count = lambda: 5
        message = self.mod._build_message(
            [
                _alert_pr("org/a", 1, age_hours=50),
                _alert_pr("org/a", 2, age_hours=80),
                _alert_pr("org/b", 3, age_hours=70),
                _alert_pr("org/c", 4, age_hours=60),
            ],
            "stale set changed",
        )
        self.assertIn("org/a — 2 PR(s), oldest 80.0h", message)
        self.assertIn("org/b — 1 PR(s), oldest 70.0h", message)
        self.assertIn("org/c — 1 PR(s), oldest 60.0h", message)
        self.assertIn("org/a#2 — 80.0h", message)
        self.assertIn("org/b#3 — 70.0h", message)
        self.assertIn("org/c#4 — 60.0h", message)
        self.assertNotIn("org/a#1 — 50.0h", message)
        self.assertIn("Verdict ledger: 5 finalizations.", message)

    def test_unreadable_ledger_footer_does_not_change_digest_shape(self):
        self.mod.finalization_count = lambda: None
        message = self.mod._build_message(
            [_alert_pr("org/repo", 7, age_hours=60)], "heartbeat"
        )
        self.assertIn("org/repo — 1 PR(s), oldest 60.0h", message)
        self.assertIn("org/repo#7 — 60.0h", message)
        self.assertIn("Verdict ledger: could not read the ledger.", message)


if __name__ == "__main__":
    unittest.main()
