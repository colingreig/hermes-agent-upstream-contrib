"""Shared read-only provider route health diagnostics.

This module intentionally stays structural by default: it inspects config,
auth state, and credential-pool metadata without making live round trips.
Consumers can opt into explicit probing via ``probe=True`` if they wire a
probe callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from hermes_cli.auth import get_auth_status, resolve_provider
from hermes_cli.config import load_config
from hermes_cli.fallback_config import get_fallback_chain


@dataclass(frozen=True)
class RouteHealthEntry:
    provider: str
    model: str
    source: str
    credential_source: str
    health: str
    reason: Optional[str] = None
    base_url: Optional[str] = None
    configured: bool = False
    logged_in: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "source": self.source,
            "credential_source": self.credential_source,
            "health": self.health,
            "configured": self.configured,
            "logged_in": self.logged_in,
        }
        if self.base_url:
            data["base_url"] = self.base_url
        if self.reason:
            data["reason"] = self.reason
        return data


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_provider(value: Any) -> str:
    text = _coerce_text(value)
    if not text:
        return ""
    try:
        return resolve_provider(text)
    except Exception:
        return text.lower()


def _entry_health_from_status(status: Dict[str, Any]) -> tuple[str, Optional[str]]:
    if not status:
        return "missing-credential", "no auth status available"

    # Cooldown / exhaustion signals used by credential pools and some OAuth flows.
    for key in (
        "cooldown",
        "cooldown_until",
        "rate_limited_until",
        "rate_limit_until",
        "exhausted_until",
    ):
        value = status.get(key)
        if value:
            return "cooldown", "credential is cooling down"

    if status.get("error"):
        return "unhealthy", "credential status reported an error"

    if status.get("logged_in") or status.get("configured") or status.get("api_key"):
        return "healthy", None

    return "missing-credential", status.get("reason") or "no usable credential found"


def _resolve_status_for_provider(provider: str) -> Dict[str, Any]:
    if not provider:
        return {}
    try:
        status = get_auth_status(provider)
        return status if isinstance(status, dict) else {}
    except Exception:
        return {}


def _credential_source_label(provider: str, status: Dict[str, Any]) -> str:
    for key in ("credential_source", "source"):
        value = _coerce_text(status.get(key))
        if value:
            return value

    if provider == "openrouter":
        if status.get("logged_in"):
            return "env:OPENAI_API_KEY/OPENROUTER_API_KEY or credential_pool"
        return "env:OPENAI_API_KEY/OPENROUTER_API_KEY"

    if status.get("api_key"):
        return "configured API key"

    if status.get("logged_in"):
        return "logged in"

    return "unconfigured"


def _maybe_probe_entry(
    provider: str,
    model: str,
    base_url: Optional[str],
    probe: bool,
    probe_fn: Optional[Callable[[str, str, Optional[str]], Dict[str, Any]]],
) -> Dict[str, Any]:
    if not probe:
        return {}
    if probe_fn is None:
        return {"health": "probe-disabled", "reason": "probe callback not provided"}
    try:
        result = probe_fn(provider, model, base_url)
    except TimeoutError:
        return {"health": "timeout", "reason": "probe timed out"}
    except Exception:
        return {"health": "unhealthy", "reason": "probe failed"}
    return result if isinstance(result, dict) else {}


def _route_entries_from_config(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    model_cfg = config.get("model") if isinstance(config, dict) else {}
    if not isinstance(model_cfg, dict):
        model_cfg = {}

    primary_provider = _normalize_provider(model_cfg.get("provider"))
    primary_model = _coerce_text(model_cfg.get("default") or model_cfg.get("name") or model_cfg.get("model"))
    primary_base_url = _coerce_text(model_cfg.get("base_url")) or None

    entries: List[Dict[str, Any]] = []
    if primary_provider or primary_model:
        entries.append(
            {
                "provider": primary_provider,
                "model": primary_model,
                "base_url": primary_base_url,
                "source": "model",
            }
        )

    try:
        fallback_chain = get_fallback_chain(config) if isinstance(config, dict) else []
    except Exception:
        fallback_chain = []

    for idx, raw in enumerate(fallback_chain, start=1):
        if not isinstance(raw, dict):
            continue
        provider = _normalize_provider(raw.get("provider"))
        model = _coerce_text(raw.get("model"))
        base_url = _coerce_text(raw.get("base_url")) or None
        if not provider and not model:
            continue
        entries.append(
            {
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "source": f"fallback_chain[{idx}]",
            }
        )

    # Preserve the explicit ordering from config, but dedupe identical provider/model
    # pairs while keeping the first occurrence.
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[str, str, Optional[str]]] = set()
    for entry in entries:
        key = (entry.get("provider", ""), entry.get("model", ""), entry.get("base_url"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped

def build_route_health(
    config: Optional[Dict[str, Any]] = None,
    *,
    probe: bool = False,
    probe_fn: Optional[Callable[[str, str, Optional[str]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a structured snapshot of the current provider route chain."""
    cfg = config if isinstance(config, dict) else load_config() or {}
    entries: List[RouteHealthEntry] = []

    for raw_entry in _route_entries_from_config(cfg):
        provider = raw_entry.get("provider", "") or ""
        model = raw_entry.get("model", "") or ""
        base_url = raw_entry.get("base_url")
        source = raw_entry.get("source", "model")

        status = _resolve_status_for_provider(provider)
        credential_source = _credential_source_label(provider, status)
        health, reason = _entry_health_from_status(status)
        configured = bool(status.get("configured") or status.get("logged_in") or status.get("api_key"))
        logged_in = bool(status.get("logged_in"))

        probed = _maybe_probe_entry(provider, model, base_url, probe, probe_fn)
        if probed:
            health = probed.get("health", health)
            reason = probed.get("reason", reason)
            if probed.get("credential_source"):
                credential_source = str(probed["credential_source"])
            configured = bool(probed.get("configured", configured))
            logged_in = bool(probed.get("logged_in", logged_in))

        entries.append(
            RouteHealthEntry(
                provider=provider,
                model=model,
                source=source,
                credential_source=credential_source,
                health=health,
                reason=reason,
                base_url=base_url,
                configured=configured,
                logged_in=logged_in,
            )
        )

    healthy_entries = [entry for entry in entries if entry.health == "healthy"]
    chain_exhausted = bool(entries) and not healthy_entries
    if chain_exhausted:
        summary = "route chain exhausted"
    elif healthy_entries:
        summary = f"{healthy_entries[0].provider or 'auto'} ready"
    else:
        summary = "no configured route"

    return {
        "summary": summary,
        "chain_exhausted": chain_exhausted,
        "healthy_count": len(healthy_entries),
        "unhealthy_count": len(entries) - len(healthy_entries),
        "entries": [entry.to_dict() for entry in entries],
    }


