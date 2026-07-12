#!/usr/bin/env python3
"""Tests for the Google Gemini image generation provider."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch, tmp_path):
    """Ensure a Gemini API key is set for all tests, and no other Google-ish
    key env vars leak in from the host environment."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_IMAGE_MODEL", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-12345")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    try:
        import hermes_cli.config as cfg_mod

        if hasattr(cfg_mod, "_invalidate_load_config_cache"):
            cfg_mod._invalidate_load_config_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Provider class tests
# ---------------------------------------------------------------------------


class TestGeminiImageGenProvider:
    def test_name(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        assert provider.name == "gemini"

    def test_display_name(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        assert provider.display_name == "Google Gemini (Nano Banana)"

    def test_is_available_with_google_api_key(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "sk-xxx")
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        assert provider.is_available() is True

    def test_is_available_without_any_key(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        assert provider.is_available() is False

    def test_list_models(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        models = provider.list_models()
        ids = [m["id"] for m in models]
        assert "gemini-3-pro-image" in ids
        assert "gemini-3.1-flash-image" in ids
        assert "gemini-2.5-flash-image" in ids

    def test_default_model(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        assert provider.default_model() == "gemini-3-pro-image"

    def test_get_setup_schema(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        schema = provider.get_setup_schema()
        assert schema["name"] == "Google Gemini (Nano Banana)"
        assert schema["badge"] == "paid"
        assert schema["env_vars"][0]["key"] == "GOOGLE_API_KEY"

    def test_capabilities(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        caps = GeminiImageGenProvider().capabilities()
        assert caps["modalities"] == ["text", "image"]
        assert caps["max_reference_images"] == 2
        assert caps["max_source_images"] == 3


# ---------------------------------------------------------------------------
# Env-var fallback order tests
# ---------------------------------------------------------------------------


class TestApiKeyFallback:
    def test_google_api_key_wins_over_others(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "google-ai-key")
        from plugins.image_gen.gemini import _resolve_api_key

        key, source = _resolve_api_key()
        assert key == "google-key"
        assert source == "GOOGLE_API_KEY"

    def test_gemini_api_key_fallback(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "google-ai-key")
        from plugins.image_gen.gemini import _resolve_api_key

        key, source = _resolve_api_key()
        assert key == "gemini-key"
        assert source == "GEMINI_API_KEY"

    def test_google_ai_api_key_last_resort(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GOOGLE_AI_API_KEY", "google-ai-key")
        from plugins.image_gen.gemini import _resolve_api_key

        key, source = _resolve_api_key()
        assert key == "google-ai-key"
        assert source == "GOOGLE_AI_API_KEY"

    def test_no_key_anywhere(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
        from plugins.image_gen.gemini import _resolve_api_key

        key, source = _resolve_api_key()
        assert key == ""
        assert source == ""


# ---------------------------------------------------------------------------
# Model resolution tests
# ---------------------------------------------------------------------------


class TestModelResolution:
    def test_default_model(self):
        from plugins.image_gen.gemini import _resolve_model

        assert _resolve_model() == "gemini-3-pro-image"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
        from plugins.image_gen.gemini import _resolve_model

        assert _resolve_model() == "gemini-2.5-flash-image"

    def test_env_override_ignores_unknown_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_IMAGE_MODEL", "not-a-real-model")
        from plugins.image_gen.gemini import _resolve_model

        assert _resolve_model() == "gemini-3-pro-image"

    def test_scoped_config_model(self, monkeypatch):
        from plugins.image_gen import gemini as gemini_mod

        monkeypatch.setattr(
            gemini_mod,
            "_load_gemini_config",
            lambda: {"gemini": {"model": "gemini-2.5-flash-image"}},
        )
        assert gemini_mod._resolve_model() == "gemini-2.5-flash-image"

    def test_top_level_config_model(self, monkeypatch):
        from plugins.image_gen import gemini as gemini_mod

        monkeypatch.setattr(
            gemini_mod,
            "_load_gemini_config",
            lambda: {"model": "gemini-2.5-flash-image"},
        )
        assert gemini_mod._resolve_model() == "gemini-2.5-flash-image"

    def test_scoped_config_wins_over_top_level(self, monkeypatch):
        from plugins.image_gen import gemini as gemini_mod

        monkeypatch.setattr(
            gemini_mod,
            "_load_gemini_config",
            lambda: {
                "model": "gemini-2.5-flash-image",
                "gemini": {"model": "gemini-3.1-flash-image"},
            },
        )
        assert gemini_mod._resolve_model() == "gemini-3.1-flash-image"

    def test_default_resolution(self):
        from plugins.image_gen.gemini import _resolve_resolution

        assert _resolve_resolution() == "2K"

    def test_custom_resolution(self, monkeypatch):
        from plugins.image_gen import gemini as gemini_mod

        monkeypatch.setattr(
            gemini_mod,
            "_load_gemini_config",
            lambda: {"gemini": {"resolution": "4k"}},
        )
        assert gemini_mod._resolve_resolution() == "4K"


# ---------------------------------------------------------------------------
# Generate tests — request shape + response parsing
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestGenerate:
    def test_missing_prompt(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        result = provider.generate(prompt="   ")
        assert result["success"] is False
        assert result["error_type"] == "invalid_argument"

    def test_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_AI_API_KEY", raising=False)
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        result = provider.generate(prompt="a cat in space")
        assert result["success"] is False
        assert result["error_type"] == "missing_api_key"
        assert "GOOGLE_API_KEY" in result["error"]

    def test_successful_text_to_image(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": "image/png", "data": _b64(b"fake-png-bytes")}},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
        }

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp), \
             patch("plugins.image_gen.gemini.save_b64_image", return_value=Path("/tmp/gemini_test.png")):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="A cat playing piano")

        assert result["success"] is True
        assert result["image"] == "/tmp/gemini_test.png"
        assert result["provider"] == "gemini"
        assert result["model"] == "gemini-3-pro-image"
        assert result["modality"] == "text"

    def test_request_shape_text_to_image(self):
        """Verifies the exact request body shape validated against the live
        API by the Banana Claude skill's generate.py fallback script."""
        from plugins.image_gen.gemini import GeminiImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": _b64(b"x")}}]}}
            ],
        }

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp) as mock_post, \
             patch("plugins.image_gen.gemini.save_b64_image", return_value=Path("/tmp/x.png")):
            provider = GeminiImageGenProvider()
            provider.generate(prompt="a cat in space", aspect_ratio="landscape")

        call = mock_post.call_args
        assert call.args[0] == (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3-pro-image:generateContent"
        )
        assert call.kwargs["params"] == {"key": "test-key-12345"}
        payload = call.kwargs["json"]
        assert payload["contents"][0]["parts"][0]["text"] == "a cat in space"
        assert payload["generationConfig"]["responseModalities"] == ["TEXT", "IMAGE"]
        assert payload["generationConfig"]["imageConfig"]["aspectRatio"] == "16:9"
        assert payload["generationConfig"]["imageConfig"]["imageSize"] == "2K"

    def test_image_to_image_inlines_source_as_base64(self, tmp_path):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        source_bytes = b"\x89PNG\r\n\x1a\nsource-image-bytes"
        img_path = tmp_path / "source.png"
        img_path.write_bytes(source_bytes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": _b64(b"edited")}}]}}
            ],
        }

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp) as mock_post, \
             patch("plugins.image_gen.gemini.save_b64_image", return_value=Path("/tmp/edited.png")):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="make it red", image_url=str(img_path))

        payload = mock_post.call_args.kwargs["json"]
        parts = payload["contents"][0]["parts"]
        assert parts[0]["text"] == "make it red"
        assert parts[1]["inlineData"]["mimeType"] == "image/png"
        assert base64.b64decode(parts[1]["inlineData"]["data"]) == source_bytes
        assert result["modality"] == "image"

    def test_too_many_source_images_rejected(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        provider = GeminiImageGenProvider()
        result = provider.generate(
            prompt="combine these",
            image_url="https://example.com/a.png",
            reference_image_urls=[
                "https://example.com/b.png",
                "https://example.com/c.png",
                "https://example.com/d.png",
            ],
        )
        assert result["success"] is False
        assert result["error_type"] == "too_many_references"

    def test_api_key_invalid_error(self):
        import requests as req_lib
        from plugins.image_gen.gemini import GeminiImageGenProvider

        response = req_lib.Response()
        response.status_code = 400
        response._content = json.dumps(
            {"error": {"message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}
        ).encode()
        response.headers["Content-Type"] = "application/json"
        response.raise_for_status = MagicMock(side_effect=req_lib.HTTPError(response=response))

        with patch("plugins.image_gen.gemini.requests.post", return_value=response):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "missing_api_key"
        assert "invalid" in result["error"].lower()

    def test_rate_limit_error(self):
        import requests as req_lib
        from plugins.image_gen.gemini import GeminiImageGenProvider

        response = req_lib.Response()
        response.status_code = 429
        response._content = json.dumps(
            {"error": {"message": "Resource has been exhausted (e.g. check quota).", "status": "RESOURCE_EXHAUSTED"}}
        ).encode()
        response.headers["Content-Type"] = "application/json"
        response.raise_for_status = MagicMock(side_effect=req_lib.HTTPError(response=response))

        with patch("plugins.image_gen.gemini.requests.post", return_value=response):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "rate-limited" in result["error"].lower() or "quota" in result["error"].lower()

    def test_generic_api_error(self):
        import requests as req_lib
        from plugins.image_gen.gemini import GeminiImageGenProvider

        response = req_lib.Response()
        response.status_code = 500
        response._content = json.dumps({"error": {"message": "Internal error"}}).encode()
        response.headers["Content-Type"] = "application/json"
        response.raise_for_status = MagicMock(side_effect=req_lib.HTTPError(response=response))

        with patch("plugins.image_gen.gemini.requests.post", return_value=response):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "api_error"
        assert "500" in result["error"]

    def test_timeout(self):
        import requests as req_lib
        from plugins.image_gen.gemini import GeminiImageGenProvider

        with patch("plugins.image_gen.gemini.requests.post", side_effect=req_lib.Timeout()):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "timeout"

    def test_connection_error(self):
        import requests as req_lib
        from plugins.image_gen.gemini import GeminiImageGenProvider

        with patch("plugins.image_gen.gemini.requests.post", side_effect=req_lib.ConnectionError("boom")):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "connection_error"

    def test_no_candidates(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [],
            "promptFeedback": {"blockReason": "SAFETY"},
        }

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "empty_response"
        assert "SAFETY" in result["error"]

    def test_no_image_in_response(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "I can't generate that."}]}, "finishReason": "SAFETY"}
            ],
        }

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "empty_response"
        assert "SAFETY" in result["error"]

    def test_invalid_json_response(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = ValueError("no JSON")

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp):
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="test")

        assert result["success"] is False
        assert result["error_type"] == "invalid_response"


