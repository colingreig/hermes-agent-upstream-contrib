"""Tests for placeholder API key detection in hermes_cli.auth."""

from hermes_cli.auth import has_usable_secret


def test_has_usable_secret_rejects_documented_placeholder_key() -> None:
    """Network-exposed API server key must reject static documentation placeholders."""
    assert not has_usable_secret("your_api_key_here", min_length=8)


def test_has_usable_secret_accepts_generated_key() -> None:
    """Random-looking keys should still be accepted."""
    assert has_usable_secret("b4d59f7fe8b857d0b367ef0f5710b6a4", min_length=8)


def test_has_usable_secret_rejects_unexpanded_template_literals() -> None:
    """Unexpanded ${VAR}/$VAR/%(name)s templates are placeholders, not secrets.

    A config value that was never interpolated (e.g. a literal
    "${GEMINI_API_KEY}" left in .env because load_env doesn't interpolate)
    must not be treated as a usable credential, while a realistic-looking
    key is still accepted.
    """
    assert has_usable_secret("${GEMINI_API_KEY}") is False
    assert has_usable_secret("$HOME") is False
    assert has_usable_secret("%(api_key)s") is False
    assert has_usable_secret("sk-ant-api03-reallooking-abc123XYZ") is True
