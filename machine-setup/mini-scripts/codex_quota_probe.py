#!/usr/bin/env python3
"""codex_quota_probe.py — early-recovery probe for stale Codex quota marks
(ClickUp 86e2kxk50, 2026-08-03).

On 2026-08-03 both Codex OAuth accounts (primary 9dc40b, backup 75ac96) were
marked ``exhausted`` with a provider-supplied ``reset_at`` around Aug 8 — but a
manual ``hermes auth reset openai-codex`` followed by one probe call succeeded
immediately. The 429-driven ``reset_at`` is a server-side *ceiling*, not a
promise the account stays blocked that long; taking it at face value starved
the fleet of Codex capacity for days after the account had actually recovered.

This script periodically probes every entry in the ``openai-codex`` credential
pool that is currently ``exhausted`` with an UNEXPIRED retry window (see
``CredentialPool.frozen_exhausted_entries``) using a single cheap,
zero-generation-token call — ``GET .../codex/models`` — the same endpoint
``hermes_cli.codex_models._fetch_models_from_api`` already treats as safe to
poll (metadata only, no completion is generated). If the probe succeeds (or
fails for a reason OTHER than the account's usage-limit rejection), the
entry's exhaustion is cleared immediately via
``CredentialPool.clear_stale_exhaustion`` — independent of every other entry,
so clearing one account can never mask a genuinely-exhausted sibling. If the
probe still reports ``usage_limit_reached``, the entry is left untouched and a
non-alerting reduced-redundancy diagnostic line is written.

Run on a short interval (see the paired launchd plist, 15 minutes) so a
stale exhausted mark clears within about an hour of the account actually
recovering, comfortably inside the drill's one-hour acceptance bound.

Thin wrapper (ClickUp 86e2mb8p0, PR 2/4): the codex-specific classification
here (``probe_entry`` / ``_default_http_get``) and this script's own state
file / CLI flags / Slack-alert semantics are all unchanged so the already-
deployed launchd job needs no migration. ``run_probe()`` now delegates its
entry loop, cost guards (10 min/entry, 12/provider/hr), and control-host
network arbitration to ``provider_probe.py``'s generalized engine — see that
module for the multi-provider adapter table (anthropic, openrouter, generic)
this script's codex-only logic was generalized into.

Usage:
  codex_quota_probe.py                  # probe + clear, human summary, exit 0
  codex_quota_probe.py --json           # emit JSON result
  codex_quota_probe.py --alert          # + Slack note on a NEW early clear
  codex_quota_probe.py --provider NAME  # default: openai-codex
  codex_quota_probe.py --dry-run        # probe only; never persists a clear
  codex_quota_probe.py --now EPOCH      # override "now" (testing only)
  DRY_RUN=1 codex_quota_probe.py --alert  # test the alert path without posting

Exit codes: 0 = ran to completion (regardless of whether anything cleared),
1 = the credential pool could not be read/probed at all.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional


def _load_sibling_module(name: str, filename: str):
    """Load a same-directory module by file path, WITHOUT touching
    ``sys.path`` or ``sys.modules``. The generalized probe engine lives
    alongside this script both in the repo and in its manifest-governed
    deploy destination (``~/.hermes/scripts`` — see
    ``machine-setup/mini-scripts/fleet_outcome_manifest.json``), but a plain
    ``import provider_probe`` would need this directory on ``sys.path``,
    which — mutated at module scope — leaks into every other file a test
    runner collects in the same process (this directory's ``tests/`` suite
    has exactly that history of cross-file pollution)."""
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


provider_probe = _load_sibling_module("codex_quota_probe_provider_probe", "provider_probe.py")

STATE_PATH = os.path.expanduser("~/.hermes/state/codex-quota-probe.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
DEFAULT_PROVIDER = "openai-codex"
DEFAULT_TIMEOUT_SECONDS = 10.0
# Non-urgent recovery note — same DM target the degraded-secrets monitor uses,
# but this script never posts a ClickUp escalation: an early clear is good
# news (restored capacity), not a fault.
SLACK_TARGET = os.environ.get("CODEX_QUOTA_PROBE_ALERT_SLACK", "slack:D0BA2PM9CFM")
SLACK_MENTION = "<@UN4CQ1EGG>"
MAX_HISTORY_EVENTS = 50


class ProbeResult(NamedTuple):
    usable: bool
    status_code: Optional[int]
    detail: str
    # Shape-compatible with provider_probe.ProbeResult (defaults to None so
    # every existing 3-positional-arg construction/comparison in this
    # module's tests is unaffected) — provider_probe.run_probe's generalized
    # loop reads this field on whatever ProbeResult an adapter returns.
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


def _default_http_get(access_token: str, base_url: Optional[str], timeout: float):
    """Real network probe. Isolated behind a thin wrapper so tests never hit
    the network — every test injects a fake ``http_get`` instead."""
    import httpx
    from hermes_cli.auth import DEFAULT_CODEX_BASE_URL
    from hermes_cli.proxy.adapters.openai_codex import _codex_cloudflare_headers

    root = (base_url or DEFAULT_CODEX_BASE_URL).rstrip("/")
    headers = {
        "Authorization": f"Bearer {access_token}",
        **_codex_cloudflare_headers(access_token),
    }
    resp = httpx.get(f"{root}/models?client_version=1.0.0", headers=headers, timeout=timeout)
    return resp.status_code, resp.content


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


def probe_entry(
    entry: Any,
    *,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Probe one pool entry's access token against the live Codex backend.

    Never raises — a transport failure classifies as "still unusable" (the
    entry stays frozen for the next tick) rather than crashing the monitor.
    Never retains or logs the access token itself.
    """
    http_get = http_get or _default_http_get
    access_token = getattr(entry, "access_token", None) or ""
    if not isinstance(access_token, str) or not access_token.strip():
        return ProbeResult(False, None, "missing_credential")
    try:
        status_code, body = http_get(access_token, getattr(entry, "base_url", None), timeout)
    except Exception as exc:
        return ProbeResult(False, None, f"probe_error:{exc.__class__.__name__}")

    if status_code == 200:
        return ProbeResult(True, status_code, "ok")
    if status_code == 429:
        error_type = _extract_error_type(body)
        if error_type == "usage_limit_reached":
            return ProbeResult(False, status_code, "usage_limit_reached")
        return ProbeResult(False, status_code, f"429:{error_type or 'unknown'}")
    if status_code == 401:
        return ProbeResult(False, status_code, "unauthorized")
    return ProbeResult(False, status_code, f"http_{status_code}")


