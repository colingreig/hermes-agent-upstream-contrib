"""Local chat-turn verification for the interactive Hermes chat default
model (86e28mq8g, docs/chat-default-model.md).

Drives a real AIAgent turn configured exactly like the mini's live
~/.hermes/config.yaml (model=gpt-5.5, provider=openai-codex,
api_mode=codex_app_server), with the Codex session transport stubbed out
(no live credentials/network — mirrors
tests/run_agent/test_codex_app_server_integration.py's established pattern).
This is the automated equivalent of a real local chat-turn check: it proves
the configured default model is actually threaded through AIAgent and a
turn completes end to end, rather than merely asserting the config value in
isolation.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import run_agent
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult


@pytest.fixture
def fake_default_model_session(monkeypatch):
    def fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(
            final_text=f"reply to: {user_input}",
            projected_messages=[
                {"role": "assistant", "content": f"reply to: {user_input}"},
            ],
            tool_iterations=0,
            interrupted=False,
            error=None,
            turn_id="turn-default-model-1",
            thread_id="thread-default-model-1",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CodexAppServerSession, "ensure_started", lambda self: "thread-default-model-1"
    )


def test_chat_turn_completes_with_the_recommended_default_model(fake_default_model_session):
    """gpt-5.5 / openai-codex / codex_app_server — the exact live mini
    configuration (docs/chat-default-model.md) — completes a real chat
    turn through AIAgent.run_conversation()."""
    agent = run_agent.AIAgent(
        api_key="stub",
        base_url="https://chatgpt.com/backend-api/codex",
        provider="openai-codex",
        api_mode="codex_app_server",
        model="gpt-5.5",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    # The configured default survives construction (and normalize_model_for_provider,
    # which is a no-op for this provider/model pair) unchanged.
    assert agent.model == "gpt-5.5"
    assert agent.provider == "openai-codex"

    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["error"] is None
    assert result["final_response"] == "reply to: hello"
