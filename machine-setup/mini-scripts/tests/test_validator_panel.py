"""Focused tests for the Mini PR validator panel trust boundary."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
_COUNTER = 0


def _load_validator_panel():
    global _COUNTER
    _COUNTER += 1
    old_path = list(sys.path)
    sys.path.insert(0, str(PIPELINE))
    try:
        name = f"validator_panel_test_{_COUNTER}"
        spec = importlib.util.spec_from_file_location(name, PIPELINE / "validator_panel.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = old_path


def _entry(label: str) -> dict[str, str]:
    return {"provider": label, "model": f"{label}-model", "label": label}


class ValidatorPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.panel = _load_validator_panel()
        self.panel.PANEL_LOG = str(Path(self.tmp.name) / "validator_panel.jsonl")

    def _log_records(self):
        path = Path(self.panel.PANEL_LOG)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_real_block_is_not_labelled_as_infrastructure_failure(self):
        with mock.patch.object(
            self.panel,
            "_hermes_oneshot",
            return_value=(0, "Fix the bug.\nVERDICT: BLOCK — writes wrong data", {"completed": True}, None),
        ):
            result = self.panel._run_lens(
                "correctness", "find defects", "diff", "medium", "", "m1", "p1"
            )

        self.assertEqual(result["verdict"], "BLOCK")
        self.assertEqual(result["reason"], "writes wrong data")
        self.assertNotIn("infra_failure", result)
        self.assertEqual(self._log_records()[-1]["event"], "lens_verdict")

    def test_provider_failure_is_error_and_secret_safe_logged(self):
        secret = "sk-" + "A" * 24
        old_secret = os.environ.get("VALIDATOR_TEST_API_KEY")
        os.environ["VALIDATOR_TEST_API_KEY"] = secret
        self.addCleanup(
            lambda: os.environ.pop("VALIDATOR_TEST_API_KEY", None)
            if old_secret is None
            else os.environ.__setitem__("VALIDATOR_TEST_API_KEY", old_secret)
        )
        raw = f"hermes -z: agent failed: No Anthropic credentials found api_key={secret}"

        with mock.patch.object(
            self.panel,
            "_hermes_oneshot",
            return_value=(0, raw, {"failed": True, "completed": False}, None),
        ):
            result = self.panel._run_lens(
                "security", "find security holes", "diff", "high", "", "m1", "anthropic"
            )

        self.assertEqual(result["verdict"], "ERROR")
        self.assertTrue(result["infra_failure"])
        self.assertEqual(result["failure_class"], "provider-error")
        log_text = Path(self.panel.PANEL_LOG).read_text(encoding="utf-8")
        self.assertNotIn(secret, log_text)
        self.assertIn("<REDACTED>", log_text)

    def test_chain_advances_after_unparseable_answer_and_repair_failure(self):
        calls = [
            (0, "Substantive prose but no final line", {"completed": True}, None),
            (0, "Still no verdict", {"completed": True}, None),
            (0, "No blocking finding.\nVERDICT: PASS", {"completed": True}, None),
        ]

        with mock.patch.object(self.panel, "_hermes_oneshot", side_effect=calls):
            result = self.panel._run_lens_chain(
                "governance", "find governance issues", "diff", "high", "",
                [_entry("first"), _entry("second")],
            )

        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["model"], "second")
        events = [record["event"] for record in self._log_records()]
        self.assertIn("lens_unparseable", events)
        self.assertIn("lens_repair_retry", events)

    def test_format_repair_can_recover_a_missing_verdict_line(self):
        calls = [
            (0, "I found no blocking issue, but forgot the required final line.", {"completed": True}, None),
            (0, "VERDICT: PASS", {"completed": True}, None),
        ]

        with mock.patch.object(self.panel, "_hermes_oneshot", side_effect=calls):
            result = self.panel._run_lens(
                "verified-live", "find shipped-blind changes", "diff", "high", "", "m1", "p1"
            )

        self.assertEqual(result["verdict"], "PASS")
        self.assertTrue(result["repaired"])

    def test_all_unparseable_high_risk_fails_closed_as_infrastructure_not_denial(self):
        with (
            mock.patch.object(self.panel.validator_model, "resolve_chain", return_value=[_entry("only")]),
            mock.patch.object(
                self.panel,
                "_hermes_oneshot",
                side_effect=[(0, "answer without verdict", {"completed": True}, None)] * 6,
            ),
        ):
            result = self.panel.run("diff", "high")

        self.assertEqual(result["verdict"], "BLOCK")
        self.assertTrue(result["infra_failure"])
        self.assertEqual(result["failure_class"], "no-parseable-verdict")
        self.assertIn("INFRA-NO-VERDICT", result["model_used"])
        self.assertIn(self.panel.INFRA_FAIL_PREFIX, result["note"])

    def test_no_chain_preserves_tier_scaled_fail_closed_behavior(self):
        with mock.patch.object(self.panel.validator_model, "resolve_chain", return_value=[]):
            high = self.panel.run("diff", "high")
            medium = self.panel.run("diff", "medium")

        self.assertEqual(high["verdict"], "BLOCK")
        self.assertTrue(high["infra_failure"])
        self.assertEqual(high["failure_class"], "no-model-chain")
        self.assertEqual(medium["verdict"], "PASS")
        self.assertTrue(medium["infra_failure"])
        self.assertEqual(medium["failure_class"], "no-model-chain")
        self.assertIn("not adversarially reviewed", medium["note"])


if __name__ == "__main__":
    unittest.main()
