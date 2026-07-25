import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "research_outcome_metrics.py"
SPEC = importlib.util.spec_from_file_location("research_outcome_metrics", SCRIPT)
metrics = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(metrics)

OPENCODE_SCRIPT = Path(__file__).resolve().parents[1] / "opencode_exec.py"
OPENCODE_SPEC = importlib.util.spec_from_file_location("outcome_test_opencode_exec", OPENCODE_SCRIPT)
opencode = importlib.util.module_from_spec(OPENCODE_SPEC)
assert OPENCODE_SPEC.loader is not None
OPENCODE_SPEC.loader.exec_module(opencode)


def _research(task_id, *, severity, grounded_pages, smoke=False):
    return {
        "ts": "2026-07-25T10:00:00Z",
        "task_id": task_id,
        "enabled": True,
        "smoke": smoke,
        "severity": severity,
        "grounded_pages": grounded_pages,
    }


def _outcome(task_id, *, pieces=1, links=0):
    return {
        "ts": "2026-07-25T11:00:00Z",
        "task_id": task_id,
        "content_pieces": pieces,
        "citation_links": links,
        "citation_link_coverage_per_piece": links / pieces if pieces else None,
        "citation_piece_coverage_rate": 1.0 if links else 0.0,
    }


def _verdict(task_id, verdict):
    return {
        "ts": "2026-07-25T12:00:00Z",
        "task_id": task_id,
        "verdict": verdict,
    }


def test_measure_content_files_counts_unique_explicit_links_without_content(tmp_path):
    (tmp_path / "article.md").write_text(
        """
        [First](https://example.com/a) and [duplicate](https://example.com/a).
        ![Image](https://example.com/image.jpg)
        <https://example.com/b>
        """,
        encoding="utf-8",
    )
    (tmp_path / "page.astro").write_text(
        '<a class="source" href="https://example.org/source">Source</a>',
        encoding="utf-8",
    )
    (tmp_path / "code.py").write_text(
        "SOURCE = 'https://should-not-count.example/'", encoding="utf-8"
    )

    result = metrics.measure_content_files(
        tmp_path, ["article.md", "page.astro", "code.py", "../outside.md"]
    )

    assert result["content_pieces"] == 2
    assert result["citation_links"] == 3
    assert result["pieces_with_citation_links"] == 2
    assert result["citation_link_coverage_per_piece"] == 1.5
    assert result["citation_piece_coverage_rate"] == 1.0
    assert result["source_paths_sha256"]
    assert "article" not in json.dumps(result)


