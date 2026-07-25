#!/usr/bin/env python3
"""pr_staleness_alert.py — notify Colin on Slack when an open PR on an
allowlisted repo sits for more than the no-verdict SLA (6h) without a fresh
validator verdict.

This is a thin, Slack-delivered cron wrapper. It uses the lower-level
scan/staleness primitives that already live in pr_pipeline_improvements.py /
pr_wake_and_sweep.py (GitHubClient, scan_repos, stale_without_verdict,
utcnow, notify) but owns its OWN dedupe and delivery decision end to end —
it does NOT call pr_pipeline_improvements.check_staleness_and_alert()
anymore.

Why: that function already had a "dedupe", but it was broken. It fingerprinted
each stale PR on `round(age_hours, 2)`, a value that changes on essentially
every 15-minute tick, so the state comparison in
`.pr_pipeline_state.json` never matched two runs in a row and the script
re-posted an identical Slack payload every 15 minutes — confirmed
byte-identical across 20:02/20:16/20:32/20:47/21:02/21:16 on 2026-07-23,
257 runs since 2026-07-22, for the same 6 unresolved stale PRs (mini cron
job `pr-staleness-alert`, id 3043a00e6df8). The signal was real; the defect
was the dedupe granularity.

Fix: fingerprint the stale set on (repo, PR number) -> a COARSE age bucket
(crossing PR_STALENESS_AGE_BUCKET_HOURS, default 24h/72h/168h) instead of a
raw, ever-changing age float. Post to Slack only when that fingerprint
changes materially:
  - a PR enters or leaves the stale set, or
  - a PR crosses an age-bucket boundary.
Otherwise stay silent. A periodic heartbeat still posts at most once every
PR_STALENESS_DIGEST_HOURS (default 24) so an unchanged-but-still-broken
state resurfaces daily instead of going quiet forever.

State lives at PR_STALENESS_STATE_PATH (default
~/.hermes/state/pr_staleness_last.json), separate from the old, now-unused
.pr_pipeline_state.json. Reading that state file fails OPEN: a missing,
corrupt, or otherwise unreadable file is treated as "no prior state," which
makes the very next stale set look new and forces a post rather than
silently swallowing a real alert. This mirrors the fail-open contract used
in the claim-retry-cap work (see claim_store.py in this same directory).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autonomous_merge
import pr_pipeline_improvements as ppi

DEFAULT_STATE_PATH = Path.home() / ".hermes/state/pr_staleness_last.json"
DEFAULT_DIGEST_HOURS = 24.0
DEFAULT_AGE_BUCKET_HOURS = (24.0, 72.0, 168.0)


def _state_path() -> Path:
    raw = os.environ.get("PR_STALENESS_STATE_PATH")
    return Path(raw) if raw else DEFAULT_STATE_PATH


def _digest_hours() -> float:
    raw = os.environ.get("PR_STALENESS_DIGEST_HOURS")
    if not raw:
        return DEFAULT_DIGEST_HOURS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DIGEST_HOURS
    return value if value > 0 else DEFAULT_DIGEST_HOURS


def _age_bucket_thresholds() -> tuple[float, ...]:
    raw = os.environ.get("PR_STALENESS_AGE_BUCKET_HOURS")
    if not raw:
        return DEFAULT_AGE_BUCKET_HOURS
    try:
        values = tuple(sorted(float(part) for part in raw.split(",") if part.strip()))
    except ValueError:
        return DEFAULT_AGE_BUCKET_HOURS
    return values if values else DEFAULT_AGE_BUCKET_HOURS


def age_bucket(age_hours: float, thresholds: tuple[float, ...] = DEFAULT_AGE_BUCKET_HOURS) -> int:
    """Coarse bucket index for an age in hours: how many thresholds it has
    crossed. Deliberately insensitive to the exact age so it does not change
    on every 15-minute tick — only when a PR ages past the next boundary."""
    bucket = 0
    for threshold in thresholds:
        if age_hours >= threshold:
            bucket += 1
    return bucket


def build_fingerprint(stale_prs: list[dict[str, Any]], thresholds: tuple[float, ...]) -> dict[str, int]:
    return {
        f"{pr['repo']}#{pr['pr']}": age_bucket(pr["age_hours"], thresholds)
        for pr in stale_prs
    }


def _load_state(path: Path) -> dict[str, Any]:
    """Load the persisted fingerprint/heartbeat state. Any failure to read
    or parse the file — missing, corrupt JSON, wrong shape, permission
    error — is treated as "no prior state" (an empty dict), which is the
    fail-open behavior: an empty prior fingerprint makes any current stale
    PR look new, so it gets posted rather than silently suppressed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _save_state(path: Path, fingerprint: dict[str, int], now: datetime) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = {"fingerprint": fingerprint, "last_posted_at": now.isoformat()}
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        # Best-effort: a failed write should not make the cron run look
        # like it crashed after we already (maybe) posted to Slack.
        print(f"[pr-staleness-alert] failed to save state: {exc}", file=sys.stderr)


