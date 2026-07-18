import pytest

from hermes_cli import routing_health as rh


@pytest.fixture(autouse=True)
def _normalize_provider(monkeypatch):
    monkeypatch.setattr(rh, "resolve_provider", lambda requested=None, **kwargs: (requested or "").lower())


@pytest.fixture

def status_map():
    return {}


@pytest.fixture(autouse=True)
def _auth_status(monkeypatch, status_map):
    monkeypatch.setattr(rh, "get_auth_status", lambda provider: status_map.get(provider, {}))
    monkeypatch.setattr(rh, "get_fallback_chain", lambda cfg: cfg.get("fallback_chain", []))


def _cfg(primary_provider, primary_model, fallback_chain=None):
    return {
        "model": {"provider": primary_provider, "default": primary_model},
        "fallback_chain": fallback_chain or [],
    }


def test_working_primary_is_reported_healthy(status_map):
    status_map["openai-codex"] = {"logged_in": True, "credential_source": "pool:manual"}

    snapshot = rh.build_route_health(_cfg("openai-codex", "gpt-5.5"))

    assert snapshot["summary"] == "openai-codex ready"
    assert snapshot["chain_exhausted"] is False
    assert snapshot["healthy_count"] == 1
    assert snapshot["entries"][0]["health"] == "healthy"
    assert snapshot["entries"][0]["credential_source"] == "pool:manual"


def test_same_provider_fallback_keeps_order_and_source(status_map):
    status_map["openrouter"] = {"logged_in": True, "credential_source": "env:OPENROUTER_API_KEY"}

    snapshot = rh.build_route_health(
        _cfg(
            "openrouter",
            "openai/gpt-5.5",
            fallback_chain=[
                {"provider": "openrouter", "model": "openai/gpt-4.1"},
            ],
        )
    )

    assert [entry["model"] for entry in snapshot["entries"]] == ["openai/gpt-5.5", "openai/gpt-4.1"]
    assert all(entry["health"] == "healthy" for entry in snapshot["entries"])
    assert snapshot["entries"][1]["source"].startswith("fallback_chain")


def test_cross_provider_fallback_is_truthful(status_map):
    status_map["z-ai"] = {}
    status_map["openai-codex"] = {"logged_in": True, "credential_source": "pool:manual"}

    snapshot = rh.build_route_health(
        _cfg(
            "z-ai",
            "glm-4.6",
            fallback_chain=[
                {"provider": "openai-codex", "model": "gpt-5.5"},
            ],
        )
    )

    assert snapshot["entries"][0]["health"] == "missing-credential"
    assert snapshot["entries"][1]["health"] == "healthy"
    assert snapshot["summary"] == "openai-codex ready"
    assert snapshot["chain_exhausted"] is False


def test_missing_credential_is_marked_unhealthy(status_map):
    status_map["gemini"] = {"logged_in": False}

    snapshot = rh.build_route_health(_cfg("gemini", "gemini-2.5-pro"))

    assert snapshot["entries"][0]["health"] == "missing-credential"
    assert snapshot["entries"][0]["reason"]


def test_cooldown_is_marked_unhealthy(status_map):
    status_map["openrouter"] = {"logged_in": True, "cooldown_until": "2099-01-01T00:00:00Z"}

    snapshot = rh.build_route_health(_cfg("openrouter", "openai/gpt-5.5"))

    assert snapshot["entries"][0]["health"] == "cooldown"
    assert snapshot["chain_exhausted"] is True


def test_probe_timeout_overrides_structural_health(status_map):
    status_map["openai-codex"] = {"logged_in": True, "credential_source": "pool:manual"}

    def _probe(*args, **kwargs):
        raise TimeoutError("timed out")

    snapshot = rh.build_route_health(
        _cfg("openai-codex", "gpt-5.5"),
        probe=True,
        probe_fn=lambda provider, model, base_url: _probe(provider, model, base_url),
    )

    assert snapshot["entries"][0]["health"] == "timeout"
    assert snapshot["entries"][0]["reason"] == "probe timed out"


def test_chain_exhaustion_is_reported(status_map):
    status_map["z-ai"] = {}
    status_map["gemini"] = {"logged_in": False}

    snapshot = rh.build_route_health(
        _cfg(
            "z-ai",
            "glm-4.6",
            fallback_chain=[
                {"provider": "gemini", "model": "gemini-2.5-pro"},
            ],
        )
    )

    assert snapshot["chain_exhausted"] is True
    assert snapshot["summary"] == "route chain exhausted"
    assert snapshot["healthy_count"] == 0
