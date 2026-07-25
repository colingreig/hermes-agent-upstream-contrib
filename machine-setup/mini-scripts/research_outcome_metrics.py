#!/usr/bin/env python3
"""Measure whether research-stage degradation predicts content quality.

The writer records content-free citation counts after each ``--content`` run.
This reporter joins those counts to the research-stage ledger and the
validator verdict store by ClickUp task id.  It deliberately reports
``insufficient-data`` until both degraded and healthy cohorts have enough
validator-observed pieces; an empty or one-sided rollout is not evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_RESEARCH_LEDGER = Path("~/.hermes/logs/research-served.jsonl").expanduser()
DEFAULT_OUTCOME_LEDGER = Path("~/.hermes/logs/content-outcomes.jsonl").expanduser()
DEFAULT_VERDICT_STORE = Path("~/.hermes/scripts/.validator_verdicts.json").expanduser()
DEFAULT_MIN_COHORT_SIZE = 5

_CONTENT_SUFFIXES = frozenset({".md", ".mdx", ".astro", ".html", ".htm"})
_CONTENT_JSON_NAMES = frozenset({"fields.json", "fields_clean.json"})
_SMOKE_TASK_ID_MARKERS = ("smoke", "recovery", "test")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(https?://[^)\s]+)", re.IGNORECASE)
_HTML_LINK_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*[\"'](https?://[^\"']+)[\"']", re.IGNORECASE
)
_AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>", re.IGNORECASE)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round(float(numerator) / float(denominator), 4)


def _json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _json_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_strings(child)


def _piece_text(path: Path) -> str | None:
    if path.suffix.lower() in _CONTENT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    if path.name.lower() in _CONTENT_JSON_NAMES:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            return None
        return "\n".join(_json_strings(value))
    return None


def citation_links(text: str) -> set[str]:
    """Return unique explicit external citation links without retaining content."""
    links: set[str] = set()
    for pattern in (_MARKDOWN_LINK_RE, _HTML_LINK_RE, _AUTOLINK_RE):
        links.update(match.rstrip(".,;") for match in pattern.findall(text))
    return links


def measure_content_files(workdir: Path, relative_files: Iterable[str]) -> dict[str, Any]:
    """Count links in eligible content files, treating each file as one piece."""
    root = workdir.resolve()
    pieces = 0
    citation_count = 0
    pieces_with_citations = 0
    observed_paths: list[str] = []

    for raw_path in sorted(set(relative_files)):
        path = Path(raw_path)
        candidate = path if path.is_absolute() else root / path
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        text = _piece_text(resolved)
        if text is None:
            continue
        links = citation_links(text)
        pieces += 1
        citation_count += len(links)
        pieces_with_citations += bool(links)
        observed_paths.append(str(resolved.relative_to(root)))

    path_fingerprint = (
        hashlib.sha256("\n".join(observed_paths).encode("utf-8")).hexdigest()
        if observed_paths
        else None
    )
    return {
        "content_pieces": pieces,
        "citation_links": citation_count,
        "pieces_with_citation_links": pieces_with_citations,
        # Primary requested metric: mean explicit citation links per content piece.
        "citation_link_coverage_per_piece": _safe_ratio(citation_count, pieces),
        # Companion guard against a high-link outlier hiding uncited pieces.
        "citation_piece_coverage_rate": _safe_ratio(pieces_with_citations, pieces),
        # Paths/content never enter the ledger; this proves which path set was counted.
        "source_paths_sha256": path_fingerprint,
    }


def append_content_outcome(
    *,
    task_id: str,
    workdir: Path,
    relative_files: Iterable[str],
    ledger: Path = DEFAULT_OUTCOME_LEDGER,
) -> dict[str, Any]:
    """Append a content-free writer outcome seed. Logging is fail-open to writing."""
    metrics = measure_content_files(workdir, relative_files)
    record = {
        "schema_version": 1,
        "ts": _utcnow(),
        "task_id": task_id,
        **metrics,
    }
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        record["ledger_write_failed"] = True
    return record


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _load_verdicts(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _latest_by_task(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = str(row.get("task_id") or "").strip()
        if not task_id:
            continue
        incumbent = latest.get(task_id)
        if incumbent is None or str(row.get("ts") or "") >= str(incumbent.get("ts") or ""):
            latest[task_id] = row
    return latest


def _severity(row: dict[str, Any]) -> str | None:
    raw = str(row.get("severity") or "").lower()
    if raw in {"material", "partial", "none"}:
        return raw
    # Do not infer the current severity model from the first deployed schema's
    # broader `degraded` bit. Its semantics predate the grounded-page thresholds.
    return None


def _is_smoke(record: dict[str, Any]) -> bool:
    """Match the research monitor's canonical legacy-smoke precedence."""
    smoke_field = record.get("smoke")
    if isinstance(smoke_field, bool):
        return smoke_field
    task_id = str(record.get("task_id") or "").lower()
    return any(marker in task_id for marker in _SMOKE_TASK_ID_MARKERS)


