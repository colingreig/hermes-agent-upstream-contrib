"""Provider failure taxonomy — shared constants for classifying WHY an API
call failed, independent of the recovery-action hints on
``agent.error_classifier.FailoverReason``.

Today every provider failure that survives the first retry collapses into
one generic "exhausted" pool state — a session-level throttle, a weekly
usage cap, an expired subscription, a DNS failure, and a genuine provider
outage are all indistinguishable in logs, alerts, and the credential pool.
This module is the single source of truth for the deeper taxonomy that
fixes that: string constants for each failure *kind*, plus a small,
dependency-light helper for the one piece of classification logic that's
shared across call sites (the rate-limit-vs-usage-cap reset-horizon split).

Dependency-light on purpose: mini-scripts (``machine-setup/mini-scripts/*.py``,
which run outside the ``agent`` package and its import tree, e.g. on the
Mac mini fleet monitor) import or vendor these constants for alerting
fan-out. This module must never import anything beyond the standard
library — no ``agent.*``, no third-party packages.

PR 1/4 of the failure-taxonomy rollout (ClickUp 86e2mb8nv):
  PR 1 (this):  classifier + credential-pool taxonomy (this module,
                ``agent.error_classifier``, ``agent.credential_pool``).
  PR 2:         an out-of-band control-host probe that can confirm or deny
                a ``suspected_*``-tagged classification (network vs local,
                auth_permanent vs transient 403) without waiting for the
                credential's own TTL to elapse.
  PR 3/4:       wire probe verdicts into DEAD promotion + dashboards/alerts.
"""
from __future__ import annotations

from typing import Optional


# ── Failure kinds ────────────────────────────────────────────────────────
# A deeper taxonomy than FailoverReason's recovery-action buckets. Several
# FailoverReason values can map to the same underlying failure kind here —
# this module is the single source of truth for the *string* constants
# persisted to auth.json (``PooledCredential.last_failure_kind``) and
# emitted in logs / monitor output, independent of how the classifier
# happens to be structured internally.

FAILURE_KIND_RATE_LIMIT_SESSION = "rate_limit_session"
FAILURE_KIND_USAGE_CAP = "usage_cap"
FAILURE_KIND_AUTH_PERMANENT = "auth_permanent"
FAILURE_KIND_NETWORK_UNREACHABLE = "network_unreachable"
FAILURE_KIND_PROVIDER_OUTAGE = "provider_outage"
FAILURE_KIND_NOT_CONFIGURED = "not_configured"

ALL_FAILURE_KINDS = frozenset({
    FAILURE_KIND_RATE_LIMIT_SESSION,
    FAILURE_KIND_USAGE_CAP,
    FAILURE_KIND_AUTH_PERMANENT,
    FAILURE_KIND_NETWORK_UNREACHABLE,
    FAILURE_KIND_PROVIDER_OUTAGE,
    FAILURE_KIND_NOT_CONFIGURED,
})

# Maps agent.error_classifier.FailoverReason.value -> failure kind, for
# call sites that only have the classifier's reason string and want the
# taxonomy-level kind without importing error_classifier (e.g. a mini-script
# reading a persisted ``last_error_reason``-shaped log line). Reasons with no
# direct taxonomy mapping (context_overflow, format_error, model_not_found,
# ...) are intentionally absent — callers should treat a missing key as "not
# taxonomy-tracked" rather than guessing.
REASON_VALUE_TO_FAILURE_KIND = {
    "rate_limit": FAILURE_KIND_RATE_LIMIT_SESSION,
    "usage_cap": FAILURE_KIND_USAGE_CAP,
    "auth_permanent": FAILURE_KIND_AUTH_PERMANENT,
    "network_unreachable": FAILURE_KIND_NETWORK_UNREACHABLE,
}


def failure_kind_for_reason(reason_value: str) -> Optional[str]:
    """Map a ``FailoverReason.value`` string to a taxonomy failure kind.

    Returns ``None`` when the reason has no direct taxonomy mapping.
    """
    return REASON_VALUE_TO_FAILURE_KIND.get(reason_value)


# ── Evidence markers ─────────────────────────────────────────────────────
# Short strings stashed in ``PooledCredential.last_failure_evidence`` /
# ``ClassifiedError.error_context["evidence"]``. A ``suspected_*`` marker
# denotes a classification made WITHOUT out-of-band corroboration (PR 2's
# probe) — downstream consumers (credential-pool quarantine logic,
# dashboards) must treat it as lower-confidence than an unambiguous marker
# and must never let it alone justify a permanent (DEAD) verdict.
EVIDENCE_SUSPECTED_NETWORK = "suspected_network"
EVIDENCE_SUSPECTED_AUTH_PERMANENT = "suspected_auth_permanent"
EVIDENCE_SUBSCRIPTION_LAPSED = "subscription_lapsed"


# ── Horizon classification ───────────────────────────────────────────────
# A retry/reset horizon this far out means "not worth waiting for" — a
# rate_limit-style backoff-and-retry burns nothing but wall clock while a
# credential that won't reset for hours just sits there. Reclassify as a
# usage cap instead: rotate/fall back now rather than sleep through it.
USAGE_CAP_HORIZON_SECONDS = 6 * 60 * 60  # 6 hours

# Cap/quota-period phrases that are unambiguous even when no numeric reset
# horizon can be parsed out of the message (e.g. "resets weekly" with no
# explicit countdown). Deliberately narrow and long-horizon-flavoured —
# generic "rate limit" / "usage limit" / "try again" text must NOT match
# here, or every session-level throttle would misclassify as a cap.
#
# Notably absent: any "5h" / "5-hour" / "session" phrasing. OpenAI Codex's
# genuine 5-hour rolling window is phrased like a usage limit but is a
# session-level throttle, not an hours-away cap — see the BINDING note in
# agent/error_classifier.py's 429 handler. Only an unambiguous long-horizon
# marker (weekly/monthly/billing-period wording) or a numeric horizon past
# USAGE_CAP_HORIZON_SECONDS may promote an ambiguous signal to usage_cap.
CAP_PATTERN_MARKERS = (
    "weekly limit",
    "weekly quota",
    "weekly cap",
    "monthly limit",
    "monthly quota",
    "monthly cap",
    "usage cap",
    "quota period",
    "billing period",
    "billing cycle",
    "plan limit",
    "subscription limit",
)


def classify_horizon(
    *,
    reset_in_seconds: Optional[float] = None,
    message: str = "",
) -> bool:
    """Return True when a rate/usage signal should classify as a usage cap
    rather than a session-level rate limit.

    ``reset_in_seconds`` at or beyond :data:`USAGE_CAP_HORIZON_SECONDS`, OR
    an unambiguous cap-pattern marker in ``message``, means "usage_cap".
    Anything else — including an unparseable/absent horizon with no cap
    marker — means "session-level" and the caller should keep the existing
    session-scoped classification. This is deliberately conservative:
    ambiguous signals default to the session bucket rather than guessing
    a multi-hour cap (see the codex 5h-window note on ``CAP_PATTERN_MARKERS``
    above).
    """
    if reset_in_seconds is not None and reset_in_seconds >= USAGE_CAP_HORIZON_SECONDS:
        return True
    haystack = (message or "").lower()
    return any(marker in haystack for marker in CAP_PATTERN_MARKERS)
