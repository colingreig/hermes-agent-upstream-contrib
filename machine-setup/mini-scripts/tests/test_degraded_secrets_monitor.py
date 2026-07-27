#!/usr/bin/env python3
"""Contract tests for degraded secret-wrapper failure detection."""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "degraded_secrets_monitor.py"
SCRIPT_DIR = SOURCE.parent
LAUNCHD_PLIST = (
    SCRIPT_DIR
    / "launchd"
    / "com.colingreig.hermes.degraded-secrets-monitor.plist"
)
SPEC = importlib.util.spec_from_file_location(
    "degraded_secrets_monitor_under_test", SOURCE
)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(monitor)


def _write_auth(path, credential_pool):
    path.write_text(
        json.dumps({"version": 1, "providers": {}, "credential_pool": credential_pool})
    )


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


class DegradedSecretsMonitorCredentialPoolTests(unittest.TestCase):
    NOW = "2026-07-27T00:00:00+00:00"

    def test_healthy_statuses_do_not_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            _write_auth(
                auth_file,
                {
                    "nous": [
                        {"id": "a", "last_status": "ok"},
                        {"id": "b", "last_status": None},
                        {"id": "c", "last_status": "cooldown"},
                    ]
                },
            )

            result = monitor.check_credential_pool(str(auth_file))

        self.assertFalse(result["triggered"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hits"], [])

    def test_degraded_statuses_trigger_and_sort_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            _write_auth(
                auth_file,
                {
                    "xai": [{"id": "x", "last_status": "invalid"}],
                    "codex": [{"id": "c", "last_status": "exhausted"}],
                    "nous": [{"id": "n", "last_status": "error"}],
                },
            )

            result = monitor.check_credential_pool(str(auth_file))

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [
                {"provider": "codex", "id": "c", "status": "exhausted"},
                {"provider": "nous", "id": "n", "status": "error"},
                {"provider": "xai", "id": "x", "status": "invalid"},
            ],
        )

    def test_missing_malformed_and_absent_auth_are_not_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            missing = base / "missing-auth.json"
            malformed = base / "malformed-auth.json"
            absent = base / "absent-auth.json"
            malformed.write_text("{not-json")
            absent.write_text(json.dumps({"version": 1, "providers": {}}))

            missing_result = monitor.check_credential_pool(str(missing))
            malformed_result = monitor.check_credential_pool(str(malformed))
            absent_result = monitor.check_credential_pool(str(absent))

        self.assertEqual(missing_result["status"], "missing")
        self.assertFalse(missing_result["triggered"])
        self.assertEqual(malformed_result["status"], "malformed")
        self.assertFalse(malformed_result["triggered"])
        self.assertEqual(absent_result["status"], "absent")
        self.assertFalse(absent_result["triggered"])

    def test_signature_has_sorted_json_roundtrippable_credential_pool_hits(self):
        credential_pool = {
            "hits": [
                {"provider": "xai", "id": "z", "status": "invalid"},
                {"provider": "codex", "id": "a", "status": "exhausted"},
            ]
        }

        sig = monitor._signature(
            {"triggered": False},
            {"triggered": False},
            {"hits": []},
            credential_pool,
        )

        self.assertEqual(
            sig["credential_pool"],
            [["codex", "a", "exhausted"], ["xai", "z", "invalid"]],
        )
        self.assertEqual(json.loads(json.dumps(sig)), sig)

    def test_old_signature_without_credential_pool_normalizes_for_dedup(self):
        old_sig = {"fatal": True, "parked_auth": False, "placeholder_keys": []}

        self.assertEqual(
            monitor._normalize_signature(old_sig),
            {
                "fatal": True,
                "parked_auth": False,
                "placeholder_keys": [],
                "credential_pool": [],
            },
        )

    def test_json_result_folds_credential_pool_into_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            log_file.write_text("")
            _write_auth(auth_file, {"nous": [{"id": "pool-1", "last_status": "error"}]})

            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE),
                    "--json",
                    "--log-file",
                    str(log_file),
                    "--auth-file",
                    str(auth_file),
                    "--now",
                    self.NOW,
                ],
                capture_output=True,
                text=True,
                cwd=SCRIPT_DIR,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["degraded"])
        self.assertEqual(
            payload["credential_pool"]["hits"],
            [{"provider": "nous", "id": "pool-1", "status": "error"}],
        )

    def test_dry_run_alert_formats_slack_and_clickup_without_secret_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            hermes_home = home / ".hermes"
            hermes_home.mkdir(parents=True)
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            log_file.write_text("")
            _write_auth(auth_file, {"codex": [{"id": "pool-1", "last_status": "exhausted"}]})
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})
            env.pop("CLICKUP_API_TOKEN", None)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE),
                    "--alert",
                    "--log-file",
                    str(log_file),
                    "--auth-file",
                    str(auth_file),
                    "--now",
                    self.NOW,
                ],
                capture_output=True,
                text=True,
                cwd=SCRIPT_DIR,
                env=env,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("[degraded-secrets-monitor] DRY_RUN slack:", result.stdout)
        self.assertIn("<@UN4CQ1EGG>", result.stdout)
        self.assertIn(
            "[degraded-secrets-monitor] DRY_RUN clickup comment on 86e2610g8:",
            result.stdout,
        )
        self.assertIn(
            "Credential pool degraded: provider 'codex' entry 'pool-1' last_status=exhausted",
            result.stdout,
        )
        self.assertIn("[degraded-secrets-monitor] alerted (slack=True clickup=True)", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_alert_recovery_clears_credential_pool_dedup_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            hermes_home = home / ".hermes"
            state_dir = hermes_home / "state"
            state_dir.mkdir(parents=True)
            state_file = state_dir / "degraded-secrets-monitor.json"
            state_file.write_text(
                json.dumps(
                    {
                        "last_alert_signature": {
                            "fatal": False,
                            "parked_auth": False,
                            "placeholder_keys": [],
                            "credential_pool": [["codex", "pool-1", "exhausted"]],
                        }
                    }
                )
            )
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            log_file.write_text("")
            _write_auth(auth_file, {"codex": [{"id": "pool-1", "last_status": "ok"}]})
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})

            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE),
                    "--alert",
                    "--log-file",
                    str(log_file),
                    "--auth-file",
                    str(auth_file),
                    "--now",
                    self.NOW,
                ],
                capture_output=True,
                text=True,
                cwd=SCRIPT_DIR,
                env=env,
                check=False,
            )
            state = json.loads(state_file.read_text())

        self.assertEqual(result.returncode, 0)
        self.assertIn("[degraded-secrets-monitor] healthy", result.stdout)
        self.assertIn("[degraded-secrets-monitor] recovered", result.stdout)
        self.assertIsNone(state["last_alert_signature"])


