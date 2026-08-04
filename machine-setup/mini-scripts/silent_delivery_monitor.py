#!/usr/bin/env python3
"""silent_delivery_monitor.py — alarm on an abnormal rate of [SILENT] cron
deliveries (ClickUp 86e2kxk4t, 2026-08-03).

On 2026-08-02, 17 of 50 executor cron runs ended [SILENT] — the agent's
deliberate "nothing new to report" sentinel (see SILENT_MARKER in
cron/scheduler.py). Silence is intentional and correct in isolation, so
``_deliver_cron_outcome`` logs it at INFO and moves on. That also means a
TOTAL fleet outage — every job's agent turn failing in a way that happens to
render as empty/silent output — is indistinguishable from routine quiet days
in the logs, and can go unnoticed for days (as it did here). This is the
"silent-monitor failure pattern": a monitor (in this case, cron's own INFO
logging) that runs green while covering almost nothing.

``cron/scheduler.py`` now appends one durable JSONL record
(``{"job_id": ..., "at": ...}``) to
``~/.hermes/state/cron-silent-deliveries.jsonl`` every time a run ends
silent (see ``_record_silent_delivery``). This script reads a trailing
window of that log and fires a REAL alert — not just another log line —
when either:

  (a) one job accumulates >= PER_JOB_THRESHOLD silent endings inside the
      window (a single broken claim/routing path repeatedly going quiet), or
  (b) the fleet accumulates >= TOTAL_THRESHOLD silent endings across ALL
      jobs inside the window (a simultaneous, cross-job silence spike —
      the "total fleet outage" shape).

The alarm is signature-based and self-clearing (same convention as
degraded_secrets_monitor.py): it alerts once per distinct breach signature
and goes quiet on repeat checks of the same signature; once the rolling
window drops back under both thresholds, the signature clears and a future
recurrence alerts again. The alarm is therefore satisfiable — it does not
latch permanently red the way an any-imperfection flag would.

ClickUp 86e2mg7jb (2026-08-03): ci-health-watch (job e835c614cfb2, a */5min
no_agent tick) emits a by-design [SILENT] ending on EVERY healthy run — it
writes a JSON artifact and delivers nothing else, so it alone produced 9-12
silent endings per rolling 60-minute window, above PER_JOB_THRESHOLD on its
own and rolling straight into a chronic false TOTAL_THRESHOLD "possible total
fleet outage" alarm. ``BY_DESIGN_SILENT_JOB_IDS`` now excludes ci-health-watch
plus the other no_agent jobs that are silent on literally every healthy tick
(review-poll-gate, spend-meter, reap-stranded-claims,
clickup-workspace-refresh) from both counters below. Agent cron jobs that go
[SILENT] only some of the time are deliberately NOT in that table — a job
that's normally chatty going quiet on every tick is exactly the regression
this monitor exists to catch.

Usage:
  silent_delivery_monitor.py                    # check, human summary, exit 1 if triggered
  silent_delivery_monitor.py --json              # emit JSON result
  silent_delivery_monitor.py --alert             # + Slack alert on a NEW breach signature
  silent_delivery_monitor.py --log-file PATH     # check a fixture instead of the live log
  silent_delivery_monitor.py --now EPOCH         # override "now" (testing only)
  DRY_RUN=1 silent_delivery_monitor.py --alert   # test the alert path without posting

Exit codes: 0 = healthy (under both thresholds), 1 = triggered.

Runbook — diagnosing and clearing a real alert:
  1. Read the alert: it names every job_id that breached PER_JOB_THRESHOLD
     and/or the fleet-wide TOTAL_THRESHOLD, with each count and the window.
  2. For a single breached job: check that job's recent cron output
     (``~/.hermes/logs/cron/<job_id>*``) and jobs.json entry — a broken
     claim query, an exhausted credential pool, or a lane-filter draw that
     never matches are the usual causes of a job going quiet repeatedly.
  3. For a fleet-wide breach: check gateway/provider health first
     (``degraded_secrets_monitor.py --json``, ``ai.hermes.gateway`` in
     MONITOR_COVERAGE.md) — a shared credential or gateway outage renders
     as simultaneous silence across many unrelated jobs.
  4. The alarm clears itself on the next tick once the rolling window's
     count(s) drop back under threshold — no manual state reset needed.
     To force a clean state during an incident, truncate
     ``~/.hermes/state/cron-silent-deliveries.jsonl`` (log-only; never
     touches cron/jobs.json or credentials) or delete
     ``~/.hermes/state/silent-delivery-monitor.json`` (dedupe state only).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

DEFAULT_LOG_PATH = os.path.expanduser("~/.hermes/state/cron-silent-deliveries.jsonl")
# Matches cron/scheduler.py's CRON_SILENT_LOG_PATH_ENV so overriding the
# writer's path in one place keeps this reader in sync automatically.
LOG_PATH = os.environ.get("HERMES_CRON_SILENT_LOG_PATH") or DEFAULT_LOG_PATH
STATE_PATH = os.path.expanduser("~/.hermes/state/silent-delivery-monitor.json")
HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")

DEFAULT_WINDOW_MIN = 60
DEFAULT_PER_JOB_THRESHOLD = 4
DEFAULT_TOTAL_THRESHOLD = 8

# Same escalation surface degraded_secrets_monitor.py pages on — "the existing
# alert channel used for other fleet failures".
SLACK_TARGET = os.environ.get("CRON_SILENT_ALERT_SLACK", "slack:D0BA2PM9CFM")
SLACK_MENTION = "<@UN4CQ1EGG>"

# ── By-design-silent no_agent jobs (ClickUp 86e2mg7jb) ──────────────────────
# These no_agent jobs are EXPECTED to end [SILENT] on essentially every
# healthy tick — see each job's own machine-setup/mini-scripts/
# fleet_outcome_contracts.json cron entry, which already treats a silent
# ending as literal SUCCESS for these jobs specifically (either a
# "Status:** silent" success_pattern, or — for ci-health-watch and
# clickup-workspace-refresh — a json_artifact outcome that never inspects
# cron_output/delivery text at all). Before this fix, ci-health-watch (job
# e835c614cfb2, a */5min no_agent tick) alone produced 9-12 [SILENT] endings
# per rolling 60-minute window on a totally healthy fleet — above
# PER_JOB_THRESHOLD (4) by itself, and its count rolled into TOTAL_THRESHOLD
# (8) fleet-wide too, driving a chronic false "possible total fleet outage"
# alarm with zero actual outage.
#
# This table is the source of truth for which no_agent jobs get excluded
# from BOTH the per-job and fleet-wide counters in evaluate_silent_rate().
# Deliberately NOT every no_agent job with a "silent" success marker: jobs
# like clickup-poll-gate, validator-live-trigger, clickup-review-sla, and
# ignite-board-sync are silent only SOME ticks (most runs have real work) —
# excluding them too would blind the monitor to a genuine claim/routing
# regression that makes one of them go quiet on every tick instead of its
# normal partial rate, exactly the failure class this monitor exists to
# catch. Only jobs that are silent on literally every healthy run belong
# here.
#
# Keep this in sync with machine-setup/fleet-config/jobs.json (mirrored to
# the live ~/.hermes/cron/jobs.json on the Mini): each id below must resolve
# to the same job name there. _fleet_config_drift_warnings() below performs
# that cross-check whenever a fleet config file is reachable and returns
# (never raises) a list of drift warnings, so a rename or retirement is loud
# instead of silently stale.
BY_DESIGN_SILENT_JOB_IDS = {
    "e835c614cfb2": "ci-health-watch",
    "8d3b1d53470d": "review-poll-gate",
    "b0c4c5cc70c1": "spend-meter",
    "dd73a5e578e4": "reap-stranded-claims",
    "bcf275768661": "clickup-workspace-refresh",
}


def _fleet_config_candidates() -> List[str]:
    """Paths to try for the live/source-of-truth fleet job config, in order.

    The live Mini path (``~/.hermes/cron/jobs.json``) is authoritative in
    production; the repo-relative fleet-config file is the fallback so this
    check still works from a plain repo checkout (tests, CI, a fresh clone).
    """
    candidates = []
    override = os.environ.get("HERMES_FLEET_CONFIG_PATH")
    if override:
        candidates.append(override)
    candidates.append(os.path.expanduser("~/.hermes/cron/jobs.json"))
    candidates.append(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "fleet-config", "jobs.json",
        )
    )
    return candidates


def _fleet_config_drift_warnings(
    job_ids: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Best-effort drift check between BY_DESIGN_SILENT_JOB_IDS and whichever
    fleet config file is reachable. Returns human-readable warning strings;
    never raises, and returns [] both when nothing is reachable and when
    everything matches — this must never blind or crash the monitor."""
    job_ids = BY_DESIGN_SILENT_JOB_IDS if job_ids is None else job_ids
    for path in _fleet_config_candidates():
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            continue
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            continue
        by_id = {
            str(job.get("id")): str(job.get("name") or "")
            for job in jobs
            if isinstance(job, dict)
        }
        warnings = []
        for job_id, expected_name in job_ids.items():
            if job_id not in by_id:
                warnings.append(
                    f"by-design-silent job id {job_id!r} ({expected_name!r}) not "
                    f"found in {path} — exclusion table may be stale"
                )
            elif by_id[job_id] != expected_name:
                warnings.append(
                    f"by-design-silent job id {job_id!r} name drifted: expected "
                    f"{expected_name!r}, fleet config has {by_id[job_id]!r}"
                )
        return warnings
    return []


