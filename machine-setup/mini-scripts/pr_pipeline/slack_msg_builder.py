#!/usr/bin/env python3
"""slack_msg_builder.py — small helper for compact status/alert Slack bodies.

Goal: keep routine Hermes->Slack messages short, emoji-led, and easy to scan.
The helper is intentionally boring: callers provide a headline, a few facts, and
an optional next step; it returns a message trimmed to a word budget.
"""
from __future__ import annotations

from typing import Iterable


def _norm_lines(lines: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for item in lines or []:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _word_count(text: str) -> int:
    return len((text or "").split())


def _trim_words(text: str, limit: int) -> str:
    words = (text or "").split()
    if limit <= 0:
        return ""
    if len(words) <= limit:
        return text.strip()
    clipped = " ".join(words[:limit]).rstrip()
    return clipped + " …"


def build_status_message(
    emoji: str,
    headline: str,
    *,
    facts: Iterable[str] | None = None,
    next_step: str | None = None,
    footer: str | None = None,
    max_words: int = 60,
) -> str:
    """Build a compact Slack body with an emoji status lead.

    The total output is trimmed to roughly `max_words` words. The first line is a
    short, direct status statement. Additional facts become single-line bullets.
    If the body still runs long, we drop footer text first, then facts, and only
    then trim the next step as a last resort so the action remains visible.
    """
    headline_line = f"{emoji} {str(headline or '').strip()}".strip()
    fact_lines = [f"• {line}" for line in _norm_lines(facts)]
    next_line = f"Next: {str(next_step).strip()}" if next_step else None
    footer_line = str(footer).strip() if footer else None

    def _parts() -> list[str]:
        parts: list[str] = [headline_line]
        parts.extend(fact_lines)
        if next_line:
            parts.append(next_line)
        if footer_line:
            parts.append(footer_line)
        return [part for part in parts if part]

    def _parts_word_count(parts: list[str]) -> int:
        return sum(_word_count(part) for part in parts)

    while _parts_word_count(_parts()) > max_words:
        if footer_line is not None:
            footer_line = None
            continue
        if fact_lines:
            fact_lines.pop()
            continue
        if next_line is not None:
            remaining = max_words - _word_count(headline_line) - sum(_word_count(f) for f in fact_lines)
            next_line = _trim_words(next_line, max(1, remaining))
            if _parts_word_count(_parts()) > max_words:
                headline_budget = max_words - _word_count(next_line) - sum(_word_count(f) for f in fact_lines)
                headline_line = _trim_words(headline_line, max(1, headline_budget))
            break
        headline_line = _trim_words(headline_line, max_words)
        break

    return "\n".join(_parts()).strip()


def build_alert_message(
    emoji: str,
    headline: str,
    *,
    facts: Iterable[str] | None = None,
    next_step: str | None = None,
    footer: str | None = None,
    max_words: int = 60,
) -> str:
    """Alias for build_status_message; kept for call sites that read more clearly."""
    return build_status_message(
        emoji,
        headline,
        facts=facts,
        next_step=next_step,
        footer=footer,
        max_words=max_words,
    )


if __name__ == "__main__":
    demo = build_status_message(
        "🛑",
        "Hermes hit a usage limit.",
        facts=["3 new signals since the last check", "provider: minimax"],
        next_step="Check logs and refill the pool.",
        max_words=60,
    )
    print(demo)
