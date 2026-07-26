from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


monitor = _load("research_stage_monitor")


NOW = datetime(2026, 7, 24, 18, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _isolate_monitor_state(tmp_path, monkeypatch):
    """CLI tests must never touch the operator's real cross-run state."""
    monkeypatch.setattr(monitor, "DEFAULT_STATE", tmp_path / "monitor-state.json")


def _record(
    task_id: str,
    *,
    served: bool,
    degraded: bool,
    minutes_ago: float = 10,
    search_failed: bool | None = None,
    grounded_pages: int | None = None,
    attempted_fetches: int | None = None,
    smoke: bool | None = None,
    search_provider: str | None = None,
) -> dict:
    ts = datetime.fromtimestamp(NOW - minutes_ago * 60, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    record = {
        "ts": ts,
        "enabled": True,
        "served": served,
        "degraded": degraded,
        "outcome": "served" if served else "analyzer-fallback",
        "task_id": task_id,
    }
    # Only add the new (post-narrowing) ledger fields when explicitly
    # provided, so tests that omit them exercise the legacy-record /
    # missing-field tolerance path exactly like a pre-migration ledger.
    if search_failed is not None:
        record["search_failed"] = search_failed
    if grounded_pages is not None:
        record["grounded_pages"] = grounded_pages
    if attempted_fetches is not None:
        record["attempted_fetches"] = attempted_fetches
    if smoke is not None:
        record["smoke"] = smoke
    if search_provider is not None:
        record["search_provider"] = search_provider
    return record


def test_provider_incidents_are_attributed_and_aggregated_for_30_days():
    records = [
        _record(
            f"scrapingbee-failure-{index}",
            served=False,
            degraded=True,
            minutes_ago=20 - index,
            search_failed=True,
            search_provider="scrapingbee",
        )
        for index in range(3)
    ] + [
        _record(
            f"tavily-failure-{index}",
            served=False,
            degraded=True,
            minutes_ago=8 * 60 + 20 - index,
            search_failed=True,
            search_provider="tavily",
        )
        for index in range(3)
    ] + [
        _record(
            "legacy-failure",
            served=False,
            degraded=True,
            minutes_ago=30,
            search_failed=True,
        )
    ]

    result = monitor.evaluate(records, **_healthy_defaults())

    assert result["provider_search_outage_incident_counts_30d"] == {
        "scrapingbee": 1,
        "tavily": 1,
    }
    assert result["unattributed_search_failures_30d"] == 1
    incidents = result["provider_search_outage_incidents_30d"]
    assert [(incident["provider"], incident["search_failure_receipts"]) for incident in incidents] == [
        ("scrapingbee", 3),
        ("tavily", 3),
    ]


def test_provider_incident_clusters_do_not_double_count_one_outage():
    records = [
        _record(
            f"failure-{index}",
            served=False,
            degraded=True,
            minutes_ago=90 - index * 30,
            search_failed=True,
            search_provider="scrapingbee",
        )
        for index in range(4)
    ]

    result = monitor.evaluate(records, **_healthy_defaults())

    assert result["provider_search_outage_incident_counts_30d"] == {"scrapingbee": 1}
    assert result["provider_search_outage_incidents_30d"][0]["search_failure_receipts"] == 4


def test_ungrounded_or_fetch_degraded_receipts_are_not_provider_outages():
    records = [
        {
            **_record(
                f"ungrounded-{index}",
                served=False,
                degraded=True,
                minutes_ago=20 - index,
                search_failed=False,
                search_provider="scrapingbee",
            ),
            "grounded_pages": 0,
            "attempted_fetches": 2,
            "failure_class": "page-blocked-http",
        }
        for index in range(3)
    ] + [
        {
            **_record(
                f"fetch-degraded-{index}",
                served=True,
                degraded=False,
                minutes_ago=40 - index,
                search_failed=False,
                search_provider="scrapingbee",
            ),
            "grounded_pages": 0,
            "attempted_fetches": 3,
            "failure_class": "page-blocked-http",
        }
        for index in range(3)
    ]

    result = monitor.evaluate(records, **_healthy_defaults())

    assert result["provider_search_outage_incidents_30d"] == []
    assert result["provider_search_outage_incident_counts_30d"] == {}


def test_below_min_attempts_reports_insufficient_data_not_degraded():
    # This mirrors the real false-alarm: a handful of real attempts, all
    # degraded, well below the sample floor.
    records = [_record(f"real-{i}", served=False, degraded=True) for i in range(5)]
    result = monitor.evaluate(
        records,
        now=NOW,
        lookback_hours=48,
        min_served_rate=0.8,
        max_degraded_rate=0.5,
        min_attempts=20,
    )
    assert result["status"] == "insufficient-data"
    assert result["status"] != "degraded"
    assert result["enabled_attempts"] == 5
    # Rates are still reported for information even though the sample is
    # too small to alarm on.
    assert result["degraded_rate"] == 1.0
    assert result["served_rate"] == 0.0


def test_smoke_and_test_task_ids_excluded_from_production_rates():
    smoke_records = [
        _record("86e25xww8-fetch-smoke", served=False, degraded=True),
        _record("ignite-smoke-86e25xww8-recovery-1", served=False, degraded=True),
        _record("ignite-smoke-86e25xww8-recovery-final", served=False, degraded=True),
        _record("nightly-TEST-run-9", served=False, degraded=True),
    ]
    # Sufficient healthy production traffic alongside the smoke burst.
    real_records = [_record(f"prod-{i}", served=True, degraded=False) for i in range(20)]
    records = smoke_records + real_records
    result = monitor.evaluate(
        records,
        now=NOW,
        lookback_hours=48,
        min_served_rate=0.8,
        max_degraded_rate=0.5,
        min_attempts=20,
    )
    assert result["smoke_attempts"] == 4
    assert result["enabled_attempts"] == 20
    assert result["degraded_attempts"] == 0
    assert result["degraded_rate"] == 0.0
    assert result["served_rate"] == 1.0
    assert result["status"] == "healthy"


def test_genuine_degraded_window_with_sufficient_real_attempts_stays_degraded():
    # 20 real attempts, 15 of them degraded -> 0.75 well over the 0.5 max,
    # comfortably above the min-attempts floor. Must still alarm.
    degraded_records = [_record(f"prod-bad-{i}", served=False, degraded=True) for i in range(15)]
    healthy_records = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(5)]
    records = degraded_records + healthy_records
    result = monitor.evaluate(
        records,
        now=NOW,
        lookback_hours=48,
        min_served_rate=0.8,
        max_degraded_rate=0.5,
        min_attempts=20,
    )
    assert result["enabled_attempts"] == 20
    assert result["smoke_attempts"] == 0
    assert result["degraded_rate"] == 0.75
    assert result["status"] == "degraded"


def test_empty_or_missing_ledger_is_not_observed():
    result = monitor.evaluate(
        [],
        now=NOW,
        lookback_hours=48,
        min_served_rate=0.8,
        max_degraded_rate=0.5,
        min_attempts=20,
    )
    assert result["status"] == "not-observed"
    assert result["recent_receipts"] == 0

    missing_path = Path("/tmp/definitely-does-not-exist-research-served.jsonl")
    assert not missing_path.exists()
    records = monitor.read_records(missing_path)
    assert records == []
    result_missing = monitor.evaluate(
        records,
        now=NOW,
        lookback_hours=48,
        min_served_rate=0.8,
        max_degraded_rate=0.5,
        min_attempts=20,
    )
    assert result_missing["status"] == "not-observed"


def test_persistently_inconclusive_escalates_after_72_hours_across_statuses(
    tmp_path,
):
    state_path = tmp_path / "monitor-state.json"
    first = monitor.apply_inconclusive_escalation(
        {"status": "not-observed"},
        state_path=state_path,
        now=NOW,
    )
    assert first["status"] == "not-observed"
    assert first["inconclusive_hours"] == 0.0

    at_boundary = monitor.apply_inconclusive_escalation(
        {"status": "insufficient-data"},
        state_path=state_path,
        now=NOW + 72 * 3600,
    )
    assert at_boundary["status"] == "insufficient-data"
    assert at_boundary["inconclusive_hours"] == 72.0

    escalated = monitor.apply_inconclusive_escalation(
        {"status": "insufficient-data"},
        state_path=state_path,
        now=NOW + 72 * 3600 + 1,
    )
    assert escalated["status"] == "persistently-inconclusive"
    assert escalated["underlying_status"] == "insufficient-data"
    assert escalated["inconclusive_hours"] > 72.0
    assert monitor._EXIT_CODES[escalated["status"]] == 6


def test_conclusive_status_resets_persistently_inconclusive_timer(tmp_path):
    state_path = tmp_path / "monitor-state.json"
    monitor.apply_inconclusive_escalation(
        {"status": "not-observed"},
        state_path=state_path,
        now=NOW,
    )
    monitor.apply_inconclusive_escalation(
        {"status": "healthy"},
        state_path=state_path,
        now=NOW + 80 * 3600,
    )
    restarted = monitor.apply_inconclusive_escalation(
        {"status": "insufficient-data"},
        state_path=state_path,
        now=NOW + 81 * 3600,
    )
    assert restarted["status"] == "insufficient-data"
    assert restarted["inconclusive_hours"] == 0.0
    assert restarted["inconclusive_since"] == datetime.fromtimestamp(
        NOW + 81 * 3600, timezone.utc
    ).isoformat()


def test_corrupt_state_fails_open_and_restarts_timer(tmp_path):
    state_path = tmp_path / "monitor-state.json"
    state_path.write_text("{not-json", encoding="utf-8")
    result = monitor.apply_inconclusive_escalation(
        {"status": "not-observed"},
        state_path=state_path,
        now=NOW,
    )
    assert result["status"] == "not-observed"
    assert result["inconclusive_hours"] == 0.0
    persisted = monitor.json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["first_inconclusive_at"] == result["inconclusive_since"]


def test_cli_emits_persistently_inconclusive_escalation_and_exit_6(
    tmp_path,
    capsys,
):
    ledger = tmp_path / "missing.jsonl"
    state_path = tmp_path / "monitor-state.json"
    base_args = [
        "--ledger",
        str(ledger),
        "--state",
        str(state_path),
        "--quiet-when-healthy",
    ]

    first_rc = monitor.main(base_args + ["--now", "2026-07-20T00:00:00Z"])
    first = monitor.json.loads(capsys.readouterr().out)
    assert first_rc == 3
    assert first["status"] == "not-observed"

    escalated_rc = monitor.main(base_args + ["--now", "2026-07-23T00:00:01Z"])
    escalated = monitor.json.loads(capsys.readouterr().out)
    assert escalated_rc == 6
    assert escalated["status"] == "persistently-inconclusive"
    assert escalated["underlying_status"] == "not-observed"
    assert escalated["inconclusive_hours"] > 72.0


def test_disabled_or_smoke_only_window():
    # Every enabled attempt in the window is smoke/test traffic -- no
    # production signal at all, so it must not read as degraded/healthy.
    records = [
        _record("nightly-smoke-1", served=False, degraded=True),
        _record("recovery-drill-2", served=True, degraded=False),
    ]
    result = monitor.evaluate(
        records,
        now=NOW,
        lookback_hours=48,
        min_served_rate=0.8,
        max_degraded_rate=0.5,
        min_attempts=20,
    )
    assert result["enabled_attempts"] == 0
    assert result["smoke_attempts"] == 2
    assert result["status"] == "disabled-or-smoke-only"


def test_exit_codes_map_to_each_documented_status(tmp_path, capsys):
    empty_ledger = tmp_path / "missing.jsonl"

    rc_not_observed = monitor.main(
        ["--ledger", str(empty_ledger), "--now", "2026-07-24T18:00:00Z"]
    )
    result_not_observed = capsys.readouterr().out
    assert rc_not_observed == 3
    assert '"status": "not-observed"' in result_not_observed

    insufficient_ledger = tmp_path / "insufficient.jsonl"
    lines = [
        monitor.json.dumps(_record(f"real-{i}", served=False, degraded=True))
        for i in range(3)
    ]
    insufficient_ledger.write_text("\n".join(lines) + "\n")
    rc_insufficient = monitor.main(
        [
            "--ledger",
            str(insufficient_ledger),
            "--now",
            "2026-07-24T18:00:00Z",
            "--min-attempts",
            "20",
        ]
    )
    result_insufficient = capsys.readouterr().out
    assert rc_insufficient == 4
    assert '"status": "insufficient-data"' in result_insufficient

    degraded_ledger = tmp_path / "degraded.jsonl"
    degraded_lines = [
        monitor.json.dumps(_record(f"prod-bad-{i}", served=False, degraded=True))
        for i in range(20)
    ]
    degraded_ledger.write_text("\n".join(degraded_lines) + "\n")
    rc_degraded = monitor.main(
        [
            "--ledger",
            str(degraded_ledger),
            "--now",
            "2026-07-24T18:00:00Z",
            "--min-attempts",
            "20",
        ]
    )
    result_degraded = capsys.readouterr().out
    assert rc_degraded == 2
    assert '"status": "degraded"' in result_degraded

    healthy_ledger = tmp_path / "healthy.jsonl"
    healthy_lines = [
        monitor.json.dumps(_record(f"prod-ok-{i}", served=True, degraded=False))
        for i in range(20)
    ]
    healthy_ledger.write_text("\n".join(healthy_lines) + "\n")
    rc_healthy = monitor.main(
        [
            "--ledger",
            str(healthy_ledger),
            "--now",
            "2026-07-24T18:00:00Z",
            "--min-attempts",
            "20",
        ]
    )
    result_healthy = capsys.readouterr().out
    assert rc_healthy == 0
    assert '"status": "healthy"' in result_healthy


def _healthy_defaults():
    return {
        "now": NOW,
        "lookback_hours": 48,
        "min_served_rate": 0.8,
        "max_degraded_rate": 0.15,
        "min_attempts": 20,
    }


def test_new_default_max_degraded_rate_is_material_not_any_block():
    # 4/20 = 0.2 degraded. Under the pre-narrowing default (0.5) this would
    # have read healthy; under the new material-only default (0.15) it
    # must alarm as 'degraded'.
    degraded_records = [_record(f"prod-bad-{i}", served=False, degraded=True) for i in range(4)]
    served_records = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(16)]
    result = monitor.evaluate(degraded_records + served_records, **_healthy_defaults())
    assert result["degraded_rate"] == 0.2
    assert result["max_degraded_rate"] == 0.15
    assert result["status"] == "degraded"


