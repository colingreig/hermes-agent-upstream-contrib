#!/usr/bin/env python3
"""provider_probe.py — generalized out-of-band control-host probe for stale
provider quota/auth marks (ClickUp 86e2mb8p0, PR 2/4 of the adversarial
provider-failure taxonomy epic — 86e2mb8k3).

``codex_quota_probe.py`` (PR 2's starting point, ClickUp 86e2kxk50) proved the
pattern for exactly one provider: periodically re-probe every
``exhausted``-with-an-unexpired-retry-window credential-pool entry with a
single cheap, zero-generation-token call, and clear the mark the moment the
probe proves the account usable again — independent of every other entry, so
clearing one account can never mask a genuinely-exhausted sibling.

This module generalizes that engine to any provider via an **adapter table**:

  openai-codex  GET  .../usage           session + weekly window headroom
  anthropic     POST /v1/messages/count_tokens   metadata-only, no completion
  openrouter    GET  /api/v1/key          key-scoped usage/limit headroom
  (default)     GET  {base_url}/models    generic OpenAI-compatible fallback

Building on 86e2mb8nv (PR 1)'s failure taxonomy: this probe never invents a
new failure *kind* — it corroborates or denies the existing
``suspected_*``-tagged classifications on a pool entry via
``agent.credential_pool.record_probe_verdict()``, and clears exhaustion via
the same ``CredentialPool.clear_stale_exhaustion()`` PR 1 already ships.

New in PR 2 (on top of the codex-only PR 2 starting point):

  * **Adapter table** — provider-specific probes instead of one hardcoded
    codex call. Unregistered providers fall back to the generic ``/models``
    check so a probe run never crashes on an unknown provider name.
  * **Cost guards** — a probe is metered work against a live account, not a
    free local check. No single entry is probed more than once every
    ``MIN_PROBE_INTERVAL_SECONDS`` (10 min), no provider is probed more than
    ``MAX_PROBES_PER_PROVIDER_HOUR`` times per rolling hour, and a pool entry
    already marked DEAD is never probed at all (structurally guaranteed by
    ``frozen_exhausted_entries()`` only ever returning STATUS_EXHAUSTED
    entries, reinforced here with an explicit filter).
  * **Control-host network arbitration** — a *transport-level* probe failure
    (DNS, connect refused, timeout — never an HTTP status code) is
    ambiguous: is the PROVIDER unreachable, or is THIS HOST unreachable? A
    single flaky Wi-Fi blip must never get recorded as "provider is down."
    Before drawing any conclusion, the probe checks a stable, unrelated
    control host; only when THAT succeeds does a transport failure count as
    a provider-side signal.
  * ``record_probe_verdict()`` integration on every probed entry (skipped
    entirely under ``--dry-run``, matching PR 1's "dry-run persists
    nothing" contract).
  * Zero configured entries for a provider is a *config* state, not
    exhaustion — no HTTP call is made at all (mirrors
    ``CredentialPool._log_no_available_entries``'s "provider has no
    configured entries" distinction from PR 1).

``codex_quota_probe.py`` becomes a thin backward-compatible wrapper around
this module: its own state file, Slack alert semantics, CLI flags, and
codex-specific classification (``probe_entry``) are all preserved byte-for-
byte so the already-deployed launchd job and its existing test suite need no
migration — only its ``run_probe()`` now delegates the entry loop, cost
guard, and network arbitration to this module.

Usage:
  provider_probe.py --provider openai-codex          # human summary, exit 0
  provider_probe.py --provider anthropic --json       # JSON result
  provider_probe.py --provider openrouter --alert     # + Slack on new clear
  provider_probe.py --provider <unregistered>         # generic /models probe
  provider_probe.py --provider openai-codex --dry-run # probe only, never persists
  provider_probe.py --provider openai-codex --now EPOCH  # testing only

Exit codes: 0 = ran to completion (regardless of whether anything cleared),
1 = the credential pool could not be read/probed at all.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from agent.failure_taxonomy import EVIDENCE_SUSPECTED_NETWORK

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
DEFAULT_PROVIDER = "openai-codex"
DEFAULT_TIMEOUT_SECONDS = 10.0

STATE_PATH = os.path.expanduser("~/.hermes/state/provider-probe.json")
COST_GUARD_STATE_PATH = os.path.expanduser(
    "~/.hermes/state/provider-probe-cost-guard.json"
)

# Same non-urgent recovery DM target codex_quota_probe.py and the
# degraded-secrets monitor use — an early clear is good news, not a fault, so
# this never posts a ClickUp escalation, only a Slack note.
SLACK_TARGET = os.environ.get(
    "PROVIDER_PROBE_ALERT_SLACK",
    os.environ.get("CODEX_QUOTA_PROBE_ALERT_SLACK", "slack:D0BA2PM9CFM"),
)
SLACK_MENTION = "<@UN4CQ1EGG>"
MAX_HISTORY_EVENTS = 50

# ── Cost guards ──────────────────────────────────────────────────────────
# A probe is metered work against a live upstream account, not a free local
# check — unmetered probing could itself trip a provider's own rate limiter.
MIN_PROBE_INTERVAL_SECONDS = 10 * 60          # 10 min/entry
MAX_PROBES_PER_PROVIDER_HOUR = 12             # 12/provider/hr

# ── Control-host network arbitration ────────────────────────────────────
# A stable, unrelated host used to distinguish "my network is broken" from
# "the provider specifically is unreachable." Deliberately NOT a provider
# endpoint — a provider-specific outage must not make this host fail too.
CONTROL_HOST_URL = "https://www.gstatic.com/generate_204"
CONTROL_HOST_TIMEOUT_SECONDS = 5.0

# ── Probe verdict strings ────────────────────────────────────────────────
# Free-form per ``record_probe_verdict()``'s own contract (PR 1) — distinct
# from (and does not extend) the six frozen failure-taxonomy *kinds* in
# ``agent.failure_taxonomy``. These describe what THIS PROBE concluded, not
# a new failure classification.
VERDICT_USABLE = "usable"
VERDICT_STILL_UNUSABLE = "still_unusable"
VERDICT_CONFIRMED_PROVIDER_SIDE = "confirmed_provider_side"
VERDICT_INCONCLUSIVE_LOCAL_NETWORK = "inconclusive_local_network"

# Codex usage windows report a percentage, not a hard reject — treat
# anything at/above this as "no headroom left," tolerating provider-side
# floating point noise just under 100.
CODEX_USAGE_EXHAUSTED_PERCENT = 99.5


class ProbeResult(NamedTuple):
    usable: bool
    status_code: Optional[int]
    detail: str
    usage_percent: Optional[float] = None


def _now() -> float:
    return time.time()


def _resolve_now(raw: Optional[str]) -> float:
    if not raw:
        return _now()
    try:
        return float(raw)
    except ValueError:
        pass
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return _now()


def _extract_error_type(body: Any) -> str:
    try:
        payload = json.loads(body) if isinstance(body, (bytes, str)) else body
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("type") or error.get("code") or "")


def _access_token(entry: Any) -> str:
    token = getattr(entry, "access_token", None) or ""
    return token if isinstance(token, str) else ""


# ── Provider adapters ────────────────────────────────────────────────────
# Each adapter is ``(entry, *, http_get=None, timeout=...) -> ProbeResult``
# and NEVER raises — a transport failure classifies as
# ``probe_error:<ExceptionClassName>`` (the marker ``run_probe`` looks for to
# trigger control-host arbitration) rather than propagating. None retains or
# logs the access token itself.

def _codex_http_get(access_token: str, base_url: Optional[str], timeout: float):
    """Real network call — GET the Codex usage-window endpoint.

    Mirrors ``agent.account_usage._codex_backend_urls``'s PathStyle split
    (ChatGPT-web ``/backend-api`` bases use ``/wham/usage``, everything else
    uses ``/api/codex/usage``) so probing hits the exact endpoint the CLI's
    own ``/usage`` command already treats as safe, read-only, metadata-only —
    it reports rate-limit window percentages, it never itself performs a
    completion."""
    import httpx
    from hermes_cli.auth import DEFAULT_CODEX_BASE_URL

    root = (base_url or DEFAULT_CODEX_BASE_URL).rstrip("/")
    normalized = root[: -len("/codex")] if root.endswith("/codex") else root
    prefix = normalized + ("/wham" if "/backend-api" in normalized else "/api/codex")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    resp = httpx.get(f"{prefix}/usage", headers=headers, timeout=timeout)
    return resp.status_code, resp.content


def probe_codex_usage_entry(
    entry: Any,
    *,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Codex adapter — session (5h) + weekly window headroom via ``/usage``."""
    http_get = http_get or _codex_http_get
    access_token = _access_token(entry)
    if not access_token.strip():
        return ProbeResult(False, None, "missing_credential")
    try:
        status_code, body = http_get(access_token, getattr(entry, "base_url", None), timeout)
    except Exception as exc:
        return ProbeResult(False, None, f"probe_error:{exc.__class__.__name__}")

    if status_code == 401:
        return ProbeResult(False, status_code, "unauthorized")
    if status_code == 429:
        error_type = _extract_error_type(body)
        if error_type == "usage_limit_reached":
            return ProbeResult(False, status_code, "usage_limit_reached")
        return ProbeResult(False, status_code, f"429:{error_type or 'unknown'}")
    if status_code != 200:
        return ProbeResult(False, status_code, f"http_{status_code}")

    try:
        payload = json.loads(body) if isinstance(body, (bytes, str)) else (body or {})
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rate_limit = payload.get("rate_limit") or {}

    used_values: List[float] = []
    exhausted_windows: List[str] = []
    for key, label in (("primary_window", "session"), ("secondary_window", "weekly")):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if isinstance(used, (int, float)):
            used_values.append(float(used))
            if float(used) >= CODEX_USAGE_EXHAUSTED_PERCENT:
                exhausted_windows.append(label)

    usage_percent = max(used_values) if used_values else None
    if exhausted_windows:
        return ProbeResult(
            False, status_code, f"usage_cap:{'+'.join(exhausted_windows)}", usage_percent
        )
    return ProbeResult(True, status_code, "ok", usage_percent)


