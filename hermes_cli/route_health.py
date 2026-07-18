"""Read-only runtime-route health snapshots for Hermes surfaces.

This module centralizes the structural checks shared by:
  * setup.status / setup.runtime_check
  * hermes config check
  * hermes doctor
  * gateway startup diagnostics
  * cron job creation snapshots

The resolver is intentionally read-only: it never selects, refreshes, or
mutates credentials. Pool inspection uses the credential-pool snapshot path
(`_available_entries(clear_expired=False, refresh=False)`) so it can report
cooldowns and exhaustion without triggering network I/O or persisting state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from agent.credential_pool import (
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    STATUS_OK,
    _exhausted_until,
    load_pool,
)
from hermes_cli.auth import PROVIDER_REGISTRY, get_auth_status, resolve_provider
from hermes_cli.config import load_config
from hermes_cli.fallback_config import get_fallback_chain


@dataclass(frozen=True)
class RouteHealthSummary:
    provider: str
    model: Optional[str]
    health: str
    configured: bool
    credential_source: Optional[str] = None
    details: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "health": self.health,
            "configured": self.configured,
        }
        if self.credential_source is not None:
            payload["credential_source"] = self.credential_source
        if self.details:
            payload.update(self.details)
        return payload


def _config_model_section(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}
    model_cfg = config.get("model") if isinstance(config, dict) else {}
    return model_cfg if isinstance(model_cfg, dict) else {}


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _provider_model_from_config(
    config: Optional[dict[str, Any]] = None,
    requested_provider: Optional[str] = None,
) -> tuple[str, Optional[str], dict[str, Any]]:
    model_cfg = _config_model_section(config)
    model = _text(model_cfg.get("default") or model_cfg.get("model")) or None
    provider = _text(requested_provider) or _text(model_cfg.get("provider"))
    if provider:
        try:
            provider = resolve_provider(provider)
        except Exception:
            provider = provider.lower()
    else:
        try:
            provider = resolve_provider(None)
        except Exception:
            provider = ""
    return provider, model, model_cfg


def _pool_route_health(provider: str) -> RouteHealthSummary:
    try:
        pool = load_pool(provider)
    except Exception as exc:
        return RouteHealthSummary(
            provider=provider,
            model=None,
            health="missing_credential",
            configured=False,
            credential_source=None,
            details={"error": str(exc)},
        )

    try:
        entries = list(pool.entries())
    except Exception:
        entries = []

    try:
        available = list(pool._available_entries(clear_expired=False, refresh=False))
    except Exception:
        available = []

    current = None
    try:
        current = pool.current() or pool.peek()
    except Exception:
        current = None
    if current is None and available:
        current = available[0]
    if current is None and entries:
        current = entries[0]

    def _entry_status(entry: Any) -> str:
        status = _text(getattr(entry, "last_status", None)).lower()
        if status == STATUS_OK:
            return "healthy"
        if status == STATUS_EXHAUSTED:
            exhausted_until = _exhausted_until(entry)
            if exhausted_until is not None:
                import time

                if time.time() < exhausted_until:
                    return "cooldown"
            return "exhausted"
        if status == STATUS_DEAD:
            return "missing_credential"
        if getattr(entry, "access_token", "") or getattr(entry, "agent_key", ""):
            return "healthy"
        return "missing_credential"

    entry_routes: list[dict[str, Any]] = []
    for entry in entries:
        status = _entry_status(entry)
        route: dict[str, Any] = {
            "id": getattr(entry, "id", None),
            "label": getattr(entry, "label", None),
            "source": getattr(entry, "source", None),
            "status": status,
            "last_status": getattr(entry, "last_status", None),
            "last_status_at": getattr(entry, "last_status_at", None),
        }
        exhausted_until = _exhausted_until(entry)
        if exhausted_until is not None:
            route["cooldown_until"] = exhausted_until
        entry_routes.append(route)

    configured = bool(available)
    health = "healthy" if configured else "missing_credential"
    if not configured and entries:
        statuses = {_entry_status(entry) for entry in entries}
        if "cooldown" in statuses:
            health = "cooldown"
        elif "exhausted" in statuses:
            health = "exhausted"

    credential_source = None
    if current is not None:
        credential_source = _text(getattr(current, "source", None)) or None
    details: Dict[str, Any] = {
        "entry_routes": entry_routes,
        "available_entries": len(available),
        "total_entries": len(entries),
    }
    if current is not None:
        details["current_entry"] = {
            "id": getattr(current, "id", None),
            "label": getattr(current, "label", None),
            "source": getattr(current, "source", None),
            "status": _entry_status(current),
        }
        exhausted_until = _exhausted_until(current)
        if exhausted_until is not None:
            details["current_entry"]["cooldown_until"] = exhausted_until

    return RouteHealthSummary(
        provider=provider,
        model=None,
        health=health,
        configured=configured,
        credential_source=credential_source,
        details=details,
    )


def _generic_provider_health(provider: str) -> RouteHealthSummary:
    status = get_auth_status(provider) or {}
    configured = bool(
        status.get("configured")
        or status.get("logged_in")
        or status.get("api_key")
        or status.get("command")
    )
    health = "healthy" if configured else "missing_credential"
    details = {k: v for k, v in status.items() if k not in {"api_key", "access_token", "refresh_token"}}
    credential_source = _text(status.get("key_source") or status.get("source")) or None
    return RouteHealthSummary(
        provider=provider,
        model=None,
        health=health,
        configured=configured,
        credential_source=credential_source,
        details=details,
    )


def _provider_health(provider: str) -> RouteHealthSummary:
    provider = _text(provider).lower()
    if not provider:
        return RouteHealthSummary(
            provider="",
            model=None,
            health="missing_credential",
            configured=False,
        )

    if provider == "openrouter":
        from hermes_cli.config import get_env_value_prefer_dotenv

        api_key = _text(get_env_value_prefer_dotenv("OPENROUTER_API_KEY")) or _text(
            get_env_value_prefer_dotenv("OPENAI_API_KEY")
        )
        configured = bool(api_key)
        return RouteHealthSummary(
            provider=provider,
            model=None,
            health="healthy" if configured else "missing_credential",
            configured=configured,
            credential_source="env" if configured else None,
            details={"key_source": "env" if configured else None},
        )

    if provider == "custom":
        # Structural custom-provider readiness is determined upstream by
        # a base_url + api_key pair. We expose the generic auth status here so
        # consumers can see whether the chosen custom route is actually wired.
        return _generic_provider_health(provider)

    pconfig = PROVIDER_REGISTRY.get(provider)
    if pconfig is None:
        # Unknown / aliased / legacy providers still get the generic auth view
        # so diagnostics never explode when config holds a deprecated id.
        return _generic_provider_health(provider)

    if pconfig.auth_type == "api_key":
        return _generic_provider_health(provider)

    if pconfig.auth_type == "external_process":
        return _generic_provider_health(provider)

    if pconfig.auth_type in {"oauth", "oauth_minimax", "aws_sdk"}:
        return _generic_provider_health(provider)

    return _generic_provider_health(provider)


def _fallback_route_health(
    fallback_entries: Iterable[dict[str, Any]],
) -> list[RouteHealthSummary]:
    results: list[RouteHealthSummary] = []
    for entry in fallback_entries:
        provider = _text(entry.get("provider")).lower()
        if not provider:
            continue
        model = _text(entry.get("model")) or None
        route = _provider_health(provider)
        route = RouteHealthSummary(
            provider=route.provider,
            model=model,
            health=route.health,
            configured=route.configured,
            credential_source=route.credential_source,
            details=route.details,
        )
        results.append(route)
    return results


def _surface_health(
    provider: str,
    model: Optional[str],
    *,
    config: Optional[dict[str, Any]] = None,
) -> RouteHealthSummary:
    provider = _text(provider).lower()
    if provider:
        try:
            pool = load_pool(provider)
            if list(pool.entries()):
                route = _pool_route_health(provider)
            else:
                route = _provider_health(provider)
        except Exception:
            route = _provider_health(provider)
    else:
        route = _provider_health(provider)
    return RouteHealthSummary(
        provider=provider,
        model=model,
        health=route.health,
        configured=route.configured,
        credential_source=route.credential_source,
        details=route.details,
    )


def resolve_route_health(
    requested_provider: Optional[str] = None,
    target_model: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a structural, read-only snapshot of the effective route chain.

    The top-level ``primary`` route is the provider/model pair that the current
    configuration points at. ``fallbacks`` contains the configured fallback
    chain in precedence order. The result is deterministic and read-only —
    no refresh, no selection, no mutation.
    """

    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}
    model_provider, model_default, model_cfg = _provider_model_from_config(
        config, requested_provider=requested_provider
    )
    effective_model = target_model or model_default
    fallback_chain = get_fallback_chain(config if isinstance(config, dict) else {})

    primary = _surface_health(model_provider, effective_model, config=config)
    primary_dict = primary.to_dict()
    primary_dict["role"] = "primary"

    fallback_routes = _fallback_route_health(fallback_chain)
    fallback_dicts: list[dict[str, Any]] = []
    for idx, route in enumerate(fallback_routes, start=1):
        payload = route.to_dict()
        payload["role"] = "fallback"
        payload["order"] = idx
        payload["fallback_kind"] = (
            "same-provider" if route.provider == primary.provider else "cross-provider"
        )
        fallback_dicts.append(payload)

    runnable = primary.configured or any(route["configured"] for route in fallback_dicts)
    if not runnable and primary.health == "cooldown":
        runnable = any(route.get("health") == "healthy" for route in fallback_dicts)

    return {
        "requested_provider": requested_provider,
        "provider": primary.provider,
        "model": primary.model,
        "primary": primary_dict,
        "fallbacks": fallback_dicts,
        "fallback_chain": fallback_chain,
        "runnable": runnable,
        "configured": primary.configured,
        "health": primary.health,
        "credential_source": primary.credential_source,
        "model_section": model_cfg,
    }


def summarize_route_health(route_health: Dict[str, Any]) -> str:
    primary = route_health.get("primary") if isinstance(route_health, dict) else None
    if not isinstance(primary, dict):
        return "unavailable"
    provider = primary.get("provider") or "unknown"
    model = primary.get("model") or route_health.get("model") or "(no model)"
    health = primary.get("health") or route_health.get("health") or "unknown"
    credential_source = primary.get("credential_source") or route_health.get("credential_source")
    parts = [f"{provider} / {model}", health]
    if credential_source:
        parts.append(f"source={credential_source}")
    fallback_count = len(route_health.get("fallbacks") or []) if isinstance(route_health, dict) else 0
    if fallback_count:
        parts.append(f"{fallback_count} fallback(s)")
    return "; ".join(parts)
