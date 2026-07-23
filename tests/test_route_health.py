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


def test_resolve_route_health_reports_same_provider_fallback(monkeypatch):
    monkeypatch.setattr(
        rh,
        "load_config",
        lambda: {
            "model": {"provider": "openrouter", "default": "openai/gpt-4.1"},
            "fallback_providers": [{"provider": "openrouter", "model": "anthropic/claude-3.5-sonnet"}],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value_prefer_dotenv",
        lambda name: "sk-test" if name == "OPENROUTER_API_KEY" else "",
    )

    result = rh.resolve_route_health()

    assert result["provider"] == "openrouter"
    assert result["primary"]["health"] == "healthy"
    assert result["fallbacks"]
    assert result["fallbacks"][0]["provider"] == "openrouter"
    assert result["fallbacks"][0]["model"] == "anthropic/claude-3.5-sonnet"
    assert result["fallbacks"][0]["fallback_kind"] == "same-provider"
    assert result["runnable"] is True


def test_resolve_route_health_reports_missing_credential(monkeypatch):
    monkeypatch.setattr(
        rh,
        "load_config",
        lambda: {"model": {"provider": "anthropic", "default": "claude-sonnet-4"}},
    )

    def _raise_load_pool(provider):
        raise RuntimeError("no credential pool for provider")

    monkeypatch.setattr(rh, "load_pool", _raise_load_pool)
    monkeypatch.setattr(rh, "get_auth_status", lambda provider: {})

    result = rh.resolve_route_health()

    assert result["provider"] == "anthropic"
    assert result["primary"]["health"] == "missing_credential"
    assert result["primary"]["configured"] is False
    # No fallback is configured, so nothing saves the chain.
    assert result["fallbacks"] == []
    assert result["runnable"] is False


def test_resolve_route_health_reports_total_chain_exhaustion(monkeypatch):
    entry = SimpleNamespace(
        id="cred-1",
        label="primary",
        source="credential-pool",
        last_status=STATUS_EXHAUSTED,
        last_status_at="2026-07-18T00:00:00Z",
    )
    fake_pool = _FakePool(entries=[entry], available=[])
    empty_pool = _FakePool(entries=[], available=[])

    monkeypatch.setattr(
        rh,
        "load_config",
        lambda: {
            "model": {"provider": "anthropic", "default": "claude-sonnet-4"},
            "fallback_providers": [{"provider": "openrouter", "model": "openai/gpt-4.1"}],
        },
    )
    # Provider-aware pool: the primary (anthropic) route is chain-exhausted;
    # the fallback (openrouter) has no pool entries at all. Fallback routes
    # now consult pool state the same way the primary does (see
    # _fallback_route_health), so this must be keyed by provider rather than
    # returning the same fake pool for every provider.
    monkeypatch.setattr(
        rh,
        "load_pool",
        lambda provider: fake_pool if provider == "anthropic" else empty_pool,
    )
    # No cooldown window remaining anywhere in the pool -> terminal "exhausted",
    # not a still-ticking "cooldown".
    monkeypatch.setattr(rh, "_exhausted_until", lambda entry: None)
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value_prefer_dotenv",
        lambda name: "",
    )

    result = rh.resolve_route_health()

    assert result["primary"]["health"] == "exhausted"
    assert result["primary"]["configured"] is False
    assert result["fallbacks"]
    assert result["fallbacks"][0]["provider"] == "openrouter"
    assert result["fallbacks"][0]["health"] == "missing_credential"
    assert result["fallbacks"][0]["configured"] is False
    assert result["runnable"] is False
    all_routes = [result["primary"], *result["fallbacks"]]
    assert all(route["health"] != "healthy" for route in all_routes)