def _now() -> float:
    import time

    return time.time()


def _resolve_now(raw: Optional[str]) -> float:
    if not raw:
        return _now()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return _now()


def read_records(path: str) -> List[Dict[str, Any]]:
    """Parse the JSONL log, skipping any malformed line rather than failing
    the whole read — a single corrupted line must not blind the monitor to
    every other (valid) record."""
    if not os.path.isfile(path):
        return []
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if isinstance(record, dict) and "job_id" in record and "at" in record:
                    records.append(record)
    except Exception:
        return []
    return records


def evaluate_silent_rate(
    records: List[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    window_min: int = DEFAULT_WINDOW_MIN,
    per_job_threshold: int = DEFAULT_PER_JOB_THRESHOLD,
    total_threshold: int = DEFAULT_TOTAL_THRESHOLD,
    excluded_job_ids: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    now = _now() if now is None else now
    window_start = now - (window_min * 60)
    in_window = [
        r for r in records
        if isinstance(r.get("at"), (int, float)) and window_start <= r["at"] <= now
    ]

    excluded_job_ids = (
        BY_DESIGN_SILENT_JOB_IDS if excluded_job_ids is None else excluded_job_ids
    )
    excluded = [r for r in in_window if str(r["job_id"]) in excluded_job_ids]
    counted = [r for r in in_window if str(r["job_id"]) not in excluded_job_ids]

    per_job_counts = Counter(str(r["job_id"]) for r in counted)
    total_count = len(counted)
    excluded_per_job_counts = Counter(str(r["job_id"]) for r in excluded)
    breached_jobs = sorted(
        job_id for job_id, count in per_job_counts.items() if count >= per_job_threshold
    )
    total_breached = total_count >= total_threshold
    triggered = bool(breached_jobs) or total_breached

    return {
        "checked_at": now,
        "window_min": window_min,
        "per_job_threshold": per_job_threshold,
        "total_threshold": total_threshold,
        "total_count": total_count,
        "per_job_counts": dict(sorted(per_job_counts.items())),
        "excluded_total_count": len(excluded),
        "excluded_per_job_counts": dict(sorted(excluded_per_job_counts.items())),
        "breached_jobs": breached_jobs,
        "total_breached": total_breached,
        "triggered": triggered,
    }


def _signature(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "breached_jobs": sorted(
            [job_id, result["per_job_counts"][job_id]] for job_id in result["breached_jobs"]
        ),
        "total_breached": result["total_breached"],
    }


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
        print(f"[silent-delivery-monitor] DRY_RUN slack:\n{msg}")
        return True
    try:
        r = subprocess.run(
            [HERMES_BIN, "send", "--to", SLACK_TARGET, msg],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception as e:
        print(f"[silent-delivery-monitor] slack send failed: {e!r}", file=sys.stderr)
        return False


def _format_alert(result: Dict[str, Any]) -> str:
    lines = ["\U0001F6A8 Hermes silent-delivery monitor: abnormal [SILENT] rate"]
    if result["breached_jobs"]:
        for job_id in result["breached_jobs"]:
            count = result["per_job_counts"][job_id]
            lines.append(
                f"- job '{job_id}': {count} silent endings in the last "
                f"{result['window_min']} min (threshold {result['per_job_threshold']})."
            )
    if result["total_breached"]:
        lines.append(
            f"- fleet-wide: {result['total_count']} silent endings across all jobs in "
            f"the last {result['window_min']} min (threshold {result['total_threshold']}) "
            "— possible total fleet outage."
        )
    lines.append(
        "See silent_delivery_monitor.py's module docstring for the diagnose/clear runbook."
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-file", default=LOG_PATH)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--alert", action="store_true")
    ap.add_argument("--now", help="epoch seconds or ISO8601 'now' override (testing only)")
    ap.add_argument("--window-min", type=int, default=DEFAULT_WINDOW_MIN)
    ap.add_argument("--per-job-threshold", type=int, default=DEFAULT_PER_JOB_THRESHOLD)
    ap.add_argument("--total-threshold", type=int, default=DEFAULT_TOTAL_THRESHOLD)
    args = ap.parse_args()

    now = _resolve_now(args.now)
    records = read_records(args.log_file)
    result = evaluate_silent_rate(
        records, now=now, window_min=args.window_min,
        per_job_threshold=args.per_job_threshold, total_threshold=args.total_threshold,
    )

    for warning in _fleet_config_drift_warnings():
        print(f"[silent-delivery-monitor] WARNING: {warning}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, indent=2))
    elif not result["triggered"]:
        print(
            f"[silent-delivery-monitor] healthy (total={result['total_count']}/"
            f"{args.total_threshold} in {args.window_min}min, "
            f"excluded={result['excluded_total_count']} by-design-silent)"
        )
    else:
        for job_id in result["breached_jobs"]:
            print(
                f"[silent-delivery-monitor] job '{job_id}' breached per-job threshold: "
                f"{result['per_job_counts'][job_id]}/{args.per_job_threshold} silents "
                f"in {args.window_min}min"
            )
        if result["total_breached"]:
            print(
                f"[silent-delivery-monitor] fleet-wide breached: "
                f"{result['total_count']}/{args.total_threshold} silents in {args.window_min}min"
            )

    if args.alert:
        state = _load_state()
        sig = _signature(result)
        last_sig = state.get("last_alert_signature")
        if result["triggered"] and sig != last_sig:
            msg = "\n".join([SLACK_MENTION, _format_alert(result)])
            if _send_slack(msg):
                state["last_alert_signature"] = sig
                state["last_alert_at"] = now
                _save_state(state)
                print("[silent-delivery-monitor] alerted")
            else:
                print("[silent-delivery-monitor] alert delivery failed; will retry next tick",
                      file=sys.stderr)
        elif not result["triggered"] and last_sig is not None:
            state["last_alert_signature"] = None
            state["recovered_at"] = now
            _save_state(state)
            print("[silent-delivery-monitor] recovered — dedup state cleared")

    sys.exit(1 if result["triggered"] else 0)


if __name__ == "__main__":
    main()
