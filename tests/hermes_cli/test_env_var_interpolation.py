"""Tests for ``${VAR}``/``$VAR`` interpolation in load_env().

Regression coverage for the fleet-wide Gemini outage: ``load_env()`` used
to be a hand-rolled parser (``key, _, value = line.partition('=')``) with
no variable interpolation, so a real .env line like
``GOOGLE_API_KEY=${GEMINI_API_KEY}`` stored the literal 17-character
string ``${GEMINI_API_KEY}`` as the value. The credential pool then sent
that literal to Google as a real ``x-goog-api-key``, producing a
permanent ``HTTP 400 API_KEY_INVALID`` fleet-wide.

The fix makes ``load_env()`` interpolate ``${VAR}``/``$VAR`` references
against earlier-defined values in the same file plus ``os.environ``, and
skips (rather than poisons) any key whose reference can't be resolved.
"""

import logging
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_env_from(content: str, monkeypatch=None):
    """Write ``content`` to a temp .env file and return load_env()'s result."""
    from hermes_cli.config import invalidate_env_cache, load_env

    invalidate_env_cache()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8"
    ) as f:
        f.write(content)
        env_path = Path(f.name)
    try:
        with patch("hermes_cli.config.get_env_path", return_value=env_path):
            return load_env()
    finally:
        env_path.unlink(missing_ok=True)
        invalidate_env_cache()


class TestLoadEnvInterpolatesEarlierDefinedVar:
    def test_google_api_key_equals_gemini_api_key_shape(self):
        """The exact incident shape: GOOGLE_API_KEY=${GEMINI_API_KEY}."""
        content = (
            "GEMINI_API_KEY=AIzaSy-real-secret-value\n"
            "GOOGLE_API_KEY=${GEMINI_API_KEY}\n"
        )
        result = _load_env_from(content)

        assert result["GEMINI_API_KEY"] == "AIzaSy-real-secret-value"
        assert result["GOOGLE_API_KEY"] == "AIzaSy-real-secret-value"
        assert result["GOOGLE_API_KEY"] != "${GEMINI_API_KEY}"

    def test_bare_dollar_var_form_also_interpolates(self):
        content = "REAL_KEY=abc123\nALIAS_KEY=$REAL_KEY\n"
        result = _load_env_from(content)
        assert result["ALIAS_KEY"] == "abc123"

    def test_falls_back_to_os_environ_when_not_in_file(self, monkeypatch):
        monkeypatch.setenv("SHELL_ONLY_SECRET_XYZ", "from-shell")
        content = "GOOGLE_API_KEY=${SHELL_ONLY_SECRET_XYZ}\n"
        result = _load_env_from(content)
        assert result["GOOGLE_API_KEY"] == "from-shell"

    def test_dollar_dollar_is_a_literal_dollar_sign(self):
        content = "PRICE_TAG=$$5\n"
        result = _load_env_from(content)
        assert result["PRICE_TAG"] == "$5"


class TestLoadEnvUnresolvableRefIsSkipped:
    def test_missing_ref_skips_key_not_literal(self, caplog):
        """An unresolvable ${MISSING} must be skipped, never stored as the literal."""
        content = "GOOGLE_API_KEY=${TOTALLY_MISSING_VAR_ABC}\n"
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            result = _load_env_from(content)

        assert "GOOGLE_API_KEY" not in result
        assert "${TOTALLY_MISSING_VAR_ABC}" not in result.values()
        assert any(
            "GOOGLE_API_KEY" in rec.message and "TOTALLY_MISSING_VAR_ABC" in rec.message
            for rec in caplog.records
        ), f"Expected a warning naming the key and the unresolved ref; got: {[r.message for r in caplog.records]}"

    def test_other_keys_in_the_same_file_still_load(self, caplog):
        """One bad reference must not take down the rest of the file."""
        content = (
            "GOOGLE_API_KEY=${TOTALLY_MISSING_VAR_ABC}\n"
            "TELEGRAM_BOT_TOKEN=1234567:ABC-token\n"
        )
        with caplog.at_level(logging.WARNING, logger="hermes_cli.config"):
            result = _load_env_from(content)

        assert "GOOGLE_API_KEY" not in result
        assert result["TELEGRAM_BOT_TOKEN"] == "1234567:ABC-token"


class TestLoadEnvNoRegressionOnPlainAndQuotedValues:
    """Plain values and legitimately quote-wrapped (1Password-style) values
    must parse exactly as before -- no accidental interpolation, and no
    reintroduced quote characters.
    """

    def test_plain_value_with_no_placeholder_unchanged(self):
        content = "ANTHROPIC_API_KEY=sk-ant-key-plain\n"
        result = _load_env_from(content)
        assert result["ANTHROPIC_API_KEY"] == "sk-ant-key-plain"

    def test_double_quoted_1password_style_value_unwrapped_and_unaffected(self):
        """1Password sometimes emits KEY="val" -- quotes must still be stripped
        and no quote characters may leak into the resolved value.
        """
        content = 'OPENAI_API_KEY="sk-openai-quoted-value"\n'
        result = _load_env_from(content)
        assert result["OPENAI_API_KEY"] == "sk-openai-quoted-value"
        assert '"' not in result["OPENAI_API_KEY"]

    def test_single_quoted_value_unwrapped_and_unaffected(self):
        content = "OPENAI_API_KEY='sk-openai-single-quoted'\n"
        result = _load_env_from(content)
        assert result["OPENAI_API_KEY"] == "sk-openai-single-quoted"

    def test_quoted_value_containing_resolvable_ref_still_interpolates(self):
        # A quoted value containing a $VAR-shaped substring that IS resolvable
        # should still interpolate -- interpolation runs after quote-stripping,
        # not instead of it. This documents the composition order.
        content = 'REAL_KEY=abc123\nCOMPOSED="prefix-${REAL_KEY}-suffix"\n'
        result = _load_env_from(content)
        assert result["COMPOSED"] == "prefix-abc123-suffix"