def test_cli_default_max_degraded_rate_via_main(tmp_path, capsys):
    ledger = tmp_path / "borderline.jsonl"
    lines = [
        monitor.json.dumps(_record(f"prod-bad-{i}", served=False, degraded=True))
        for i in range(4)
    ] + [
        monitor.json.dumps(_record(f"prod-ok-{i}", served=True, degraded=False))
        for i in range(16)
    ]
    ledger.write_text("\n".join(lines) + "\n")
    # No --max-degraded-rate passed: must use the new 0.15 default, not 0.5.
    rc = monitor.main(["--ledger", str(ledger), "--now", "2026-07-24T18:00:00Z"])
    out = capsys.readouterr().out
    assert rc == 2
    assert '"status": "degraded"' in out
    assert '"max_degraded_rate": 0.15' in out


def test_fetch_success_rate_alarm_below_0_50():
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=49
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    result = monitor.evaluate([coverage_record] + filler, **_healthy_defaults())
    assert result["fetch_success_rate"] == 0.49
    assert result["fetch_success_band"] == "alarm"
    assert result["status"] == "fetch-degraded"
    assert monitor._EXIT_CODES[result["status"]] == 5


def test_fetch_success_rate_exactly_at_alarm_boundary_is_not_alarmed():
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=50
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    result = monitor.evaluate([coverage_record] + filler, **_healthy_defaults())
    assert result["fetch_success_rate"] == 0.5
    assert result["fetch_success_band"] == "warn"
    assert result["status"] == "healthy"


