#!/usr/bin/env python3
"""hermes_usage_alert.py — notify Colin on Slack when Hermes hits a usage or
execution fault that should never sit unseen.

Signals:
- provider / credential exhaustion in the live Hermes logs
- fallback-chain exhaustion (all providers failed) with a short cooldown
- cron jobs that flip their stored last_status to error, OR remain in error
  past a re-alert cadence (see RC1 below)
- cron/jobs.json itself being unreadable (see RC2 below)

The script is deliberately zero-LLM and cheap. It runs on a short cron,
scans incrementally, and stays silent when there is nothing new to report.

(RC1, 2026-07-26) `_scan_cron_errors` used to alert ONLY on a transition
into error (`prev_status != "error"`) and explicitly skipped the first
observation of any job (`if prev_status is None: continue`). Two silent
failure modes fell out of that: a persistently-red job alerted exactly once
and then went quiet forever, and a `.usage_alert_state.json` reset (e.g. a
redeploy, a manual state wipe) made `prev_status is None` true for every
already-red job on the next run — permanently silencing them, since "first
observation" was treated as "nothing to report" rather than "here's what I
found." Fixed: a job's first-ever observed state is now itself alert-worthy
if that state is `error` (prev_status is None is no longer special-cased to
mean silence), and a job that STAYS red keeps re-alerting on a bounded
cadence (`HERMES_CRON_ERROR_REALERT_MIN`, default 360min/6h) rather than
going silent after the first alert.

(RC2, 2026-07-26) `_scan_cron_errors` used to catch any jobs.json read/parse
failure and `return []` — indistinguishable from "read fine, no errors."
Fixed: an unreadable jobs.json now produces its own distinct `monitor_error`
alert (own cooldown, so a persistently broken file re-alerts periodically
instead of spamming every tick) instead of silently reporting all-clear.
"""

from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

HOME = os.path.expanduser("~")
LOGS = [
    os.path.join(HOME, ".hermes/logs/agent.log"),
    os.path.join(HOME, ".hermes/logs/gateway.log"),
]
# Overridable so tests can point at fixture paths without clobbering (or
# depending on the exact shape of) live mini state.
JOBS_PATH = os.environ.get("HERMES_USAGE_ALERT_JOBS_PATH") or os.path.join(HOME, ".hermes/cron/jobs.json")
STATE_PATH = os.environ.get("HERMES_USAGE_ALERT_STATE_PATH") or os.path.join(
    HOME, ".hermes/scripts/.usage_alert_state.json"
)
RECEIPT_PATH = os.environ.get("HERMES_USAGE_ALERT_RECEIPT_PATH") or os.path.join(
    HOME, ".hermes/state/hermes-usage-alert.json"
)
HERMES_BIN = os.path.join(HOME, ".local/bin/hermes")
SLACK_TARGET = "slack:hermes"

# Separate cooldowns by signal class so a cron error is not hidden by a recent
# usage alert.
DEFAULT_COOLDOWN_S = int(os.environ.get("HERMES_USAGE_ALERT_COOLDOWN_MIN", "1440")) * 60
FALLBACK_EXHAUSTED_COOLDOWN_S = int(os.environ.get("HERMES_FALLBACK_ALERT_COOLDOWN_MIN", "60")) * 60
# A job that STAYS red re-alerts at this cadence instead of going silent
# after its first (transition) alert — see RC1 in the module docstring.
CRON_ERROR_REALERT_S = int(os.environ.get("HERMES_CRON_ERROR_REALERT_MIN", "360")) * 60
# An unreadable jobs.json alarms at this cadence rather than either spamming
# every tick or silently returning "no cron errors" — see RC2.
JOBS_UNREADABLE_COOLDOWN_S = int(os.environ.get("HERMES_JOBS_UNREADABLE_ALERT_COOLDOWN_MIN", "60")) * 60

