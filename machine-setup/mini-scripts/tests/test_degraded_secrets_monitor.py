#!/usr/bin/env python3
"""Contract tests for degraded secret-wrapper failure detection."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "degraded_secrets_monitor.py"
SPEC = importlib.util.spec_from_file_location(
    "degraded_secrets_monitor_under_test", SOURCE
)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


class DegradedSecretsMonitorClassifierTests(unittest.TestCase):
    NOW = datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)

    def _check(self, lines):
        return monitor.check_fatal_loop(lines, now=self.NOW)

    def test_legacy_failure_format_still_escalates(self):
        line = (
            "2026-07-27T03:59:00Z gateway_secrets_wrap: "
            ">>> FATAL: 1Password unreachable after retries\n"
        )
        result = self._check([line] * 3)
        self.assertTrue(result["triggered"])
        self.assertEqual(result["count"], 3)

    def test_classified_transient_formats_escalate(self):
        formats = (
            "classification=transient-exhausted",
            "classification=transient_exhausted",
        )
        for classification in formats:
            with self.subTest(classification=classification):
                line = (
                    "2026-07-27T03:59:00Z gateway_secrets_wrap: FATAL "
                    f"{classification} exit=75: bounded retries exhausted\n"
                )
                result = self._check([line] * 3)
                self.assertTrue(result["triggered"])
                self.assertEqual(result["count"], 3)

    def test_permanent_auth_parks_and_escalates_on_first_record(self):
        for classification in (
            "classification=auth",
            "classification=permanent-auth",
            "classification=permanent_auth",
        ):
            with self.subTest(classification=classification):
                line = (
                    "2026-07-27T03:59:00Z gateway_secrets_wrap: FATAL "
                    f"{classification} exit=77: credential repair required\n"
                )
                result = monitor.check_parked_auth([line], now=self.NOW)
                self.assertTrue(result["triggered"])
                self.assertEqual(result["count"], 1)

    def test_detection_result_never_retains_secret_bearing_log_payload(self):
        secret = "opaque-secret-that-must-not-be-retained"
        line = (
            "2026-07-27T03:59:00Z gateway_secrets_wrap: FATAL "
            "classification=transient_exhausted exit=75: "
            f"resolver failed token={secret}\n"
        )
        result = self._check([line] * 3)

        self.assertTrue(result["triggered"])
        self.assertNotIn(secret, repr(result))
        self.assertEqual(
            result["timestamps"],
            ["2026-07-27T03:59:00+00:00"] * 3,
        )

    def test_unrelated_fatal_classification_does_not_change_contract(self):
        line = (
            "2026-07-27T03:59:00Z gateway_secrets_wrap: "
            "FATAL classification=config: missing file\n"
        )
        result = self._check([line] * 3)
        self.assertFalse(result["triggered"])
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main()
