#!/usr/bin/env python3
"""
spend_meter.py — per-provider daily spend meter + threshold alert (86e260vnu, 2026-07-05).

Companion to spend_guard.py (which tracks the GLOBAL daily cap). This meter
breaks spend down by PROVIDER (zai / openai / google / anthropic / minimax) so
Colin can see at a glance which fallback tail is burning the money.

Data sources:

  1. ~/.hermes/state.db  sessions table:
       SELECT billing_provider, COALESCE(actual_cost_usd, estimated_cost_usd, 0) AS cost
       FROM sessions WHERE started_at >= today_epoch
     Hermes gateway / orchestrator sessions.

  2. ~/.hermes/logs/fallback-receipts.jsonl (added 86e260vnu):
     Count quota-driven failovers per provider per day (informational — they
     don't add a $$ figure, just a "this tier burned during fallback" indicator).
     A bad/unreadable line here is still skipped and warned about
     (informational-only signal, not the primary spend figure).

Public API:
    per_provider_spend(today_str=None) -> dict[str, float]
        Raises SpendDataUnavailable if state.db could not be read at all.
    is_over_threshold(cap_usd=10.0, today_str=None) -> list[tuple[str, float]]
        Raises SpendDataUnavailable for the same case — callers must not
        treat "couldn't check" the same as "checked, nobody's over."
    emit_alert(over_providers, cap_usd)         -> prints alert to stdout
    main()                                      -> CLI; --cap N, --date YYYYMMDD

Env:
    HERMES_PROVIDER_DAILY_ALERT_USD   default $10/day/provider
    HERMES_SPEND_METER_DISABLE        "1" => always returns no-over-threshold

(RC, 2026-07-26) A state.db read failure used to be swallowed
(`except Exception: return {}`), which made is_over_threshold() return []  —
indistinguishable from "checked, everyone's under cap." A read failure now
RAISES SpendDataUnavailable instead of silently returning an empty/zero
result; main() turns that into a non-zero exit plus a loud stdout/stderr
message so the `spend-meter` cron job's failure-delivery path
(cron/scheduler.py: non-zero exit -> "Cron watchdog script failed" Slack
alert) actually fires, instead of the run going silent. This script has no
blocking power over agent execution — spend_guard.py owns the $50/day hard
cap that blocks new spend and is fixed separately for the identical root
cause with a fail-closed/stale-cache policy. This meter can only alarm, so
alarming loudly on "can't check" is the correct and sufficient fix here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

HOME = os.path.expanduser("~")
STATE_DB = os.path.join(HOME, ".hermes", "state.db")
FALLBACK_RECEIPTS = os.path.join(HOME, ".hermes", "logs", "fallback-receipts.jsonl")

DEFAULT_CAP = 10.0

# Maps a step_finish `model` string (e.g. "openai/gpt-5") into a provider label.
# Same coarse taxonomy spend_guard already uses, plus the new tiers introduced
# in opencode_exec.py (2026-07-05 / 86e260vnu). Keep in sync with the cascade
# tables in opencode_exec.py.
_MODEL_TO_PROVIDER = {
    "openai/gpt-5.4":            "openai-codex",
    "openai/gpt-5":              "openai",
    "anthropic/claude-opus-4-8": "anthropic",
    "anthropic/claude-sonnet-5": "anthropic",
    "anthropic/claude-sonnet-4-6": "anthropic",
    "zai-coding/glm-5.2":        "zai",
    "zai-coding/glm-4.7":        "zai",
    "google/gemini-3.1-pro-preview": "google-3.1-pro",
    "google/gemini-3.5-flash":   "google-3.5-flash",
    "minimax/MiniMax-M3":        "minimax",
}

# Sessions-table `billing_provider` values map straight to provider labels.
_SESSION_PROVIDER_NORMALIZE = {
    "openai":      "openai",
    "openai-codex": "openai-codex",
    "anthropic":   "anthropic",
    "zai":         "zai",
    "zai-coding":  "zai",
    "google":      "google-3.5-flash",  # sessions table records the provider, not model — bucket together
    "google-decomposer": "google-3.1-pro",
    "google-flash": "google-3.5-flash",
    "minimax":     "minimax",
    "minimax-m3":  "minimax",
}


def _today_str():
    return date.today().strftime("%Y%m%d")


def _today_epoch():
    return datetime.combine(date.today(), datetime.min.time()).timestamp()


class SpendDataUnavailable(Exception):
    """state.db could not be read/queried at all.

    Distinct on purpose from a read that succeeded and found zero sessions
    today: those two states must never be conflated, or the daily-cap alarm
    goes silently blind exactly when it's needed (a broken/locked/missing
    DB looks identical to "nobody spent anything").
    """


def _state_db_provider_spend(today_epoch: float) -> dict[str, float]:
    """Per-provider spend from Hermes gateway/orchestrator sessions (state.db).

    RAISES SpendDataUnavailable if the DB exists but cannot be opened/queried.
    A genuinely missing state.db (e.g. brand-new install, nothing has run
    yet) is a legitimate empty state, not a read failure, and still returns
    an empty dict.
    """
    out: dict[str, float] = defaultdict(float)
    if not os.path.isfile(STATE_DB):
        return out
    try:
        con = sqlite3.connect(STATE_DB)
        try:
            cur = con.cursor()
            # Schema is permissive: pick any provider column + any cost column.
            for row in cur.execute(
                "SELECT billing_provider, COALESCE(actual_cost_usd, estimated_cost_usd, 0) "
                "FROM sessions WHERE started_at >= ?",
                (today_epoch,),
            ):
                bp, cost = row[0], float(row[1] or 0.0)
                norm = _SESSION_PROVIDER_NORMALIZE.get(bp, bp)
                out[norm] += cost
        finally:
            con.close()
    except Exception as e:
        raise SpendDataUnavailable(f"state.db read failed: {e!r}") from e
    return out


def _fallback_quota_count(today_str: str) -> dict[str, int]:
    """Count quota-driven failover receipts per provider today (informational)."""
    out: dict[str, int] = defaultdict(int)
    try:
        if not os.path.isfile(FALLBACK_RECEIPTS):
            return out
        # Accept both YYYYMMDD ("20260705") and ISO ("2026-07-05") date formats —
        # the meter is invoked with the compact form, the ledger uses ISO.
        today_compact = today_str
        today_iso = (f"{today_str[:4]}-{today_str[4:6]}-{today_str[6:]}"
                     if len(today_str) == 8 and today_str.isdigit() else today_str)
        with open(FALLBACK_RECEIPTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = ev.get("ts", "")
                if today_compact not in ts and today_iso not in ts:
                    continue
                if ev.get("reason") != "quota exhausted":
                    continue
                primary = ev.get("primary", "")
                provider = _MODEL_TO_PROVIDER.get(primary, primary.split("/", 1)[0] if "/" in primary else "unknown")
                out[provider] += 1
    except Exception as e:
        sys.stderr.write(f"[spend_meter] fallback receipts read failed (fail-open): {e!r}\n")
    return out


def per_provider_spend(today_str: str | None = None) -> dict[str, float]:
    """Return {provider_label: usd} for today (sessions only).

    Raises SpendDataUnavailable if state.db could not be read — see
    _state_db_provider_spend.
    """
    today_str = today_str or _today_str()
    today_epoch = _today_epoch()
    combined: dict[str, float] = defaultdict(float)
    for k, v in _state_db_provider_spend(today_epoch).items():
        # Normalize None / empty keys to "unknown" so the result is JSON-safe.
        label = k if isinstance(k, str) and k else "unknown"
        combined[label] += float(v or 0.0)
    return dict(combined)


def is_over_threshold(cap_usd: float = DEFAULT_CAP,
                      today_str: str | None = None) -> list[tuple[str, float]]:
    """List of (provider, spend) tuples that are at-or-above the daily cap.

    Raises SpendDataUnavailable if state.db could not be read (propagated
    from per_provider_spend) — an empty [] must mean "checked, nobody's
    over," never "couldn't check."
    """
    if os.environ.get("HERMES_SPEND_METER_DISABLE") == "1":
        return []
    spend = per_provider_spend(today_str)
    return [(p, s) for p, s in sorted(spend.items(), key=lambda kv: -kv[1]) if s >= cap_usd]


def emit_alert(over_providers: list[tuple[str, float]], cap_usd: float) -> None:
    """Print a Slack-compatible alert to stdout (mirrors spend_guard.emit_alert)."""
    if not over_providers:
        return
    lines = [f":warning: Hermes per-provider daily spend alert (cap=${cap_usd:.2f}/provider):"]
    for provider, spend in over_providers:
        lines.append(f"  - `{provider}`: ${spend:.2f}")
    print("\n".join(lines))


def main():
    ap = __import__("argparse").ArgumentParser(
        description="Per-provider daily spend meter + threshold alert (86e260vnu)."
    )
    cap = float(os.environ.get("HERMES_PROVIDER_DAILY_ALERT_USD", DEFAULT_CAP))
    ap.add_argument("--cap", type=float, default=cap,
                    help=f"Per-provider daily alert threshold (default ${cap:.2f})")
    ap.add_argument("--date", default=None, help="YYYYMMDD override (default: today)")
    ap.add_argument("--json", action="store_true",
                    help="Print full per-provider spend as JSON instead of alert-only.")
    args = ap.parse_args()
    today_str = args.date or _today_str()

    if os.environ.get("HERMES_SPEND_METER_DISABLE") == "1":
        if args.json:
            print(json.dumps(
                {"today": today_str, "cap_usd": args.cap, "per_provider_usd": {},
                 "quota_fallback_counts": {}, "disabled": True},
                indent=2, sort_keys=True,
            ))
        return

    try:
        spend = per_provider_spend(today_str)
    except SpendDataUnavailable as e:
        # Never let this look like a quiet/healthy run: print to stdout (so
        # the cron watchdog's non-empty-output delivery path carries it) AND
        # exit non-zero (so scheduler.py's "script exited non-zero -> alert
        # delivery" path fires even if stdout were ever empty). Both together
        # make this alarm testable independent of either single mechanism.
        msg = (
            f"spend_meter DATA UNAVAILABLE for {today_str}: {e}\n"
            "Per-provider daily spend could NOT be read this run. This is "
            "NOT a healthy $0 day — the $10/provider alarm is BLIND until "
            "the next successful read. Check ~/.hermes/state.db readability."
        )
        print(msg)
        sys.stderr.write(f"[spend_meter] ALARM: state_db read failed (not fail-open): {e!r}\n")
        sys.exit(3)

    quota_counts = _fallback_quota_count(today_str)
    if args.json:
        out = {"today": today_str, "cap_usd": args.cap,
               "per_provider_usd": spend,
               "quota_fallback_counts": quota_counts}
        print(json.dumps(out, indent=2, sort_keys=True))
        return
    over = [(p, s) for p, s in sorted(spend.items(), key=lambda kv: -kv[1]) if s >= args.cap]
    if over:
        emit_alert(over, args.cap)
        sys.exit(2)
    # quiet-success path: nothing to do
    if spend:
        sys.stderr.write(
            f"[spend_meter] {today_str} OK — under ${args.cap:.2f}/provider "
            f"(top: {max(spend.items(), key=lambda kv: kv[1])})\n"
        )


if __name__ == "__main__":
    main()