def _heartbeat_due(last_posted_at: str | None, now: datetime, digest_hours: float) -> bool:
    if not last_posted_at:
        return True
    try:
        last = datetime.fromisoformat(last_posted_at)
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(hours=digest_hours)


def decide(
    prev_fingerprint: dict[str, int],
    curr_fingerprint: dict[str, int],
    last_posted_at: str | None,
    now: datetime,
    digest_hours: float,
) -> tuple[bool, str]:
    """Return (should_post, reason). Silent when the fingerprint is
    unchanged and the heartbeat interval has not elapsed; a fingerprint
    change (new PR, resolved PR, or an age-bucket crossing) always posts,
    and an unchanged-but-nonempty stale set still posts once per
    digest_hours so it cannot go quiet forever."""
    if curr_fingerprint != prev_fingerprint:
        return True, "stale set changed"
    if curr_fingerprint and _heartbeat_due(last_posted_at, now, digest_hours):
        return True, "heartbeat"
    return False, "no change"


def _build_message(stale_prs: list[dict[str, Any]], reason: str):
    import slack_msg_builder as smb

    if not stale_prs:
        return smb.build_status_message(
            "✅",
            "Previously stale PR(s) are no longer stale.",
            next_step="No action needed.",
            max_words=40,
        )
    facts = [
        f"{pr['repo']}#{pr['pr']} — age {pr['age_hours']:.1f}h, merge_state {pr['merge_state']}"
        for pr in stale_prs
    ]
    headline = f"{len(stale_prs)} PR(s) stale without a fresh verdict ({reason})."
    return smb.build_alert_message(
        "⏰",
        headline,
        facts=facts,
        next_step="Open the PR(s) and re-run validation or merge if already clear.",
        max_words=200,
    )


def run(allowlist: list[str]) -> int:
    now = ppi.utcnow()
    gh = ppi.GitHubClient()
    errors: list[str] = []
    states, _ = ppi.scan_repos(allowlist, now, gh, errors)
    for error in errors:
        print(f"[pr-staleness-alert] {error}", file=sys.stderr)

    stale_prs = [
        {
            "repo": pr_state.repo,
            "pr": pr_state.number,
            "url": pr_state.url,
            "merge_state": pr_state.mergeable_state,
            "age_hours": round((now - pr_state.created_at).total_seconds() / 3600.0, 2),
        }
        for pr_state in states
        if ppi.stale_without_verdict(pr_state.created_at, pr_state.latest_verdict_at, now)
    ]

    thresholds = _age_bucket_thresholds()
    digest_hours = _digest_hours()
    state_path = _state_path()
    curr_fingerprint = build_fingerprint(stale_prs, thresholds)

    try:
        state = _load_state(state_path)
        prev_fingerprint = state.get("fingerprint") or {}
        last_posted_at = state.get("last_posted_at")
        should_post, reason = decide(prev_fingerprint, curr_fingerprint, last_posted_at, now, digest_hours)
    except Exception as exc:
        # Fail OPEN: a bug anywhere in the dedupe decision must never
        # suppress a real alert. If there is currently a stale PR, post it.
        print(f"[pr-staleness-alert] dedupe decision failed, failing open: {exc}", file=sys.stderr)
        should_post, reason = bool(stale_prs), "dedupe error (fail-open)"

    if not should_post:
        return 0

    message = _build_message(stale_prs, reason)
    ppi.notify(message, alert_log_path=ppi.ALERT_LOG_PATH)
    _save_state(state_path, curr_fingerprint, now)
    return 0


def main() -> int:
    try:
        allowlist = sorted(autonomous_merge._load_allowlist())
    except Exception as exc:
        print(f"[pr-staleness-alert] failed to load allowlist: {exc}", file=sys.stderr)
        return 1
    if not allowlist:
        print("[pr-staleness-alert] allowlist is empty — nothing to check", file=sys.stderr)
        return 0
    return run(allowlist)


if __name__ == "__main__":
    raise SystemExit(main())
