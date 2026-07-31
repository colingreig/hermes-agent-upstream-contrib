#!/usr/bin/env python3
"""Tests for adversarial_review.py (ClickUp 86e2k3qe2 — adversarial review
pass for in-review/needs-validation tasks).

Covers the three failure classes the module exists to catch, each hermetic
(no live `gh`/network calls):
  - wrong-repo: delegates to validator_repo_guard.compare_refs(); a rename
    alias must read SAME (no finding), a genuinely different repo must read
    DIFFERENT (high finding), and an unresolvable identity must degrade to a
    non-blocking medium finding (fail-closed per validator_repo_guard's rule
    — never a spurious high/BLOCK).
  - stale-evidence: a live head that disagrees with the recorded head is a
    high finding; a matching head or a missing live head are not blocking.
  - missing-ci: reuses autonomous_merge.pr_state() (mocked here, no gh calls)
    — a failing gating check is high, no green gating check yet is a
    non-blocking medium, and a healthy green gate produces no finding.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MINI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PIPELINE_DIR = MINI_SCRIPTS_DIR / "pr_pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

# Keep every test off the operator's real identity cache / alias file (same
# isolation convention as test_validator_repo_guard.py).
_ISOLATED = tempfile.mkdtemp(prefix="adversarial-review-test-")
os.environ.setdefault("HERMES_REPO_IDENTITY_CACHE",
                      str(Path(_ISOLATED) / "cache.json"))
os.environ.setdefault("HERMES_REPO_ALIASES",
                      str(Path(_ISOLATED) / "aliases.json"))

import autonomous_merge  # noqa: E402 — must be importable before adversarial_review is loaded


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ar = _load_module(PIPELINE_DIR / "adversarial_review.py", "adversarial_review")

HERMES_NODE = "R_kgDOS5KWsw"   # colingreig/hermes-agent-upstream-contrib
BRAIN_NODE = "R_kgDOS4oHNQ"    # colingreig/brain


def _seed_cache(path: Path, entries: dict) -> None:
    now = 2_000_000_000.0  # far future so nothing is ever "stale" in tests
    path.write_text(json.dumps(
        {slug: {"node_id": node, "full_name": full, "ts": now}
         for slug, (node, full) in entries.items()}
    ), encoding="utf-8")


class CheckWrongRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adversarial-review-case-")
        self.cache = Path(self.tmp) / "cache.json"
        self.aliases = Path(self.tmp) / "aliases.json"
        self.aliases.write_text(json.dumps({"aliases": []}))
        _seed_cache(self.cache, {
            "colingreig/hermes-agent":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
            "colingreig/hermes-agent-upstream-contrib":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
            "colingreig/brain": (BRAIN_NODE, "colingreig/brain"),
        })
        self._env = mock.patch.dict(os.environ, {
            "HERMES_REPO_GUARD_NO_NETWORK": "1",
            "HERMES_REPO_IDENTITY_CACHE": str(self.cache),
            "HERMES_REPO_ALIASES": str(self.aliases),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_blank_sides_produce_no_finding(self):
        self.assertEqual(ar.check_wrong_repo("", "colingreig/hermes-agent"), [])
        self.assertEqual(ar.check_wrong_repo("colingreig/hermes-agent", ""), [])

    def test_identical_spelling_produces_no_finding_without_network(self):
        self.assertEqual(
            ar.check_wrong_repo("colingreig/thermal", "colingreig/thermal"), [])

    def test_rename_alias_produces_no_finding(self):
        # actual repo is the NEW spelling; task is chartered against the OLD
        # spelling — a naive string compare would flag this as wrong-repo.
        findings = ar.check_wrong_repo(
            "colingreig/hermes-agent-upstream-contrib", "colingreig/hermes-agent")
        self.assertEqual(findings, [])

    def test_genuinely_different_repo_is_a_high_finding(self):
        findings = ar.check_wrong_repo("colingreig/brain", "colingreig/hermes-agent")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "wrong-repo")
        self.assertEqual(findings[0]["severity"], "high")
        self.assertIn("brain", findings[0]["detail"])

    def test_unresolvable_identity_is_a_non_blocking_medium_finding(self):
        findings = ar.check_wrong_repo(
            "colingreig/some-repo-with-no-cached-identity", "colingreig/hermes-agent")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "wrong-repo")
        self.assertEqual(findings[0]["severity"], "medium")


class CheckStaleEvidenceTests(unittest.TestCase):
    def test_no_recorded_head_is_not_this_checks_job(self):
        self.assertEqual(ar.check_stale_evidence("acme/widget", 7), [])

    def test_matching_heads_produce_no_finding(self):
        self.assertEqual(
            ar.check_stale_evidence("acme/widget", 7, "a" * 40, "a" * 40), [])

    def test_mismatched_heads_are_a_high_finding(self):
        findings = ar.check_stale_evidence("acme/widget", 7, "a" * 40, "b" * 40)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "stale-evidence")
        self.assertEqual(findings[0]["severity"], "high")

    def test_unresolvable_live_head_is_a_non_blocking_medium_finding(self):
        findings = ar.check_stale_evidence("acme/widget", 7, "a" * 40, "")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "stale-evidence")
        self.assertEqual(findings[0]["severity"], "medium")


class CheckMissingCiTests(unittest.TestCase):
    def test_no_repo_or_pr_produces_no_finding(self):
        self.assertEqual(ar.check_missing_ci("", 7), [])
        self.assertEqual(ar.check_missing_ci("acme/widget", None), [])

    def test_failing_gating_check_is_a_high_finding(self):
        info = {"failing": ["lint"], "pending": [], "gating_green": []}
        with mock.patch.object(autonomous_merge, "pr_state", return_value=(info, None)):
            findings = ar.check_missing_ci("acme/widget", 7)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "missing-ci")
        self.assertEqual(findings[0]["severity"], "high")
        self.assertIn("lint", findings[0]["detail"])

    def test_no_green_gating_check_yet_is_a_non_blocking_medium_finding(self):
        info = {"failing": [], "pending": ["build"], "gating_green": []}
        with mock.patch.object(autonomous_merge, "pr_state", return_value=(info, None)):
            findings = ar.check_missing_ci("acme/widget", 7)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["check"], "missing-ci")
        self.assertEqual(findings[0]["severity"], "medium")
        self.assertIn("build", findings[0]["detail"])

    def test_healthy_green_gate_produces_no_finding(self):
        info = {"failing": [], "pending": [], "gating_green": ["lint"]}
        with mock.patch.object(autonomous_merge, "pr_state", return_value=(info, None)):
            findings = ar.check_missing_ci("acme/widget", 7)
        self.assertEqual(findings, [])

    def test_ci_lookup_error_is_fail_open_non_blocking(self):
        with mock.patch.object(autonomous_merge, "pr_state",
                                return_value=(None, "gh pr view rc=1: not found")):
            findings = ar.check_missing_ci("acme/widget", 7)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "medium")


class RunTests(unittest.TestCase):
    def test_all_clean_passes(self):
        info = {"failing": [], "pending": [], "gating_green": ["lint"]}
        with mock.patch.object(autonomous_merge, "pr_state", return_value=(info, None)):
            result = ar.run("acme/widget", 7, expected_repo="acme/widget",
                            recorded_head_sha="a" * 40, live_head_sha="a" * 40)
        self.assertTrue(result["pass"])
        self.assertEqual(result["findings"], [])

    def test_any_high_finding_fails_the_pass(self):
        info = {"failing": ["lint"], "pending": [], "gating_green": []}
        with mock.patch.object(autonomous_merge, "pr_state", return_value=(info, None)):
            result = ar.run("acme/widget", 7, expected_repo="acme/widget",
                            recorded_head_sha="a" * 40, live_head_sha="a" * 40)
        self.assertFalse(result["pass"])
        self.assertTrue(any(f["severity"] == "high" for f in result["findings"]))

    def test_without_a_pr_only_wrong_repo_runs(self):
        result = ar.run("acme/widget", expected_repo="acme/widget")
        self.assertTrue(result["pass"])
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
