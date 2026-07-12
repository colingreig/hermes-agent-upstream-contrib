"""Google Gemini image generation backend ("Nano Banana").

Exposes Gemini's native image-output models as an :class:`ImageGenProvider`
implementation. Unlike the OpenRouter-compatible backend (which talks to
Gemini via an OpenAI-shaped ``/chat/completions`` proxy), this plugin hits
Google's own REST endpoint directly:

    POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent

with ``generationConfig.responseModalities: ["TEXT", "IMAGE"]``. Generated
images come back as inline base64 parts (``candidates[0].content.parts[].
inlineData.data``) — no ephemeral URL to babysit, unlike xAI/OpenAI's URL
fallback path. Image-to-image / editing works the same way in reverse: source
images are fetched and embedded as ``inlineData`` parts in the request, since
Gemini's ``generateContent`` doesn't accept arbitrary web URLs as image input
(only pre-uploaded Files API resources do, which this plugin doesn't use).

The request/response shape here mirrors two already-proven callers in this
codebase: ``agent/gemini_native_adapter.py`` (the main chat adapter, which
established the ``inlineData``/``mimeType`` camelCase field convention and
the ``?key=`` query-param auth style) and the Banana Claude skill's
``generate.py`` fallback script (which validated the exact
``generationConfig.imageConfig.{aspectRatio,imageSize}`` request body against
the live API).

Selection precedence (first hit wins), matching the xAI/OpenAI plugins:

1. ``GEMINI_IMAGE_MODEL`` env var (escape hatch for scripts / tests)
2. ``image_gen.gemini.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml`` (when it's one of our model IDs)
4. :data:`DEFAULT_MODEL` — ``gemini-3-pro-image`` ("Nano Banana Pro")

Auth: ``GOOGLE_API_KEY``, falling back to ``GEMINI_API_KEY`` then
``GOOGLE_AI_API_KEY`` — the same tolerant lookup order used by the Banana
Claude skill and ``tools/tts_tool.py``'s Gemini TTS path.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    success_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

_MODELS: Dict[str, Dict[str, Any]] = {
    "gemini-3-pro-image": {
        "display": "Gemini 3 Pro Image (Nano Banana Pro)",
        "speed": "~20-40s",
        "strengths": "Premium reasoning-driven model; studio-quality 4K, best prompt adherence and text rendering",
    },
    "gemini-3.1-flash-image": {
        "display": "Gemini 3.1 Flash Image (Nano Banana 2)",
        "speed": "~10-20s",
        "strengths": "High-efficiency production-scale tier; faster/cheaper than Pro",
    },
    "gemini-2.5-flash-image": {
        "display": "Gemini 2.5 Flash Image",
        "speed": "~5-10s",
        "strengths": "Budget / legacy GA fallback — cheaper and generally available",
    },
}

DEFAULT_MODEL = "gemini-3-pro-image"

_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Semantic aspect ratio (the image_gen contract) → Gemini's
# generationConfig.imageConfig.aspectRatio strings.
_ASPECT_RATIOS = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
}

# generationConfig.imageConfig.imageSize — must be uppercase; lowercase values
# are silently rejected by the API (confirmed against the live endpoint by
# the Banana Claude skill's generate.py fallback script).
_RESOLUTIONS = {"1K", "2K", "4K"}
DEFAULT_RESOLUTION = "2K"

# Gemini Flash Image accepts up to 3 input images per prompt (1 primary +
# up to 2 references) — mirrors the xAI provider's source-image cap.
_MAX_REFERENCE_IMAGES = 2
_MAX_TOTAL_SOURCE_IMAGES = 3

# Env vars accepted for the API key, in precedence order. GOOGLE_API_KEY is
# primary; GEMINI_API_KEY / GOOGLE_AI_API_KEY are tolerant fallbacks mirroring
# the Banana Claude skill and tools/tts_tool.py's Gemini TTS lookup.
_API_KEY_ENV_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_AI_API_KEY")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_gemini_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml (returns {} on any failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_api_key() -> Tuple[str, str]:
    """Return ``(api_key, source_env_var)`` — first non-empty hit wins.

    Empty string / empty source when no key is set anywhere.
    """
    for env_var in _API_KEY_ENV_VARS:
        value = os.environ.get(env_var, "").strip()
        if value:
            return value, env_var
    return "", ""


def _resolve_model() -> str:
    """Decide which model id to use."""
    env_override = os.environ.get("GEMINI_IMAGE_MODEL", "").strip()
    if env_override and env_override in _MODELS:
        return env_override

    cfg = _load_gemini_config()
    gemini_cfg = cfg.get("gemini") if isinstance(cfg.get("gemini"), dict) else {}
    if isinstance(gemini_cfg, dict):
        value = gemini_cfg.get("model")
        if isinstance(value, str) and value in _MODELS:
            return value

    top = cfg.get("model")
    if isinstance(top, str) and top in _MODELS:
        return top

    return DEFAULT_MODEL


def _resolve_resolution() -> str:
    """Get the configured ``imageConfig.imageSize`` (default 2K)."""
    cfg = _load_gemini_config()
    gemini_cfg = cfg.get("gemini") if isinstance(cfg.get("gemini"), dict) else {}
    res = gemini_cfg.get("resolution") if isinstance(gemini_cfg, dict) else None
    if isinstance(res, str):
        candidate = res.strip().upper()
        if candidate in _RESOLUTIONS:
            return candidate
    return DEFAULT_RESOLUTION


# ---------------------------------------------------------------------------
# Source-image loading (for image-to-image / edit)
# ---------------------------------------------------------------------------


def _guess_mime_type(name: str) -> str:
    """Best-effort image MIME type from a filename/URL, defaulting to PNG."""
    guessed, _ = mimetypes.guess_type(name)
    return guessed if guessed and guessed.startswith("image/") else "image/png"


# Extension inference for the ``inlineData.mimeType`` Gemini hands back on the
# generation response — mirrors ``_URL_IMAGE_CONTENT_TYPES`` in
# agent/image_gen_provider.py's save_url_image, kept local since Gemini's own
# generateContent responses are the only caller here. Gemini's image-output
# models (Nano Banana / Nano Banana Pro) can return JPEG or WEBP bytes, not
# just PNG, so a hardcoded ``.png`` extension silently mismatches the actual
# bytes on disk.
_INLINE_DATA_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def _extension_from_mime_type(mime: Optional[str]) -> str:
    """Map an ``inlineData`` ``mimeType`` to a bare file extension (no dot).

    Falls back to ``png`` for missing/unrecognized types so a save never
    fails outright — but the common Gemini output types (png/jpeg/webp) are
    mapped to their correct extension instead of always assuming PNG.
    """
    if not mime:
        return "png"
    return _INLINE_DATA_EXTENSIONS.get(mime.strip().lower(), "png")


def _load_image_bytes(ref: str) -> Tuple[bytes, str]:
    """Load image bytes + a MIME type from a URL, local path, or data: URI.

    Raises on any network / IO error so the caller can surface a clean
    error_response. Mirrors the loader helpers in the OpenAI/xAI/OpenRouter
    image_gen plugins.
    """
    ref = ref.strip()
    lower = ref.lower()
    if lower.startswith(("http://", "https://")):
        import requests

        resp = requests.get(ref, timeout=60)
        resp.raise_for_status()
        content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        mime = content_type if content_type.startswith("image/") else _guess_mime_type(ref)
        return resp.content, mime
    if lower.startswith("data:"):
        header, _, encoded = ref.partition(",")
        mime = "image/png"
        if "image/" in header:
            mime = header.split(";", 1)[0].split(":", 1)[1] or "image/png"
        return base64.b64decode(encoded), mime
    # Local file path — enforce the shared credential-read guard before reading.
    from agent.file_safety import raise_if_read_blocked

    raise_if_read_blocked(ref)
    path = Path(ref).expanduser()
    with open(path, "rb") as fh:
        data = fh.read()
    return data, _guess_mime_type(str(path))


def _inline_data_part(ref: str) -> Dict[str, Any]:
    """Build a Gemini ``inlineData`` content part from an image reference."""
    data, mime = _load_image_bytes(ref)
    return {
        "inlineData": {
            "mimeType": mime,
            "data": base64.b64encode(data).decode("ascii"),
        }
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class GeminiImageGenProvider(ImageGenProvider):
    """Google Gemini native ``generateContent`` image backend ("Nano Banana")."""

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def display_name(self) -> str:
        return "Google Gemini (Nano Banana)"

    def is_available(self) -> bool:
        api_key, _ = _resolve_api_key()
        return bool(api_key)

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta.get("display", model_id),
                "speed": meta.get("speed", ""),
                "strengths": meta.get("strengths", ""),
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "Google Gemini (Nano Banana)",
            "badge": "paid",
            "tag": (
                "gemini-3-pro-image (Nano Banana Pro) — "
                "text-to-image & image editing"
            ),
            "env_vars": [
                {
                    "key": "GOOGLE_API_KEY",
                    "prompt": "Google AI Studio API key (also accepts GEMINI_API_KEY / GOOGLE_AI_API_KEY)",
                    "url": "https://aistudio.google.com/apikey",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        # Gemini Flash Image accepts inline image input for editing/grounding;
        # capped at 3 total source images (1 primary + 2 references).
        return {
            "modalities": ["text", "image"],
            "max_reference_images": _MAX_REFERENCE_IMAGES,
            "max_source_images": _MAX_TOTAL_SOURCE_IMAGES,
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate an image (text-to-image) or edit source image(s) (image-to-image).

        Routing: when ``image_url`` and/or ``reference_image_urls`` are given,
        the source images are fetched and embedded as ``inlineData`` parts
        alongside the text prompt; otherwise the request is text-only.
        """
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="gemini",
                aspect_ratio=aspect,
            )

        api_key, _key_source = _resolve_api_key()
        if not api_key:
            return error_response(
                error=(
                    "No Gemini API key found. Set GOOGLE_API_KEY (GEMINI_API_KEY / "
                    "GOOGLE_AI_API_KEY also accepted) — get one at "
                    "https://aistudio.google.com/apikey"
                ),
                error_type="missing_api_key",
                provider="gemini",
                aspect_ratio=aspect,
            )

        model_id = _resolve_model()
        resolution = _resolve_resolution()
        gemini_ar = _ASPECT_RATIOS.get(aspect, "1:1")

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        for ref in normalize_reference_images(reference_image_urls) or []:
            sources.append(ref)

        if len(sources) > _MAX_TOTAL_SOURCE_IMAGES:
            return error_response(
                error=f"Gemini image editing supports at most {_MAX_TOTAL_SOURCE_IMAGES} source images",
                error_type="too_many_references",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        is_edit = bool(sources)
        modality = "image" if is_edit else "text"

        parts: List[Dict[str, Any]] = [{"text": prompt}]
        if is_edit:
            try:
                for source in sources:
                    parts.append(_inline_data_part(source))
            except Exception as exc:
                return error_response(
                    error=f"Could not load source image for editing: {exc}",
                    error_type="io_error",
                    provider="gemini",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

        payload: Dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {
                    "aspectRatio": gemini_ar,
                    "imageSize": resolution,
                },
            },
        }

        url = f"{_API_BASE}/{model_id}:generateContent"
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(
                url,
                params={"key": api_key},
                headers=headers,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            response = exc.response
            status = response.status_code if response is not None else 0
            try:
                err_body = response.json() if response is not None else {}
            except Exception:
                err_body = {}
            err_msg = (
                (err_body.get("error") or {}).get("message")
                if isinstance(err_body, dict)
                else None
            ) or (response.text[:300] if response is not None else str(exc))
            logger.error("Gemini image gen failed (%d): %s", status, err_msg)

            low = err_msg.lower()
            if "api_key_invalid" in low or "api key not valid" in low:
                return error_response(
                    error=(
                        f"Gemini API key is invalid: {err_msg}. Check GOOGLE_API_KEY "
                        "at https://aistudio.google.com/apikey"
                    ),
                    error_type="missing_api_key",
                    provider="gemini",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            if status == 429 or "resource_exhausted" in low or "quota" in low:
                return error_response(
                    error=f"Gemini image generation rate-limited / quota exceeded ({status}): {err_msg}",
                    error_type="api_error",
                    provider="gemini",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            return error_response(
                error=f"Gemini image generation failed ({status}): {err_msg}",
                error_type="api_error",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.Timeout:
            return error_response(
                error="Gemini image generation timed out (120s)",
                error_type="timeout",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"Gemini connection error: {exc}",
                error_type="connection_error",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:
            return error_response(
                error=f"Gemini returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        candidates = result.get("candidates") or []
        if not candidates:
            block_reason = ((result.get("promptFeedback") or {}).get("blockReason")) or "UNKNOWN"
            return error_response(
                error=f"Gemini returned no candidates (reason: {block_reason})",
                error_type="empty_response",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first_candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        cand_parts = ((first_candidate.get("content") or {}).get("parts")) or []

        b64_data: Optional[str] = None
        image_mime_type: Optional[str] = None
        text_response = ""
        for part in cand_parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if isinstance(inline, dict) and inline.get("data"):
                b64_data = inline["data"]
                image_mime_type = inline.get("mimeType")
            elif isinstance(part.get("text"), str):
                text_response += part["text"]

        if not b64_data:
            finish_reason = first_candidate.get("finishReason") or "UNKNOWN"
            return error_response(
                error=f"Gemini response contained no image data (finishReason: {finish_reason})",
                error_type="empty_response",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            saved_path = save_b64_image(
                b64_data,
                prefix=f"gemini_{model_id.replace('.', '_').replace('-', '_')}",
                extension=_extension_from_mime_type(image_mime_type),
            )
        except Exception as exc:
            return error_response(
                error=f"Could not save image to cache: {exc}",
                error_type="io_error",
                provider="gemini",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"resolution": resolution}
        if text_response.strip():
            extra["text"] = text_response.strip()
        usage = result.get("usageMetadata")
        if usage:
            extra["usage"] = usage

        return success_response(
            image=str(saved_path),
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="gemini",
            modality=modality,
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Plugin entry point — wire ``GeminiImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(GeminiImageGenProvider())