def _cohort_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pieces = sum(int(row.get("content_pieces") or 0) for row in rows)
    links = sum(int(row.get("citation_links") or 0) for row in rows)
    evaluated = [row for row in rows if row.get("validator_failed") is not None]
    failures = sum(bool(row["validator_failed"]) for row in evaluated)
    return {
        "joined_tasks": len(rows),
        "content_pieces": pieces,
        "citation_links": links,
        "citation_link_coverage_per_piece": _safe_ratio(links, pieces),
        "validator_evaluated_tasks": len(evaluated),
        "validator_failures": failures,
        "validator_fail_rate_for_content": _safe_ratio(failures, len(evaluated)),
    }


def build_report(
    research_rows: list[dict[str, Any]],
    outcome_rows: list[dict[str, Any]],
    verdict_rows: list[dict[str, Any]],
    *,
    min_cohort_size: int = DEFAULT_MIN_COHORT_SIZE,
) -> dict[str, Any]:
    """Join task-level receipts and summarize association, never causation."""
    production_research = [
        row
        for row in research_rows
        if row.get("enabled") is True
        # An explicit bool is authoritative. Legacy rows without one use the
        # same smoke/recovery/test task-id markers as research_stage_monitor.py.
        and not _is_smoke(row)
    ]
    research = _latest_by_task(production_research)
    outcomes = _latest_by_task(outcome_rows)
    verdicts = _latest_by_task(verdict_rows)

    joined: list[dict[str, Any]] = []
    for task_id in sorted(research.keys() & outcomes.keys()):
        research_row = research[task_id]
        outcome = outcomes[task_id]
        verdict = str((verdicts.get(task_id) or {}).get("verdict") or "").upper()
        joined.append(
            {
                "task_id": task_id,
                "severity": _severity(research_row),
                "grounded_pages": research_row.get("grounded_pages"),
                "content_pieces": int(outcome.get("content_pieces") or 0),
                "citation_links": int(outcome.get("citation_links") or 0),
                "citation_link_coverage_per_piece": outcome.get(
                    "citation_link_coverage_per_piece"
                ),
                "citation_piece_coverage_rate": outcome.get("citation_piece_coverage_rate"),
                "validator_verdict": verdict or None,
                "validator_failed": (
                    True if verdict in {"BLOCK", "FAIL"} else False if verdict == "PASS" else None
                ),
            }
        )

    by_severity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in joined:
        by_severity[row.get("severity") or "unknown"].append(row)
    cohort_summaries = {
        name: _cohort_summary(by_severity.get(name, []))
        for name in ("material", "partial", "none", "unknown")
    }

    degraded = [row for row in joined if row.get("severity") in {"material", "partial"}]
    healthy = [row for row in joined if row.get("severity") == "none"]
    degraded_summary = _cohort_summary(degraded)
    healthy_summary = _cohort_summary(healthy)

    prediction: dict[str, Any] = {
        "status": "insufficient-data",
        "minimum_validator_observed_tasks_per_cohort": min_cohort_size,
        "statement": (
            "Research-stage degradation cannot yet be evaluated as a predictor of "
            "content quality."
        ),
    }
    degraded_n = degraded_summary["validator_evaluated_tasks"]
    healthy_n = healthy_summary["validator_evaluated_tasks"]
    if degraded_n >= min_cohort_size and healthy_n >= min_cohort_size:
        fail_delta = round(
            degraded_summary["validator_fail_rate_for_content"]
            - healthy_summary["validator_fail_rate_for_content"],
            4,
        )
        degraded_citations = degraded_summary["citation_link_coverage_per_piece"]
        healthy_citations = healthy_summary["citation_link_coverage_per_piece"]
        citation_delta = (
            round(degraded_citations - healthy_citations, 4)
            if degraded_citations is not None and healthy_citations is not None
            else None
        )
        if fail_delta > 0 and (citation_delta is None or citation_delta < 0):
            status = "association-observed"
            statement = (
                "Degraded research is associated with more validator failures"
                + (" and lower citation-link coverage." if citation_delta is not None else ".")
            )
        elif fail_delta <= 0 and (citation_delta is None or citation_delta >= 0):
            status = "association-not-observed"
            statement = (
                "The observed cohorts do not show worse content outcomes after degraded research."
            )
        else:
            status = "mixed-signal"
            statement = "Validator failures and citation-link coverage move in different directions."
        prediction = {
            "status": status,
            "minimum_validator_observed_tasks_per_cohort": min_cohort_size,
            "validator_fail_rate_delta_degraded_minus_healthy": fail_delta,
            "citation_links_per_piece_delta_degraded_minus_healthy": citation_delta,
            "statement": statement + " This is observational evidence, not causation.",
        }

    return {
        "schema_version": 1,
        "generated_at": _utcnow(),
        "definitions": {
            "citation_link_coverage_per_piece": (
                "unique explicit external Markdown/HTML citation links divided by content pieces"
            ),
            "validator_fail_rate_for_content": (
                "latest BLOCK/FAIL verdicts divided by latest PASS/BLOCK/FAIL verdicts "
                "for joined content tasks"
            ),
            "join_key": "ClickUp task_id",
        },
        "instrumentation": {
            "production_research_tasks": len(research),
            "content_outcome_tasks": len(outcomes),
            "joined_tasks": len(joined),
            "joined_with_known_severity": sum(row["severity"] is not None for row in joined),
            "joined_with_validator_verdict": sum(
                row["validator_failed"] is not None for row in joined
            ),
        },
        "overall": _cohort_summary(joined),
        "cohorts_by_severity": cohort_summaries,
        "degraded_cohort": degraded_summary,
        "healthy_cohort": healthy_summary,
        "prediction": prediction,
        "joined_rows": joined,
    }


