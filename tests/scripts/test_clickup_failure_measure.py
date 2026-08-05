from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.clickup_failure_measure import compare_windows, load_events


def _event(ts: str, outcome: str, attempts: int = 1, failure_class: str | None = None):
    return {
        "schema": "clickup-client-call/v1",
        "timestamp": ts,
        "client": "clickup_poll_gate",
        "outcome": outcome,
        "attempts": attempts,
        "failure_class": failure_class,
    }


def test_load_events_ignores_unrelated_and_malformed_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "not-json\n"
        + json.dumps({"schema": "other/v1"})
        + "\n"
        + json.dumps(_event("2026-08-03T10:00:00Z", "success"))
        + "\n",
        encoding="utf-8",
    )

    events = load_events(path)

    assert len(events) == 1
    assert events[0]["client"] == "clickup_poll_gate"


def test_load_events_filters_unknown_clients_outcomes_and_invalid_attempts(tmp_path):
    path = tmp_path / "events.jsonl"
    records = [
        _event("2026-08-03T10:00:00Z", "success"),
        {**_event("2026-08-03T10:01:00Z", "success"), "client": "other_client"},
        _event("2026-08-03T10:02:00Z", "future_outcome"),
        _event("2026-08-03T10:03:00Z", "failure", attempts=0),
        _event("2026-08-03T10:04:00Z", "failure", attempts=True),
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    events = load_events(path)

    assert events == [records[0]]


def test_load_events_includes_rotated_generations(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(_event("2026-08-04T10:00:00Z", "success")) + "\n")
    (tmp_path / "events.jsonl.1").write_text(
        json.dumps(_event("2026-08-03T10:00:00Z", "failure", 4)) + "\n"
    )

    events = load_events(path)

    assert [event["outcome"] for event in events] == ["failure", "success"]


def test_compare_windows_reports_counts_rates_and_material_improvement():
    events = [
        _event("2026-08-03T10:00:00Z", "failure", 4, "http_503"),
        _event("2026-08-03T11:00:00Z", "success"),
        _event("2026-08-04T10:00:00Z", "recovered", 2, "http_503"),
        _event("2026-08-04T11:00:00Z", "success"),
    ]

    report = compare_windows(
        events,
        before_start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        before_end=datetime(2026, 8, 4, tzinfo=timezone.utc),
        after_start=datetime(2026, 8, 4, tzinfo=timezone.utc),
        after_end=datetime(2026, 8, 5, tzinfo=timezone.utc),
        material_reduction=0.5,
    )

    assert report["before"]["calls"] == 2
    assert report["before"]["terminal_failures"] == 1
    assert report["before"]["failure_rate"] == 0.5
    assert report["after"]["calls"] == 2
    assert report["after"]["terminal_failures"] == 0
    assert report["after"]["recovered_calls"] == 1
    assert report["after"]["failure_rate"] == 0.0
    assert report["relative_failure_rate_reduction"] == 1.0
    assert report["material_improvement"] is True
    assert report["comparison_status"] == "measured"


def test_compare_windows_refuses_to_infer_improvement_from_zero_failure_baseline():
    events = [
        _event("2026-08-03T10:00:00Z", "success"),
        _event("2026-08-04T10:00:00Z", "success"),
    ]

    report = compare_windows(
        events,
        before_start=datetime(2026, 8, 3, tzinfo=timezone.utc),
        before_end=datetime(2026, 8, 4, tzinfo=timezone.utc),
        after_start=datetime(2026, 8, 4, tzinfo=timezone.utc),
        after_end=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )

    assert report["material_improvement"] is None
    assert report["relative_failure_rate_reduction"] is None
    assert report["comparison_status"] == "insufficient_baseline_failures"


def test_compare_windows_refuses_empty_window():
    with pytest.raises(ValueError, match="after window has no retained calls"):
        compare_windows(
            [_event("2026-08-03T10:00:00Z", "failure", 4, "timeout")],
            before_start=datetime(2026, 8, 3, tzinfo=timezone.utc),
            before_end=datetime(2026, 8, 4, tzinfo=timezone.utc),
            after_start=datetime(2026, 8, 4, tzinfo=timezone.utc),
            after_end=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("before_start", "before_end", "after_start", "after_end", "message"),
    [
        (
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 6, tzinfo=timezone.utc),
            "ordered and non-overlapping",
        ),
        (
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            "ordered and non-overlapping",
        ),
        (
            datetime(2026, 8, 3, tzinfo=timezone.utc),
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 4, tzinfo=timezone.utc),
            datetime(2026, 8, 6, tzinfo=timezone.utc),
            "equal duration",
        ),
    ],
)
def test_compare_windows_requires_comparable_ordered_non_overlapping_windows(
    before_start, before_end, after_start, after_end, message
):
    with pytest.raises(ValueError, match=message):
        compare_windows(
            [],
            before_start=before_start,
            before_end=before_end,
            after_start=after_start,
            after_end=after_end,
        )


def test_compare_windows_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        compare_windows(
            [],
            before_start=datetime(2026, 8, 3),
            before_end=datetime(2026, 8, 4),
            after_start=datetime(2026, 8, 4),
            after_end=datetime(2026, 8, 5),
        )
