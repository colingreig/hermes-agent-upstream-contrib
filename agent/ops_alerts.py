"""Shared "loud failure" Slack alerting for auxiliary/background LLM tasks.

Some auxiliary tasks ship with no reliable in-process fallback — notably
``auxiliary.vision`` (no vision-capable backend the account can bill once the
built-in auto-chain is exhausted) and ``auxiliary.curator`` (the review fork
never calls ``call_llm()``, so it can't use ``_try_task_fallback_once()``; it
only degrades via the main agent's top-level ``fallback_providers`` chain).
Both omissions are deliberate (see ``hermes_cli/config.py`` ``DEFAULT_CONFIG``
comments), but the net effect used to be a silent hard-failure with zero
signal — ``auxiliary.vision`` returned hard tool-errors for 2+ days,
unnoticed, during the 2026-07 Gemini outage (audit H1,
``reports/audit-hermes-setup-2026-07-10.md``).

This module gives those call sites one place to say "I'm dead" out loud,
reusing the same Slack-DM convention as
``~/.hermes/scripts/degraded_secrets_monitor.py``
(``hermes send --to slack:D0BA2PM9CFM`` + ``<@UN4CQ1EGG>`` mention) rather
than inventing a new alert channel or primitive.

Dedup is in-process and signature-based (a module-level set, mirroring
``_LOGGED_UNHANDLED_AUTHTYPE_KEYS`` in ``agent/auxiliary_client.py``): a given
failure signature alerts once and stays quiet on repeat calls with the same
signature, so a stuck backend doesn't spam Slack every turn. A distinct
signature (a different provider failing, or the process restarting) alerts
again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Same Slack DM + mention as the degraded-secrets monitor's established
# convention — override via env if these alerts should ever redirect to a
# channel instead of Colin's DM.
OPS_ALERT_SLACK_TARGET = os.environ.get("OPS_ALERT_SLACK_TARGET", "slack:D0BA2PM9CFM")
OPS_ALERT_SLACK_MENTION = os.environ.get("OPS_ALERT_SLACK_MENTION", "<@UN4CQ1EGG>")
OPS_ALERT_RECEIPTS_LEDGER = os.path.expanduser(
    os.environ.get("HERMES_OPS_ALERT_RECEIPTS_LEDGER", "~/.hermes/logs/ops-alert-receipts.jsonl")
)

# Process-lifetime dedup. Intentionally not persisted to disk: these alerts
# fire from inside the gateway/agent process itself (not a standalone cron
# like degraded_secrets_monitor.py), so a gateway restart is itself a natural
# "the situation may have changed" reset point.
_ALERTED_SIGNATURES: set = set()
_ALERTED_SIGNATURES_LOCK = threading.Lock()
USAGE_HEADROOM_WARNING_PERCENT = 85.0
_RECEIPT_HEADER_SECRET = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
)
_RECEIPT_SCHEME_SECRET = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9+/=._~-]+")
_RECEIPT_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(auth(?:orization)?|token|secret|password|passwd|api[_-]?key|"
    r"access[_-]?key|client[_-]?secret)(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


def alert_once(signature: str, message: str) -> bool:
    """Send a deduped Slack alert for a hard auxiliary-task failure.

    ``signature`` scopes the dedup: the same signature will not re-alert
    until the process restarts or a caller passes a different signature
    (e.g. a different failing provider). Returns True if this call was the
    one that fired the alert (i.e. it was new), False if it was suppressed
    as a repeat. Never raises — alerting must never interfere with the
    caller's own error handling / re-raise.
    """
    with _ALERTED_SIGNATURES_LOCK:
        if signature in _ALERTED_SIGNATURES:
            return False
        _ALERTED_SIGNATURES.add(signature)
    try:
        _send_slack(message, signature=signature)
    except Exception:
        logger.debug("ops_alerts: alert send failed", exc_info=True)
    return True


def alert_provider_failure(failure_kind: str, *, provider: str = "unknown") -> bool:
    """Emit a one-shot, taxonomy-preserving provider failure alert."""
    normalized_kind = str(failure_kind or "unknown").strip().lower() or "unknown"
    normalized_provider = str(provider or "unknown").strip().lower() or "unknown"
    return alert_once(
        f"provider_failure:{normalized_provider}:{normalized_kind}",
        f"Provider failure ({normalized_kind}) · provider={normalized_provider}",
    )


def alert_usage_headroom(
    snapshot: Any,
    *,
    threshold_percent: float = USAGE_HEADROOM_WARNING_PERCENT,
) -> bool:
    """Warn once for usage windows nearing, but not at, exhaustion.

    The exclusion of 100%-used windows keeps this an early warning path rather
    than an exhaustion simulator. Repeated /usage polls dedupe via alert_once.
    """
    provider = str(getattr(snapshot, "provider", "unknown") or "unknown").strip().lower()
    fired = False
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            continue
        if not threshold_percent <= float(used) < 100.0:
            continue
        label = str(getattr(window, "label", "window") or "window").strip().lower()
        signature = f"usage_headroom:{provider}:{label}:{threshold_percent:g}"
        remaining = max(0.0, 100.0 - float(used))
        fired = alert_once(
            signature,
            f"Usage headroom low · provider={provider} · {label}={used:g}% used "
            f"({remaining:g}% remaining)",
        ) or fired
    return fired


def _redact_receipt_text(value: object, *, limit: int = 500) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = _RECEIPT_HEADER_SECRET.sub(
        lambda match: f"{match.group(1)}: <redacted>",
        text,
    )
    text = _RECEIPT_SCHEME_SECRET.sub(
        lambda match: f"{match.group(1)} <redacted>",
        text,
    )
    text = _RECEIPT_ASSIGNMENT_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    return text[:limit]


def _parse_send_payload(stdout: str) -> dict:
    if not isinstance(stdout, (str, bytes, bytearray)):
        return {}
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_receipt(entry: dict) -> None:
    try:
        path = Path(OPS_ALERT_RECEIPTS_LEDGER)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        logger.debug("ops_alerts: receipt write failed", exc_info=True)


def _receipt_entry(
    *,
    signature: str,
    message: str,
    target: str,
    hermes_bin: str,
    dry_run: bool,
    success: bool,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
    error: object = None,
) -> dict:
    payload = _parse_send_payload(stdout)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "signature": signature or None,
        "target": target,
        "hermes_bin": hermes_bin,
        "dry_run": dry_run,
        "success": success,
        "returncode": returncode,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "stdout_tail": _redact_receipt_text(stdout),
        "stderr_tail": _redact_receipt_text(stderr),
        "error": _redact_receipt_text(error) if error is not None else None,
        "send_payload": {
            key: payload.get(key)
            for key in ("success", "platform", "chat_id", "message_id", "error", "note")
            if key in payload
        },
    }


def _send_slack(message: str, *, signature: str = "") -> bool:
    full_message = (
        f"{OPS_ALERT_SLACK_MENTION}\n{message}" if OPS_ALERT_SLACK_MENTION else message
    )
    hermes_bin = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    if os.environ.get("DRY_RUN"):
        logger.info("[ops_alerts] DRY_RUN slack:\n%s", full_message)
        _write_receipt(
            _receipt_entry(
                signature=signature,
                message=full_message,
                target=OPS_ALERT_SLACK_TARGET,
                hermes_bin=hermes_bin,
                dry_run=True,
                success=True,
            )
        )
        return True
    try:
        result = subprocess.run(
            [hermes_bin, "send", "--to", OPS_ALERT_SLACK_TARGET, full_message, "--json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        _write_receipt(
            _receipt_entry(
                signature=signature,
                message=full_message,
                target=OPS_ALERT_SLACK_TARGET,
                hermes_bin=hermes_bin,
                dry_run=False,
                success=True,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        )
        return True
    except Exception as e:
        logger.warning("ops_alerts: slack send failed: %r", e)
        stdout = getattr(e, "stdout", "") or ""
        stderr = getattr(e, "stderr", "") or ""
        returncode = getattr(e, "returncode", None)
        _write_receipt(
            _receipt_entry(
                signature=signature,
                message=full_message,
                target=OPS_ALERT_SLACK_TARGET,
                hermes_bin=hermes_bin,
                dry_run=False,
                success=False,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                error=e,
            )
        )
        return False


def reset_for_tests() -> None:
    """Test-only: clear in-process dedup state between test cases."""
    with _ALERTED_SIGNATURES_LOCK:
        _ALERTED_SIGNATURES.clear()
