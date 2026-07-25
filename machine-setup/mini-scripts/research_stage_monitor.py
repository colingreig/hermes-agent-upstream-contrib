#!/usr/bin/env python3
"""Independent liveness monitor for the content research-stage ledger.

The exec stage's ``degraded`` ledger flag now means *materially* degraded
(search outage / ungrounded brief / single-sourced) rather than "any one
page was blocked". Narrowing that flag without compensating for the lost
detection surface would be a net loss, so this monitor also tracks
coverage metrics (fetch success, ungrounded-brief share) and a
short-window / streak check that can catch a search outage well before a
48h rate would move.

Status precedence (most-urgent first) and exit codes -- the JSON
``status`` field is authoritative; exit codes exist so a cron/watchdog
that only checks process exit status can still tell the conditions apart:

    status                 exit  meaning
    ----------------------------------------------------------------------
    not-observed             3   stage never ran / ledger missing or stale
    disabled-or-smoke-only    0  nothing enabled, or every enabled attempt
                                  in the window was smoke/test traffic
    search-outage             2  short-window search-failure rate > 0.30
                                  (>=3 attempts in --search-window-hours),
                                  OR 3+ consecutive most-recent production
                                  records have search_failed=true. Fires
                                  even when total attempts are below
                                  --min-attempts -- an outage is detectable
                                  from a handful of samples and must not
                                  be masked by insufficient-data.
    degraded                  2  served-rate/degraded-rate breach on
                                  sufficient real (material-degradation)
                                  traffic
    ungrounded                2  >10% of production attempts shipped with
                                  grounded_pages == 0 (content emergency
                                  that sits under any degraded-rate
                                  threshold)
    fetch-degraded             5 fetch_success_rate (grounded_pages /
                                  attempted_fetches) fell below 0.50
    insufficient-data          4 fewer than --min-attempts real
                                  (non-smoke) attempts to judge
    healthy                    0 everything above is within bounds

``--quiet-when-healthy``: mini ``no_agent`` cron jobs deliver a message on
ANY non-empty stdout (see website/docs/guides/cron-script-only.md), so
wiring this monitor to a cron/Slack watchdog without this flag would post
on every single tick regardless of health. When this flag is passed, stdout
is suppressed for a genuinely healthy result (still exits 0), but a
``fetch_success_band`` of ``warn`` remains visible so the documented 0.70
warning threshold is actionable without changing the established status or
exit-code contract. Every non-success status also prints the full JSON exactly
as without the flag. Omitting the flag leaves behavior unchanged (always
prints).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_LEDGER = Path("~/.hermes/logs/research-served.jsonl").expanduser()

# task_id substrings (case-insensitive) that mark smoke/test traffic, e.g.
# "86e25xww8-fetch-smoke" or "ignite-smoke-86e25xww8-recovery-1". This is
# ONLY a fallback for legacy records that predate the explicit `smoke`
# ledger field -- see _is_smoke() below, where the field is authoritative
# whenever present. Left in place, unqualified substring matching would
# silently drop a real content task whose slug happens to contain "test"
# (e.g. an article about A/B testing) from production rate math.
_SMOKE_TASK_ID_MARKERS = ("smoke", "recovery", "test")

# Default floor on real (non-smoke) enabled attempts before status/degraded
# math is trusted at all. Below this, a single failure can swing the rate
# to 100% on a handful of samples -- report "insufficient-data" instead of
# a false-confidence "degraded" or "healthy". NOTE: the search-outage check
# (short-window rate + consecutive-failure streak) deliberately bypasses
# this floor -- see evaluate().
DEFAULT_MIN_ATTEMPTS = 20

# Default lookback for the short-window search-outage rate check. A 48h
# main-window rate mathematically cannot detect a ~12h outage (only ~25%
# of the window), so this runs a second, much shorter window alongside it.
DEFAULT_SEARCH_WINDOW_HOURS = 4.0

# Minimum production attempts required within --search-window-hours before
# the short-window search_failure_rate is trusted (below this it's None,
# not a false 0% or 100%).
MIN_SEARCH_WINDOW_ATTEMPTS = 3

# Alarm/warn thresholds for the coverage metrics. These are deliberately
# separate from --max-degraded-rate: they catch blind spots that a
# material-only `degraded` flag can miss entirely (fetch success silently
# drifting while every record still reads "healthy"; a spike in
# zero-source briefs that sits under any degraded-rate threshold).
FETCH_SUCCESS_ALARM = 0.50
FETCH_SUCCESS_WARN = 0.70
UNGROUNDED_RATE_ALARM = 0.10
SEARCH_FAILURE_RATE_ALARM = 0.30
SEARCH_FAILURE_STREAK_ALARM = 3


def _parse_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _is_smoke_task_id(task_id: Any) -> bool:
    if not isinstance(task_id, str) or not task_id:
        return False
    lowered = task_id.lower()
    return any(marker in lowered for marker in _SMOKE_TASK_ID_MARKERS)


def _is_smoke(record: dict[str, Any]) -> bool:
    """Is this record smoke/test traffic (excluded from production math)?

    Precedence: the explicit `smoke` ledger field is AUTHORITATIVE
    whenever it is a bool -- `smoke: false` on a record always means
    production, even if its task_id happens to contain "test" (e.g. an
    A/B-testing article), and `smoke: true` always means smoke even if
    the task_id gives no hint. Only legacy records that lack a boolean
    `smoke` field at all fall back to the task_id substring heuristic.
    """
    smoke_field = record.get("smoke")
    if isinstance(smoke_field, bool):
        return smoke_field
    return _is_smoke_task_id(record.get("task_id"))


def _safe_int(value: Any) -> int:
    """Best-effort int coercion; missing/None/non-int -> 0 ("missing")."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _safe_int_or_none(value: Any) -> int | None:
    """Like _safe_int but preserves "missing" as None instead of 0, for
    checks (e.g. `grounded_pages == 0`) where absent must not be conflated
    with an observed zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def evaluate(
    records: list[dict[str, Any]],
    *,
    now: float,
    lookback_hours: float,
    min_served_rate: float,
    max_degraded_rate: float,
    min_attempts: int = DEFAULT_MIN_ATTEMPTS,
    search_window_hours: float = DEFAULT_SEARCH_WINDOW_HOURS,
) -> dict[str, Any]:
    cutoff = now - lookback_hours * 3600
    recent = [r for r in records if (_parse_ts(str(r.get("ts", ""))) or 0) >= cutoff]
    enabled_all = [r for r in recent if r.get("enabled") and r.get("outcome") != "fetch-only"]
    smoke = [r for r in enabled_all if _is_smoke(r)]
    # "enabled" from here on means production (non-smoke) traffic only --
    # that is what served/degraded rates, the coverage metrics, and the
    # min-attempts gate judge.
    enabled = [r for r in enabled_all if not _is_smoke(r)]
    served = [r for r in enabled if r.get("served")]
    degraded = [r for r in enabled if r.get("degraded")]
    rate = len(served) / len(enabled) if enabled else None
    degraded_rate = len(degraded) / len(enabled) if enabled else None

    # -- Coverage metrics (compensate for `degraded` now meaning MATERIAL
    # degradation only) --------------------------------------------------
    grounded_pages_total = sum(_safe_int(r.get("grounded_pages")) for r in enabled)
    attempted_fetches_total = sum(_safe_int(r.get("attempted_fetches")) for r in enabled)
    fetch_success_rate = (
        grounded_pages_total / attempted_fetches_total
        if attempted_fetches_total > 0
        else None
    )
    if fetch_success_rate is None:
        fetch_success_band = None
    elif fetch_success_rate < FETCH_SUCCESS_ALARM:
        fetch_success_band = "alarm"
    elif fetch_success_rate < FETCH_SUCCESS_WARN:
        fetch_success_band = "warn"
    else:
        fetch_success_band = "healthy"
    ungrounded_count = sum(
        1 for r in enabled if _safe_int_or_none(r.get("grounded_pages")) == 0
    )
    ungrounded_rate = ungrounded_count / len(enabled) if enabled else None

    # -- Short-window search-outage rate. Deliberately computed from the
    # full record list (not just `recent`/`enabled`) and independent of
    # --min-attempts: an outage must be detectable from a handful of
    # samples in a short window, not masked by insufficient-data. -------
    search_cutoff = now - max(0.0, search_window_hours) * 3600
    search_window_records = [
        r for r in records if (_parse_ts(str(r.get("ts", ""))) or 0) >= search_cutoff
    ]
    search_window_enabled_all = [
        r
        for r in search_window_records
        if r.get("enabled") and r.get("outcome") != "fetch-only"
    ]
    search_window_enabled = [r for r in search_window_enabled_all if not _is_smoke(r)]
    search_window_attempts = len(search_window_enabled)
    search_window_failures = sum(
        1 for r in search_window_enabled if r.get("search_failed")
    )
    search_failure_rate = (
        search_window_failures / search_window_attempts
        if search_window_attempts >= MIN_SEARCH_WINDOW_ATTEMPTS
        else None
    )
    search_outage_by_rate = (
        search_failure_rate is not None and search_failure_rate > SEARCH_FAILURE_RATE_ALARM
    )

    # -- Consecutive-failure streak, over the most recent production
    # (non-smoke) records in the main window, regardless of --min-attempts.
    enabled_by_recency = sorted(
        enabled,
        key=lambda r: _parse_ts(str(r.get("ts", ""))) or 0,
        reverse=True,
    )
    search_failure_streak = 0
    for r in enabled_by_recency:
        if r.get("search_failed"):
            search_failure_streak += 1
        else:
            break
    search_outage_by_streak = search_failure_streak >= SEARCH_FAILURE_STREAK_ALARM
    search_outage = search_outage_by_rate or search_outage_by_streak

    served_rate_ok = rate is not None and rate >= min_served_rate
    degraded_rate_ok = degraded_rate is not None and degraded_rate <= max_degraded_rate
    ungrounded_alarm = ungrounded_rate is not None and ungrounded_rate > UNGROUNDED_RATE_ALARM
    fetch_alarm = fetch_success_rate is not None and fetch_success_rate < FETCH_SUCCESS_ALARM

    if not recent:
        status = "not-observed"
    elif not enabled:
        # Either nothing was enabled at all, or every enabled attempt in the
        # window was smoke/test traffic -- either way there is no
        # production signal to alarm on.
        status = "disabled-or-smoke-only"
    elif search_outage:
        status = "search-outage"
    elif len(enabled) < min_attempts:
        status = "insufficient-data"
    elif not (served_rate_ok and degraded_rate_ok):
        status = "degraded"
    elif ungrounded_alarm:
        status = "ungrounded"
    elif fetch_alarm:
        status = "fetch-degraded"
    else:
        status = "healthy"

    return {
        "status": status,
        "lookback_hours": lookback_hours,
        "recent_receipts": len(recent),
        "enabled_attempts": len(enabled),
        "smoke_attempts": len(smoke),
        "min_attempts": min_attempts,
        "served_attempts": len(served),
        "degraded_attempts": len(degraded),
        "served_rate": (round(rate, 4) if rate is not None else None),
        "degraded_rate": (round(degraded_rate, 4) if degraded_rate is not None else None),
        "max_degraded_rate": max_degraded_rate,
        "grounded_pages_total": grounded_pages_total,
        "attempted_fetches_total": attempted_fetches_total,
        "fetch_success_rate": (
            round(fetch_success_rate, 4) if fetch_success_rate is not None else None
        ),
        "fetch_success_band": fetch_success_band,
        "ungrounded_rate": (
            round(ungrounded_rate, 4) if ungrounded_rate is not None else None
        ),
        "search_window_hours": search_window_hours,
        "search_window_attempts": search_window_attempts,
        "search_failure_rate": (
            round(search_failure_rate, 4) if search_failure_rate is not None else None
        ),
        "search_failure_streak": search_failure_streak,
        "last_outcome": recent[-1].get("outcome") if recent else None,
        "last_task_id": recent[-1].get("task_id") if recent else None,
    }


# Exit code per JSON `status` value. The JSON `status` field stays
# authoritative; this exists only so a cron/watchdog that can't parse JSON
# (checks exit status alone) can still distinguish "provider is genuinely
# failing" (2) from "stage never ran / ledger stale" (3) from "not enough
# real traffic to judge yet" (4) from "fetch coverage silently drifting"
# (5), instead of collapsing them into one opaque non-zero code.
_EXIT_CODES = {
    "healthy": 0,
    "disabled-or-smoke-only": 0,
    "degraded": 2,
    "search-outage": 2,
    "ungrounded": 2,
    "not-observed": 3,
    "insufficient-data": 4,
    "fetch-degraded": 5,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Independent liveness/degradation monitor for the content "
            "research-stage served ledger."
        ),
        epilog=(
            "Status / exit code table (most-urgent precedence first):\n"
            "  not-observed (3)          -- stage never ran / ledger missing or stale\n"
            "  disabled-or-smoke-only (0) -- nothing enabled, or every enabled attempt\n"
            "                                 in the window was smoke/test traffic\n"
            "  search-outage (2)         -- short-window search_failure_rate > "
            f"{SEARCH_FAILURE_RATE_ALARM} (>= {MIN_SEARCH_WINDOW_ATTEMPTS} attempts in "
            "--search-window-hours), OR "
            f"{SEARCH_FAILURE_STREAK_ALARM}+ consecutive most-recent production "
            "records have search_failed=true. Fires even below --min-attempts.\n"
            "  degraded (2)              -- served-rate/degraded-rate (now MATERIAL "
            "degradation only) breach on sufficient real traffic\n"
            "  ungrounded (2)            -- >"
            f"{UNGROUNDED_RATE_ALARM} of production attempts have grounded_pages == 0\n"
            "  fetch-degraded (5)        -- fetch_success_rate "
            "(grounded_pages / attempted_fetches) < "
            f"{FETCH_SUCCESS_ALARM} (warn below {FETCH_SUCCESS_WARN}; warning JSON "
            "remains visible with --quiet-when-healthy, but status/exit stay healthy/0)\n"
            "  insufficient-data (4)     -- fewer than --min-attempts real, non-smoke "
            "attempts in the lookback window\n"
            "  healthy (0)               -- everything above is within bounds\n"
            "The JSON `status` field printed to stdout is authoritative; these exit "
            "codes exist for consumers that only check process exit status."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    parser.add_argument("--lookback-hours", type=float, default=48)
    parser.add_argument("--min-served-rate", type=float, default=0.8)
    parser.add_argument(
        "--max-degraded-rate",
        type=float,
        default=0.15,
        help=(
            "Max share of production attempts allowed to be `degraded` "
            "before status is 'degraded'. NOTE: `degraded` now means "
            "MATERIAL degradation (search outage / ungrounded brief / "
            "single-sourced), not 'any one page was blocked' -- so this "
            "threshold is deliberately tight (default %(default)s, down "
            "from the pre-narrowing 0.5) since it now tolerates real "
            "content-quality failures, not incidental page blocks."
        ),
    )
    parser.add_argument(
        "--search-window-hours",
        type=float,
        default=DEFAULT_SEARCH_WINDOW_HOURS,
        help=(
            "Short window (hours) for the search-outage rate check, run "
            "alongside --lookback-hours. A 48h rate cannot detect a ~12h "
            "outage; this catches it early. Requires at least "
            f"{MIN_SEARCH_WINDOW_ATTEMPTS} production attempts in the "
            "window to trust the rate. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--min-attempts",
        type=int,
        default=DEFAULT_MIN_ATTEMPTS,
        help=(
            "Minimum real (non-smoke/test) enabled attempts required in "
            "the lookback window before served/degraded/coverage rates "
            "are trusted enough to report 'healthy' or 'degraded'/"
            "'ungrounded'/'fetch-degraded'. Below this, status is "
            "'insufficient-data' (exit 4) UNLESS a search-outage condition "
            "is independently detected, which always takes precedence. "
            "Default: %(default)s."
        ),
    )
    parser.add_argument("--now", help="ISO8601 test override")
    parser.add_argument(
        "--quiet-when-healthy",
        action="store_true",
        help=(
            "Print nothing to stdout (and still exit 0) when status is "
            "'healthy' or 'disabled-or-smoke-only', except that a "
            "fetch_success_band of 'warn' remains visible and actionable. "
            "For every other status the full JSON is printed exactly as "
            "without this flag. Use this when wiring the monitor to a mini "
            "no_agent cron job: those deliver on ANY non-empty stdout, so "
            "without this flag every tick would post regardless of health."
        ),
    )
    args = parser.parse_args(argv)
    now = _parse_ts(args.now) if args.now else time.time()
    if now is None:
        print(json.dumps({"status": "error", "error": "invalid --now"}))
        return 2
    result = evaluate(
        read_records(Path(args.ledger).expanduser()),
        now=now,
        lookback_hours=max(0.1, args.lookback_hours),
        min_served_rate=max(0.0, min(args.min_served_rate, 1.0)),
        max_degraded_rate=max(0.0, min(args.max_degraded_rate, 1.0)),
        min_attempts=max(0, args.min_attempts),
        search_window_hours=max(0.01, args.search_window_hours),
    )
    result["checked_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
    exit_code = _EXIT_CODES.get(result["status"], 2)
    quiet = (
        args.quiet_when_healthy
        and result["status"] in ("healthy", "disabled-or-smoke-only")
        and result["fetch_success_band"] != "warn"
    )
    if not quiet:
        print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
