"""Tests for credential_pool .env fallback and auth credential_pool lookup.

Covers the fix from #15914 / PR #15920 and the rotation fix from #20591:
- _seed_from_env reads API keys from ~/.hermes/.env when not in os.environ
- _resolve_api_key_provider_secret falls back to credential_pool when env vars are empty
- ~/.hermes/.env takes priority over os.environ for Hermes-managed credentials
  (so a deliberate rotation in .env wins over a stale shell export)
- env / dotenv values take priority over credential pool (pool fires only when both are empty)
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_pconfig(provider_id="deepseek", env_vars=None):
    """Create a minimal ProviderConfig for testing.

    Default provider_id is 'deepseek' because it's a real api_key provider
    in PROVIDER_REGISTRY (needed for _seed_from_env's generic path).
    """
    from hermes_cli.auth import ProviderConfig
    return ProviderConfig(
        id=provider_id,
        name=provider_id.title(),
        auth_type="api_key",
        api_key_env_vars=tuple(env_vars or [f"{provider_id.upper()}_API_KEY"]),
    )


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir and clear known API key env vars.

    Also invalidates any cached get_env_value state by patching Path.home().
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Clear all known API key env vars so get_env_value falls through to .env
    for key in [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        "ZAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_BASE_URL",
    ]:
        monkeypatch.delenv(key, raising=False)

    return home


def _write_env_file(home: Path, **kwargs) -> None:
    """Write key=value pairs to ~/.hermes/.env."""
    lines = [f"{k}={v}" for k, v in kwargs.items()]
    (home / ".env").write_text("\n".join(lines) + "\n")


class TestCredentialPoolSeedsFromDotEnv:
    """_seed_from_env must read keys from ~/.hermes/.env, not just os.environ.

    This is the load-bearing behaviour for the fix: when a user adds a key to
    .env mid-session or via a non-CLI entry point that doesn't run
    load_hermes_dotenv, the credential pool must still discover it.
    """

    def test_deepseek_key_from_dotenv_only(self, isolated_hermes_home):
        """Key in .env but not os.environ → _seed_from_env adds a pool entry."""
        _write_env_file(isolated_hermes_home, DEEPSEEK_API_KEY="sk-dotenv-only-12345")
        assert "DEEPSEEK_API_KEY" not in os.environ

        from agent.credential_pool import _seed_from_env
        entries = []
        changed, active_sources = _seed_from_env("deepseek", entries)

        assert changed is True
        assert "env:DEEPSEEK_API_KEY" in active_sources
        assert any(
            e.access_token == "sk-dotenv-only-12345"
            and e.source == "env:DEEPSEEK_API_KEY"
            for e in entries
        ), f"Expected seeded entry with dotenv key, got: {[(e.source, e.access_token) for e in entries]}"

    def test_openrouter_key_from_dotenv_only(self, isolated_hermes_home):
        """OpenRouter path has its own branch — verify it also reads .env."""
        _write_env_file(isolated_hermes_home, OPENROUTER_API_KEY="sk-or-dotenv-abc")
        assert "OPENROUTER_API_KEY" not in os.environ

        from agent.credential_pool import _seed_from_env
        entries = []
        changed, active_sources = _seed_from_env("openrouter", entries)

        assert changed is True
        assert "env:OPENROUTER_API_KEY" in active_sources
        assert any(
            e.access_token == "sk-or-dotenv-abc" for e in entries
        )

    def test_empty_dotenv_no_entries(self, isolated_hermes_home):
        """No .env file, no env vars → no entries seeded (and no crash)."""
        from agent.credential_pool import _seed_from_env
        entries = []
        changed, active_sources = _seed_from_env("deepseek", entries)
        assert changed is False
        assert active_sources == set()
        assert entries == []

    def test_dotenv_wins_over_stale_os_environ(self, isolated_hermes_home, monkeypatch):
        """Regression for #20591: a fresh key rotated into ~/.hermes/.env must
        win over a stale value inherited from os.environ (parent shell export
        from Codex CLI, test runner, login profile, etc.). Without this, key
        rotation produces persistent 401s.
        """
        _write_env_file(isolated_hermes_home, DEEPSEEK_API_KEY="sk-dotenv-fresh")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-stale-xyz")

        from agent.credential_pool import _seed_from_env
        entries = []
        changed, _ = _seed_from_env("deepseek", entries)

        assert changed is True
        seeded = [e for e in entries if e.source == "env:DEEPSEEK_API_KEY"]
        assert len(seeded) == 1
        assert seeded[0].access_token == "sk-dotenv-fresh"


class TestAuthResolvesFromDotEnv:
    """_resolve_api_key_provider_secret must also read from ~/.hermes/.env."""

    def test_key_from_dotenv_only(self, isolated_hermes_home):
        """Key in .env but not os.environ → _resolve returns it with the env var source."""
        _write_env_file(isolated_hermes_home, DEEPSEEK_API_KEY="sk-dotenv-resolve-789")
        assert "DEEPSEEK_API_KEY" not in os.environ

        from hermes_cli.auth import _resolve_api_key_provider_secret
        key, source = _resolve_api_key_provider_secret(
            provider_id="deepseek",
            pconfig=_make_pconfig(),
        )
        assert key == "sk-dotenv-resolve-789"
        assert source == "DEEPSEEK_API_KEY"

    def test_dotenv_wins_over_stale_os_environ_on_resolve(
        self, isolated_hermes_home, monkeypatch
    ):
        """Regression for #20591: when both ~/.hermes/.env and os.environ define
        the key, the .env value wins. Symmetric with the pool seeding rule —
        without this, the pool gets re-seeded with the fresh .env key while the
        live request path keeps returning the stale shell export, producing
        persistent 401s after rotation.
        """
        _write_env_file(isolated_hermes_home, DEEPSEEK_API_KEY="dotenv-fresh-deepseek")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "stale-shell-deepseek")

        from hermes_cli.auth import _resolve_api_key_provider_secret
        key, source = _resolve_api_key_provider_secret(
            provider_id="deepseek",
            pconfig=_make_pconfig(),
        )
        assert key == "dotenv-fresh-deepseek"
        assert source == "DEEPSEEK_API_KEY"

    def test_get_anthropic_key_prefers_dotenv_over_stale_os_environ(
        self, isolated_hermes_home, monkeypatch
    ):
        """Regression for #20591 (sibling site): get_anthropic_key() must also
        prefer ~/.hermes/.env over a stale shell export. This path resolves
        ANTHROPIC_API_KEY/ANTHROPIC_TOKEN/CLAUDE_CODE_OAUTH_TOKEN and had the
        identical os.environ-first rotation bug that the api-key resolution
        path did, just for Anthropic.
        """
        _write_env_file(isolated_hermes_home, ANTHROPIC_API_KEY="dotenv-fresh-anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-shell-anthropic")

        from hermes_cli.auth import get_anthropic_key
        assert get_anthropic_key() == "dotenv-fresh-anthropic"


class TestAuthCredentialPoolFallback:
    """_resolve_api_key_provider_secret falls back to credential pool when env + dotenv are empty."""

    def test_credential_pool_fallback_structure(self, isolated_hermes_home):
        """Empty env + empty .env → auth falls back to credential pool."""
        mock_entry = MagicMock()
        mock_entry.access_token = "test-pool-key-12345"
        mock_entry.runtime_api_key = ""

        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = True
        mock_pool.peek.return_value = mock_entry

        from hermes_cli.auth import _resolve_api_key_provider_secret
        with patch("agent.credential_pool.load_pool", return_value=mock_pool):
            key, source = _resolve_api_key_provider_secret(
                provider_id="deepseek",
                pconfig=_make_pconfig(),
            )
        assert "test-pool-key-12345" in key
        assert "credential_pool" in source

    def test_credential_pool_empty_returns_empty(self, isolated_hermes_home):
        """Empty env + empty .env + empty pool → empty string."""
        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = False

        from hermes_cli.auth import _resolve_api_key_provider_secret
        with patch("agent.credential_pool.load_pool", return_value=mock_pool):
            key, source = _resolve_api_key_provider_secret(
                provider_id="deepseek",
                pconfig=_make_pconfig(),
            )
        assert key == ""

    def test_env_var_takes_priority_over_pool(self, isolated_hermes_home, monkeypatch):
        """os.environ key wins — credential pool is NEVER consulted."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-key-first-abc123")

        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = True

        from hermes_cli.auth import _resolve_api_key_provider_secret
        with patch("agent.credential_pool.load_pool", return_value=mock_pool) as mp:
            key, source = _resolve_api_key_provider_secret(
                provider_id="deepseek",
                pconfig=_make_pconfig(),
            )
        assert key == "sk-env-key-first-abc123"
        assert source == "DEEPSEEK_API_KEY"
        # Pool should not even have been loaded — env var satisfied the request first
        mp.assert_not_called()

    def test_dotenv_takes_priority_over_pool(self, isolated_hermes_home):
        """Key in .env beats credential pool — pool only fires when both env sources are empty."""
        _write_env_file(isolated_hermes_home, DEEPSEEK_API_KEY="sk-dotenv-priority-xyz")
        assert "DEEPSEEK_API_KEY" not in os.environ

        mock_pool = MagicMock()
        mock_pool.has_credentials.return_value = True

        from hermes_cli.auth import _resolve_api_key_provider_secret
        with patch("agent.credential_pool.load_pool", return_value=mock_pool) as mp:
            key, source = _resolve_api_key_provider_secret(
                provider_id="deepseek",
                pconfig=_make_pconfig(),
            )
        assert key == "sk-dotenv-priority-xyz"
        assert source == "DEEPSEEK_API_KEY"
        mp.assert_not_called()