def _anthropic_http_get(access_token: str, base_url: Optional[str], timeout: float):
    """Real network call — POST count_tokens (no completion is generated;
    the same metadata-only contract every adapter here follows)."""
    import httpx

    root = (base_url or "https://api.anthropic.com").rstrip("/")
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    # sk-ant-oat* tokens are Anthropic-issued OAuth setup-tokens: Bearer auth
    # + the oauth beta header, never x-api-key (see agent/anthropic_adapter.py).
    if access_token.startswith("sk-ant-oat"):
        headers["Authorization"] = f"Bearer {access_token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        headers["x-api-key"] = access_token
    body = {
        "model": "claude-3-5-haiku-20241022",
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp = httpx.post(f"{root}/v1/messages/count_tokens", headers=headers, json=body, timeout=timeout)
    return resp.status_code, resp.content


def probe_anthropic_entry(
    entry: Any,
    *,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Anthropic adapter — ``count_tokens`` is metadata-only (counts tokens,
    never generates a completion) and accepts both API-key and OAuth tokens."""
    http_get = http_get or _anthropic_http_get
    access_token = _access_token(entry)
    if not access_token.strip():
        return ProbeResult(False, None, "missing_credential")
    try:
        status_code, body = http_get(access_token, getattr(entry, "base_url", None), timeout)
    except Exception as exc:
        return ProbeResult(False, None, f"probe_error:{exc.__class__.__name__}")

    if status_code == 200:
        return ProbeResult(True, status_code, "ok")
    if status_code == 401:
        return ProbeResult(False, status_code, "unauthorized")
    if status_code == 403:
        return ProbeResult(False, status_code, "forbidden")
    if status_code == 429:
        error_type = _extract_error_type(body)
        return ProbeResult(False, status_code, f"429:{error_type or 'unknown'}")
    return ProbeResult(False, status_code, f"http_{status_code}")


def _openrouter_http_get(access_token: str, base_url: Optional[str], timeout: float):
    """Real network call — GET the key-scoped usage/limit endpoint."""
    import httpx

    root = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    url = f"{root}/key" if root.endswith("/v1") else f"{root}/v1/key"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(url, headers=headers, timeout=timeout)
    return resp.status_code, resp.content


def probe_openrouter_entry(
    entry: Any,
    *,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeResult:
    """OpenRouter adapter — the ``/key`` endpoint reports this key's own
    usage/limit, a cheap read with no completion tokens spent."""
    http_get = http_get or _openrouter_http_get
    access_token = _access_token(entry)
    if not access_token.strip():
        return ProbeResult(False, None, "missing_credential")
    try:
        status_code, body = http_get(access_token, getattr(entry, "base_url", None), timeout)
    except Exception as exc:
        return ProbeResult(False, None, f"probe_error:{exc.__class__.__name__}")

    if status_code == 401:
        return ProbeResult(False, status_code, "unauthorized")
    if status_code == 429:
        return ProbeResult(False, status_code, "rate_limited")
    if status_code != 200:
        return ProbeResult(False, status_code, f"http_{status_code}")

    usage_percent = None
    try:
        payload = json.loads(body) if isinstance(body, (bytes, str)) else (body or {})
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            usage = data.get("usage")
            limit = data.get("limit")
            if isinstance(usage, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
                usage_percent = min(100.0, max(0.0, (float(usage) / float(limit)) * 100.0))
    except Exception:
        pass
    return ProbeResult(True, status_code, "ok", usage_percent)


def _generic_http_get(access_token: str, base_url: Optional[str], timeout: float):
    """Real network call — generic OpenAI-compatible ``/models`` fallback for
    any provider with no dedicated adapter."""
    import httpx

    root = (base_url or "").rstrip("/")
    if not root:
        raise ValueError("generic provider probe requires a configured base_url")
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = httpx.get(f"{root}/models", headers=headers, timeout=timeout)
    return resp.status_code, resp.content


def probe_generic_entry(
    entry: Any,
    *,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Fallback adapter for any provider without a dedicated one above."""
    http_get = http_get or _generic_http_get
    access_token = _access_token(entry)
    if not access_token.strip():
        return ProbeResult(False, None, "missing_credential")
    try:
        status_code, body = http_get(access_token, getattr(entry, "base_url", None), timeout)
    except Exception as exc:
        return ProbeResult(False, None, f"probe_error:{exc.__class__.__name__}")

    if status_code == 200:
        return ProbeResult(True, status_code, "ok")
    if status_code == 401:
        return ProbeResult(False, status_code, "unauthorized")
    if status_code == 429:
        return ProbeResult(False, status_code, "rate_limited")
    return ProbeResult(False, status_code, f"http_{status_code}")


ADAPTERS: Dict[str, Callable[..., ProbeResult]] = {
    "openai-codex": probe_codex_usage_entry,
    "anthropic": probe_anthropic_entry,
    "openrouter": probe_openrouter_entry,
}


def resolve_adapter(provider: str) -> Callable[..., ProbeResult]:
    """Provider adapter table lookup. Unregistered providers fall back to
    the generic ``/models`` probe rather than raising — a probe run must
    never crash just because a provider has no dedicated adapter yet."""
    return ADAPTERS.get((provider or "").strip().lower(), probe_generic_entry)


# ── Cost guards ──────────────────────────────────────────────────────────

def _cost_guard_allows(state: Dict[str, Any], provider: str, entry_id: str, now: float) -> bool:
    """True when probing this entry right now does not violate either cost
    guard: 10 min since its own last probe, and under 12 probes/hour for the
    whole provider. Pure — never mutates ``state``."""
    entries = state.get("entries") or {}
    key = f"{provider}:{entry_id}"
    last = entries.get(key)
    if isinstance(last, (int, float)) and (now - float(last)) < MIN_PROBE_INTERVAL_SECONDS:
        return False
    hourly = state.get("provider_hourly") or {}
    stamps = [
        t for t in (hourly.get(provider) or [])
        if isinstance(t, (int, float)) and (now - float(t)) < 3600
    ]
    return len(stamps) < MAX_PROBES_PER_PROVIDER_HOUR


def _cost_guard_record(state: Dict[str, Any], provider: str, entry_id: str, now: float) -> None:
    """Record that ``entry_id`` was just probed. Mutates ``state`` in place
    so a caller that persists ``state`` across invocations gets a real,
    durable cost guard; a caller that discards it after one call effectively
    gets a per-invocation-only guard (still correct, just not durable)."""
    entries = state.setdefault("entries", {})
    entries[f"{provider}:{entry_id}"] = now
    hourly = state.setdefault("provider_hourly", {})
    stamps = [
        t for t in (hourly.get(provider) or [])
        if isinstance(t, (int, float)) and (now - float(t)) < 3600
    ]
    stamps.append(now)
    # Bound growth generously past the cap — a burst of skipped/late probes
    # must not lose the timestamps the cap itself needs to enforce the window.
    hourly[provider] = stamps[-(MAX_PROBES_PER_PROVIDER_HOUR * 4):]


# ── Control-host network arbitration ────────────────────────────────────

def _default_control_host_check(timeout: float = CONTROL_HOST_TIMEOUT_SECONDS) -> bool:
    """Real reachability check against a stable, provider-unrelated host.
    Isolated behind a thin wrapper so tests never hit the network — every
    test injects a fake ``control_host_check`` instead. Never raises."""
    import httpx

    try:
        resp = httpx.get(CONTROL_HOST_URL, timeout=timeout)
        return resp.status_code < 500
    except Exception:
        return False


# ── State I/O (shared by the alert-dedup and cost-guard state files) ────

def _load_json_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _save_json_state(path: str, obj: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _send_slack(msg: str) -> bool:
    if os.environ.get("DRY_RUN"):
        print(f"[provider-probe] DRY_RUN slack:\n{msg}")
        return True
    try:
        r = subprocess.run(
            [HERMES_BIN, "send", "--to", SLACK_TARGET, msg],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"[provider-probe] slack send failed: {e!r}", file=sys.stderr)
        return False


# ── Core engine ───────────────────────────────────────────────────────────

def run_probe(
    provider: str,
    *,
    now: Optional[float] = None,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
    adapter: Optional[Callable[..., ProbeResult]] = None,
    control_host_check: Optional[Callable[[], bool]] = None,
    cost_guard_state: Optional[Dict[str, Any]] = None,
    apply_cost_guard: bool = True,
) -> Dict[str, Any]:
    """Probe and (unless dry_run) clear every cost-guard-eligible
    frozen-exhausted entry for ``provider``. Returns a JSON-safe diagnostics
    dict; never retains credential material.

    ``cost_guard_state`` is an in-memory dict the caller owns: pass the same
    dict across invocations (loading/saving it around calls, e.g. from a
    CLI's ``main()``) for a durable cost guard, or omit it for a fresh,
    per-call-only guard. ``run_probe`` never touches disk itself.
    """
    now = _now() if now is None else now
    from agent.credential_pool import STATUS_DEAD, load_pool, record_probe_verdict

    provider_key = (provider or "").strip().lower()
    pool = load_pool(provider_key)
    adapter_fn = adapter or resolve_adapter(provider_key)
    control_host_check = control_host_check or _default_control_host_check
    state = {} if cost_guard_state is None else cost_guard_state

    # A provider with ZERO configured entries is a config state ("nobody
    # ever authenticated"), not exhaustion — no HTTP call is made at all.
    if not pool.entries():
        return {
            "provider": provider_key,
            "checked_at": now,
            "configured": False,
            "frozen_count": 0,
            "cleared": [],
            "still_exhausted": [],
            "skipped_cost_guard": [],
            "inconclusive_network": [],
        }

    # Belt-and-suspenders: frozen_exhausted_entries() already only returns
    # STATUS_EXHAUSTED entries (never STATUS_DEAD), but a DEAD entry must
    # never be probed even if that filter's contract ever loosens.
    frozen = [
        e for e in pool.frozen_exhausted_entries(now=now)
        if getattr(e, "last_status", None) != STATUS_DEAD
    ]

    cleared: List[Dict[str, Any]] = []
    still_exhausted: List[Dict[str, Any]] = []
    skipped_cost_guard: List[Dict[str, Any]] = []
    inconclusive_network: List[Dict[str, Any]] = []

    for entry in frozen:
        reset_at = getattr(entry, "last_error_reset_at", None)

        if apply_cost_guard and not _cost_guard_allows(state, provider_key, entry.id, now):
            skipped_cost_guard.append({
                "provider": provider_key,
                "id": entry.id,
                "reason": "cost_guard_throttled",
            })
            continue

        result = adapter_fn(entry, http_get=http_get, timeout=timeout)

        if apply_cost_guard:
            _cost_guard_record(state, provider_key, entry.id, now)

        if result.detail.startswith("probe_error:"):
            # A transport-level failure is ambiguous until arbitrated
            # against a control host — never blame the credential/provider
            # for what might be a local network blip.
            if not control_host_check():
                inconclusive_network.append({
                    "provider": provider_key,
                    "id": entry.id,
                    "reset_at": reset_at,
                    "probed_at": now,
                    "probe_detail": result.detail,
                })
                if not dry_run:
                    record_probe_verdict(
                        provider_key, entry.id, VERDICT_INCONCLUSIVE_LOCAL_NETWORK,
                        evidence=EVIDENCE_SUSPECTED_NETWORK,
                    )
                continue
            still_exhausted.append({
                "provider": provider_key,
                "id": entry.id,
                "reset_at": reset_at,
                "probed_at": now,
                "probe_status_code": result.status_code,
                "probe_detail": result.detail,
            })
            if not dry_run:
                record_probe_verdict(
                    provider_key, entry.id, VERDICT_CONFIRMED_PROVIDER_SIDE,
                    evidence=result.detail,
                )
            continue

        if result.usable:
            did_clear = dry_run or pool.clear_stale_exhaustion(entry.id)
            cleared.append({
                "provider": provider_key,
                "id": entry.id,
                "stale_reset_at": reset_at,
                "probed_at": now,
                "probe_detail": result.detail,
                "usage_percent": result.usage_percent,
                "dry_run": dry_run,
                "persisted": did_clear and not dry_run,
            })
            if not dry_run:
                record_probe_verdict(provider_key, entry.id, VERDICT_USABLE, evidence=result.detail)
        else:
            still_exhausted.append({
                "provider": provider_key,
                "id": entry.id,
                "reset_at": reset_at,
                "probed_at": now,
                "probe_status_code": result.status_code,
                "probe_detail": result.detail,
                "usage_percent": result.usage_percent,
            })
            if not dry_run:
                record_probe_verdict(provider_key, entry.id, VERDICT_STILL_UNUSABLE, evidence=result.detail)

    return {
        "provider": provider_key,
        "checked_at": now,
        "configured": True,
        "frozen_count": len(frozen),
        "cleared": cleared,
        "still_exhausted": still_exhausted,
        "skipped_cost_guard": skipped_cost_guard,
        "inconclusive_network": inconclusive_network,
    }


def _print_human_summary(provider: str, result: Dict[str, Any]) -> None:
    if not result.get("configured", True):
        print(f"[provider-probe] {provider}: provider has no configured entries (not_configured)")
        return
    if result["frozen_count"] == 0:
        print(f"[provider-probe] {provider}: no frozen-exhausted entries to probe")
    for c in result["cleared"]:
        suffix = " (dry-run, not persisted)" if c["dry_run"] else ""
        print(
            f"[provider-probe] {provider}: entry {c['id']} probed usable early "
            f"({c['probe_detail']}) — cleared stale exhaustion{suffix}"
        )
    for s in result["still_exhausted"]:
        print(
            f"[provider-probe] {provider}: entry {s['id']} still unavailable "
            f"(status={s['probe_status_code']} detail={s['probe_detail']}); reduced redundancy"
        )
    for n in result.get("inconclusive_network") or []:
        print(
            f"[provider-probe] {provider}: entry {n['id']} probe failed transport-level "
            f"({n['probe_detail']}) but the control host is ALSO unreachable — "
            "inconclusive, treating as local network, not provider-side"
        )
    for g in result.get("skipped_cost_guard") or []:
        print(f"[provider-probe] {provider}: entry {g['id']} skipped (cost guard throttled)")


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alert", action="store_true", help="send a Slack note on a NEW early clear")
    ap.add_argument("--dry-run", action="store_true", help="probe only; never persist a clear")
    ap.add_argument("--now", help="epoch seconds or ISO8601 'now' override (testing only)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = ap.parse_args(argv)

    now = _resolve_now(args.now)
    cost_guard_state = _load_json_state(COST_GUARD_STATE_PATH)

    try:
        result = run_probe(
            args.provider, now=now, dry_run=args.dry_run, timeout=args.timeout,
            cost_guard_state=cost_guard_state,
        )
    except Exception as exc:
        error = {"provider": args.provider, "error": exc.__class__.__name__, "checked_at": now}
        if args.json:
            print(json.dumps(error, indent=2))
        else:
            print(
                f"[provider-probe] could not read/probe pool '{args.provider}': {exc.__class__.__name__}",
                file=sys.stderr,
            )
        sys.exit(1)

    if not args.dry_run and result.get("frozen_count", 0) > 0:
        _save_json_state(COST_GUARD_STATE_PATH, cost_guard_state)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human_summary(args.provider, result)

    if args.alert:
        state = _load_json_state(STATE_PATH)
        alerted = set(state.get("alerted_clear_ids") or [])
        new_clears = [
            c for c in result["cleared"]
            if not c["dry_run"] and f"{result['provider']}:{c['id']}:{c['probed_at']}" not in alerted
        ]
        if new_clears:
            lines = [f"\U0001F7E2 Hermes provider-probe ({result['provider']}): early recovery"]
            for c in new_clears:
                lines.append(
                    f"- {result['provider']} entry '{c['id']}' was marked exhausted "
                    f"until {c['stale_reset_at']} but is usable again now — cleared "
                    f"the stale mark ({c['probe_detail']})."
                )
            msg = "\n".join([SLACK_MENTION, *lines])
            if _send_slack(msg):
                alerted.update(f"{result['provider']}:{c['id']}:{c['probed_at']}" for c in new_clears)
                state["alerted_clear_ids"] = list(alerted)[-MAX_HISTORY_EVENTS:]
                state["last_alert_at"] = now
                _save_json_state(STATE_PATH, state)
                print(f"[provider-probe] alerted ({len(new_clears)} early clear(s))")
            else:
                print("[provider-probe] alert delivery failed; will retry next tick", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
