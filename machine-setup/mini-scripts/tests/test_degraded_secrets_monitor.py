#!/usr/bin/env python3
"""Contract tests for degraded secret-wrapper failure detection."""
from __future__ import annotations

import base64
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
from unittest import mock


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


def _jwt(claims):
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


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
                    "openrouter": [
                        {"id": "a", "access_token": "token-a", "last_status": "ok"},
                        {"id": "b", "access_token": "token-b", "last_status": None},
                        {"id": "c", "access_token": "token-c", "last_status": "cooldown"},
                    ]
                },
            )

            result = monitor.check_credential_pool(str(auth_file))

        self.assertFalse(result["triggered"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hits"], [])

    def test_all_unavailable_statuses_trigger_and_sort_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / "auth.json"
            _write_auth(
                auth_file,
                {
                    "xai": [{"id": "x", "access_token": "x-token", "last_status": "invalid"}],
                    "codex": [{
                        "id": "c",
                        "access_token": "c-token",
                        "last_status": "exhausted",
                        "last_error_reset_at": "2099-01-01T00:00:00Z",
                    }],
                    "nous": [{"id": "n", "access_token": "n-token", "last_status": "error"}],
                    "openrouter": [{"id": "o", "access_token": "o-token", "last_status": "dead"}],
                },
            )

            result = monitor.check_credential_pool(str(auth_file))

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [
                {"provider": "codex", "id": "c", "status": "exhausted_cap", "retry_at": "2099-01-01T00:00:00+00:00"},
                {"provider": "nous", "id": "n", "status": "missing_credential", "retry_at": None},
                {"provider": "openrouter", "id": "o", "status": "missing_credential", "retry_at": None},
                {"provider": "xai", "id": "x", "status": "missing_credential", "retry_at": None},
            ],
        )

    def test_mixed_pool_is_healthy_with_sanitized_redundancy_diagnostics(self):
        secret = "secret-value-that-must-not-leak"
        payload = {
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "primary",
                        "access_token": secret,
                        "last_status": "exhausted",
                        "last_error_reset_at": "2026-07-27T01:00:00Z",
                    },
                    {"id": "backup", "access_token": "backup-secret", "last_status": "ok"},
                ]
            }
        }

        result = monitor.classify_credential_pool(
            payload,
            datetime.fromisoformat(self.NOW),
        )

        self.assertFalse(result["triggered"])
        self.assertEqual(result["hits"], [])
        self.assertEqual(
            result["credential_pool_diagnostics"],
            [
                {
                    "provider": "openai-codex",
                    "total_slot_count": 2,
                    "usable_count": 1,
                    "unavailable_count": 1,
                    "unavailable_slots": [
                        {
                            "id": "primary",
                            "status": "exhausted_session",
                            "retry_at": "2026-07-27T01:00:00+00:00",
                        }
                    ],
                }
            ],
        )
        self.assertNotIn(secret, repr(result))
        self.assertNotIn("backup-secret", repr(result))

    def test_expired_exhaustion_cooldown_is_usable(self):
        now = datetime.fromisoformat(self.NOW)
        for entry in (
            {
                "id": "explicit-reset",
                "access_token": "token",
                "last_status": "exhausted",
                "last_error_reset_at": "2026-07-26T23:59:59Z",
            },
            {
                "id": "derived-reset",
                "access_token": "token",
                "last_status": "exhausted",
                "last_status_at": "2026-07-26T23:54:59Z",
                "last_error_code": 401,
            },
        ):
            with self.subTest(entry=entry["id"]):
                result = monitor.classify_credential_pool(
                    {"credential_pool": {"openai-codex": [entry]}},
                    now,
                )
                self.assertFalse(result["triggered"])
                self.assertEqual(result["credential_pool_diagnostics"], [])

    def test_exhausted_without_future_retry_window_is_immediately_usable(self):
        now = datetime.fromisoformat(self.NOW)
        entries = [
            {
                "id": "missing-timestamps",
                "access_token": "token",
                "last_status": "exhausted",
            },
            {
                "id": "exact-boundary",
                "access_token": "token",
                "last_status": "exhausted",
                "last_error_reset_at": now.timestamp(),
            },
        ]

        result = monitor.classify_credential_pool(
            {"credential_pool": {"openai-codex": entries}},
            now,
        )

        self.assertFalse(result["triggered"])
        self.assertEqual(result["credential_pool_diagnostics"], [])

    def test_refresh_token_and_api_key_fields_are_not_selectable_credentials(self):
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "openai-codex": [{"id": "refresh-only", "refresh_token": "secret"}],
                    "openrouter": [{"id": "wrong-field", "api_key": "secret"}],
                }
            },
            datetime.fromisoformat(self.NOW),
        )

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [
                {"provider": "openai-codex", "id": "refresh-only", "status": "missing_credential", "retry_at": None},
                {"provider": "openrouter", "id": "wrong-field", "status": "missing_credential", "retry_at": None},
            ],
        )
        self.assertNotIn("secret", repr(result))

    def test_env_sourced_api_key_with_fingerprint_is_not_missing_credential(self):
        """Borrowed env:-sourced entries never persist access_token to disk

        (see agent/credential_persistence.py sanitize_borrowed_credential_payload)
        — only a secret_fingerprint survives the disk-boundary sanitizer as
        proof a real secret was seeded. The monitor must not false-alarm
        missing_credential for these live-verified shapes.
        """
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "anthropic": [
                        {
                            "id": "7de3fd",
                            "source": "env:ANTHROPIC_API_KEY",
                            "auth_type": "api_key",
                            "last_status": None,
                            "secret_fingerprint": "sha256:fdef7abc12340000",
                        }
                    ],
                    "zai": [
                        {
                            "id": "bf0337",
                            "source": "env:ZAI_API_KEY",
                            "auth_type": "api_key",
                            "last_status": None,
                            "secret_fingerprint": "sha256:bdff7abc12340000",
                        }
                    ],
                }
            },
            datetime.fromisoformat(self.NOW),
        )

        self.assertFalse(result["triggered"])
        self.assertEqual(result["hits"], [])

    def test_env_sourced_entry_without_fingerprint_is_still_missing_credential(self):
        """An env:-sourced entry that never seeded (no fingerprint, no token)

        is genuinely unusable and must still alarm.
        """
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "anthropic": [
                        {
                            "id": "unseeded",
                            "source": "env:ANTHROPIC_API_KEY",
                            "auth_type": "api_key",
                            "last_status": None,
                        }
                    ]
                }
            },
            datetime.fromisoformat(self.NOW),
        )

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [{"provider": "anthropic", "id": "unseeded", "status": "missing_credential", "retry_at": None}],
        )

    def test_non_env_sourced_entry_with_fingerprint_only_is_still_missing_credential(self):
        """secret_fingerprint alone only substitutes for a live token on

        env:-sourced (borrowed) entries. A non-env source with no
        access_token must still be treated as missing_credential — the
        fingerprint-trust carve-out is scoped, not blanket.
        """
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "openrouter": [
                        {
                            "id": "manual-no-token",
                            "source": "manual:1",
                            "secret_fingerprint": "sha256:aaaaaaaaaaaaaaaa",
                        }
                    ]
                }
            },
            datetime.fromisoformat(self.NOW),
        )

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [{"provider": "openrouter", "id": "manual-no-token", "status": "missing_credential", "retry_at": None}],
        )

    def test_nous_agent_key_requires_runtime_usable_inference_jwt(self):
        now = datetime.fromisoformat(self.NOW)
        valid_jwt = _jwt({"scope": "inference:invoke", "exp": now.timestamp() + 3600})
        invalid_jwt = _jwt({"scope": "billing:manage", "exp": now.timestamp() + 3600})
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "nous": [
                        {"id": "valid", "agent_key": valid_jwt},
                        {"id": "invalid", "agent_key": invalid_jwt},
                    ]
                }
            },
            now,
        )

        self.assertFalse(result["triggered"])
        self.assertEqual(
            result["credential_pool_diagnostics"][0]["unavailable_slots"],
            [{"id": "invalid", "status": "missing_credential", "retry_at": None}],
        )
        self.assertNotIn(valid_jwt, repr(result))
        self.assertNotIn(invalid_jwt, repr(result))

    def test_empty_provider_is_unconfigured_notice_not_a_page(self):
        """ClickUp 86e2mdfhx regression: copilot/gemini stale EMPTY-list keys

        left behind by a credential-hygiene sweep must classify as
        unconfigured_provider and NEVER page — nothing routes to a provider
        with zero configured entries. Distinct from an absent pool key
        entirely (no change there).
        """
        now = datetime.fromisoformat(self.NOW)

        empty = monitor.classify_credential_pool(
            {"credential_pool": {"openai-codex": [], "copilot": [], "gemini": []}},
            now,
        )
        absent = monitor.classify_credential_pool({}, now)

        self.assertFalse(empty["triggered"])
        self.assertEqual(empty["hits"], [])
        self.assertEqual(
            empty["credential_pool_notices"],
            [
                {"provider": "copilot", "id": "pool", "status": "unconfigured_provider", "retry_at": None},
                {"provider": "gemini", "id": "pool", "status": "unconfigured_provider", "retry_at": None},
                {"provider": "openai-codex", "id": "pool", "status": "unconfigured_provider", "retry_at": None},
            ],
        )
        self.assertFalse(absent["triggered"])
        self.assertEqual(absent["status"], "absent")

    # Comprehensive per-class page/no-page coverage (including the live
    # regression cases this classification change fixes) lives in
    # tests/machine_setup/test_degraded_secrets_monitor_taxonomy.py — the
    # main CI lane. This file is NOT wired into any CI workflow.

    def test_supported_shapes_match_runtime_has_available(self):
        from agent.credential_pool import CredentialPool, PooledCredential

        now = datetime.fromisoformat(self.NOW)
        now_epoch = now.timestamp()
        valid_nous_jwt = _jwt({"scp": ["inference:invoke"], "exp": now_epoch + 3600})
        wrong_scope_jwt = _jwt({"scope": "billing:manage", "exp": now_epoch + 3600})
        nested_scope_jwt = _jwt({"scp": [["inference:invoke"]], "exp": now_epoch + 3600})
        malformed_scope_jwt = _jwt({"scope": {"value": "inference:invoke"}, "exp": now_epoch + 3600})
        boundary_jwt = _jwt({"scope": "inference:invoke", "exp": now_epoch + 120})
        fallback_expiry_jwt = _jwt({"scope": "inference:invoke"})
        cases = [
            ("openrouter", [{"id": "valid", "auth_type": "api_key", "access_token": "token"}]),
            ("openrouter", [{"id": "missing", "auth_type": "api_key", "access_token": ""}]),
            (
                "openai-codex",
                [{
                    "id": "active-cooldown",
                    "auth_type": "api_key",
                    "access_token": "token",
                    "last_status": "exhausted",
                    "last_error_reset_at": now_epoch + 60,
                }],
            ),
            (
                "openai-codex",
                [{
                    "id": "boundary",
                    "auth_type": "api_key",
                    "access_token": "token",
                    "last_status": "exhausted",
                    "last_error_reset_at": now_epoch,
                }],
            ),
            (
                "nous",
                [{"id": "agent", "auth_type": "api_key", "access_token": "", "agent_key": valid_nous_jwt}],
            ),
            (
                "nous",
                [{"id": "wrong-scope", "auth_type": "api_key", "access_token": wrong_scope_jwt}],
            ),
            (
                "nous",
                [{"id": "nested-scope", "auth_type": "api_key", "access_token": nested_scope_jwt}],
            ),
            (
                "nous",
                [{"id": "malformed-scope", "auth_type": "api_key", "access_token": malformed_scope_jwt}],
            ),
            (
                "nous",
                [{"id": "expiry-boundary", "auth_type": "api_key", "access_token": boundary_jwt}],
            ),
            (
                "nous",
                [{
                    "id": "expiry-fallback",
                    "auth_type": "api_key",
                    "access_token": fallback_expiry_jwt,
                    "expires_at": "2026-07-27T01:00:00Z",
                }],
            ),
            (
                "nous",
                [{"id": "invalid-jwt", "auth_type": "api_key", "access_token": "not-a-jwt"}],
            ),
        ]

        for provider, entries in cases:
            with self.subTest(provider=provider, entry=entries[0]["id"]):
                runtime_entries = [PooledCredential.from_dict(provider, entry) for entry in entries]
                with mock.patch("agent.credential_pool.time.time", return_value=now_epoch):
                    with mock.patch("hermes_cli.auth.time.time", return_value=now_epoch):
                        runtime_available = CredentialPool(provider, runtime_entries).has_available()
                monitored = monitor.classify_credential_pool(
                    {"credential_pool": {provider: entries}},
                    now,
                )
                self.assertEqual(not monitored["triggered"], runtime_available)

    def test_missing_and_malformed_entries_are_unavailable(self):
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "codex": [
                        {"id": "missing", "access_token": "  ", "last_status": "ok"},
                        "not-an-entry",
                    ],
                    "malformed-provider": {"id": "not-a-list"},
                }
            },
            datetime.fromisoformat(self.NOW),
        )

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [
                {"provider": "codex", "id": "index:1", "status": "malformed", "retry_at": None},
                {"provider": "codex", "id": "missing", "status": "missing_credential", "retry_at": None},
                {"provider": "malformed-provider", "id": "index:0", "status": "malformed", "retry_at": None},
            ],
        )

    def test_provider_health_isolated_from_other_providers(self):
        result = monitor.classify_credential_pool(
            {
                "credential_pool": {
                    "healthy": [{"id": "ok", "access_token": "token", "last_status": "ok"}],
                    "partial": [
                        {"id": "bad", "access_token": "token", "last_status": "dead"},
                        {"id": "ok", "access_token": "token", "last_status": "ok"},
                    ],
                    "outage": [{"id": "bad", "access_token": "token", "last_status": "error"}],
                }
            },
            datetime.fromisoformat(self.NOW),
        )

        self.assertTrue(result["triggered"])
        self.assertEqual(
            result["hits"],
            [{"provider": "outage", "id": "bad", "status": "missing_credential", "retry_at": None}],
        )
        self.assertEqual(
            [item["provider"] for item in result["credential_pool_diagnostics"]],
            ["partial"],
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
            [{"provider": "nous", "id": "pool-1", "status": "missing_credential", "retry_at": None}],
        )

    def test_mixed_pool_json_is_healthy_and_alert_transports_stay_silent(self):
        secret = "mixed-pool-secret-value"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            hermes_home = home / ".hermes"
            hermes_home.mkdir(parents=True)
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            log_file.write_text("")
            _write_auth(
                auth_file,
                {
                    "openai-codex": [
                        {"id": "primary", "access_token": secret, "last_status": "dead"},
                        {"id": "backup", "access_token": "backup-token", "last_status": "ok"},
                    ]
                },
            )
            env = os.environ.copy()
            env.update({"DRY_RUN": "1", "HOME": str(home), "HERMES_HOME": str(hermes_home)})

            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE),
                    "--json",
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

        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["degraded"])
        self.assertNotIn("credential_pool_diagnostics", payload["credential_pool"])
        self.assertEqual(
            payload["credential_pool_diagnostics"][0]["unavailable_slots"],
            [{"id": "primary", "status": "missing_credential", "retry_at": None}],
        )
        self.assertNotIn("DRY_RUN slack", result.stdout)
        self.assertNotIn("DRY_RUN clickup", result.stdout)
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_mixed_pool_human_output_logs_reduced_redundancy_without_alert(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            log_file.write_text("")
            _write_auth(
                auth_file,
                {
                    "codex": [
                        {"id": "primary", "access_token": "token", "last_status": "invalid"},
                        {"id": "backup", "access_token": "token", "last_status": "ok"},
                    ]
                },
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SOURCE),
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

        self.assertEqual(result.returncode, 0)
        self.assertIn("[degraded-secrets-monitor] healthy", result.stdout)
        self.assertIn(
            "credential pool reduced redundancy: provider=codex usable=1/2 unavailable=primary:missing_credential",
            result.stdout,
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
            _write_auth(
                auth_file,
                {
                    "codex": [{
                        "id": "pool-1",
                        "access_token": "token",
                        "last_status": "exhausted",
                        "last_error_reset_at": "2099-01-01T00:00:00Z",
                    }]
                },
            )
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
            "Credential pool degraded: provider 'codex' entry 'pool-1' status=exhausted_cap",
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
            _write_auth(
                auth_file,
                {"codex": [{"id": "pool-1", "access_token": "token", "last_status": "ok"}]},
            )
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

    def test_failed_alarm_delivery_does_not_advance_dedup_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            state_file = base / "state.json"
            log_file.write_text("")
            _write_auth(
                auth_file,
                {"codex": [{"id": "pool-1", "last_status": "exhausted"}]},
            )
            argv = [
                str(SOURCE),
                "--alert",
                "--log-file",
                str(log_file),
                "--auth-file",
                str(auth_file),
                "--now",
                self.NOW,
            ]
            with mock.patch.object(monitor, "STATE_PATH", str(state_file)):
                with mock.patch.object(monitor, "_send_slack", return_value=False):
                    with mock.patch.object(
                        monitor, "_post_clickup_comment", return_value=True
                    ):
                        with mock.patch.object(sys, "argv", argv):
                            with self.assertRaises(SystemExit) as raised:
                                monitor.main()
            state = json.loads(state_file.read_text())

        self.assertEqual(raised.exception.code, 1)
        self.assertNotIn("last_alert_signature", state)
        self.assertEqual(
            state["pending_deliveries"],
            {"slack": False, "clickup": True},
        )

    def test_each_destination_is_deduped_after_its_own_confirmed_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            auth_file = base / "auth.json"
            log_file = base / "gateway.error.log"
            state_file = base / "state.json"
            log_file.write_text("")
            _write_auth(
                auth_file,
                {"codex": [{"id": "pool-1", "last_status": "exhausted"}]},
            )
            argv = [
                str(SOURCE),
                "--alert",
                "--log-file",
                str(log_file),
                "--auth-file",
                str(auth_file),
                "--now",
                self.NOW,
            ]
            slack = mock.Mock(return_value=True)
            clickup = mock.Mock(side_effect=[False, True])
            with mock.patch.object(monitor, "STATE_PATH", str(state_file)):
                with mock.patch.object(monitor, "_send_slack", slack):
                    with mock.patch.object(monitor, "_post_clickup_comment", clickup):
                        with mock.patch.object(sys, "argv", argv):
                            for _ in range(2):
                                with self.assertRaises(SystemExit):
                                    monitor.main()
            state = json.loads(state_file.read_text())

        self.assertEqual(slack.call_count, 1)
        self.assertEqual(clickup.call_count, 2)
        self.assertIn("last_alert_signature", state)
        self.assertNotIn("pending_deliveries", state)


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