class TestSanitizedPoolReferenceRehydration:
    """Borrowed pool entries remain usable without persisting raw secrets."""

    @staticmethod
    def _write_sanitized_gemini_pool(
        home: Path, *, suppressed_sources=None
    ) -> None:
        payload = {
            "version": 1,
            "providers": {},
            "credential_pool": {
                "gemini": [
                    {
                        "id": "gemini-ref",
                        "label": "GEMINI_API_KEY",
                        "auth_type": "api_key",
                        "priority": 0,
                        "source": "env:GEMINI_API_KEY",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta",
                        "secret_fingerprint": "sha256:0123456789abcdef",
                    }
                ]
            },
        }
        if suppressed_sources:
            payload["suppressed_sources"] = suppressed_sources
        (home / "auth.json").write_text(json.dumps(payload))

    @staticmethod
    def _write_suppressed_reference(
        home: Path, *, provider: str, env_var: str, base_url: str
    ) -> None:
        (home / "auth.json").write_text(json.dumps({
            "version": 1,
            "providers": {},
            "suppressed_sources": {provider: [f"env:{env_var}"]},
            "credential_pool": {
                provider: [{
                    "id": f"{provider}-ref",
                    "label": env_var,
                    "auth_type": "api_key",
                    "priority": 0,
                    "source": f"env:{env_var}",
                    "base_url": base_url,
                    "secret_fingerprint": "sha256:0123456789abcdef",
                }]
            },
        }))

    def test_runtime_rehydrates_exact_pool_source_without_global_env(
        self, isolated_hermes_home, monkeypatch
    ):
        """The real runtime path receives a pool-backed key and keeps disk clean."""
        self._write_sanitized_gemini_pool(isolated_hermes_home)
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        # The broad rollout flag is intentionally absent: an existing,
        # fingerprinted pool reference is a narrower authorization boundary.
        monkeypatch.delenv("HERMES_LAZY_SECRET_RESOLUTION", raising=False)

        calls = []

        def lazy_get(name):
            calls.append(name)
            return "gemini-live-from-1password" if name == "GEMINI_API_KEY" else None

        monkeypatch.setattr("agent.lazy_secret_resolver.get", lazy_get)

        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="gemini")

        assert runtime["api_key"] == "gemini-live-from-1password"
        assert runtime["source"] == "env:GEMINI_API_KEY"
        assert runtime["credential_pool"] is not None
        assert calls == ["GEMINI_API_KEY"]
        assert "GEMINI_API_KEY" not in os.environ

        persisted_text = (isolated_hermes_home / "auth.json").read_text()
        assert "gemini-live-from-1password" not in persisted_text
        persisted = json.loads(persisted_text)["credential_pool"]["gemini"][0]
        assert "access_token" not in persisted
        assert persisted["secret_fingerprint"].startswith("sha256:")
        assert persisted["secret_fingerprint"] != "sha256:0123456789abcdef"

    def test_multiplex_scoped_miss_never_uses_process_global_lazy_secret(
        self, isolated_hermes_home, monkeypatch
    ):
        """A profile without Gemini cannot inherit the default profile's 1P key."""
        self._write_sanitized_gemini_pool(isolated_hermes_home)
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "true")

        import agent.secret_scope as secret_scope

        calls = []
        monkeypatch.setattr(
            "agent.lazy_secret_resolver.get",
            lambda name: calls.append(name) or "must-not-cross-profiles",
        )
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({})
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY, _resolve_api_key_provider_secret

            key, source = _resolve_api_key_provider_secret(
                "gemini", PROVIDER_REGISTRY["gemini"]
            )
        finally:
            secret_scope.reset_secret_scope(token)
            secret_scope.set_multiplex_active(False)

        assert (key, source) == ("", "")
        assert calls == []
        assert "GEMINI_API_KEY" not in os.environ

    def test_runtime_never_lazily_resolves_suppressed_pool_sources(
        self, isolated_hermes_home, monkeypatch
    ):
        """Removing Gemini env sources is authoritative through runtime fallback."""
        self._write_sanitized_gemini_pool(
            isolated_hermes_home,
            suppressed_sources={"gemini": ["env:GEMINI_API_KEY"]},
        )
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        for key in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "true")

        calls = []

        def lazy_get(name):
            if name == "GEMINI_API_KEY":
                calls.append(name)
                return "SHOULD-NOT-RESOLVE"
            return None

        monkeypatch.setattr(
            "agent.lazy_secret_resolver.get",
            lazy_get,
        )

        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="gemini")

        assert runtime["api_key"] == ""
        assert runtime.get("credential_pool") is None
        assert calls == []
        assert "GOOGLE_API_KEY" not in os.environ
        assert "GEMINI_API_KEY" not in os.environ

    def test_anthropic_runtime_never_lazily_resolves_suppressed_api_key(
        self, isolated_hermes_home, monkeypatch
    ):
        self._write_suppressed_reference(
            isolated_hermes_home,
            provider="anthropic",
            env_var="ANTHROPIC_API_KEY",
            base_url="https://api.anthropic.com",
        )
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "true")
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials", lambda: None
        )

        calls = []

        def lazy_get(name):
            if name == "ANTHROPIC_API_KEY":
                calls.append(name)
                return "SHOULD-NOT-RESOLVE"
            return None

        monkeypatch.setattr("agent.lazy_secret_resolver.get", lazy_get)

        from hermes_cli.auth import AuthError
        from hermes_cli.runtime_provider import resolve_runtime_provider

        with pytest.raises(AuthError, match="No Anthropic credentials found"):
            resolve_runtime_provider(requested="anthropic")

        assert calls == []
        assert "ANTHROPIC_API_KEY" not in os.environ

    def test_anthropic_runtime_never_reads_suppressed_claude_code_source(
        self, isolated_hermes_home, monkeypatch
    ):
        """A removed Claude Code source cannot be resurrected by a second read."""
        (isolated_hermes_home / "auth.json").write_text(json.dumps({
            "version": 1,
            "active_provider": "anthropic",
            "providers": {},
            "suppressed_sources": {
                "anthropic": ["claude_code", "hermes_pkce"],
            },
            "credential_pool": {"anthropic": []},
        }))
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        for key in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)

        reader_calls = []

        def forbidden_claude_code_read():
            reader_calls.append("claude_code")
            raise AssertionError("suppressed Claude Code credentials were read")

        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials",
            forbidden_claude_code_read,
        )

        from hermes_cli.auth import AuthError
        from hermes_cli.runtime_provider import resolve_runtime_provider

        with pytest.raises(AuthError, match="No Anthropic credentials found"):
            resolve_runtime_provider(requested="anthropic")

        assert reader_calls == []

    def test_openrouter_runtime_never_lazily_resolves_suppressed_api_key(
        self, isolated_hermes_home, monkeypatch
    ):
        self._write_suppressed_reference(
            isolated_hermes_home,
            provider="openrouter",
            env_var="OPENROUTER_API_KEY",
            base_url="https://openrouter.ai/api/v1",
        )
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "true")

        calls = []

        def lazy_get(name):
            if name == "OPENROUTER_API_KEY":
                calls.append(name)
                return "SHOULD-NOT-RESOLVE"
            return None

        monkeypatch.setattr("agent.lazy_secret_resolver.get", lazy_get)

        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="openrouter")

        assert runtime["api_key"] == ""
        assert calls == []
        assert "OPENROUTER_API_KEY" not in os.environ

    def test_azure_runtime_never_lazily_resolves_suppressed_api_key(
        self, isolated_hermes_home, monkeypatch
    ):
        self._write_suppressed_reference(
            isolated_hermes_home,
            provider="azure-foundry",
            env_var="AZURE_FOUNDRY_API_KEY",
            base_url="https://example.services.ai.azure.com/models",
        )
        monkeypatch.setattr(
            Path, "home", lambda: isolated_hermes_home.parent / "user-home"
        )
        monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
        monkeypatch.setenv("HERMES_LAZY_SECRET_RESOLUTION", "true")

        calls = []

        def lazy_get(name):
            if name == "AZURE_FOUNDRY_API_KEY":
                calls.append(name)
                return "SHOULD-NOT-RESOLVE"
            return None

        monkeypatch.setattr("agent.lazy_secret_resolver.get", lazy_get)

        from hermes_cli.auth import AuthError, _get_azure_foundry_auth_status
        from hermes_cli.runtime_provider import resolve_runtime_provider

        with pytest.raises(AuthError, match="Azure Foundry requires an API key"):
            resolve_runtime_provider(
                requested="azure-foundry",
                explicit_base_url="https://example.services.ai.azure.com/models",
            )

        assert _get_azure_foundry_auth_status()["logged_in"] is False
        assert calls == []
        assert "AZURE_FOUNDRY_API_KEY" not in os.environ


