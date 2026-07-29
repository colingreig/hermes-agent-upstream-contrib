#!/usr/bin/env python3
"""Hermes skill size-cap monitor.

Deterministic no_agent replacement for the old daily hermes-validate cron.
Scans every SKILL.md under ~/.hermes/skills/ for the 100K loader cap and
suppresses delivery when the report is byte-for-byte identical to the prior run
and no SKILL.md has changed since then.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
SKILLS_ROOT = HOME / ".hermes" / "skills"
JOBS_PATH = HOME / ".hermes" / "cron" / "jobs.json"
OUTPUT_ROOT = HOME / ".hermes" / "cron" / "output"
CAP = 100_000
WARN = 80_000
JOB_NAME_CANDIDATES = ("skill-size-monitor", "hermes-validate")


def _load_jobs() -> list[dict]:
    try:
        data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        jobs = data.get("jobs") or []
        return jobs if isinstance(jobs, list) else []
    return data if isinstance(data, list) else []


def _job_id_and_name() -> tuple[str, str]:
    jobs = _load_jobs()
    for target in JOB_NAME_CANDIDATES:
        for job in jobs:
            if job.get("name") == target:
                return str(job.get("id") or ""), target
    for job in jobs:
        if job.get("name") == "hermes-validate":
            return str(job.get("id") or ""), "hermes-validate"
    return "", "skill-size-monitor"


def _skill_files() -> list[Path]:
    if not SKILLS_ROOT.exists():
        return []
    return sorted(SKILLS_ROOT.rglob("SKILL.md"))


def _report_lines() -> list[str]:
    rows = []
    for path in _skill_files():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        pct = size / CAP * 100.0
        if size >= CAP:
            status = "OVER"
        elif size >= WARN:
            status = "WARN"
        else:
            status = "OK"
        rows.append((size, pct, status, path))

    rows.sort(key=lambda item: item[0], reverse=True)

    lines = ["| SIZE | %CAP | STATUS | PATH |", "|---|---:|---|---|"]
    for size, pct, status, path in rows[:10]:
        lines.append(f"| {size:,} | {pct:5.1f}% | {status} | {path.relative_to(SKILLS_ROOT)} |")

    over = sum(1 for size, *_ in rows if size >= CAP)
    warn = sum(1 for size, *_ in rows if WARN <= size < CAP)
    ok = sum(1 for size, *_ in rows if size < WARN)
    lines.append("")
    lines.append(f"Totals: OVER={over}, WARN={warn}, OK={ok}, scanned={len(rows)}")
    return lines


def _prior_output_path(job_id: str) -> Path | None:
    if not job_id:
        return None
    out_dir = OUTPUT_ROOT / job_id
    if not out_dir.exists():
        return None
    md_files = sorted(out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return md_files[0] if md_files else None


def main() -> int:
    job_id, job_name = _job_id_and_name()
    lines = _report_lines()
    body = "\n".join(lines).rstrip() + "\n"

    prior = _prior_output_path(job_id)
    if prior is not None:
        try:
            prior_text = prior.read_text(encoding="utf-8")
            prior_mtime = prior.stat().st_mtime
            unchanged = prior_text == body
            no_newer_skill = not any(p.stat().st_mtime > prior_mtime for p in _skill_files())
            if unchanged and no_newer_skill:
                print("[SILENT]")
                return 0
        except Exception:
            pass

    print(f"Hermes skill size-cap monitor — job={job_name} id={job_id or '?'}")
    print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
