#!/usr/bin/env python3
"""
spend_guard.py — hard daily spend cap for Hermes.

Two spend sources are summed for "today" (midnight-local to now):

  1. ~/.hermes/state.db  sessions table: SUM(COALESCE(actual_cost_usd,
                          estimated_cost_usd, 0)) WHERE started_at >= today_epoch
     This covers Hermes gateway/orchestrator sessions (gpt-5-mini, haiku, minimax, etc.)

  2. ~/.hermes/logs/opencode/<task>-YYYYMMDD-HHMMSS.jsonl  step_finish events:
     SUM(part.cost) from every file whose filename contains today's YYYYMMDD string.
     This covers OpenCode/gpt-5 delegations (the main money-spender).

Public API:
    daily_spend_usd(today_str=None) -> float   # today_str: "YYYYMMDD" or None=today
    is_over_cap(cap_usd=50.0, today_str=None) -> bool
    emit_alert(spend, cap)                      # prints Slack-style alert to stdout

Env:
    HERMES_DAILY_SPEND_CAP_USD          override the $50 default (float)
    HERMES_SPEND_GUARD_DISABLE          "1" => is_over_cap always returns False (off-switch)
    HERMES_SPEND_GUARD_STATE_PATH       override the last-known-good cache path (testing)
    HERMES_SPEND_GUARD_STALENESS_SECONDS  how long a cached figure stays trustworthy
                                         once live reads start failing (default 900s)

(RC, 2026-07-26) `is_over_cap()` — the function that actually gates every
opencode_exec.py delegation — used to catch ALL read/parse/DB errors and
return False ("not over cap"), identically to a genuinely healthy $0 day.
That made the $50/day hard cap silently stop enforcing on any state.db or
opencode-log read hiccup, with nothing distinguishing "checked, you're fine"
from "couldn't check." Fixed with a three-tier policy, chosen because this
function runs synchronously before *every* delegation (can be many times/hour
under swarm load) and gates real work, so a naive fail-closed-on-any-blip
would halt the fleet on transient noise, while naive fail-open (the old
behavior) can silently overspend for an unbounded outage:

  1. Read succeeds -> normal over/under-cap comparison (byte-identical to
     the old behavior), and the figure is cached as last-known-good.
  2. Read fails, but a last-known-good figure exists within
     HERMES_SPEND_GUARD_STALENESS_SECONDS (default 900s / 15min) -> ALARM
     loudly (stderr + a distinct Slack-style stdout block) and decide from
     that recent cached figure. Bridges a transient sqlite lock or disk
     hiccup without halting the fleet on noise.
  3. Read fails and there is no usable last-known-good (first run, or the
     outage has outlasted the staleness bound) -> ALARM loudly and FAIL
     CLOSED (treat as over-cap, block new spend). Zero reliable signal is
     exactly the case a "fail open forever" guard would silently overspend
     through; a visible false-positive block is the safer failure mode, and
     it self-heals the moment a read succeeds again.

`daily_spend_usd()`, `_state_db_spend()`, and `_opencode_log_spend()` keep
their EXACT original fail-open (returns 0.0 on error) contract — they are
still used by opencode_exec.py's block-message builder and this file's own
CLI, and must not raise. The new tiered policy lives entirely in
`is_over_cap()`, via internal `_state_db_spend_strict()` /
`_opencode_log_spend_strict()` / `_daily_spend_usd_strict()` helpers that
raise `SpendDataUnavailable` instead of substituting 0.0.
"""

# 2026-06-24: the cron executor runs opencode_exec.py (and thus imports this
# module) under system /usr/bin/python3 == 3.9.6, where PEP 604 `str | None`
# annotations raise TypeError at def-eval time. That made every spend_guard
# import fail -> opencode_exec fail-open -> the $50/day cap silently never
# enforced. Deferring annotation evaluation (PEP 563) keeps the unions as
# strings so this module imports cleanly on 3.9+ as well as the 3.11 venv.
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime

HOME = os.path.expanduser("~")
STATE_DB = os.path.join(HOME, ".hermes", "state.db")
OC_LOG_DIR = os.path.join(HOME, ".hermes", "logs", "opencode")

_DEFAULT_CAP = 50.0

# Where is_over_cap() persists the last successfully-read total, so a read
# failure can serve a recent, trustworthy figure instead of jumping straight
# to fail-closed on a single transient blip. Override for tests so a test
# run never reads or clobbers the live guard's cache/decision state.
LAST_KNOWN_PATH = os.environ.get("HERMES_SPEND_GUARD_STATE_PATH") or os.path.join(
    HOME, ".hermes", "state", "spend_guard_last_known.json"
)
CACHE_STALENESS_S = float(os.environ.get("HERMES_SPEND_GUARD_STALENESS_SECONDS", "900"))


class SpendDataUnavailable(Exception):
    """A spend data source could not be read at all — distinct from a source
    that read cleanly and found $0. Raised only by the *_strict() helpers
    used internally by is_over_cap(); the public daily_spend_usd() keeps its
    original fail-open (returns 0.0) contract for backward compatibility."""


def _today_str():
    """Return today's date as 'YYYYMMDD' (local time)."""
    return date.today().strftime("%Y%m%d")


def _today_epoch():
    """Return unix epoch for local midnight today (start of day)."""
    return datetime.combine(date.today(), datetime.min.time()).timestamp()


def _state_db_spend(today_epoch: float) -> float:
    """Sum costs from state.db sessions starting on or after today_epoch.

    Returns 0.0 on any error (fail-open).
    """
    try:
        conn = sqlite3.connect(STATE_DB, timeout=5)
        row = conn.execute(
            """
            SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0)
            FROM sessions
            WHERE started_at >= ?
            """,
            (today_epoch,),
        ).fetchone()
        conn.close()
        return float(row[0] or 0.0)
    except Exception as e:
        print(f"[spend_guard] WARN: state.db read failed (fail-open): {e!r}", file=sys.stderr)
        return 0.0


def _opencode_log_spend(today_str: str) -> float:
    """Sum step_finish.part.cost from opencode JSONL logs for today.

    Log filenames contain the date as YYYYMMDD (e.g. taskid-20260623-154853.jsonl).
    Returns 0.0 on any error (fail-open).
    """
    total = 0.0
    try:
        pattern = os.path.join(OC_LOG_DIR, f"*{today_str}*.jsonl")
        for log_path in glob.glob(pattern):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if ev.get("type") == "step_finish":
                            cost = ev.get("part", {}).get("cost")
                            if cost is not None:
                                total += float(cost)
            except Exception as e:
                print(
                    f"[spend_guard] WARN: failed reading {log_path} (skipping, fail-open): {e!r}",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"[spend_guard] WARN: opencode log glob failed (fail-open): {e!r}", file=sys.stderr)
    return total


