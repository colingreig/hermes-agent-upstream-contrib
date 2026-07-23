"""Local chat-turn verification for the interactive Hermes chat default
model (86e28mq8g, docs/chat-default-model.md).

Drives a real AIAgent turn configured exactly like the mini's live
~/.hermes/config.yaml (model=gpt-5.5, provider=openai-codex,
api_mode=codex_app_server), using a wire-level fake Codex client rather than
stubbing CodexAppServerSession.run_turn. This proves the configured model
reaches the stable app-server thread/start request and a turn completes end
to end without live credentials or network access.
"""

from __future__ import annotations

from unittest.mock import patch

import run_agent


class RecordingCodexClient:
    """Minimal app-server client fake that records the protocol boundary."""

    instances = []

    def __init__(self, **_kwargs):
        instance_number = len(type(self).instances) + 1
        self.thread_id = f"thread-profile-{instance_number}"
        self.turn_number = 0
        self.requests = []
        self.notifications = []
        self.closed = False
        type(self).instances.append(self)

    def initialize(self, **_kwargs):
        return {"userAgent": "fake", "codexHome": "/tmp"}

    def request(self, method, params=None, timeout=30.0):
        params = params or {}
        self.requests.append((method, params, timeout))
        if method == "thread/start":
            return {"thread": {"id": self.thread_id}}
        if method == "turn/start":
            self.turn_number += 1
            turn_id = f"{self.thread_id}-turn-{self.turn_number}"
            text = params["input"][0]["text"]
            self.notifications.extend(
                [
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turnId": turn_id,
                            "item": {
                                "type": "agentMessage",
                                "id": f"message-{turn_id}",
                                "text": f"reply to: {text}",
                            },
                        },
                    },
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": self.thread_id,
                            "turn": {
                                "id": turn_id,
                                "status": "completed",
                                "error": None,
                            },
                        },
                    },
                ]
            )
            return {"turn": {"id": turn_id}}
        return {}

    def take_notification(self, timeout=0.0):
        return self.notifications.pop(0) if self.notifications else None

    def take_server_request(self, timeout=0.0):
        return None

    def is_alive(self):
        return not self.closed

    def stderr_tail(self, _lines=20):
        return []

    def close(self):
        self.closed = True


def test_chat_turn_sends_the_configured_default_to_codex_app_server(monkeypatch):
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

    # The configured default survives construction (and provider normalization)
    # unchanged, then must reach the real session's thread/start request.
    assert agent.model == "gpt-5.5"
    assert agent.provider == "openai-codex"

    RecordingCodexClient.instances.clear()
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerClient",
        RecordingCodexClient,
    )
    with patch.object(agent, "_spawn_background_review", return_value=None):
        result = agent.run_conversation("hello")

    assert result["completed"] is True
    assert result["error"] is None
    assert result["final_response"] == "reply to: hello"
    client = RecordingCodexClient.instances[0]
    _, thread_params, _ = next(
        request for request in client.requests if request[0] == "thread/start"
    )
    assert thread_params["model"] == agent.model
    _, turn_params, _ = next(
        request for request in client.requests if request[0] == "turn/start"
    )
    assert turn_params["model"] == agent.model


def _make_profile_agent(model):
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://chatgpt.com/backend-api/codex",
        provider="openai-codex",
        api_mode="codex_app_server",
        model=model,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def _run_without_review(agent, message):
    with patch.object(agent, "_spawn_background_review", return_value=None):
        return agent.run_conversation(message)


def test_mid_session_model_switch_keeps_thread_and_profiles_isolated(monkeypatch):
    RecordingCodexClient.instances.clear()
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerClient",
        RecordingCodexClient,
    )
    primary = _make_profile_agent("gpt-5.5")
    secondary = _make_profile_agent("gpt-5.4-mini")

    assert _run_without_review(primary, "primary first")["completed"] is True
    assert _run_without_review(secondary, "secondary first")["completed"] is True
    primary_session = primary._codex_session
    secondary_session = secondary._codex_session

    primary.switch_model(
        "gpt-5.4",
        "openai-codex",
        api_key="stub",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_app_server",
    )
    assert _run_without_review(primary, "primary switched")["completed"] is True
    assert _run_without_review(secondary, "secondary again")["completed"] is True

    # The switch applies through turn/start on the retained primary thread;
    # the other profile retains both its own thread and model.
    assert primary._codex_session is primary_session
    assert secondary._codex_session is secondary_session
    primary_client = primary_session._client
    secondary_client = secondary_session._client
    assert primary_client is not secondary_client
    assert primary_client.thread_id != secondary_client.thread_id
    assert len([r for r in primary_client.requests if r[0] == "thread/start"]) == 1
    assert len([r for r in secondary_client.requests if r[0] == "thread/start"]) == 1
    assert [
        params["model"]
        for method, params, _ in primary_client.requests
        if method == "turn/start"
    ] == ["gpt-5.5", "gpt-5.4"]
    assert [
        params["model"]
        for method, params, _ in secondary_client.requests
        if method == "turn/start"
    ] == ["gpt-5.4-mini", "gpt-5.4-mini"]