class TestAnthropicEnvAuthTypeClassification:
    """_seed_from_env must classify Anthropic env tokens by the sk-ant-oat prefix.

    Regression for PR #16733: the previous heuristic tagged any token NOT
    starting with `sk-ant-api` as OAuth. That misclassified admin keys
    (`sk-ant-admin-*`), workspace keys, and any future API-key prefix as OAuth.
    OAuth-typed entries with no refresh token are immediately marked exhausted
    in _refresh_entry, so a legitimate admin key gets stuck EXHAUSTED on first
    use and the pool rotates away from a working credential.

    Only real Claude Code OAuth tokens (`sk-ant-oat-…`) should flow into the
    OAuth refresh path.
    """

    def _seed(self, env_var, token):
        from agent.credential_pool import _seed_from_env
        entries = []
        _seed_from_env("anthropic", entries)
        # The seeded entry whose label is the env var we wrote.
        matching = [e for e in entries if getattr(e, "label", None) == env_var]
        assert matching, f"expected a seeded entry for {env_var}, got {entries}"
        return matching[0]

    def test_oauth_token_classified_as_oauth(self, isolated_hermes_home):
        """sk-ant-oat- token from CLAUDE_CODE_OAUTH_TOKEN → AUTH_TYPE_OAUTH."""
        from agent.credential_pool import AUTH_TYPE_OAUTH
        _write_env_file(isolated_hermes_home, CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat-fake-12345")
        entry = self._seed("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-fake-12345")
        assert entry.auth_type == AUTH_TYPE_OAUTH

    def test_admin_key_classified_as_api_key(self, isolated_hermes_home):
        """sk-ant-admin- key from ANTHROPIC_API_KEY → AUTH_TYPE_API_KEY, not OAuth.

        This is the bug the fix targets: previously this was tagged OAuth.
        """
        from agent.credential_pool import AUTH_TYPE_API_KEY
        _write_env_file(isolated_hermes_home, ANTHROPIC_API_KEY="sk-ant-admin-fake-12345")
        entry = self._seed("ANTHROPIC_API_KEY", "sk-ant-admin-fake-12345")
        assert entry.auth_type == AUTH_TYPE_API_KEY

    def test_standard_api_key_classified_as_api_key(self, isolated_hermes_home):
        """sk-ant-api- key → AUTH_TYPE_API_KEY (unchanged behaviour)."""
        from agent.credential_pool import AUTH_TYPE_API_KEY
        _write_env_file(isolated_hermes_home, ANTHROPIC_API_KEY="sk-ant-api-fake-12345")
        entry = self._seed("ANTHROPIC_API_KEY", "sk-ant-api-fake-12345")
        assert entry.auth_type == AUTH_TYPE_API_KEY