def render_markdown(report: dict[str, Any]) -> str:
    inst = report["instrumentation"]
    prediction = report["prediction"]
    lines = [
        "# Research-stage outcome validity report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Result",
        "",
        f"**{prediction['status']}** — {prediction['statement']}",
        "",
        "## Instrumentation coverage",
        "",
        f"- Production research tasks: {inst['production_research_tasks']}",
        f"- Content outcome tasks: {inst['content_outcome_tasks']}",
        f"- Joined by ClickUp task ID: {inst['joined_tasks']}",
        f"- Joined with known research severity: {inst['joined_with_known_severity']}",
        f"- Joined with a validator verdict: {inst['joined_with_validator_verdict']}",
        "",
        "## Cohorts",
        "",
        "| Severity | Joined tasks | Pieces | Citation links/piece | Validator fail rate |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for severity in ("material", "partial", "none", "unknown"):
        cohort = report["cohorts_by_severity"][severity]
        citation = cohort["citation_link_coverage_per_piece"]
        fail_rate = cohort["validator_fail_rate_for_content"]
        lines.append(
            f"| {severity} | {cohort['joined_tasks']} | {cohort['content_pieces']} | "
            f"{'N/A' if citation is None else f'{citation:.4f}'} | "
            f"{'N/A' if fail_rate is None else f'{fail_rate:.1%}'} |"
        )
    lines.extend(
        [
            "",
            "## Metric definitions",
            "",
            f"- Citation coverage: {report['definitions']['citation_link_coverage_per_piece']}.",
            f"- Validator fail rate: {report['definitions']['validator_fail_rate_for_content']}.",
            "- Cohort comparisons require validator-observed degraded and healthy samples; "
            "the default floor is five tasks per cohort.",
            "- The report describes association only. It does not claim research degradation "
            "caused a validator outcome.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_output(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="record citation metrics for content files")
    record_parser.add_argument("--task-id", required=True)
    record_parser.add_argument("--workdir", type=Path, required=True)
    record_parser.add_argument("--file", action="append", default=[], dest="files")
    record_parser.add_argument("--outcome-ledger", type=Path, default=DEFAULT_OUTCOME_LEDGER)

    report_parser = subparsers.add_parser("report", help="join ledgers and report outcome validity")
    report_parser.add_argument("--research-ledger", type=Path, default=DEFAULT_RESEARCH_LEDGER)
    report_parser.add_argument("--outcome-ledger", type=Path, default=DEFAULT_OUTCOME_LEDGER)
    report_parser.add_argument("--verdict-store", type=Path, default=DEFAULT_VERDICT_STORE)
    report_parser.add_argument("--min-cohort-size", type=int, default=DEFAULT_MIN_COHORT_SIZE)
    report_parser.add_argument("--format", choices=("json", "markdown"), default="json")
    report_parser.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "record":
        result = append_content_outcome(
            task_id=args.task_id,
            workdir=args.workdir,
            relative_files=args.files,
            ledger=args.outcome_ledger,
        )
        print(json.dumps(result, sort_keys=True))
        return 0

    report = build_report(
        _load_jsonl(args.research_ledger),
        _load_jsonl(args.outcome_ledger),
        _load_verdicts(args.verdict_store),
        min_cohort_size=max(1, args.min_cohort_size),
    )
    text = (
        render_markdown(report)
        if args.format == "markdown"
        else json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    if args.output:
        _write_output(args.output, text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
