"""Tests for the vision auto-chain-exhausted loud-failure alert.

ClickUp 86e29q8nc / hermes-agent audit H1: auxiliary.vision ships with no
config-only fallback (see DEFAULT_CONFIG["auxiliary"]["vision"] comment), so
when ``resolve_vision_provider_client()``'s built-in auto-chain (main
provider → OpenRouter → Nous → Anthropic) is exhausted, ``call_llm`` /
``async_call_llm`` must emit a single deduped Slack alert (via
``agent.ops_alerts``) before re-raising — instead of failing silently, which
is what actually happened live for 2+ days during the 2026-07 Gemini outage.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import agent.auxiliary_client as ac
import agent.ops_alerts as ops_alerts


@pytest.fixture(autouse=True)
def _reset_alert_dedup():
    ops_alerts.reset_for_tests()
    yield
    ops_alerts.reset_for_tests()


class TestSyncVisionChainExhaustedAlert:
    def test_call_llm_alerts_and_reraises_when_auto_chain_exhausted(self):
        with (
            patch(
                "agent.auxiliary_client.resolve_vision_provider_client",
                return_value=(None, None, None),
            ),
            patch.object(ops_alerts, "_send_slack") as mock_send,
        ):
            with pytest.raises(RuntimeError, match="No LLM provider configured"):
                ac.call_llm(
                    task="vision",
                    provider="gemini",
                    messages=[{"role": "user", "content": "describe this image"}],
                )
        mock_send.assert_called_once()
        alert_text = mock_send.call_args[0][0]
        assert "vision" in alert_text.lower()
        assert "gemini" in alert_text

    def test_healthy_vision_call_never_alerts(self):
        from unittest.mock import MagicMock

        healthy_client = MagicMock()
        healthy_client.base_url = "https://example.test"
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="a description"))]
        healthy_client.chat.completions.create.return_value = response

        with (
            patch(
                "agent.auxiliary_client.resolve_vision_provider_client",
                return_value=("gemini", healthy_client, "gemini-3.5-flash"),
            ),
            patch(
                "agent.auxiliary_client._validate_llm_response",
                side_effect=lambda r, t, **_metadata: r,
            ),
            patch.object(ops_alerts, "_send_slack") as mock_send,
        ):
            resp = ac.call_llm(
                task="vision",
                provider="gemini",
                messages=[{"role": "user", "content": "describe this image"}],
            )
        assert resp is response
        mock_send.assert_not_called()

    def test_repeated_exhaustion_alerts_once_deduped(self):
        with (
            patch(
                "agent.auxiliary_client.resolve_vision_provider_client",
                return_value=(None, None, None),
            ),
            patch.object(ops_alerts, "_send_slack") as mock_send,
        ):
            for _ in range(3):
                with pytest.raises(RuntimeError):
                    ac.call_llm(
                        task="vision",
                        provider="gemini",
                        messages=[{"role": "user", "content": "hi"}],
                    )
        mock_send.assert_called_once()


class TestAsyncVisionChainExhaustedAlert:
    def test_async_call_llm_alerts_and_reraises_when_auto_chain_exhausted(self):
        with (
            patch(
                "agent.auxiliary_client.resolve_vision_provider_client",
                return_value=(None, None, None),
            ),
            patch.object(ops_alerts, "_send_slack") as mock_send,
        ):
            with pytest.raises(RuntimeError, match="No LLM provider configured"):
                asyncio.run(
                    ac.async_call_llm(
                        task="vision",
                        provider="gemini",
                        messages=[{"role": "user", "content": "describe this image"}],
                    )
                )
        mock_send.assert_called_once()
