from __future__ import annotations

import time

from agent.route_health import resolve_effective_routes


def _cfg(provider="zai", model="glm-5.1", fallbacks=None):
    return {
        "model": {"provider": provider, "default": model},
        "fallback_providers": fallbacks or [],
    }


def test_route_health_working_primary_env(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "sk-test-zai")

    report = resolve_effective_routes("interactive", config=_cfg())

    entry = report.chains[0].entries[0]
    assert entry.provider == "zai"
    assert entry.healthy is True
    assert entry.credential_source == "env:ZAI_API_KEY"


def test_route_health_same_provider_fallback(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "sk-test-zai")

    report = resolve_effective_routes(
        "interactive",
        config=_cfg(fallbacks=[{"provider": "zai", "model": "glm-5.1-air"}]),
    )

    entries = report.chains[0].entries
    assert [(entry.provider, entry.model, entry.healthy) for entry in entries] == [
        ("zai", "glm-5.1", True),
        ("zai", "glm-5.1-air", True),
    ]


def test_route_health_cross_provider_fallback_survives_primary_outage(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test")

    report = resolve_effective_routes(
        "interactive",
        config=_cfg(fallbacks=[{"provider": "gemini", "model": "gemini-2.5-flash"}]),
    )

    primary, fallback = report.chains[0].entries
    assert primary.healthy is False
    assert "missing env credential" in primary.reason
    assert fallback.provider == "gemini"
    assert fallback.healthy is True
    assert report.healthy is True


def test_route_health_missing_credential_is_unhealthy(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)

    report = resolve_effective_routes("interactive", config=_cfg())

    entry = report.chains[0].entries[0]
    assert entry.healthy is False
    assert entry.health == "unhealthy"
    assert "missing env credential" in entry.reason


def test_route_health_pool_cooldown_is_unhealthy(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    def read_pool(provider):
        assert provider == "gemini"
        return [
            {
                "id": "g1",
                "label": "gemini-free",
                "auth_type": "api_key",
                "source": "manual",
                "access_token": "gemini-test",
                "last_status": "exhausted",
                "last_status_at": time.time(),
                "last_error_code": 429,
            }
        ]

    monkeypatch.setattr("hermes_cli.auth.read_credential_pool", read_pool)

    report = resolve_effective_routes("interactive", config=_cfg("gemini", "gemini-2.5-flash"))


    entry = report.chains[0].entries[0]
    assert entry.healthy is False
    assert "cooldown" in entry.reason


def test_route_health_pool_timeout_is_unhealthy(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)

    def read_pool(provider):
        assert provider == "zai"
        return [
            {
                "id": "z1",
                "label": "zai-primary",
                "auth_type": "api_key",
                "source": "manual",
                "access_token": "zai-test",
                "last_status": "exhausted",
                "last_status_at": time.time(),
                "last_error_code": 504,
                "last_error_reason": "timeout",
            }
        ]

    monkeypatch.setattr("hermes_cli.auth.read_credential_pool", read_pool)

    report = resolve_effective_routes("interactive", config=_cfg())

    entry = report.chains[0].entries[0]
    assert entry.healthy is False
    assert "timeout" in entry.reason