def test_fetch_success_rate_warn_band_is_explicit_without_changing_status():
    # 0.60 sits in the documented warn band (below 0.70, at/above 0.50) --
    # it must remain informational only, never its own status/exit code.
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=60
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    result = monitor.evaluate([coverage_record] + filler, **_healthy_defaults())
    assert result["fetch_success_rate"] == 0.6
    assert result["fetch_success_band"] == "warn"
    assert result["status"] == "healthy"


def test_fetch_success_rate_exactly_at_warn_boundary_is_healthy_band():
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=70
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    result = monitor.evaluate([coverage_record] + filler, **_healthy_defaults())
    assert result["fetch_success_rate"] == 0.7
    assert result["fetch_success_band"] == "healthy"
    assert result["status"] == "healthy"


def test_fetch_success_rate_skips_cleanly_when_no_attempted_fetches():
    records = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(20)]
    result = monitor.evaluate(records, **_healthy_defaults())
    assert result["attempted_fetches_total"] == 0
    assert result["fetch_success_rate"] is None
    assert result["fetch_success_band"] is None
    assert result["status"] == "healthy"


def test_quiet_when_healthy_prints_warn_band_without_changing_exit(tmp_path, capsys):
    ledger = tmp_path / "fetch-warning.jsonl"
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=60
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    ledger.write_text(
        "\n".join(monitor.json.dumps(r) for r in [coverage_record] + filler) + "\n"
    )

    rc = monitor.main(
        [
            "--ledger",
            str(ledger),
            "--now",
            "2026-07-24T18:00:00Z",
            "--quiet-when-healthy",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert '"status": "healthy"' in out
    assert '"fetch_success_band": "warn"' in out


def test_quiet_when_healthy_still_suppresses_healthy_fetch_band(tmp_path, capsys):
    ledger = tmp_path / "fetch-healthy.jsonl"
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=70
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    ledger.write_text(
        "\n".join(monitor.json.dumps(r) for r in [coverage_record] + filler) + "\n"
    )

    rc = monitor.main(
        [
            "--ledger",
            str(ledger),
            "--now",
            "2026-07-24T18:00:00Z",
            "--quiet-when-healthy",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_ungrounded_rate_alarm_above_0_10():
    # 3/20 = 0.15 > 0.10 must alarm; the rest report a nonzero grounded
    # count so they aren't accidentally swept into the ungrounded bucket.
    ungrounded = [
        _record(f"prod-ungrounded-{i}", served=True, degraded=False, grounded_pages=0)
        for i in range(3)
    ]
    grounded = [
        _record(f"prod-grounded-{i}", served=True, degraded=False, grounded_pages=2)
        for i in range(17)
    ]
    result = monitor.evaluate(ungrounded + grounded, **_healthy_defaults())
    assert result["ungrounded_rate"] == 0.15
    assert result["status"] == "ungrounded"
    assert monitor._EXIT_CODES[result["status"]] == 2


def test_ungrounded_rate_exactly_at_boundary_is_not_alarmed():
    # 2/20 = 0.10, not strictly greater than 0.10 -- must not alarm.
    ungrounded = [
        _record(f"prod-ungrounded-{i}", served=True, degraded=False, grounded_pages=0)
        for i in range(2)
    ]
    grounded = [
        _record(f"prod-grounded-{i}", served=True, degraded=False, grounded_pages=2)
        for i in range(18)
    ]
    result = monitor.evaluate(ungrounded + grounded, **_healthy_defaults())
    assert result["ungrounded_rate"] == 0.1
    assert result["status"] == "healthy"


def test_short_window_search_outage_fires_below_min_attempts():
    # Only 3 production attempts total, well under min_attempts=20, and a
    # 2/3 = 0.667 short-window search-failure rate. This must still alarm
    # as 'search-outage' rather than being swallowed by insufficient-data.
    records = [
        _record("prod-a", served=False, degraded=True, minutes_ago=1, search_failed=True),
        _record("prod-b", served=False, degraded=True, minutes_ago=2, search_failed=True),
        _record("prod-c", served=True, degraded=False, minutes_ago=3, search_failed=False),
    ]
    result = monitor.evaluate(records, **_healthy_defaults())
    assert result["enabled_attempts"] == 3
    assert result["enabled_attempts"] < result["min_attempts"]
    assert result["search_window_attempts"] == 3
    assert result["search_failure_rate"] == round(2 / 3, 4)
    assert result["status"] == "search-outage"
    assert monitor._EXIT_CODES[result["status"]] == 2


def test_search_failure_rate_none_below_min_search_window_attempts():
    # Only 2 attempts in the short window -- below the 3-attempt floor, so
    # the short-window rate must not be trusted (None), even though it's
    # a 100% failure rate on the samples that exist.
    records = [
        _record("prod-a", served=False, degraded=True, minutes_ago=1, search_failed=True),
        _record("prod-b", served=False, degraded=True, minutes_ago=2, search_failed=True),
    ]
    result = monitor.evaluate(records, **_healthy_defaults())
    assert result["search_window_attempts"] == 2
    assert result["search_failure_rate"] is None
    assert result["search_failure_streak"] == 2
    # 2 consecutive failures is below the 3-streak alarm floor too.
    assert result["status"] != "search-outage"


def test_consecutive_search_failure_streak_overrides_low_aggregate_rate():
    # 20 healthy older records followed by 3 consecutive most-recent
    # search_failed=true records. Aggregate short-window rate is
    # 3/23 ~= 0.13, well under the 0.30 rate-alarm threshold, but the
    # streak check must fire regardless of that rate.
    healthy_records = [
        _record(f"prod-ok-{i}", served=True, degraded=False, minutes_ago=30 + i, search_failed=False)
        for i in range(20)
    ]
    failing_tail = [
        _record(f"prod-fail-{i}", served=False, degraded=True, minutes_ago=i + 1, search_failed=True)
        for i in range(3)
    ]
    records = healthy_records + failing_tail
    result = monitor.evaluate(records, **_healthy_defaults())
    assert result["enabled_attempts"] == 23
    assert result["search_failure_rate"] < 0.30
    assert result["search_failure_streak"] == 3
    assert result["status"] == "search-outage"


def test_smoke_field_is_authoritative_over_task_id_substring():
    # Legacy record with no `smoke` field and a "test"-looking task_id --
    # falls back to the substring heuristic and is excluded (unchanged
    # legacy behavior).
    legacy_smoke_like = _record("nightly-TEST-run-9", served=False, degraded=True)

    # A REAL content task about A/B testing, explicitly marked smoke=False.
    # Must NOT be excluded even though its task_id contains "test" --
    # the explicit field wins over the substring heuristic.
    real_ab_testing = _record(
        "ab-testing-article-launch", served=True, degraded=False, smoke=False
    )
    real_recovery_brief = _record(
        "quarterly-recovery-plan-brief", served=True, degraded=False, smoke=False
    )

    # Explicit smoke=True with a task_id that gives no substring hint at
    # all -- must still be excluded because the field is authoritative.
    explicit_smoke_no_marker = _record("prod-42-abcde", served=False, degraded=True, smoke=True)

    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(17)]

    records = [
        legacy_smoke_like,
        real_ab_testing,
        real_recovery_brief,
        explicit_smoke_no_marker,
    ] + filler

    result = monitor.evaluate(records, **_healthy_defaults())
    # 2 smoke: the legacy substring-matched record + the explicit smoke=True
    # record. The two smoke=False records are production despite their
    # task_id substrings.
    assert result["smoke_attempts"] == 2
    assert result["enabled_attempts"] == 19


def test_legacy_records_missing_new_fields_do_not_crash():
    # A pre-migration record has none of grounded_pages, attempted_fetches,
    # search_failed, smoke, severity, partial_degraded. Must be tolerated
    # as "missing", not crash, and not be misread as a zero/failure.
    legacy_records = [
        {
            "ts": datetime.fromtimestamp(NOW - 60, timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "enabled": True,
            "served": True,
            "degraded": False,
            "outcome": "served",
            "task_id": f"legacy-{i}",
        }
        for i in range(20)
    ]
    result = monitor.evaluate(legacy_records, **_healthy_defaults())
    assert result["grounded_pages_total"] == 0
    assert result["attempted_fetches_total"] == 0
    assert result["fetch_success_rate"] is None
    assert result["ungrounded_rate"] == 0.0
    # 20 legacy attempts fall inside the default 4h search window, so the
    # short-window rate IS trusted (>= 3 attempts) -- it's just 0.0
    # because `search_failed` is absent/falsy on every one of them.
    assert result["search_failure_rate"] == 0.0
    assert result["search_failure_streak"] == 0
    assert result["status"] == "healthy"


def test_legacy_records_with_none_values_for_new_fields_do_not_crash():
    legacy_records = [
        _record(f"prod-{i}", served=True, degraded=False) for i in range(20)
    ]
    for record in legacy_records:
        record["grounded_pages"] = None
        record["attempted_fetches"] = None
        record["search_failed"] = None
        record["smoke"] = None
    result = monitor.evaluate(legacy_records, **_healthy_defaults())
    assert result["fetch_success_rate"] is None
    assert result["ungrounded_rate"] == 0.0
    assert result["search_failure_streak"] == 0
    assert result["status"] == "healthy"


def test_search_outage_status_and_exit_code_via_main(tmp_path, capsys):
    ledger = tmp_path / "outage.jsonl"
    records = [
        _record("prod-a", served=False, degraded=True, minutes_ago=1, search_failed=True),
        _record("prod-b", served=False, degraded=True, minutes_ago=2, search_failed=True),
        _record("prod-c", served=False, degraded=True, minutes_ago=3, search_failed=True),
    ]
    ledger.write_text("\n".join(monitor.json.dumps(r) for r in records) + "\n")
    rc = monitor.main(
        ["--ledger", str(ledger), "--now", "2026-07-24T18:00:00Z", "--min-attempts", "20"]
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert '"status": "search-outage"' in out


def test_ungrounded_status_and_exit_code_via_main(tmp_path, capsys):
    ledger = tmp_path / "ungrounded.jsonl"
    ungrounded = [
        _record(f"prod-ungrounded-{i}", served=True, degraded=False, grounded_pages=0)
        for i in range(3)
    ]
    grounded = [
        _record(f"prod-grounded-{i}", served=True, degraded=False, grounded_pages=2)
        for i in range(17)
    ]
    records = ungrounded + grounded
    ledger.write_text("\n".join(monitor.json.dumps(r) for r in records) + "\n")
    rc = monitor.main(["--ledger", str(ledger), "--now", "2026-07-24T18:00:00Z"])
    out = capsys.readouterr().out
    assert rc == 2
    assert '"status": "ungrounded"' in out


def test_fetch_degraded_status_and_exit_code_via_main(tmp_path, capsys):
    ledger = tmp_path / "fetch-degraded.jsonl"
    coverage_record = _record(
        "prod-cov-0", served=True, degraded=False, attempted_fetches=100, grounded_pages=49
    )
    filler = [_record(f"prod-ok-{i}", served=True, degraded=False) for i in range(19)]
    records = [coverage_record] + filler
    ledger.write_text("\n".join(monitor.json.dumps(r) for r in records) + "\n")
    rc = monitor.main(["--ledger", str(ledger), "--now", "2026-07-24T18:00:00Z"])
    out = capsys.readouterr().out
    assert rc == 5
    assert '"status": "fetch-degraded"' in out
