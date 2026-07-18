#!/usr/bin/env python3
"""
orphan_pr_sweep.py — real, ACTING sweep for pushed-but-PR-less branches (86e29q8pg).

WHY THIS EXISTS: the fleet's only prior orphan-branch check (pr_pipeline_no_pr_left_behind.py,
mini-local) is read-only/diagnostic and only matches `agent/*` branches. Two gaps that let
branches strand forever:

  1. It NEVER creates the missing PR — it just reports, so a stranded branch stays stranded
     until a human notices the report and acts.
  2. It only matches `agent/*` — any branch pushed under the `hermes/*` naming convention
     (see skills/github/github-pr-workflow/SKILL.md) is invisible to it. This is exactly the
     failure mode `gh pr create` 401s produce (see that skill's retry-recipe fix, same task):
     the branch is pushed, the PR call fails, and the branch is never looked at again because
     nothing was watching `hermes/*`.

This script closes both gaps: it lists origin branches under EITHER prefix, checks each for
an existing PR (open or closed — a closed PR still means "not orphaned", just resolved some
other way), and for anything genuinely orphaned (ahead of main, no PR at all) it actually runs
`gh pr create`. It also best-effort cross-references ClickUp tasks tagged `needs-validation`
with no linked PR, since that's the tag the PR-workflow skill's retry recipe applies when it
gives up after retries.

Model-selection note (content-repo policy, ClickUp 86e2bjah4): PR creation here invokes no
model at all — it is pure git/gh/ClickUp-API plumbing — so "which model" does not apply to
this script itself. Anything downstream that this sweep might trigger (e.g. an agent turn to
address CI feedback on the newly-opened PR) must still stay Sonnet-only / fail-closed per that
policy; this script does not spawn or route to any model and makes no exception to it.

Safety / idempotency:
  - --dry-run (default False) prints what it would do without calling `gh pr create`.
  - Never force-pushes, never deletes branches, never touches ClickUp task status — only adds
    the `needs-validation` tag is out of scope here (the PR-workflow skill's retry path owns
    tagging at the point of failure); this script only reads that tag for cross-referencing.
  - Re-running after a PR now exists for a branch is a silent no-op for that branch (idempotent).
  - Every ClickUp/gh call is wrapped best-effort: a single branch's failure (rate limit,
    transient network error, malformed remote metadata) is logged and skipped, never fatal
    to the whole sweep.
  - NOT wired into any live cron here — add that separately once reviewed.

Usage:
  python3 scripts/ops/orphan_pr_sweep.py [--dry-run] [--base main] [--prefix agent/ --prefix hermes/]

Env:
  CLICKUP_API_TOKEN — optional; enables the needs-validation cross-reference. Without it the
  script still does the git/gh orphan sweep and just skips the ClickUp cross-reference (logged,
  not fatal).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request

DEFAULT_PREFIXES = ("agent/", "hermes/")
NEEDS_VALIDATION_TAG = "needs-validation"
CLICKUP_TEAM_ID = "9017245888"  # same team id as scripts/clickup_poll_gate.py


def _run(cmd, **kwargs):
    """subprocess.run wrapper: capture output, never raise on non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _remote_branches(prefixes):
    """Return origin branch names (no refs/heads/ prefix) matching any of `prefixes`."""
    proc = _run(["git", "ls-remote", "--heads", "origin"])
    if proc.returncode != 0:
        print(f"[orphan-sweep] git ls-remote failed: {proc.stderr.strip()}", file=sys.stderr)
        return []
    names = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/heads/"):
            continue
        name = ref[len("refs/heads/"):]
        if any(name.startswith(p) for p in prefixes):
            names.append(name)
    return names


