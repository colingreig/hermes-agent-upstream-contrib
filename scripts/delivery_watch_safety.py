"""Shared persistence-safe helpers for the delivery watcher."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_SENSITIVE_KEY = re.compile(
    r"(?i)(?:auth(?:orization)?|bearer|basic|cookie|session|token|secret|"
    r"password|passwd|api[_-]?key|access[_-]?key|client[_-]?secret)"
)
_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
)
_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=._~-]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(auth(?:orization)?|token|secret|password|passwd|api[_-]?key|"
    r"access[_-]?key|client[_-]?secret)(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_URL = re.compile(r"https?://[^\s<>\"']+")


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,);]}":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        if not parts.scheme or not host:
            return "<redacted-url>" + trailing
        # Persist only the origin. Paths can themselves contain webhook tokens
        # or authorization codes, and every query value and fragment is secret
        # regardless of its key name.
        return f"{parts.scheme}://{host}/" + trailing
    except ValueError:
        return "<redacted-url>" + trailing


def redact_sensitive(value: object, *, limit: int = 500) -> str:
    """Redact common credential forms before a value can be persisted."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _HEADER.sub(lambda match: f"{match.group(1)}: <redacted>", text)
    text = _SCHEME.sub(lambda match: f"{match.group(1)} <redacted>", text)
    text = _ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text
    )
    text = _URL.sub(_redact_url, text)
    return text[:limit]


__all__ = ["redact_sensitive"]
