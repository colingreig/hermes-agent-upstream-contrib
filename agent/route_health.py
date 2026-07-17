"""Read-only provider routing health diagnostics."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class RouteEntry:
    provider: str
    model: str
    credential_source: str
    healthy: bool
    health: str
    reason: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class RouteChain:
    name: str
    entries: list[RouteEntry] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return any(entry.healthy for entry in self.entries)


@dataclass(frozen=True)
class RouteHealthReport:
    role: str
    chains: list[RouteChain] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return any(chain.healthy for chain in self.chains)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "healthy": self.healthy,
            "chains": [
                {
                    "name": chain.name,
                    "healthy": chain.healthy,
                    "entries": [entry.__dict__ for entry in chain.entries],
                }
                for chain in self.chains
            ],
        }


def _load_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(config, dict):
        return config
    try:
        from hermes_cli.config import load_config

        loaded = load_config()
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _model_config(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("model")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        return {"default": raw.strip()}
    return {}


def _default_provider_model(config: dict[str, Any]) -> tuple[str, str, str]:
    model_cfg = _model_config(config)
    provider = str(model_cfg.get("provider") or os.getenv("HERMES_INFERENCE_PROVIDER") or "").strip()
    model = str(
        model_cfg.get("default")
        or model_cfg.get("model")
        or os.getenv("HERMES_MODEL")
        or ""
    ).strip()
    base_url = str(model_cfg.get("base_url") or "").strip().rstrip("/")
    if provider.lower() in {"", "auto"}:
        provider = _infer_provider_from_credentials() or provider
    return provider, model, base_url


def _infer_provider_from_credentials() -> str:
    if _has_secret(_env_value("OPENROUTER_API_KEY")) or _has_secret(_env_value("OPENAI_API_KEY")):
        return "openrouter"
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY

        for provider, pconfig in PROVIDER_REGISTRY.items():
            for env_var in tuple(getattr(pconfig, "api_key_env_vars", ()) or ()):
                if _has_secret(_env_value(env_var)):
                    return provider
    except Exception:
        pass
    for provider in ("nous", "openai-codex", "anthropic", "gemini", "zai", "openrouter"):
        has_pool, pool_health, _, _ = _pool_entry_health(provider)
        if has_pool and pool_health == "healthy":
            return provider
    return ""


def _has_secret(value: Any) -> bool:
    try:
        from hermes_cli.auth import has_usable_secret

        return bool(has_usable_secret(value))
    except Exception:
        return bool(str(value or "").strip())


def _env_value(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value

        return str(get_env_value(name) or "").strip()
    except Exception:
        return str(os.getenv(name, "") or "").strip()


def _pool_entry_health(provider: str) -> tuple[bool, str, str, str]:
    """Return (has_pool, health, reason, source) for persisted pool state.

    This intentionally reads the persisted pool rows directly instead of calling
    load_pool(), because diagnostics must not seed, refresh, prune, or rewrite
    credentials.
    """
    try:
        from agent.credential_pool import (
            AUTH_TYPE_API_KEY,
            STATUS_DEAD,
            STATUS_EXHAUSTED,
            PooledCredential,
            _exhausted_until,
        )
        from hermes_cli.auth import read_credential_pool

        raw_entries = read_credential_pool(provider)
        entries = [
            PooledCredential.from_dict(provider, entry)
            for entry in raw_entries
            if isinstance(entry, dict)
        ]
    except Exception as exc:
        return False, "unhealthy", f"credential pool unreadable: {type(exc).__name__}", "credential_pool"

    if not entries:
        return False, "unknown", "", ""

    now = time.time()
    sources = sorted({str(entry.source or entry.label or "pool").strip() for entry in entries})
    source = "credential_pool:" + ",".join(src for src in sources if src)
    unusable_reasons: list[str] = []
    for entry in entries:
        label = str(entry.label or entry.id or "entry").strip()
        if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
            unusable_reasons.append(f"{label}: missing usable credential")
            continue
        if entry.last_status == STATUS_DEAD:
            unusable_reasons.append(f"{label}: dead credential")
            continue
        if entry.last_status == STATUS_EXHAUSTED:
            until = _exhausted_until(entry)
            if until is not None and now < until:
                remaining = max(0, int(until - now))
                reason = str(entry.last_error_reason or entry.last_error_code or "cooldown").strip()
                unusable_reasons.append(f"{label}: cooldown {remaining}s ({reason})")
                continue
        return True, "healthy", "", source
    return True, "unhealthy", "; ".join(unusable_reasons) or "no usable credential", source


def _credential_status(provider: str, entry: dict[str, Any] | None = None) -> tuple[str, bool, str, str]:
    provider = (provider or "").strip().lower()
    entry = entry or {}
    inline_key = str(entry.get("api_key") or "").strip()
    if inline_key:
        return "inline api_key", True, "healthy", ""

    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        if _has_secret(_env_value(key_env)):
            return f"env:{key_env}", True, "healthy", ""
        return f"env:{key_env}", False, "unhealthy", "missing env credential"

    has_pool, pool_health, pool_reason, pool_source = _pool_entry_health(provider)
    if has_pool:
        return pool_source or "credential_pool", pool_health == "healthy", pool_health, pool_reason

    if provider == "openrouter":
        for env_var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            if _has_secret(_env_value(env_var)):
                return f"env:{env_var}", True, "healthy", ""
        return "env:OPENROUTER_API_KEY/OPENAI_API_KEY", False, "unhealthy", "missing env credential"

    try:
        from hermes_cli.auth import PROVIDER_REGISTRY, get_provider_auth_state

        pconfig = PROVIDER_REGISTRY.get(provider)
    except Exception:
        pconfig = None
        get_provider_auth_state = None  # type: ignore[assignment]

    if pconfig is not None:
        env_vars = tuple(getattr(pconfig, "api_key_env_vars", ()) or ())
        for env_var in env_vars:
            if _has_secret(_env_value(env_var)):
                return f"env:{env_var}", True, "healthy", ""
        auth_type = str(getattr(pconfig, "auth_type", "") or "")
        if auth_type.startswith("oauth") or auth_type in {"external_process"}:
            try:
                state = get_provider_auth_state(provider) if get_provider_auth_state else {}
            except Exception:
                state = {}
            if isinstance(state, dict) and any(_has_secret(state.get(key)) for key in ("access_token", "refresh_token", "api_key")):
                return "auth.json", True, "healthy", ""
            return "auth.json", False, "unhealthy", "provider not logged in"
        if env_vars:
            return "env:" + "/".join(env_vars), False, "unhealthy", "missing env credential"

    if provider in {"ollama", "lmstudio"}:
        return "local endpoint", True, "healthy", ""
    if provider in {"moa"}:
        return "virtual provider", True, "healthy", ""
    return "unknown", False, "unhealthy", "provider not configured"


def _route_entry(provider: str, model: str, base_url: str = "", raw_entry: dict[str, Any] | None = None) -> RouteEntry:
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if not provider:
        return RouteEntry(provider="", model=model, credential_source="unknown", healthy=False, health="unhealthy", reason="provider not configured", base_url=base_url)
    if not model:
        return RouteEntry(provider=provider, model="", credential_source="unknown", healthy=False, health="unhealthy", reason="model not configured", base_url=base_url)
    source, healthy, health, reason = _credential_status(provider, raw_entry)
    return RouteEntry(provider=provider, model=model, credential_source=source, healthy=healthy, health=health, reason=reason, base_url=base_url)


def _fallback_entries(config: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from hermes_cli.fallback_config import get_fallback_chain

        return get_fallback_chain(config)
    except Exception:
        return []


def _chain_for(name: str, primary: tuple[str, str, str], config: dict[str, Any]) -> RouteChain:
    provider, model, base_url = primary
    entries = [_route_entry(provider, model, base_url)]
    for fallback in _fallback_entries(config):
        fb_provider = str(fallback.get("provider") or "").strip()
        fb_model = str(fallback.get("model") or "").strip()
        fb_base_url = str(fallback.get("base_url") or "").strip().rstrip("/")
        entries.append(_route_entry(fb_provider, fb_model, fb_base_url, fallback))
    return RouteChain(name=name, entries=entries)


def _job_primary(job: dict[str, Any] | None, config: dict[str, Any]) -> tuple[str, str, str]:
    default_provider, default_model, default_base = _default_provider_model(config)
    if not isinstance(job, dict):
        return default_provider, default_model, default_base
    return (
        str(job.get("provider") or default_provider or "").strip(),
        str(job.get("model") or default_model or "").strip(),
        str(job.get("base_url") or default_base or "").strip().rstrip("/"),
    )


def _chain_identity(chain: RouteChain) -> tuple[tuple[str, str, str], ...]:
    return tuple((entry.provider, entry.model, entry.base_url) for entry in chain.entries)


def resolve_effective_routes(
    role: str = "interactive",
    job: dict[str, Any] | None = None,
    *,
    config: dict[str, Any] | None = None,
    jobs: Iterable[dict[str, Any]] | None = None,
) -> RouteHealthReport:
    """Resolve effective route chains and local credential health.

    The resolver is deliberately structural: no provider round trips, no runtime
    credential refresh, no credential mutation, and no secret values in output.
    """
    cfg = _load_config(config)
    normalized_role = (role or "interactive").strip().lower()
    if normalized_role == "cron":
        chains: list[RouteChain] = [_chain_for("cron default", _job_primary(None, cfg), cfg)]
        seen = {_chain_identity(chains[0])}
        if job is not None:
            candidate_jobs = [job]
        elif jobs is not None:
            candidate_jobs = list(jobs)
        else:
            try:
                from cron.jobs import list_jobs

                candidate_jobs = list_jobs(include_disabled=True)
            except Exception:
                candidate_jobs = []
        for candidate in candidate_jobs:
            if not isinstance(candidate, dict) or bool(candidate.get("no_agent")):
                continue
            chain = _chain_for(
                f"cron job {candidate.get('name') or candidate.get('id') or 'override'}",
                _job_primary(candidate, cfg),
                cfg,
            )
            identity = _chain_identity(chain)
            if identity in seen:
                continue
            seen.add(identity)
            chains.append(chain)
        return RouteHealthReport(role="cron", chains=chains)

    return RouteHealthReport(
        role="interactive",
        chains=[_chain_for("interactive", _default_provider_model(cfg), cfg)],
    )


def format_route_health(report: RouteHealthReport, *, indent: str = "  ") -> str:
    lines: list[str] = []
    for chain in report.chains:
        chain_mark = "ok" if chain.healthy else "unhealthy"
        lines.append(f"{indent}{chain.name}: {chain_mark}")
        for idx, entry in enumerate(chain.entries, start=1):
            status = "ok" if entry.healthy else "unhealthy"
            target = f"{entry.provider or '(none)'}/{entry.model or '(none)'}"
            detail = f"cred={entry.credential_source}; health={status}"
            if entry.reason:
                detail += f"; reason={entry.reason}"
            lines.append(f"{indent}  {idx}. {target} ({detail})")
    return "\n".join(lines)
