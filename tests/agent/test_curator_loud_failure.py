"""Tests for the curator full-fallback-chain-exhausted loud-failure alert.

ClickUp 86e29q8nc / hermes-agent audit H1: ``agent/curator.py::_run_llm_review``
spawns an ``AIAgent`` fork that never calls ``call_llm()``, so it can't use
``auxiliary.curator.fallback`` — its only degradation path is the main
agent's top-level ``fallback_providers`` chain, engaged internally by
``AIAgent.run_conversation()``. If that chain is also exhausted, the
exception surfaces to ``_run_llm_review``'s except block, which must now
emit a single deduped Slack alert (via ``agent.ops_alerts``) instead of
just recording a silent ``result_meta["error"]``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import agent.curator as curator
import agent.ops_alerts as ops_alerts


class _DummyAgentFullChainFails:
    """Stands in for AIAgent whose internal fallback_providers chain is dead."""

    def __init__(self, **kwargs):
        self._session_messages = []

    def run_conversation(self, user_message):
        raise RuntimeError("all configured providers exhausted")

    def close(self):
        pass


class _DummyAgentSucceeds:
    def __init__(self, **kwargs):
        self._session_messages = []

    def run_conversation(self, user_message):
        return {"final_response": "reviewed the skill collection, no changes needed"}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_alert_dedup():
    ops_alerts.reset_for_tests()
    yield
    ops_alerts.reset_for_tests()


@pytest.fixture
def _stub_runtime_resolution(monkeypatch):
    """Neutralize config/provider resolution so tests don't need real creds."""
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kw: {
            "api_key": None,
            "base_url": None,
            "api_mode": None,
            "provider": "openai-codex",
        },
    )


class TestCuratorChainExhaustedAlert:
    def test_full_chain_failure_alerts_and_records_error(self, _stub_runtime_resolution, monkeypatch):
        monkeypatch.setattr("run_agent.AIAgent", _DummyAgentFullChainFails)
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            result = curator._run_llm_review("review prompt")

        assert result["error"]
        mock_send.assert_called_once()
        alert_text = mock_send.call_args[0][0]
        assert "curator" in alert_text.lower()
        assert "openai-codex" in alert_text

    def test_healthy_review_pass_never_alerts(self, _stub_runtime_resolution, monkeypatch):
        monkeypatch.setattr("run_agent.AIAgent", _DummyAgentSucceeds)
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            result = curator._run_llm_review("review prompt")

        assert result["error"] is None
        assert "reviewed" in result["final"]
        mock_send.assert_not_called()

    def test_repeated_full_chain_failure_alerts_once_deduped(self, _stub_runtime_resolution, monkeypatch):
        monkeypatch.setattr("run_agent.AIAgent", _DummyAgentFullChainFails)
        with patch.object(ops_alerts, "_send_slack") as mock_send:
            for _ in range(3):
                curator._run_llm_review("review prompt")

        mock_send.assert_called_once()

    def test_never_raises_out_of_run_llm_review(self, _stub_runtime_resolution, monkeypatch):
        monkeypatch.setattr("run_agent.AIAgent", _DummyAgentFullChainFails)
        with patch.object(ops_alerts, "_send_slack", side_effect=RuntimeError("slack is also down")):
            # Alert-send failures must not leak past _run_llm_review's own
            # "never raises" contract.
            result = curator._run_llm_review("review prompt")
        assert result["error"]
