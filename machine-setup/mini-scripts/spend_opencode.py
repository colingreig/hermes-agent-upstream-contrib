#!/usr/bin/env python3
"""OpenCode JSONL spend helpers shared by spend_guard and spend_meter."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse


_CODEX_PROVIDER_ALIASES = {
    "codex",
    "codex-oauth",
    "openai-codex",
    "openai-codex-proxy",
}

_CODEX_PROXY_PORTS = ("8646", "8647")


@dataclass(frozen=True)
class BillingRoute:
    provider: str
    model: str
    base_url: str = ""
    billing_mode: str = "unknown"


def _host_matches(base_url, domain):
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        host = ""
    return host == domain or host.endswith("." + domain)


def _codex_proxy_base_url_is_proven(base_url):
    try:
        parsed = urlparse(base_url or "")
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost"} and str(port) in _CODEX_PROXY_PORTS


def _strip_jsonc_comments(text):
    out = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_jsonc_trailing_commas(text):
    out = []
    in_string = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _configured_base_urls(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"baseURL", "base_url"} and isinstance(item, str):
                yield item.strip()
            for found in _configured_base_urls(item):
                yield found
    elif isinstance(value, list):
        for item in value:
            for found in _configured_base_urls(item):
                yield found


def _local_resolve_billing_route(model, provider=None, base_url=None):
    provider_name = (provider or "").strip().lower()
    base = (base_url or "").strip().lower()
    model_name = (model or "").strip()
    if provider_name in _CODEX_PROVIDER_ALIASES:
        return BillingRoute("openai-codex", model_name, base_url or "", "subscription_included")
    if provider_name == "openrouter" or _host_matches(base_url or "", "openrouter.ai"):
        return BillingRoute("openrouter", model_name, base_url or "", "official_models_api")
    if provider_name in {"anthropic", "content-anthropic"}:
        return BillingRoute("anthropic", model_name.split("/")[-1], base_url or "", "official_docs_snapshot")
    if provider_name in {"openai", "openai-api"}:
        return BillingRoute("openai", model_name.split("/")[-1], base_url or "", "official_docs_snapshot")
    if provider_name in {"minimax", "minimax-cn"}:
        return BillingRoute(provider_name, model_name.split("/")[-1], base_url or "", "official_docs_snapshot")
    if provider_name in {"gemini", "google", "google-flash", "google-decomposer", "vertex"} or _host_matches(base_url or "", "aiplatform.googleapis.com"):
        return BillingRoute("gemini", model_name.split("/")[-1], base_url or "", "official_docs_snapshot")
    if provider_name in {"zai", "zai-coding"}:
        return BillingRoute("zai", model_name.split("/")[-1], base_url or "", "unknown")
    if provider_name in {"custom", "local"} or "localhost" in base or "127.0.0.1" in base:
        return BillingRoute(provider_name or "custom", model_name, base_url or "", "unknown")
    return BillingRoute(provider_name or "unknown", model_name.split("/")[-1] if model_name else "", base_url or "", "unknown")


def resolve_billing_route(model, provider=None, base_url=None):
    """Dependency-safe billing route resolver for system-python mini scripts."""
    provider_name = (provider or "").strip().lower()
    if provider_name in _CODEX_PROVIDER_ALIASES and not _codex_proxy_base_url_is_proven(base_url):
        model_name = (model or "").strip()
        return BillingRoute("openai", model_name.split("/")[-1] if model_name else "", base_url or "", "official_docs_snapshot")
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from agent.usage_pricing import resolve_billing_route as canonical_resolver

        return canonical_resolver(model, provider=provider, base_url=base_url)
    except Exception:
        return _local_resolve_billing_route(model, provider=provider, base_url=base_url)


def _first_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _routing_dict(ev):
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    for value in (
        ev.get("routing"), ev.get("route"), ev.get("metadata"),
        part.get("routing"), part.get("route"), part.get("metadata"),
    ):
        if isinstance(value, dict):
            return value
    return {}


def opencode_event_route(ev, route_metadata=None):
    """Return (model, provider, base_url) for an OpenCode event.

    OpenCode has changed field names across versions, so read the top-level
    event, nested part, and common routing/metadata containers. Unknown routes
    intentionally stay unknown so their recorded ``part.cost`` still counts.
    """
    route_metadata = route_metadata if isinstance(route_metadata, dict) else {}
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    routing = _routing_dict(ev)
    model = _first_text(
        route_metadata.get("model"), route_metadata.get("selected_model"),
        ev.get("model"), part.get("model"), routing.get("model"),
        ev.get("modelID"), part.get("modelID"), routing.get("modelID"),
    )
    provider = _first_text(
        route_metadata.get("provider"), route_metadata.get("billing_provider"),
        ev.get("provider"), part.get("provider"), routing.get("provider"),
        ev.get("providerID"), part.get("providerID"), routing.get("providerID"),
        ev.get("provider_id"), part.get("provider_id"), routing.get("provider_id"),
    )
    base_url = _first_text(
        route_metadata.get("base_url"), route_metadata.get("baseURL"),
        ev.get("base_url"), part.get("base_url"), routing.get("base_url"),
        ev.get("baseURL"), part.get("baseURL"), routing.get("baseURL"),
    )

    provider_key = provider.lower()
    if provider_key in _CODEX_PROVIDER_ALIASES:
        provider = "openai-codex"
    return model, provider, base_url


def route_marginal_cost(raw_cost, model, provider=None, base_url=None):
    """Return ``(billable_cost, route)`` for a recorded raw cost on one route.

    THE single implementation of the subscription-vs-billed conditional. Every
    consumer of an OpenCode cost — the spend cap/alert meters (spend_guard,
    spend_meter) and the writer-served liveness ledger (opencode_exec) — routes
    through here so they can never diverge. ``billable_cost`` is 0.0 ONLY for a
    credential/base-url-PROVEN subscription route (Codex OAuth via the local
    proxy); an unproven or non-codex route keeps its full recorded cost, so a
    genuinely billed provider still accrues real spend.
    """
    route = resolve_billing_route(model, provider=provider or None, base_url=base_url or None)
    if raw_cost is None:
        return None, route
    cost = float(raw_cost)
    if route.billing_mode == "subscription_included":
        return 0.0, route
    return cost, route


def opencode_event_marginal_cost(ev, route_metadata=None):
    """Return marginal USD cost for one OpenCode ``step_finish`` event."""
    if ev.get("type") != "step_finish":
        return 0.0
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    raw_cost = part.get("cost")
    if raw_cost is None:
        return 0.0
    model, provider, base_url = opencode_event_route(ev, route_metadata=route_metadata)
    cost, _route = route_marginal_cost(raw_cost, model, provider=provider or None, base_url=base_url or None)
    return 0.0 if cost is None else cost


def opencode_event_provider_cost(ev, route_metadata=None):
    """Return (provider_label, marginal_cost) for one OpenCode event."""
    cost = opencode_event_marginal_cost(ev, route_metadata=route_metadata)
    if ev.get("type") != "step_finish":
        return "", 0.0
    model, provider, base_url = opencode_event_route(ev, route_metadata=route_metadata)
    route = resolve_billing_route(model, provider=provider or None, base_url=base_url or None)
    label = route.provider if route.provider != "unknown" else (provider or model.split("/", 1)[0] if model else "unknown")
    return label or "unknown", cost


def opencode_route_metadata_event(model, provider, base_url="", task_id=None, cascade_label=""):
    return {
        "type": "hermes_route_metadata",
        "part": {
            "type": "hermes-route-metadata",
            "task_id": task_id or "",
            "model": model or "",
            "provider": provider or "",
            "base_url": base_url or "",
            "cascade": cascade_label or "",
            "billing_route_source": "opencode_exec",
        },
    }


def route_metadata_from_event(ev):
    if not isinstance(ev, dict) or ev.get("type") != "hermes_route_metadata":
        return None
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    return {
        "model": _first_text(part.get("model"), ev.get("model")),
        "provider": _first_text(part.get("provider"), ev.get("provider")),
        "base_url": _first_text(part.get("base_url"), part.get("baseURL"), ev.get("base_url"), ev.get("baseURL")),
    }


def configured_codex_oauth_proxy_metadata():
    """Best-effort historical-log classifier for the current OpenCode proxy."""
    try:
        import json
    except Exception:
        return None
    paths = [
        os.path.expanduser("~/.config/opencode/opencode.jsonc"),
        os.path.expanduser("~/.config/opencode/opencode.json"),
        os.path.expanduser("~/.config/opencode/config.jsonc"),
        os.path.expanduser("~/.config/opencode/config.json"),
    ]
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            continue
        lowered = text.lower()
        if "baseurl" not in lowered and "base_url" not in lowered:
            continue
        try:
            data = json.loads(_strip_jsonc_trailing_commas(_strip_jsonc_comments(text)))
        except Exception:
            continue
        for base_url in _configured_base_urls(data):
            if _codex_proxy_base_url_is_proven(base_url):
                return {"provider": "openai-codex", "model": "", "base_url": base_url}
    return None


def served_row_cost(row):
    """Billable USD for one ``~/.hermes/logs/writer-served.jsonl`` row.

    Rows written after 86e2hap1g already carry a ROUTED ``cost_usd`` and are
    stamped with ``billing_mode`` — those are returned as-is. LEGACY rows carry
    the raw, unrouted OpenCode ``part.cost``, so they are re-routed here at READ
    time (the append-only ledger is never rewritten). Legacy rows have no
    base_url, so a codex-tier row is proven the same way the historical-log
    meter proves one: against the currently configured OpenCode proxy. Any
    failure falls back to the recorded cost — never silently zero real spend.
    """
    if not isinstance(row, dict):
        return 0.0
    raw = row.get("cost_usd")
    if raw is None:
        return 0.0
    try:
        cost = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if row.get("billing_mode"):
        return cost  # already routed at write time
    try:
        provider = (row.get("billing_provider") or row.get("served_provider") or "").strip()
        base_url = (row.get("billing_base_url") or "").strip()
        if not base_url and provider.lower() in _CODEX_PROVIDER_ALIASES:
            metadata = configured_codex_oauth_proxy_metadata()
            if metadata:
                base_url = metadata.get("base_url") or ""
        billable, _route = route_marginal_cost(
            cost,
            row.get("served_model") or "",
            provider=provider or None,
            base_url=base_url or None,
        )
    except Exception:
        return cost
    return cost if billable is None else billable