def test_resolve_route_health_reports_timeout_as_pool_exhaustion(monkeypatch):
    # route_health is a structural, read-only resolver — it has no live
    # "timeout" state of its own. A request timeout against a provider
    # manifests one layer down: the credential pool records the failure and
    # puts the entry into STATUS_EXHAUSTED (with a cooldown/reset window,
    # mirrored here via _exhausted_until), so the structural snapshot must
    # report that route as unavailable ("cooldown"/"exhausted") rather than
    # "healthy" so the chain correctly routes around a timed-out provider.
    entry = SimpleNamespace(
        id="cred-timeout",
        label="primary",
        source="credential-pool",
        last_status=STATUS_EXHAUSTED,
        last_status_at="2026-07-18T00:00:00Z",
        last_error_reason="request_timeout",
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

    assert result["primary"]["health"] in {"cooldown", "exhausted"}
    assert result["primary"]["health"] != "healthy"
    assert result["primary"]["configured"] is False
    assert result["primary"]["entry_routes"][0]["last_status"] == STATUS_EXHAUSTED
    assert result["runnable"] is False


def test_resolve_route_health_no_fallback_suppresses_fallback_chain(monkeypatch):
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

    result = rh.resolve_route_health(no_fallback=True)

    assert result["primary"]["health"] == "healthy"
    assert result["fallbacks"] == []
    assert result["fallback_chain"] == []


def test_resolve_route_health_fallback_reports_pool_unusable_api_key_provider(monkeypatch):
    # zai/gemini are auth_type="api_key" providers: a bare env-presence check
    # (_generic_provider_health/get_auth_status) reports "healthy" the moment
    # ZAI_API_KEY is set, even if every real call against that key has failed.
    # The credential pool records that outcome (STATUS_EXHAUSTED here), and
    # the shared resolver must consult it for fallback routes too — not just
    # the primary route — so a present-but-pool-flagged-unusable key is
    # reported unhealthy instead of silently "healthy".
    zai_entry = SimpleNamespace(
        id="zai-cred-1",
        label="env:ZAI_API_KEY",
        source="env:ZAI_API_KEY",
        access_token="zai-key-that-always-401s",
        last_status=STATUS_EXHAUSTED,
        last_status_at="2026-07-18T00:00:00Z",
        last_error_code=429,
    )
    zai_pool = _FakePool(entries=[zai_entry], available=[])

    monkeypatch.setattr(
        rh,
        "load_config",
        lambda: {
            "model": {"provider": "anthropic", "default": "claude-sonnet-4"},
            "fallback_providers": [{"provider": "zai", "model": "glm-4.6"}],
        },
    )

    def _load_pool(provider):
        if provider == "zai":
            return zai_pool
        raise RuntimeError(f"no fake pool configured for provider={provider!r}")

    monkeypatch.setattr(rh, "load_pool", _load_pool)
    # Terminal exhaustion (no active cooldown window remaining).
    monkeypatch.setattr(rh, "_exhausted_until", lambda entry: None)
    # Bare env-presence check would say "healthy" for zai if it were ever
    # consulted here (the key is "present") — this proves the pool state,
    # not the env check, wins for the fallback route.
    monkeypatch.setattr(
        rh,
        "get_auth_status",
        lambda provider: {"configured": True, "logged_in": True, "source": "oauth"}
        if provider == "anthropic"
        else {"configured": True, "api_key": "zai-key-that-always-401s"},
    )

    result = rh.resolve_route_health()

    assert result["fallbacks"]
    zai_route = result["fallbacks"][0]
    assert zai_route["provider"] == "zai"
    assert zai_route["health"] != "healthy"
    assert zai_route["health"] == "exhausted"


def test_summarize_route_health_verbose_lists_primary_and_each_fallback(monkeypatch):
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
    verbose_lines = rh.summarize_route_health_verbose(result)
    one_line = rh.summarize_route_health(result)

    assert len(result["fallbacks"]) == 1
    assert verbose_lines[0].startswith("Primary:")
    assert "openrouter" in verbose_lines[0]
    assert len(verbose_lines) == 1 + len(result["fallbacks"])
    fallback_line = verbose_lines[1]
    assert fallback_line.startswith("Fallback:")
    assert "anthropic" in fallback_line
    assert "claude-3.5-sonnet" in fallback_line
    # The verbose form exposes each hop; the one-liner collapses them to a count.
    assert len(verbose_lines) > 1
    assert one_line.count("\n") == 0