def test_fields_json_is_one_piece_and_invalid_json_is_ignored(tmp_path):
    (tmp_path / "fields.json").write_text(
        json.dumps(
            {
                "title": "A title",
                "body": "[Source](https://example.com/source)",
                "nested": ["<a href='https://example.org/other'>Other</a>"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "fields_clean.json").write_text("{broken", encoding="utf-8")

    result = metrics.measure_content_files(
        tmp_path, ["fields.json", "fields_clean.json"]
    )

    assert result["content_pieces"] == 1
    assert result["citation_links"] == 2
    assert result["citation_link_coverage_per_piece"] == 2.0


def test_append_content_outcome_writes_content_free_receipt(tmp_path):
    (tmp_path / "piece.md").write_text(
        "[Evidence](https://example.com/evidence)", encoding="utf-8"
    )
    ledger = tmp_path / "logs" / "outcomes.jsonl"

    result = metrics.append_content_outcome(
        task_id="86-task",
        workdir=tmp_path,
        relative_files=["piece.md"],
        ledger=ledger,
    )

    stored = json.loads(ledger.read_text(encoding="utf-8"))
    assert stored == result
    assert stored["task_id"] == "86-task"
    assert stored["content_pieces"] == 1
    assert stored["citation_links"] == 1
    assert "Evidence" not in ledger.read_text(encoding="utf-8")
    assert "example.com" not in ledger.read_text(encoding="utf-8")


def test_opencode_content_hook_records_measured_receipt(tmp_path, monkeypatch):
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    (workdir / "piece.md").write_text(
        "[Evidence](https://example.com/evidence)", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        opencode,
        "_changed_content_paths",
        lambda *_args: ["piece.md"],
    )

    result = opencode._record_content_outcome("86-task", str(workdir), [], [])

    assert result["task_id"] == "86-task"
    assert result["citation_link_coverage_per_piece"] == 1.0
    ledger = tmp_path / ".hermes" / "logs" / "content-outcomes.jsonl"
    assert json.loads(ledger.read_text(encoding="utf-8"))["citation_links"] == 1


def test_report_joins_grounding_severity_citations_and_validator_outcomes():
    report = metrics.build_report(
        [
            _research("degraded", severity="material", grounded_pages=0),
            _research("healthy", severity="none", grounded_pages=3),
        ],
        [
            _outcome("degraded", links=0),
            _outcome("healthy", links=2),
        ],
        [
            _verdict("degraded", "BLOCK"),
            _verdict("healthy", "PASS"),
        ],
        min_cohort_size=1,
    )

    assert report["instrumentation"] == {
        "production_research_tasks": 2,
        "content_outcome_tasks": 2,
        "joined_tasks": 2,
        "joined_with_known_severity": 2,
        "joined_with_validator_verdict": 2,
    }
    assert report["cohorts_by_severity"]["material"][
        "validator_fail_rate_for_content"
    ] == 1.0
    assert report["cohorts_by_severity"]["none"][
        "validator_fail_rate_for_content"
    ] == 0.0
    assert report["cohorts_by_severity"]["material"][
        "citation_link_coverage_per_piece"
    ] == 0.0
    assert report["cohorts_by_severity"]["none"][
        "citation_link_coverage_per_piece"
    ] == 2.0
    assert report["prediction"]["status"] == "association-observed"
    assert report["prediction"]["validator_fail_rate_delta_degraded_minus_healthy"] == 1.0
    assert report["prediction"][
        "citation_links_per_piece_delta_degraded_minus_healthy"
    ] == -2.0
    assert report["joined_rows"][0]["grounded_pages"] == 0


def test_report_stays_insufficient_until_both_validator_cohorts_reach_floor():
    report = metrics.build_report(
        [
            _research("degraded", severity="material", grounded_pages=0),
            _research("healthy", severity="none", grounded_pages=3),
        ],
        [_outcome("degraded"), _outcome("healthy")],
        [_verdict("degraded", "BLOCK"), _verdict("healthy", "PASS")],
        min_cohort_size=5,
    )

    assert report["prediction"]["status"] == "insufficient-data"
    assert "cannot yet be evaluated" in report["prediction"]["statement"]


def test_old_research_schema_is_joined_but_not_misclassified_and_smoke_is_excluded():
    report = metrics.build_report(
        [
            {
                "ts": "2026-07-24T10:00:00Z",
                "task_id": "old",
                "enabled": True,
                "served": True,
                "outcome": "served",
            },
            _research("smoke", severity="material", grounded_pages=0, smoke=True),
            {
                "ts": "2026-07-24T09:00:00Z",
                "task_id": "86-task-fetch-smoke",
                "enabled": True,
                "degraded": True,
            },
        ],
        [_outcome("old"), _outcome("smoke"), _outcome("86-task-fetch-smoke")],
        [
            _verdict("old", "PASS"),
            _verdict("smoke", "BLOCK"),
            _verdict("86-task-fetch-smoke", "BLOCK"),
        ],
    )

    assert report["instrumentation"]["production_research_tasks"] == 1
    assert report["instrumentation"]["joined_tasks"] == 1
    assert report["instrumentation"]["joined_with_known_severity"] == 0
    assert report["cohorts_by_severity"]["unknown"]["joined_tasks"] == 1
    assert report["prediction"]["status"] == "insufficient-data"


def test_legacy_recovery_and_test_markers_follow_monitor_smoke_semantics():
    report = metrics.build_report(
        [
            {
                "ts": "2026-07-24T08:00:00Z",
                "task_id": "provider-recovery-drill",
                "enabled": True,
            },
            {
                "ts": "2026-07-24T09:00:00Z",
                "task_id": "nightly-TEST-run",
                "enabled": True,
            },
            {
                "ts": "2026-07-24T10:00:00Z",
                "task_id": "production-piece",
                "enabled": True,
            },
        ],
        [
            _outcome("provider-recovery-drill"),
            _outcome("nightly-TEST-run"),
            _outcome("production-piece"),
        ],
        [],
    )

    assert report["instrumentation"]["production_research_tasks"] == 1
    assert report["instrumentation"]["joined_tasks"] == 1
    assert report["joined_rows"][0]["task_id"] == "production-piece"


def test_explicit_smoke_false_overrides_legacy_task_id_markers():
    report = metrics.build_report(
        [
            {
                "ts": "2026-07-24T10:00:00Z",
                "task_id": "ab-testing-recovery-article",
                "enabled": True,
                "smoke": False,
                "severity": "none",
                "grounded_pages": 3,
            }
        ],
        [_outcome("ab-testing-recovery-article", links=2)],
        [_verdict("ab-testing-recovery-article", "PASS")],
    )

    assert report["instrumentation"]["production_research_tasks"] == 1
    assert report["instrumentation"]["joined_tasks"] == 1
    assert report["joined_rows"][0]["task_id"] == "ab-testing-recovery-article"


def test_latest_validator_verdict_per_task_wins():
    report = metrics.build_report(
        [_research("task", severity="material", grounded_pages=0)],
        [_outcome("task")],
        [
            {
                "ts": "2026-07-25T10:00:00Z",
                "task_id": "task",
                "verdict": "BLOCK",
            },
            {
                "ts": "2026-07-25T11:00:00Z",
                "task_id": "task",
                "verdict": "PASS",
            },
        ],
    )

    assert report["overall"]["validator_failures"] == 0
    assert report["overall"]["validator_fail_rate_for_content"] == 0.0


def test_markdown_report_quotes_counts_and_definitions():
    report = metrics.build_report([], [], [])
    rendered = metrics.render_markdown(report)

    assert "**insufficient-data**" in rendered
    assert "Production research tasks: 0" in rendered
    assert "Joined by ClickUp task ID: 0" in rendered
    assert "Citation coverage:" in rendered
    assert "association only" in rendered
