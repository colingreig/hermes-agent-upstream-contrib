"""Tests for agent/ops_alerts.py — shared loud-failure Slack alert helper.

Covers dedup (fires once per signature, suppressed on repeat, re-fires on a
distinct signature), DRY_RUN behavior, and the subprocess invocation shape
(`hermes send --to <target> <message>`, mirroring the degraded-secrets
monitor's established convention).
"""

from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import agent.ops_alerts as ops_alerts


class TestAlertOnceDedup:
    def setup_method(self):
        ops_alerts.reset_for_tests()

    def test_first_call_for_a_signature_sends(self):
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            fired = ops_alerts.alert_once("vision:gemini", "vision is dead")
        assert fired is True
        mock_send.assert_called_once()

    def test_repeat_same_signature_is_suppressed(self):
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            first = ops_alerts.alert_once("vision:gemini", "vision is dead")
            second = ops_alerts.alert_once("vision:gemini", "vision is dead again")
            third = ops_alerts.alert_once("vision:gemini", "still dead")
        assert (first, second, third) == (True, False, False)
        mock_send.assert_called_once()

    def test_distinct_signature_alerts_again(self):
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            ops_alerts.alert_once("vision:gemini", "vision is dead")
            ops_alerts.alert_once("vision:openrouter", "different backend is dead")
        assert mock_send.call_count == 2

    def test_curator_and_vision_signatures_are_independent(self):
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            ops_alerts.alert_once("vision:auto", "vision dead")
            ops_alerts.alert_once("curator:auto", "curator dead")
        assert mock_send.call_count == 2

    def test_send_failure_does_not_raise(self):
        with patch.object(ops_alerts, "_send_slack", side_effect=RuntimeError("boom")):
            # Must never propagate — alerting must not break the caller.
            fired = ops_alerts.alert_once("vision:gemini", "vision is dead")
        assert fired is True

    def test_concurrent_calls_send_exactly_once(self, monkeypatch):
        class _SlowContainsSet(set):
            def __contains__(self, item):
                present = super().__contains__(item)
                time.sleep(0.02)
                return present

        monkeypatch.setattr(ops_alerts, "_ALERTED_SIGNATURES", _SlowContainsSet())
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            with ThreadPoolExecutor(max_workers=12) as pool:
                fired = list(
                    pool.map(
                        lambda _: ops_alerts.alert_once("provider:shared", "provider failed"),
                        range(12),
                    )
                )

        assert fired.count(True) == 1
        assert fired.count(False) == 11
        mock_send.assert_called_once()


class TestSendSlackShape:
    def test_dry_run_does_not_invoke_subprocess(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        with patch("agent.ops_alerts.subprocess.run") as mock_run:
            ok = ops_alerts._send_slack("hello")
        assert ok is True
        mock_run.assert_not_called()

    def test_live_send_invokes_hermes_send_with_target_and_mention(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        with (
            patch("agent.ops_alerts.subprocess.run") as mock_run,
            patch("agent.ops_alerts.shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            ok = ops_alerts._send_slack("vision is dead")
        assert ok is True
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "/usr/local/bin/hermes"
        assert cmd[1:4] == ["send", "--to", ops_alerts.OPS_ALERT_SLACK_TARGET]
        assert ops_alerts.OPS_ALERT_SLACK_MENTION in cmd[4]
        assert "vision is dead" in cmd[4]
        assert "--json" in cmd
        assert kwargs.get("check") is True
        assert kwargs.get("text") is True

    def test_subprocess_failure_returns_false_not_raise(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        with patch("agent.ops_alerts.subprocess.run", side_effect=OSError("no such binary")):
            ok = ops_alerts._send_slack("vision is dead")
        assert ok is False

    def test_default_target_matches_degraded_secrets_monitor_convention(self):
        # Same Slack DM + mention as the established
        # degraded_secrets_monitor.py convention — do not invent a new
        # channel/primitive for this alert.
        assert ops_alerts.OPS_ALERT_SLACK_TARGET == "slack:D0BA2PM9CFM"
        assert ops_alerts.OPS_ALERT_SLACK_MENTION == "<@UN4CQ1EGG>"


class TestOpsAlertReceipts:
    def test_successful_send_writes_non_secret_delivery_receipt(self, monkeypatch, tmp_path):
        receipt_path = tmp_path / "ops-alert-receipts.jsonl"
        monkeypatch.setattr(ops_alerts, "OPS_ALERT_RECEIPTS_LEDGER", str(receipt_path))
        monkeypatch.delenv("DRY_RUN", raising=False)
        completed = subprocess.CompletedProcess(
            args=["hermes"],
            returncode=0,
            stdout=json.dumps({
                "success": True,
                "platform": "slack",
                "chat_id": "D123",
                "message_id": "1785449999.000100",
            }),
            stderr="",
        )
        with (
            patch("agent.ops_alerts.subprocess.run", return_value=completed),
            patch("agent.ops_alerts.shutil.which", return_value="/usr/local/bin/hermes"),
        ):
            ok = ops_alerts._send_slack("fallback fired", signature="fallback:test")

        assert ok is True
        entry = json.loads(receipt_path.read_text(encoding="utf-8").strip())
        assert entry["signature"] == "fallback:test"
        assert entry["success"] is True
        assert entry["dry_run"] is False
        assert entry["returncode"] == 0
        assert entry["send_payload"]["platform"] == "slack"
        assert entry["send_payload"]["message_id"] == "1785449999.000100"
        assert "fallback fired" not in json.dumps(entry)

    def test_failed_send_receipt_redacts_stderr(self, monkeypatch, tmp_path):
        receipt_path = tmp_path / "ops-alert-receipts.jsonl"
        monkeypatch.setattr(ops_alerts, "OPS_ALERT_RECEIPTS_LEDGER", str(receipt_path))
        monkeypatch.delenv("DRY_RUN", raising=False)
        error = subprocess.CalledProcessError(
            1,
            ["hermes"],
            output="",
            stderr="Authorization: Bearer sk-secret-token\napi_key=abc123",
        )
        with patch("agent.ops_alerts.subprocess.run", side_effect=error):
            ok = ops_alerts._send_slack("fallback fired", signature="fallback:test")

        assert ok is False
        entry = json.loads(receipt_path.read_text(encoding="utf-8").strip())
        assert entry["success"] is False
        assert entry["returncode"] == 1
        assert "sk-secret-token" not in entry["stderr_tail"]
        assert "abc123" not in entry["stderr_tail"]
