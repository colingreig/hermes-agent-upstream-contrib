#!/usr/bin/env python3
"""Regression tests for the validator panel's parse-failure handling.

Guards the 2026-07-26 fix for the false-BLOCK class whose reason string was
"no parseable verdict (default-deny)" — an INFRASTRUCTURE failure that read
exactly like a substantive code-review denial and stranded 7 PRs for a week.

Contract under test:
  1. default-deny is PRESERVED (an unobtainable verdict still holds the merge)
  2. an unparseable response advances the model chain instead of being terminal
  3. a format-repair retry runs once before the lens gives up
  4. the raw response is logged (truncated, secrets redacted)
  5. the infra text is DISTINGUISHABLE from a real denial
  6. a real BLOCK with a substantive reason is untouched
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "pr_pipeline"))
import validator_panel as vp  # noqa: E402


CHAIN = [
    {"provider": "anthropic", "model": "m1", "label": "anthropic/opus->m1"},
    {"provider": "anthropic", "model": "m2", "label": "anthropic/sonnet->m2"},
]


class _Harness(unittest.TestCase):
    def setUp(self):
        self.log = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self.log.close()
        self._orig_log, vp.PANEL_LOG = vp.PANEL_LOG, self.log.name
        self._orig_oneshot = vp._hermes_oneshot
        self.calls = []

    def tearDown(self):
        vp.PANEL_LOG = self._orig_log
        vp._hermes_oneshot = self._orig_oneshot
        os.unlink(self.log.name)

    def stub(self, responder):
        def _fake(prompt, model, provider, timeout):
            self.calls.append({"model": model, "is_repair": "PREVIOUS REPLY START" in prompt})
            return responder(prompt, model, provider)
        vp._hermes_oneshot = _fake

    def log_events(self):
        with open(self.log.name, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class TestParseVerdict(_Harness):
    def test_absent_verdict_returns_none_not_a_fabricated_block(self):
        self.assertEqual(vp._parse_verdict("some prose, no token"), (None, ""))

    def test_real_block_is_preserved(self):
        v, r = vp._parse_verdict("analysis\n**VERDICT: BLOCK — unauthed endpoint**")
        self.assertEqual(v, "BLOCK")
        self.assertEqual(r, "unauthed endpoint")

    def test_pass_is_preserved(self):
        self.assertEqual(vp._parse_verdict("ok\nVERDICT: PASS")[0], "PASS")


class TestRepairRetry(_Harness):
    def test_repair_retry_recovers_the_verdict(self):
        def responder(prompt, model, provider):
            if "PREVIOUS REPLY START" in prompt:
                return 0, "VERDICT: BLOCK — missing auth on the new route\n", {"failed": False}, None
            return 0, "Long analysis with no final line at all.", {"failed": False}, None
        self.stub(responder)
        res = vp._run_lens_chain("security", "focus", "diff", "high", "", CHAIN)
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertEqual(res["reason"], "missing auth on the new route")
        self.assertTrue(res.get("repaired"))
        self.assertFalse(res.get("infra_failure"))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1]["is_repair"])
        self.assertTrue(any(e["event"] == "lens_repair_retry" for e in self.log_events()))

    def test_repair_is_attempted_only_once_per_lens(self):
        self.stub(lambda p, m, pr: (0, "prose, no verdict token", {"failed": False}, None))
        vp._run_lens_chain("security", "focus", "diff", "high", "", CHAIN)
        self.assertEqual(sum(1 for c in self.calls if c["is_repair"]), 1)


class TestChainAdvancement(_Harness):
    def test_unparseable_first_model_falls_through_to_the_next(self):
        def responder(prompt, model, provider):
            if model == "m1":
                return 0, "prose with no verdict", {"failed": False}, None
            return 0, "fine\nVERDICT: PASS\n", {"failed": False}, None
        self.stub(responder)
        res = vp._run_lens_chain("security", "focus", "diff", "high", "", CHAIN)
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(res["model"], "anthropic/sonnet->m2")

    def test_all_unparseable_still_default_denies_but_labelled_infra(self):
        self.stub(lambda p, m, pr: (0, "prose with no verdict", {"failed": False}, None))
        res = vp._run_lens_chain("security", "focus", "diff", "high", "", CHAIN)
        self.assertEqual(res["verdict"], "BLOCK")          # default-deny preserved
        self.assertTrue(res["infra_failure"])
        self.assertEqual(res["failure_class"], "no-parseable-verdict")
        self.assertIn(vp.INFRA_FAIL_PREFIX, res["reason"])
        self.assertIn("NOT reviewed", res["reason"])


class TestInfraClassification(_Harness):
    def test_credential_error_on_rc0_is_infra_not_a_block(self):
        # The literal string this mini emits when Anthropic creds are missing.
        text = "hermes -z: agent failed: No Anthropic credentials found. Set ANTHROPIC_TOKEN."
        self.assertTrue(vp._looks_like_api_failure(text))
        self.stub(lambda p, m, pr: (0, text, {"failed": True}, None))
        res = vp._run_lens("security", "focus", "diff", "high", "", "m1", "anthropic")
        self.assertEqual(res["verdict"], "ERROR")
        self.assertTrue(res["infra_failure"])
        self.assertEqual(res["failure_class"], "provider-error")

    def test_hermes_reported_failure_beats_plausible_prose(self):
        # rc=0 and text that looks like an ordinary answer, but hermes says the
        # run failed -> infra, never a review verdict.
        self.stub(lambda p, m, pr: (0, "Looks fine to me overall.", {"failed": True}, None))
        res = vp._run_lens("security", "focus", "diff", "high", "", "m1", "anthropic")
        self.assertEqual(res["verdict"], "ERROR")
        self.assertEqual(res["failure_class"], "provider-error")

    def test_subprocess_launch_failure_is_infra(self):
        self.stub(lambda p, m, pr: (None, "", {}, "could not launch: OSError(7, 'Argument list too long')"))
        res = vp._run_lens("security", "focus", "diff", "high", "", "m1", "anthropic")
        self.assertEqual(res["verdict"], "ERROR")
        self.assertEqual(res["failure_class"], "subprocess")


class TestRawLogging(_Harness):
    def test_unparseable_raw_response_is_logged(self):
        self.stub(lambda p, m, pr: (0, "SOME UNPARSEABLE MODEL PROSE", {"failed": False}, None))
        vp._run_lens_chain("security", "focus", "diff", "high", "", CHAIN)
        events = self.log_events()
        unparse = [e for e in events if e["event"] == "lens_unparseable"]
        self.assertTrue(unparse, "the raw unparseable response must be logged")
        self.assertIn("SOME UNPARSEABLE MODEL PROSE", unparse[0]["raw"])

    def test_secrets_are_redacted_and_output_is_clipped(self):
        os.environ["PANEL_TEST_API_KEY"] = "supersecretvalue1234567890"
        try:
            leak = ("sk-ant-api03-" + "A" * 40 + " and supersecretvalue1234567890 "
                    + "ghp_" + "b" * 36)
            red = vp._redact(leak)
            self.assertNotIn("supersecretvalue1234567890", red)
            self.assertNotIn("sk-ant-api03-AAAA", red)
            self.assertNotIn("ghp_bbbb", red)
        finally:
            del os.environ["PANEL_TEST_API_KEY"]
        clipped = vp._clip("x" * 10000, limit=100)
        self.assertLess(len(clipped), 200)
        self.assertIn("elided", clipped)


class TestPanelAggregation(_Harness):
    def _chain(self):
        vp.validator_model.resolve_chain = lambda tier: CHAIN

    def test_real_block_is_not_relabelled_as_infra(self):
        self._chain()
        self.stub(lambda p, m, pr: (
            0, "VERDICT: BLOCK — vendor checkout writes bypass the human-approval gate\n",
            {"failed": False}, None))
        res = vp.run("diff", "high")
        self.assertEqual(res["verdict"], "BLOCK")
        self.assertFalse(res["infra_failure"])
        self.assertNotIn("note", res)
        self.assertIn("human-approval gate", res["lenses"][0]["reason"])

    def test_all_lenses_unparseable_yields_distinguishable_infra_block(self):
        self._chain()
        self.stub(lambda p, m, pr: (0, "prose with no verdict", {"failed": False}, None))
        res = vp.run("diff", "high")
        self.assertEqual(res["verdict"], "BLOCK")           # default-deny preserved
        self.assertTrue(res["infra_failure"])
        self.assertEqual(res["failure_class"], "no-parseable-verdict")
        self.assertIn("INFRA-NO-VERDICT", res["model_used"])
        self.assertIn(vp.INFRA_FAIL_PREFIX, res["note"])
        self.assertIn("NOT a denial on the merits", res["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
