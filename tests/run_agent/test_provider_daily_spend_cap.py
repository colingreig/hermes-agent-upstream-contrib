from unittest.mock import patch

from run_agent import AIAgent


class _FakeSessionDB:
    def __init__(self, spend_by_day):
        self.spend_by_day = spend_by_day

    def get_daily_provider_spend(self, day):
        return dict(self.spend_by_day.get(day, {}))


class _FakeUtcNow:
    def __init__(self, day):
        self.day = day

    def strftime(self, _fmt):
        return self.day


class _FakeDateTime:
    day = "1970-01-01"

    @classmethod
    def utcnow(cls):
        return _FakeUtcNow(cls.day)


def _make_agent(*, provider="anthropic", platform="cli", parent_session_id=None):
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            api_key="test-key",
            provider=provider,
            model="claude-sonnet-4.6",
            base_url="https://api.anthropic.com/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
            parent_session_id=parent_session_id,
        )


def test_default_config_sets_anthropic_daily_spend_cap_to_five():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["spend_caps"]["anthropic"] == 5.0


def test_provider_daily_spend_cap_block_uses_loaded_config_for_gateway_sessions(monkeypatch):
    agent = _make_agent(platform="telegram")
    day = "2026-07-05"
    agent._session_db = _FakeSessionDB({day: {"anthropic": 5.25}})
    _FakeDateTime.day = day
    monkeypatch.setattr("run_agent.datetime", _FakeDateTime)

    with patch("hermes_cli.config.load_config", return_value={"spend_caps": {"anthropic": 5.0}}):
        block = agent._get_provider_daily_spend_cap_block()

    assert block == {
        "provider": "anthropic",
        "day": day,
        "spend_usd": 5.25,
        "cap_usd": 5.0,
    }


def test_provider_daily_spend_cap_block_applies_to_orchestrator_subagents(monkeypatch):
    agent = _make_agent(parent_session_id="parent-session")
    day = "2026-07-05"
    agent._session_db = _FakeSessionDB({day: {"anthropic": 5.0}})
    _FakeDateTime.day = day
    monkeypatch.setattr("run_agent.datetime", _FakeDateTime)

    with patch("hermes_cli.config.load_config", return_value={"spend_caps": {"anthropic": 5.0, "openrouter": 10.0}}):
        block = agent._get_provider_daily_spend_cap_block()

    assert block is not None
    assert block["provider"] == "anthropic"
    assert block["cap_usd"] == 5.0


def test_non_anthropic_caps_remain_unchanged_when_not_configured(monkeypatch):
    agent = _make_agent(provider="openrouter")
    day = "2026-07-05"
    agent._session_db = _FakeSessionDB({day: {"openrouter": 999.0}})
    _FakeDateTime.day = day
    monkeypatch.setattr("run_agent.datetime", _FakeDateTime)

    with patch("hermes_cli.config.load_config", return_value={"spend_caps": {"anthropic": 5.0}}):
        assert agent._get_provider_daily_spend_cap_block() is None
