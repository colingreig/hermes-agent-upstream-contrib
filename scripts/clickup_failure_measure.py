#!/usr/bin/env python3
"""Quantify ClickUp logical-call failure rates over explicit UTC windows.

This tool only reports retained observations. It refuses to label a change a
material improvement when either window is empty or the baseline contains no
terminal failures; zero failures cannot establish a positive reduction.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "clickup-client-call/v1"
EXPECTED_CLIENT = "clickup_poll_gate"
VALID_OUTCOMES = frozenset({"success", "recovered", "failure"})
DEFAULT_EVENTS = Path(
    os.path.expanduser(
        os.environ.get(
            "HERMES_CLICKUP_CALL_METRICS_PATH",
            "~/.hermes/state/clickup-client-calls.jsonl",
        )
    )
)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def load_events(path: Path) -> list[dict]:
    events = []
    rotated = []
    prefix = path.name + "."
    for candidate in path.parent.glob(prefix + "*"):
        suffix = candidate.name[len(prefix) :]
        if suffix.isdigit():
            rotated.append((int(suffix), candidate))
    # Highest generation is oldest. Preserve chronological file order before
    # reading the active stream so retained windows survive rotation.
    sources = [candidate for _, candidate in sorted(rotated, reverse=True)] + [path]
    for source_path in sources:
        if not source_path.exists():
            continue
        with source_path.open(encoding="utf-8") as source:
            for line in source:
                try:
                    event = json.loads(line)
                    attempts = event.get("attempts")
                    if (
                        event.get("schema") == SCHEMA
                        and event.get("client") == EXPECTED_CLIENT
                        and event.get("outcome") in VALID_OUTCOMES
                        and isinstance(attempts, int)
                        and not isinstance(attempts, bool)
                        and attempts >= 1
                    ):
                        _parse_timestamp(event["timestamp"])
                        events.append(event)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
    return events


def _summarize(events: list[dict], start: datetime, end: datetime) -> dict:
    selected = [
        event
        for event in events
        if start <= _parse_timestamp(event["timestamp"]) < end
    ]
    calls = len(selected)
    terminal_failures = sum(event.get("outcome") == "failure" for event in selected)
    recovered_calls = sum(event.get("outcome") == "recovered" for event in selected)
    classes = Counter(
        event.get("failure_class")
        for event in selected
        if event.get("failure_class")
    )
    return {
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "calls": calls,
        "terminal_failures": terminal_failures,
        "recovered_calls": recovered_calls,
        "failure_rate": terminal_failures / calls if calls else None,
        "failure_classes": dict(sorted(classes.items())),
    }


def compare_windows(
    events: list[dict],
    *,
    before_start: datetime,
    before_end: datetime,
    after_start: datetime,
    after_end: datetime,
    material_reduction: float = 0.5,
) -> dict:
    window_values = (before_start, before_end, after_start, after_end)
    if any(value.tzinfo is None or value.utcoffset() is None for value in window_values):
        raise ValueError("measurement windows must use timezone-aware datetimes")
    if not (before_start < before_end and after_start < after_end):
        raise ValueError("each measurement window must have positive duration")
    if before_end > after_start or before_start >= after_start:
        raise ValueError("measurement windows must be ordered and non-overlapping")
    if before_end - before_start != after_end - after_start:
        raise ValueError("measurement windows must have equal duration")
    if not 0 <= material_reduction <= 1:
        raise ValueError("material reduction threshold must be between 0 and 1")
    before = _summarize(events, before_start, before_end)
    after = _summarize(events, after_start, after_end)
    if not before["calls"]:
        raise ValueError("before window has no retained calls")
    if not after["calls"]:
        raise ValueError("after window has no retained calls")

    report = {
        "schema": "clickup-failure-comparison/v1",
        "before": before,
        "after": after,
        "material_reduction_threshold": material_reduction,
        "relative_failure_rate_reduction": None,
        "material_improvement": None,
        "comparison_status": "insufficient_baseline_failures",
    }
    if before["terminal_failures"]:
        reduction = (before["failure_rate"] - after["failure_rate"]) / before["failure_rate"]
        report.update(
            relative_failure_rate_reduction=reduction,
            material_improvement=reduction >= material_reduction,
            comparison_status="measured",
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS)
    parser.add_argument("--before-start", required=True, type=_parse_timestamp)
    parser.add_argument("--before-end", required=True, type=_parse_timestamp)
    parser.add_argument("--after-start", required=True, type=_parse_timestamp)
    parser.add_argument("--after-end", required=True, type=_parse_timestamp)
    parser.add_argument("--material-reduction", type=float, default=0.5)
    args = parser.parse_args()
    try:
        report = compare_windows(
            load_events(args.events),
            before_start=args.before_start,
            before_end=args.before_end,
            after_start=args.after_start,
            after_end=args.after_end,
            material_reduction=args.material_reduction,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["comparison_status"] == "measured" else 3


if __name__ == "__main__":
    raise SystemExit(main())