def run_probe(
    provider: str,
    *,
    now: Optional[float] = None,
    http_get: Optional[Callable[[str, Optional[str], float], Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Probe and (unless dry_run) clear every frozen-exhausted entry for
    ``provider``. Returns a JSON-safe diagnostics dict; never retains
    credential material.

    Thin delegation to ``provider_probe.run_probe()``'s generalized engine
    (cost guards + control-host network arbitration + record_probe_verdict),
    passing THIS module's own ``probe_entry`` as the adapter override so the
    codex-specific /models classification (and any test monkeypatch of it or
    of ``_default_http_get``) still governs what actually executes — the
    generalized engine only owns the loop, not the per-entry codex
    classification.
    """
    return provider_probe.run_probe(
        provider,
        now=now,
        http_get=http_get,
        timeout=timeout,
        dry_run=dry_run,
        adapter=probe_entry,
    )


def _load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(obj: Dict[str, Any]) -> None:
    tmp = STATE_PATH + ".tmp"
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _send_slack(msg: str) -> bool:
    if os.environ.get("DRY_RUN"):
        print(f"[codex-quota-probe] DRY_RUN slack:\n{msg}")
        return True
    try:
        r = subprocess.run(
            [HERMES_BIN, "send", "--to", SLACK_TARGET, msg],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"[codex-quota-probe] slack send failed: {e!r}", file=sys.stderr)
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alert", action="store_true", help="send a Slack note on a NEW early clear")
    ap.add_argument("--dry-run", action="store_true", help="probe only; never persist a clear")
    ap.add_argument("--now", help="epoch seconds or ISO8601 'now' override (testing only)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = ap.parse_args()

    now = _resolve_now(args.now)

    try:
        result = run_probe(args.provider, now=now, dry_run=args.dry_run, timeout=args.timeout)
    except Exception as exc:
        error = {"provider": args.provider, "error": exc.__class__.__name__, "checked_at": now}
        if args.json:
            print(json.dumps(error, indent=2))
        else:
            print(f"[codex-quota-probe] could not read/probe pool '{args.provider}': {exc.__class__.__name__}",
                  file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["frozen_count"] == 0:
            print(f"[codex-quota-probe] {args.provider}: no frozen-exhausted entries to probe")
        for c in result["cleared"]:
            suffix = " (dry-run, not persisted)" if c["dry_run"] else ""
            print(
                f"[codex-quota-probe] {args.provider}: entry {c['id']} probed usable early "
                f"({c['probe_detail']}) — cleared stale exhaustion{suffix}"
            )
        for s in result["still_exhausted"]:
            print(
                f"[codex-quota-probe] {args.provider}: entry {s['id']} still unavailable "
                f"(status={s['probe_status_code']} detail={s['probe_detail']}); reduced redundancy"
            )

    if args.alert:
        state = _load_state()
        alerted = set(state.get("alerted_clear_ids") or [])
        new_clears = [
            c for c in result["cleared"]
            if not c["dry_run"] and f"{c['id']}:{c['probed_at']}" not in alerted
        ]
        if new_clears:
            lines = ["\U0001F7E2 Hermes codex-quota-probe: early recovery"]
            for c in new_clears:
                lines.append(
                    f"- {result['provider']} entry '{c['id']}' was marked exhausted "
                    f"until {c['stale_reset_at']} but is usable again now — cleared "
                    f"the stale mark ({c['probe_detail']})."
                )
            msg = "\n".join([SLACK_MENTION, *lines])
            if _send_slack(msg):
                alerted.update(f"{c['id']}:{c['probed_at']}" for c in new_clears)
                # Cap unbounded growth — only dedup needs to survive restarts.
                state["alerted_clear_ids"] = list(alerted)[-MAX_HISTORY_EVENTS:]
                state["last_alert_at"] = now
                _save_state(state)
                print(f"[codex-quota-probe] alerted ({len(new_clears)} early clear(s))")
            else:
                print("[codex-quota-probe] alert delivery failed; will retry next tick",
                      file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