def _ahead_of_base(branch, base):
    """True iff `branch` has commits not on `base` (best-effort; False on any git error
    so a single malformed ref never crashes the whole sweep)."""
    proc = _run(["git", "rev-list", "--count", f"origin/{base}..origin/{branch}"])
    if proc.returncode != 0:
        print(
            f"[orphan-sweep] rev-list failed for {branch}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    try:
        return int(proc.stdout.strip()) > 0
    except ValueError:
        return False


def _existing_pr(branch):
    """Return the PR number for `branch` (open OR closed/merged — any of those means
    "not orphaned"), or None if no PR references it at all."""
    proc = _run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "all",
            "--json", "number,state",
            "--limit", "1",
        ]
    )
    if proc.returncode != 0:
        print(
            f"[orphan-sweep] gh pr list failed for {branch}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return None  # unknown — treated as "no PR found" below, sweep will try to create;
                     # `gh pr create` itself is idempotent-safe (errors if one already exists)
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return rows[0]["number"] if rows else None


def _last_commit_subject_body(branch):
    proc = _run(["git", "log", "-1", "--pretty=%s%x00%b", f"origin/{branch}"])
    if proc.returncode != 0 or "\x00" not in proc.stdout:
        return f"chore: recover orphaned branch {branch}", ""
    subject, _, body = proc.stdout.partition("\x00")
    return subject.strip() or f"chore: recover orphaned branch {branch}", body.strip()


def _create_pr(branch, base, dry_run):
    title, body = _last_commit_subject_body(branch)
    full_body = (
        f"{body}\n\n" if body else ""
    ) + (
        "_Opened automatically by scripts/ops/orphan_pr_sweep.py — this branch was pushed "
        "but never got a PR (see ClickUp 86e29q8pg: `gh pr create` had no retry/re-auth, so "
        "a 401 silently stranded it)._"
    )
    if dry_run:
        print(f"[orphan-sweep] DRY-RUN would create PR for {branch}: {title!r}")
        return True
    proc = _run(
        [
            "gh", "pr", "create",
            "--head", branch,
            "--base", base,
            "--title", title,
            "--body", full_body,
        ]
    )
    if proc.returncode != 0:
        print(
            f"[orphan-sweep] gh pr create failed for {branch}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    print(f"[orphan-sweep] created PR for {branch}: {proc.stdout.strip()}")
    return True


def _clickup_needs_validation_tasks():
    """Best-effort: ClickUp tasks tagged `needs-validation`. Returns [] (logged, not fatal)
    if CLICKUP_API_TOKEN is unset or any call fails — the git/gh sweep above does not depend
    on this."""
    import os

    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token:
        print(
            "[orphan-sweep] CLICKUP_API_TOKEN not set — skipping needs-validation "
            "cross-reference (git/gh sweep still runs)",
            file=sys.stderr,
        )
        return []
    try:
        url = (
            f"https://api.clickup.com/api/v2/team/{CLICKUP_TEAM_ID}/task"
            f"?tags[]={NEEDS_VALIDATION_TAG}&include_closed=true&subtasks=true"
        )
        req = urllib.request.Request(url, headers={"Authorization": token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data.get("tasks", [])
    except Exception as e:
        print(f"[orphan-sweep] ClickUp needs-validation lookup failed: {e!r}", file=sys.stderr)
        return []


def sweep(base="main", prefixes=DEFAULT_PREFIXES, dry_run=False):
    branches = _remote_branches(prefixes)
    if not branches:
        print("[orphan-sweep] no agent/* or hermes/* branches on origin — nothing to do")
        return 0

    needs_validation_tasks = _clickup_needs_validation_tasks()
    if needs_validation_tasks:
        print(
            f"[orphan-sweep] {len(needs_validation_tasks)} ClickUp task(s) tagged "
            f"'{NEEDS_VALIDATION_TAG}' (cross-reference only; no PR linkage in the "
            "ClickUp API response is used as a heuristic, not authoritative)"
        )

    created, skipped, errors = 0, 0, 0
    for branch in branches:
        if not _ahead_of_base(branch, base):
            skipped += 1
            continue
        pr_number = _existing_pr(branch)
        if pr_number is not None:
            skipped += 1
            continue
        print(f"[orphan-sweep] orphan candidate: {branch} (ahead of {base}, no PR)")
        if not _create_pr(branch, base, dry_run):
            errors += 1
            continue
        created += 1

    print(
        f"[orphan-sweep] done: {created} PR(s) created, {skipped} branch(es) already "
        f"covered, {errors} error(s)"
    )
    return 0 if errors == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report only, create nothing")
    parser.add_argument("--base", default="main", help="Base branch for new PRs (default: main)")
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        help="Branch prefix to match; repeatable (default: agent/ and hermes/)",
    )
    args = parser.parse_args()
    prefixes = tuple(args.prefixes) if args.prefixes else DEFAULT_PREFIXES
    return sweep(base=args.base, prefixes=prefixes, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