# Usage-limit signatures. Kept tight to avoid false positives — these are the
# provider quota / rate-limit / pool-exhaustion signals, not generic errors.
SIGNATURES = [
    {
        "rx": re.compile(r"no available entries \(all exhausted", re.I),
        "label": "credential pool fully exhausted",
        "kind": "usage_limit",
        "cooldown_s": DEFAULT_COOLDOWN_S,
    },
    {
        "rx": re.compile(r"marking\s+([A-Z0-9_]+)\s+exhausted\s*\(status=429\)", re.I),
        "label": "API key hit 429 (quota/rate limit)",
        "kind": "usage_limit",
        "cooldown_s": DEFAULT_COOLDOWN_S,
    },
    {
        "rx": re.compile(r"error_type=RateLimitError", re.I),
        "label": "RateLimitError on an API call",
        "kind": "usage_limit",
        "cooldown_s": DEFAULT_COOLDOWN_S,
    },
    {
        "rx": re.compile(r"insufficient[_ ]?quota|quota exceeded|quota exhaust|usage limit", re.I),
        "label": "provider reported quota/usage limit",
        "kind": "usage_limit",
        "cooldown_s": DEFAULT_COOLDOWN_S,
    },
    {
        "rx": re.compile(r"fallback chain exhausted|all providers failed", re.I),
        "label": "fallback chain exhausted / all providers failed",
        "kind": "fallback_exhausted",
        "cooldown_s": FALLBACK_EXHAUSTED_COOLDOWN_S,
    },
]

GROUP_ORDER = ("monitor_error", "cron_error", "fallback_exhausted", "usage_limit")
GROUP_META = {
    "monitor_error": {
        "emoji": "🆘",
        "headline": "This alarm's own input (cron/jobs.json) is unreadable — cron-error monitoring is BLIND.",
        "next_step": "Check ~/.hermes/cron/jobs.json exists and parses; until fixed, red cron jobs will NOT be reported.",
        "cooldown_s": JOBS_UNREADABLE_COOLDOWN_S,
    },
    "cron_error": {
        "emoji": "🚨",
        "headline": "Cron job entered (or remains in) error state.",
        "next_step": "Inspect the latest cron output and fix the failing job or dependency.",
        "cooldown_s": 0,
    },
    "fallback_exhausted": {
        "emoji": "🛑",
        "headline": "Fallback chain exhausted / all providers failed.",
        "next_step": "Check provider quotas, auth, or upstream errors before the next run.",
        "cooldown_s": FALLBACK_EXHAUSTED_COOLDOWN_S,
    },
    "usage_limit": {
        "emoji": "🛑",
        "headline": "Hermes hit a usage / quota limit.",
        "next_step": "Check the logs and refill or rotate the exhausted pool entry.",
        "cooldown_s": DEFAULT_COOLDOWN_S,
    },
}


def _load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
            return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _save_state(state):
    _save_json(STATE_PATH, state)


def _save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _write_receipt(*, status: str, delivery: str, now: float) -> None:
    _save_json(
        RECEIPT_PATH,
        {
            "schema_version": 1,
            "checked_at": datetime_from_timestamp(now),
            "status": status,
            "delivery": delivery,
        },
    )