def format_route_health_lines(snapshot: Dict[str, Any]) -> List[str]:
    """Render a compact human-readable route chain summary."""
    entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
    lines: List[str] = []
    if not entries:
        lines.append("  • No provider route configured")
        return lines

    for idx, entry in enumerate(entries, start=1):
        provider = _coerce_text(entry.get("provider")) or "auto"
        model = _coerce_text(entry.get("model")) or "(default)"
        health = _coerce_text(entry.get("health")) or "unknown"
        credential_source = _coerce_text(entry.get("credential_source")) or "unknown"
        source = _coerce_text(entry.get("source")) or "model"
        reason = _coerce_text(entry.get("reason"))
        base_url = _coerce_text(entry.get("base_url"))
        line = (
            f"  {idx}. {provider}/{model} — {health} "
            f"(credential_source={credential_source}, source={source})"
        )
        if base_url:
            line += f" [base_url={base_url}]"
        if reason:
            line += f" — {reason}"
        lines.append(line)
    return lines


def print_route_health(label: str = "Provider Route Health", *, config: Optional[Dict[str, Any]] = None, probe: bool = False, probe_fn: Optional[Callable[[str, str, Optional[str]], Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Convenience printer used by CLI surfaces."""
    snapshot = build_route_health(config, probe=probe, probe_fn=probe_fn)
    print()
    print(label)
    for line in format_route_health_lines(snapshot):
        print(line)
    return snapshot
