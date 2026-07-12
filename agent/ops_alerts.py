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

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# Same Slack DM + mention as the degraded-secrets monitor's established
# convention — override via env if these alerts should ever redirect to a
# channel instead of Colin's DM.
OPS_ALERT_SLACK_TARGET = os.environ.get("OPS_ALERT_SLACK_TARGET", "slack:D0BA2PM9CFM")
OPS_ALERT_SLACK_MENTION = os.environ.get("OPS_ALERT_SLACK_MENTION", "<@UN4CQ1EGG>")

# Process-lifetime dedup. Intentionally not persisted to disk: these alerts
# fire from inside the gateway/agent process itself (not a standalone cron
# like degraded_secrets_monitor.py), so a gateway restart is itself a natural
# "the situation may have changed" reset point.
_ALERTED_SIGNATURES: set = set()


def alert_once(signature: str, message: str) -> bool:
    """Send a deduped Slack alert for a hard auxiliary-task failure.

    ``signature`` scopes the dedup: the same signature will not re-alert
    until the process restarts or a caller passes a different signature
    (e.g. a different failing provider). Returns True if this call was the
    one that fired the alert (i.e. it was new), False if it was suppressed
    as a repeat. Never raises — alerting must never interfere with the
    caller's own error handling / re-raise.
    """
    if signature in _ALERTED_SIGNATURES:
        return False
    _ALERTED_SIGNATURES.add(signature)
    try:
        _send_slack(message)
    except Exception:
        logger.debug("ops_alerts: alert send failed", exc_info=True)
    return True


def _send_slack(message: str) -> bool:
    full_message = (
        f"{OPS_ALERT_SLACK_MENTION}\n{message}" if OPS_ALERT_SLACK_MENTION else message
    )
    if os.environ.get("DRY_RUN"):
        logger.info("[ops_alerts] DRY_RUN slack:\n%s", full_message)
        return True
    hermes_bin = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    try:
        subprocess.run(
            [hermes_bin, "send", "--to", OPS_ALERT_SLACK_TARGET, full_message],
            check=True,
            capture_output=True,
            timeout=20,
        )
        return True
    except Exception as e:
        logger.warning("ops_alerts: slack send failed: %r", e)
        return False


def reset_for_tests() -> None:
    """Test-only: clear in-process dedup state between test cases."""
    _ALERTED_SIGNATURES.clear()