def datetime_from_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _scan_new_lines(path, offset):
    """Return (new_events, new_offset). Handles rotation/truncation."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if offset > size:  # file rotated/truncated — start over
        offset = 0
    matches = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                for sig in SIGNATURES:
                    m = sig["rx"].search(line)
                    if m:
                        key = m.group(1) if m.groups() else ""
                        matches.append(
                            {
                                "kind": sig["kind"],
                                "label": sig["label"],
                                "cooldown_s": sig["cooldown_s"],
                                "key": key,
                                "line": line.rstrip()[-300:],
                            }
                        )
                        break
            new_offset = f.tell()
    except OSError:
        return [], offset
    return matches, new_offset


def _scan_cron_errors(state, now: float | None = None):
    """Return cron-error events: a job entering error, a job STAYING in error
    past its re-alert cadence, or (as its own distinct event) jobs.json
    itself being unreadable.

    An unreadable jobs.json is never treated as "no cron errors" — see RC2
    in the module docstring. It returns a `monitor_error` event instead of
    an empty list so main()'s "no events -> silent" short-circuit can never
    be reached for this failure mode.
    """
    now = time.time() if now is None else now
    try:
        with open(JOBS_PATH, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        return [
            {
                "kind": "monitor_error",
                "label": "cron jobs.json unreadable — cron-error monitoring is blind",
                "cooldown_s": GROUP_META["monitor_error"]["cooldown_s"],
                "key": "jobs.json",
                "line": f"{JOBS_PATH}: {e!r}",
            }
        ]

    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
    seen = set()
    job_statuses = state.setdefault("cron_job_statuses", {})
    # Per-job "last time we alerted about this job's error state" — separate
    # from job_statuses so a re-alert cadence survives even though
    # job_statuses is overwritten every run (see RC1: a status-cache reset
    # must not by itself either silence or spuriously re-trigger an alert
    # beyond the cadence).
    error_alerts = state.setdefault("cron_job_error_last_alert", {})
    events = []

    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        seen.add(job_id)
        current_status = str(job.get("last_status") or "").strip()
        prev_status = job_statuses.get(job_id)
        job_statuses[job_id] = current_status

        if current_status != "error":
            # Recovered (or never-red): clear its alert cadence so a FUTURE
            # error is treated as a fresh transition, not a cadence resume.
            error_alerts.pop(job_id, None)
            continue

        # current_status == "error". Alert-worthy if:
        #   - this is a transition into error (prev_status was anything else,
        #     INCLUDING None — a first-ever observation of a red job is
        #     exactly when you most want to know, not a reason to stay
        #     silent), OR
        #   - the job has been red long enough since its last alert to be
        #     due for a re-alert (standing-red cadence).
        last_alert_ts = error_alerts.get(job_id)
        became_red = prev_status != "error"
        due_for_realert = last_alert_ts is None or (now - float(last_alert_ts)) >= CRON_ERROR_REALERT_S
        if became_red or due_for_realert:
            events.append(
                {
                    "kind": "cron_error",
                    "label": (
                        "cron job entered error state" if became_red
                        else "cron job still in error state (standing-red re-alert)"
                    ),
                    "cooldown_s": GROUP_META["cron_error"]["cooldown_s"],
                    "key": job_id,
                    "line": (
                        f"{job.get('name') or job_id} | "
                        f"last_error={job.get('last_error') or job.get('last_delivery_error') or 'unknown'}"
                    ),
                }
            )
            error_alerts[job_id] = now

    # Drop vanished jobs from both caches so we don't keep stale entries.
    for job_id in list(job_statuses):
        if job_id not in seen:
            job_statuses.pop(job_id, None)
    for job_id in list(error_alerts):
        if job_id not in seen:
            error_alerts.pop(job_id, None)

    return events


def _build_alert(kind: str, events: list[dict[str, Any]], now: float) -> str:
    script_root = Path(__file__).resolve().parent
    formatter = script_root / "slack_msg_builder.py"
    if not formatter.is_file():
        # The source bundle retains the shared formatter under pr_pipeline;
        # release installation flattens it beside this script.
        formatter = script_root / "pr_pipeline" / "slack_msg_builder.py"
    spec = importlib.util.spec_from_file_location("slack_msg_builder", formatter)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Slack formatter: {formatter}")
    smb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smb)
    meta = GROUP_META[kind]
    facts = [f"{len(events)} new signal(s) since the last check."]
    labels = {}
    keys = set()
    for event in events:
        labels[event["label"]] = labels.get(event["label"], 0) + 1
        if event.get("key"):
            keys.add(str(event["key"]))
    facts.extend(f"{label} ×{count}" for label, count in labels.items())
    if keys:
        facts.append(f"source/key: {', '.join(sorted(keys))}")
    facts.append(f"latest signal: {events[-1]['line']}")

    return smb.build_alert_message(
        meta["emoji"],
        meta["headline"],
        facts=facts,
        next_step=meta["next_step"],
        footer=f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}",
        max_words=60,
    )


def _cooldown_key(kind: str) -> str:
    return f"last_alert_ts:{kind}"


def _send_slack(message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [HERMES_BIN, "send", "--to", SLACK_TARGET, message],
        capture_output=True,
        text=True,
        timeout=30,
    )


def main():
    if "--probe" in sys.argv[1:]:
        # Pure collector: no state, receipt, cooldown, or Slack mutation.
        state: dict[str, Any] = {}
        events: list[dict[str, Any]] = []
        for path in LOGS:
            matches, _offset = _scan_new_lines(path, 0)
            events.extend(matches)
        events.extend(_scan_cron_errors(state))
        print(json.dumps({"status": "alert" if events else "clean", "events": events}, sort_keys=True))
        return 1 if events else 0

    if os.environ.get("HERMES_USAGE_ALERT_DISABLE") == "1":
        return 0

    state = _load_state()
    offsets = state.setdefault("offsets", {})
    last_alert_by_kind = state.setdefault("last_alert_ts_by_kind", {})

    events_by_kind: dict[str, list[dict[str, Any]]] = {}

    for path in LOGS:
        if not os.path.exists(path):
            continue
        prev = offsets.get(path)
        if prev is None:
            # First time we've seen this file: jump to EOF, don't alert on history.
            try:
                offsets[path] = os.path.getsize(path)
            except OSError:
                offsets[path] = 0
            continue
        matches, new_off = _scan_new_lines(path, prev)
        offsets[path] = new_off
        for event in matches:
            events_by_kind.setdefault(event["kind"], []).append(event)

    for event in _scan_cron_errors(state):
        events_by_kind.setdefault(event["kind"], []).append(event)

    if not events_by_kind:
        _save_state(state)
        _write_receipt(status="clean", delivery="confirmed", now=time.time())
        return 0  # silent — empty stdout

    now = time.time()
    printed = []
    for kind in GROUP_ORDER:
        events = events_by_kind.get(kind, [])
        if not events:
            continue
        cooldown_s = GROUP_META[kind]["cooldown_s"]
        last_alert = float(last_alert_by_kind.get(kind, 0) or 0)
        if cooldown_s and (now - last_alert) < cooldown_s:
            continue
        msg = _build_alert(kind, events, now)
        print(msg)  # → delivered to Slack by the cron job
        last_alert_by_kind[kind] = now
        printed.append(kind)

    if printed:
        # This script owns its Slack alarm.  Relying on an enclosing LLM
        # digest to notice stdout made the zero-LLM alarm both delayed and
        # unsatisfiable when the digest itself stopped.  Persist offsets,
        # cooldowns, and red-job re-alert timestamps only after Slack confirms
        # delivery; a failed send therefore remains eligible next tick.
        try:
            delivery = _send_slack("\n\n".join(
                _build_alert(kind, events_by_kind[kind], now)
                for kind in printed
            ))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[hermes_usage_alert] Slack delivery failed: {exc}", file=sys.stderr)
            _write_receipt(status="alert", delivery="failed", now=now)
            return 2
        if delivery.returncode != 0:
            detail = (delivery.stderr or delivery.stdout or "unknown failure").strip()
            print(
                f"[hermes_usage_alert] Slack delivery exited {delivery.returncode}: {detail}",
                file=sys.stderr,
            )
            _write_receipt(status="alert", delivery="failed", now=now)
            return 2
        _save_state(state)
        _write_receipt(status="alert", delivery="confirmed", now=now)
    else:
        # We still updated offsets / job status caches above, so this run stays
        # silent but does not re-alert the same already-seen signals forever.
        _save_state(state)
        _write_receipt(status="deduped", delivery="confirmed", now=now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