def _state_db_spend_strict(today_epoch: float) -> float:
    """Like _state_db_spend but RAISES SpendDataUnavailable instead of
    silently substituting 0.0. Used only by is_over_cap()'s guard decision."""
    try:
        conn = sqlite3.connect(STATE_DB, timeout=5)
        try:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 0)
                FROM sessions
                WHERE started_at >= ?
                """,
                (today_epoch,),
            ).fetchone()
        finally:
            conn.close()
        return float(row[0] or 0.0)
    except Exception as e:
        raise SpendDataUnavailable(f"state.db read failed: {e!r}") from e


def _opencode_log_spend_strict(today_str: str) -> float:
    """Like _opencode_log_spend but RAISES SpendDataUnavailable if the log
    directory itself cannot be enumerated. A single corrupt/unreadable
    individual log FILE within an otherwise-listable directory stays a
    partial fail-open (skipped + warned) — losing one file's figures is a
    different, much lower-severity failure than losing the ability to read
    spend data at all."""
    total = 0.0
    try:
        pattern = os.path.join(OC_LOG_DIR, f"*{today_str}*.jsonl")
        log_paths = glob.glob(pattern)
    except Exception as e:
        raise SpendDataUnavailable(f"opencode log glob failed: {e!r}") from e
    for log_path in log_paths:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "step_finish":
                        cost = ev.get("part", {}).get("cost")
                        if cost is not None:
                            total += float(cost)
        except Exception as e:
            print(
                f"[spend_guard] WARN: failed reading {log_path} (skipping, fail-open): {e!r}",
                file=sys.stderr,
            )
    return total


def _daily_spend_usd_strict(today_str: str | None = None) -> float:
    """Like daily_spend_usd but RAISES SpendDataUnavailable if either source
    is unreadable, instead of silently substituting 0.0."""
    if today_str is None:
        today_str = _today_str()
    try:
        d = datetime.strptime(today_str, "%Y%m%d").date()
        epoch = datetime.combine(d, datetime.min.time()).timestamp()
    except Exception:
        epoch = _today_epoch()
    db_spend = _state_db_spend_strict(epoch)
    oc_spend = _opencode_log_spend_strict(today_str)
    return db_spend + oc_spend


def _load_last_known() -> tuple[float | None, float | None]:
    try:
        with open(LAST_KNOWN_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return float(d["spend_usd"]), float(d["ts"])
    except Exception:
        return None, None


def _save_last_known(spend_usd: float) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_KNOWN_PATH), exist_ok=True)
        tmp = LAST_KNOWN_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"spend_usd": spend_usd, "ts": time.time()}, f)
        os.replace(tmp, LAST_KNOWN_PATH)
    except Exception as e:
        # Best-effort: losing the cache just means the next outage fails
        # closed a little sooner (no last-known-good left to serve).
        print(f"[spend_guard] WARN: could not persist last-known-good cache: {e!r}", file=sys.stderr)


def _alarm(msg: str) -> None:
    print(msg, file=sys.stderr)


def emit_unavailable_alert(err: object, cap_usd: float, blocked: bool) -> None:
    """Print a Slack-compatible alert when spend DATA ITSELF is unreadable.

    Deliberately distinct wording from emit_alert() (which fires on a real
    over-cap figure) so an unreadable guard is never confused with, or
    silently downgraded from, a healthy under-cap run.
    """
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    verdict = ("BLOCKING new spend (fail-closed — no reliable figure available)"
               if blocked else "serving a recent last-known-good figure, spend allowed")
    lines = [
        "🚨 *Hermes spend guard could not read today's spend data.*",
        f"_{when}_  {err}",
        f"Verdict: {verdict}",
        "_This is NOT a healthy $0.00 day — investigate state.db / opencode log readability._",
    ]
    print("\n".join(lines))


def daily_spend_usd(today_str: str | None = None) -> float:
    """Return today's total spend in USD (state.db + opencode logs).

    FAIL-OPEN: returns 0.0 on any read/parse error so a bug never halts work.
    """
    if today_str is None:
        today_str = _today_str()
    # Derive epoch from today_str for the DB query
    try:
        d = datetime.strptime(today_str, "%Y%m%d").date()
        epoch = datetime.combine(d, datetime.min.time()).timestamp()
    except Exception:
        epoch = _today_epoch()

    db_spend = _state_db_spend(epoch)
    oc_spend = _opencode_log_spend(today_str)
    total = db_spend + oc_spend
    return total


def is_over_cap(cap_usd: float = _DEFAULT_CAP, today_str: str | None = None) -> bool:
    """Return True iff today's spend exceeds cap_usd, OR the guard could not
    establish a reliable reading and must fail closed.

    Three distinguishable outcomes on a read failure (never silently
    "healthy" — see the module docstring for the full policy rationale):
      1. Read succeeds -> ordinary over/under-cap comparison (unchanged).
      2. Read fails, last-known-good exists within CACHE_STALENESS_S ->
         ALARM + decide from the cached figure.
      3. Read fails, no usable last-known-good -> ALARM + FAIL CLOSED
         (return True / blocked).

    Off-switch: HERMES_SPEND_GUARD_DISABLE=1 always returns False (unchanged).
    A bug in this function's OWN control flow (not a recognized data-read
    failure) keeps the original fail-open contract — a bug here must never
    by itself halt the fleet — but is now a loud ALARM, not a quiet WARN.
    """
    if os.environ.get("HERMES_SPEND_GUARD_DISABLE") == "1":
        return False
    try:
        spend = _daily_spend_usd_strict(today_str)
    except SpendDataUnavailable as e:
        last_spend, last_ts = _load_last_known()
        now = time.time()
        if last_spend is not None and last_ts is not None and (now - last_ts) <= CACHE_STALENESS_S:
            age_s = now - last_ts
            over = last_spend > cap_usd
            _alarm(
                f"[spend_guard] ALARM: spend data unreadable ({e}); serving last-known-good "
                f"${last_spend:.2f} from {age_s:.0f}s ago (staleness bound {CACHE_STALENESS_S:.0f}s), "
                f"over_cap={over}"
            )
            emit_unavailable_alert(e, cap_usd, blocked=False)
            if over:
                emit_alert(last_spend, cap_usd)
            return over
        _alarm(
            f"[spend_guard] ALARM: spend data unreadable ({e}) and no last-known-good within "
            f"{CACHE_STALENESS_S:.0f}s — FAILING CLOSED (blocking new spend)."
        )
        emit_unavailable_alert(e, cap_usd, blocked=True)
        return True
    except Exception as e:
        _alarm(f"[spend_guard] ALARM: unexpected error in is_over_cap (fail-open -> not over cap): {e!r}")
        return False

    _save_last_known(spend)
    over = spend > cap_usd
    if over:
        emit_alert(spend, cap_usd)
    return over


def emit_alert(spend: float, cap: float) -> None:
    """Print a Slack-compatible alert to stdout.

    Stdout is delivered to Slack by the Hermes cron job (same mechanism as
    hermes_usage_alert.py). On a normal (under-cap) run, this function is
    never called so stdout stays empty = silent.
    """
    when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "🛑 *Hermes daily spend cap reached — new expensive work HALTED.*",
        f"_{when}_  Spend today: *${spend:.2f}* / cap ${cap:.2f}",
        "",
        "OpenCode delegations (gpt-5) are blocked until midnight (local).",
        "To override: set `HERMES_SPEND_GUARD_DISABLE=1` in Doppler or `HERMES_DAILY_SPEND_CAP_USD=<higher>` and restart the gateway.",
        "",
        "_Check `~/.hermes/logs/opencode/` and state.db for breakdown._",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    # CLI: print today's spend for debugging
    import argparse

    ap = argparse.ArgumentParser(description="Report today's Hermes spend.")
    ap.add_argument("--cap", type=float, default=float(os.environ.get("HERMES_DAILY_SPEND_CAP_USD", _DEFAULT_CAP)))
    ap.add_argument("--date", default=None, help="YYYYMMDD override (default: today)")
    args = ap.parse_args()

    today_str = args.date or _today_str()
    d = date.today() if args.date is None else datetime.strptime(args.date, "%Y%m%d").date()
    epoch = datetime.combine(d, datetime.min.time()).timestamp()
    db_s = _state_db_spend(epoch)
    oc_s = _opencode_log_spend(today_str)
    total = db_s + oc_s
    over = total > args.cap

    print(f"Spend date:        {today_str}")
    print(f"  state.db:        ${db_s:.4f}")
    print(f"  opencode logs:   ${oc_s:.4f}")
    print(f"  TOTAL:           ${total:.4f}")
    print(f"  Cap:             ${args.cap:.2f}")
    print(f"  Over cap:        {over}")
    if over:
        print()
        emit_alert(total, args.cap)
    sys.exit(1 if over else 0)