# ---------------------------------------------------------------------------
# mimeType -> file extension tests (bug: JPEG/WEBP bytes saved with a
# hardcoded .png extension because save_b64_image's default extension was
# never overridden from the response's inlineData.mimeType)
# ---------------------------------------------------------------------------


class TestMimeTypeExtension:
    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("image/png", "png"),
            ("image/jpeg", "jpg"),
            ("image/jpg", "jpg"),
            ("image/webp", "webp"),
            ("image/gif", "gif"),
            ("IMAGE/PNG", "png"),
            ("image/tiff", "png"),  # unrecognized -> safe fallback
            (None, "png"),
            ("", "png"),
        ],
    )
    def test_extension_from_mime_type(self, mime, expected):
        from plugins.image_gen.gemini import _extension_from_mime_type

        assert _extension_from_mime_type(mime) == expected

    @pytest.mark.parametrize(
        "mime,expected_ext",
        [
            ("image/png", "png"),
            ("image/jpeg", "jpg"),
            ("image/webp", "webp"),
        ],
    )
    def test_generate_saves_with_extension_matching_response_mime_type(self, mime, expected_ext):
        """End-to-end: the real save path must derive its extension from the
        Gemini response's inlineData.mimeType, not assume PNG."""
        from plugins.image_gen.gemini import GeminiImageGenProvider

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"inlineData": {"mimeType": mime, "data": _b64(b"fake-bytes")}},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ],
        }

        with patch("plugins.image_gen.gemini.requests.post", return_value=mock_resp), \
             patch(
                 "plugins.image_gen.gemini.save_b64_image",
                 return_value=Path(f"/tmp/gemini_test.{expected_ext}"),
             ) as mock_save:
            provider = GeminiImageGenProvider()
            result = provider.generate(prompt="A cat playing piano")

        assert result["success"] is True
        assert result["image"].endswith(f".{expected_ext}")
        assert mock_save.call_args.kwargs["extension"] == expected_ext


# ---------------------------------------------------------------------------
# Registration test
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register(self):
        from plugins.image_gen.gemini import GeminiImageGenProvider, register

        mock_ctx = MagicMock()
        register(mock_ctx)
        mock_ctx.register_image_gen_provider.assert_called_once()
        provider = mock_ctx.register_image_gen_provider.call_args[0][0]
        assert isinstance(provider, GeminiImageGenProvider)
        assert provider.name == "gemini"
