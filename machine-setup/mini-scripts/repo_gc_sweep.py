#!/usr/bin/env python3
"""
repo_gc_sweep.py — daily janitor sweep. Runs `repo_baseline.py gc` on every
Hermes MAIN clone so per-task agent/* worktrees never accumulate (answers the
"could this sprawl/bloat over time?" concern, 2026-06-17).

Safe by construction: gc only removes worktrees that are merged into the baseline
AND clean AND idle >= 3 days, and never the primary checkout. A clone with no
stale worktrees is a cheap no-op (fetch + prune).

Scans top-level git repos under ~/Projects and ~/dev. A MAIN clone has a `.git`
DIRECTORY; a linked worktree has a `.git` FILE — so the per-task worktrees under
~/.hermes/worktrees are never themselves treated as clones (their main clone
handles them). Run by the `repo-worktree-gc` cron (no_agent, daily).
"""
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
ROOTS = [os.path.join(HOME, "Projects"), os.path.join(HOME, "dev")]
GC = os.path.join(HOME, ".hermes", "scripts", "repo_baseline.py")

# Wall-clock budget. The cron runner kills this script at 120s
# (scheduler._DEFAULT_SCRIPT_TIMEOUT); each per-clone `repo_baseline.py gc` does a
# network `git fetch` (up to 90s), so once the clone set grew past ~80 repos a single
# slow fetch could blow the 120s budget and the cron was KILLED mid-sweep -> last_status
# error every day (2026-06-27). Self-bound to a safe margin under that limit: process
# clones until the budget is spent, then stop CLEANLY (the sweep is idempotent and
# resumes on the next daily run). Override with HERMES_GC_SWEEP_BUDGET_SECONDS.
_BUDGET_SECONDS = int(float(os.getenv("HERMES_GC_SWEEP_BUDGET_SECONDS", "100")))


def _is_main_clone(p):
    return os.path.isdir(os.path.join(p, ".git"))  # dir = main clone; file = linked worktree


def main():
    clones = []
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if os.path.isdir(full) and _is_main_clone(full):
                clones.append(full)
    if not clones:
        return 0

    # Quiet-on-no-op (watchdog convention: empty stdout = healthy/silent). Only
    # surface clones where gc actually removed/swept something, plus any errors.
    actions, errors = [], []
    deadline = time.monotonic() + _BUDGET_SECONDS
    skipped = 0
    for c in clones:
        if time.monotonic() >= deadline:
            skipped += 1
            continue  # out of budget; resume on the next daily run (idempotent)
        # Cap each per-clone gc at whatever budget remains so one slow `git fetch`
        # can't run us past the cron's 120s kill.
        remaining = max(5, int(deadline - time.monotonic()))
        try:
            r = subprocess.run([sys.executable, GC, "gc", c],
                               capture_output=True, text=True, timeout=remaining)
            line = r.stdout.strip()
            noop = ("removed 0 " in line) and ("swept 0 " in line)
            if r.returncode != 0:
                errors.append(f"{c}: {r.stderr.strip()[:4000]}")
            elif not noop:
                actions.append(line)  # an actual removal or orphan sweep happened
        except subprocess.TimeoutExpired:
            # Budget ran out mid-fetch on this clone — not a fault; it resumes next run.
            skipped += 1
        except Exception as e:
            errors.append(f"{c}: {e!r}")

    if actions:
        print("repo-worktree-gc:")
        for a in actions:
            print("  " + a)
    if skipped:
        # Surface (non-fatal) that the budget capped the sweep so it's visible but green.
        print(f"repo-worktree-gc: budget reached, {skipped} clone(s) deferred to next run")
    if errors:
        sys.stderr.write("repo-worktree-gc errors:\n  " + "\n  ".join(errors) + "\n")
        return 1
    return 0  # silent: nothing to reclaim


if __name__ == "__main__":
    sys.exit(main())