class DegradedSecretsMonitorLaunchdContractTests(unittest.TestCase):
    def test_launchagent_contract_targets_live_monitor_and_alert_destinations(self):
        payload = plistlib.loads(LAUNCHD_PLIST.read_bytes())

        self.assertEqual(
            payload["Label"],
            "com.colingreig.hermes.degraded-secrets-monitor",
        )
        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/usr/bin/python3",
                "/Users/colingreig/.hermes/scripts/degraded_secrets_monitor.py",
                "--alert",
            ],
        )
        self.assertEqual(payload["StartInterval"], 300)
        self.assertTrue(payload["RunAtLoad"])

        env = payload["EnvironmentVariables"]
        self.assertEqual(env["HOME"], "/Users/colingreig")
        self.assertEqual(env["HERMES_HOME"], "/Users/colingreig/.hermes")
        self.assertEqual(env["DEGRADED_SECRETS_ALERT_TASK_ID"], "86e2610g8")
        self.assertEqual(env["DEGRADED_SECRETS_ALERT_SLACK"], "slack:D0BA2PM9CFM")
        self.assertIn("/Users/colingreig/.local/bin", env["PATH"].split(":"))
        self.assertIn("/usr/bin", env["PATH"].split(":"))

        self.assertEqual(
            payload["StandardOutPath"],
            "/Users/colingreig/.hermes/logs/degraded-secrets-monitor.launchd.log",
        )
        self.assertEqual(
            payload["StandardErrorPath"],
            "/Users/colingreig/.hermes/logs/degraded-secrets-monitor.launchd.error.log",
        )
        self.assertNotIn("KeepAlive", payload)
        serialized = LAUNCHD_PLIST.read_text(encoding="utf-8")
        self.assertNotIn("CLICKUP_API_TOKEN", serialized)
        self.assertNotIn("op://", serialized)


if __name__ == "__main__":
    unittest.main()
