#!/usr/bin/env python3
"""Read-only: render the skills catalog for a cron job's scope and print its
byte size, alongside the current unfiltered size, so a human can diff
before/after without running the actual cron job. Never writes anything.

Usage:
    python3 scripts/measure_skill_catalog.py --job-id 62714b869845
    python3 scripts/measure_skill_catalog.py --role dev-executor
    python3 scripts/measure_skill_catalog.py            # just prints full size
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.prompt_builder import (
    build_skills_system_prompt,
    clear_skills_system_prompt_cache,
    _resolve_skill_dir_scope,
)
from hermes_constants import get_hermes_home


def _role_for_job(job_id: str) -> str:
    jobs_path = get_hermes_home() / "cron" / "jobs.json"
    data = json.loads(jobs_path.read_text())
    for job in data.get("jobs", []):
        if job.get("id") == job_id:
            return str(job.get("skill_scope") or "")
    raise SystemExit(f"job id {job_id!r} not found in {jobs_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id", help="cron job id to look up skill_scope for")
    ap.add_argument("--role", help="skill_scope role name directly")
    args = ap.parse_args()

    role = args.role or (_role_for_job(args.job_id) if args.job_id else "")

    clear_skills_system_prompt_cache()
    full = build_skills_system_prompt()
    full_bytes = len(full.encode("utf-8"))
    print(f"FULL (unfiltered) catalog: {full_bytes} bytes")

    if not role:
        print("No role/job scope given — nothing to compare.")
        return

    scope = _resolve_skill_dir_scope(role)
    if scope is None:
        print(f"role {role!r} not recognized by _SKILL_ROLE_GROUPS — "
              f"resolves to unfiltered, same as above (fail-open).")
        return

    import os
    os.environ["HERMES_SESSION_SKILL_SCOPE"] = role  # os.environ fallback path, no gateway needed
    clear_skills_system_prompt_cache()
    scoped = build_skills_system_prompt()
    del os.environ["HERMES_SESSION_SKILL_SCOPE"]
    clear_skills_system_prompt_cache()

    scoped_bytes = len(scoped.encode("utf-8"))
    print(f"SCOPED ({role}) catalog:    {scoped_bytes} bytes")
    print(f"Reduction: {full_bytes - scoped_bytes} bytes "
          f"({100 * (1 - scoped_bytes / full_bytes):.1f}%)")


if __name__ == "__main__":
    main()
