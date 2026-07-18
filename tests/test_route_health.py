from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import route_health as rh
from agent.credential_pool import STATUS_EXHAUSTED


class _FakePool:
    def __init__(self, entries, available=None):
        self._entries = list(entries)
        self._available = list(available) if available is not None else list(entries)

    def entries(self):
        return list(self._entries)

    def _available_entries(self, clear_expired=False, refresh=False):
        return list(self._available)

    def current(self):
        return self._available[0] if self._available else (self._entries[0] if self._entries else None)

    def peek(self):
        return self.current()


def test_resolve_route_health_reports_primary_and_fallbacks(monkeypatch):
    monkeypatch.setattr(
        rh,
        "load_config",
        lambda: {
            "model": {"provider": "openrouter", "default": "openai/gpt-4.1"},
            "fallback_providers": [{"provider": "anthropic", "model": "claude-3.5-sonnet"}],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value_prefer_dotenv",
        lambda name: "sk-test" if name == "OPENROUTER_API_KEY" else "",
    )
    monkeypatch.setattr(
        rh,
        "get_auth_status",
        lambda provider: {"configured": True, "logged_in": True, "source": "oauth"}
        if provider == "anthropic"
        else {},
    )
    result = rh.resolve_route_health()

    assert result["provider"] == "openrouter"
    assert result["primary"]["health"] == "healthy"
    assert result["fallbacks"]
    assert result["fallbacks"][0]["provider"] == "anthropic"
    assert result["fallbacks"][0]["fallback_kind"] == "cross-provider"
    assert result["runnable"] is True


def test_resolve_route_health_reports_pool_cooldown(monkeypatch):
    entry = SimpleNamespace(
        id="cred-1",
        label="primary",
        source="credential-pool",
        last_status=STATUS_EXHAUSTED,
        last_status_at="2026-07-18T00:00:00Z",
    )
    fake_pool = _FakePool(entries=[entry], available=[])

    monkeypatch.setattr(
        rh,
        "load_config",
        lambda: {"model": {"provider": "anthropic", "default": "claude-sonnet-4"}},
    )
    monkeypatch.setattr(rh, "load_pool", lambda provider: fake_pool)
    monkeypatch.setattr(rh, "_exhausted_until", lambda entry: 9_999_999_999.0)

    result = rh.resolve_route_health()

    assert result["provider"] == "anthropic"
    assert result["primary"]["health"] == "cooldown"
    assert result["primary"]["configured"] is False
    assert result["primary"]["entry_routes"][0]["status"] == "cooldown"
    assert result["runnable"] is False
