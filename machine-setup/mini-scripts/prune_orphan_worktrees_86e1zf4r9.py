#! /Users/colingreig/.hermes/runtime-current/venv/bin/python3.11
"""
Safe prune script for Hermes: identify candidate ~/dev/ignite-<id> worktrees that look orphaned
and (if not --dry-run) remove them. Conservative rules:
 - skip any worktree with a .git and with commits ahead of origin/HEAD (>0)
 - skip any worktree containing a 'deliverable' dir (preserve drafts)
 - candidate if older than 30 days (mtime of dir)

This script is intentionally conservative. Use --dry-run to list candidates.
"""
import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Strict ClickUp-task-id shape: `ignite-<lowercase alnum id, 8+ chars>`. Deliberately does
# NOT match real repo checkouts like `ignite-digital-engine`, `ignite-marketplace`,
# `ignite-sentinel`, `ignite-skills` (those have a `-word` suffix with letters only and
# don't fit the id shape) or short/ambiguous names. Combined with the linked-worktree check
# below (`.git` must be a FILE, never a directory) this can never target a real full clone.
TASK_DIR_RE = re.compile(r"^ignite-[0-9a-z]{8,}$")


def _git(args, cwd, timeout=30):
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None


AHEAD_UNKNOWN = None  # sentinel: git error / unparseable output — caller must NOT treat as 0


def git_commits_ahead(path):
    """Return the commit count HEAD is ahead of origin/HEAD, or AHEAD_UNKNOWN (None) if the
    check could not be performed reliably. FAILS CLOSED (2026-07-10 hardening, mirrored from
    worktree_backstop_sweep.py): a git error, non-zero exit, or unparseable output must never
    be silently read as ahead=0 — that reading is what let an unpushed, no-remote clone
    (ignite-86e251a3e) look deletion-safe elsewhere. Callers must treat AHEAD_UNKNOWN as
    "cannot verify" -> protect, never as "0 -> safe"."""
    proc = _git(["rev-list", "--count", "origin/HEAD..HEAD"], path)
    if proc is None or proc.returncode != 0:
        return AHEAD_UNKNOWN
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return AHEAD_UNKNOWN


def _has_origin_remote(path: Path) -> bool:
    """True only if `origin` is configured with a non-empty URL. No remote means there is
    nowhere the content could have been safely pushed to — deletion must never proceed
    without this being affirmatively true."""
    proc = _git(["remote", "get-url", "origin"], path)
    return bool(proc is not None and proc.returncode == 0 and proc.stdout.strip())


def _has_write_tree() -> bool:
    """Repo-independent feature probe: `git merge-tree -h` prints usage (exit 0) on any
    git new enough to support the modern `--write-tree` mode, even outside a repo."""
    try:
        proc = subprocess.run(
            ["git", "merge-tree", "-h"], capture_output=True, text=True, timeout=10, check=False,
        )
        return "--write-tree" in (proc.stdout + proc.stderr)
    except Exception:
        return False


HAS_WRITE_TREE = _has_write_tree()


def _default_ref(path: Path):
    """Resolve the repo's default branch ref (e.g. 'origin/main'), or None if it can't be
    determined. Returning None is the safe direction — callers treat it as NOT landed."""
    proc = _git(["rev-parse", "--abbrev-ref", "origin/HEAD"], path)
    if proc is not None and proc.returncode == 0:
        ref = proc.stdout.strip()
        if ref and ref != "HEAD":
            return ref
    for candidate in ("origin/main", "origin/master"):
        proc = _git(["rev-parse", "--verify", "--quiet", candidate], path)
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return candidate
    return None


def _content_landed(path: Path) -> bool:
    """Return True ONLY when the worktree's HEAD contributes NOTHING new relative to the
    default branch — i.e. it's safe to delete even though `origin/HEAD..HEAD` shows commits
    ahead (which happens after a squash/rebase merge rewrites history). Conservative by
    design: any error, ambiguity, or missing default ref returns False (worktree survives).
    A false "not landed" is fine; a false "landed" is forbidden."""
    _git(["fetch", "origin", "--quiet"], path, timeout=60)

    default_ref = _default_ref(path)
    if not default_ref:
        return False

    # (a) Normal (non-squash) merge: HEAD is already an ancestor of the default branch.
    proc = _git(["merge-base", "--is-ancestor", "HEAD", default_ref], path)
    if proc is not None and proc.returncode == 0:
        return True

    # (b) Squash/rebase merge: merging HEAD into the default branch would be a content
    # no-op — the merge's resulting tree is byte-identical to the default branch's tree.
    if HAS_WRITE_TREE:
        proc = _git(["merge-tree", "--write-tree", default_ref, "HEAD"], path, timeout=60)
        if proc is None or proc.returncode != 0:
            return False
        out_lines = proc.stdout.strip().splitlines()
        merged_tree = out_lines[0].strip() if out_lines else ""
        base_tree_proc = _git(["rev-parse", f"{default_ref}^{{tree}}"], path)
        if base_tree_proc is None or base_tree_proc.returncode != 0:
            return False
        base_tree = base_tree_proc.stdout.strip()
        return bool(merged_tree) and bool(base_tree) and merged_tree == base_tree
    else:
        # Fallback for git too old for `merge-tree --write-tree`: a weaker but still-safe
        # identical-tree check (misses some legitimate squash cases, never a false positive).
        proc = _git(["diff", "--quiet", default_ref, "HEAD"], path)
        return proc is not None and proc.returncode == 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--days", type=int, default=30, help="age threshold in days to consider for pruning (default 30)")
    args = p.parse_args()

    dev = Path(os.path.expanduser("~/dev"))
    if not dev.exists():
        print("no-dev-dir")
        return 0

    now = time.time()
    candidates = []
    for name in sorted(os.listdir(dev)):
        # Strict ClickUp-task-id shape only — never matches a real repo checkout name like
        # `ignite-digital-engine` or `ignite-marketplace` (see TASK_DIR_RE comment above).
        if not TASK_DIR_RE.match(name):
            continue
        wdir = dev / name
        if not wdir.is_dir():
            continue
        git_dir = wdir / '.git'
        # Must be a *linked* worktree (`.git` is a FILE pointing at a bare mirror's admin
        # dir), never a real full clone (`.git` is a directory). This is the second,
        # independent guardrail against ever touching a canonical repo checkout.
        if git_dir.exists() and not git_dir.is_file():
            print(f"SKIP_NOT_LINKED_WORKTREE: {name}")
            continue
        # Skip if deliverable present
        if (wdir / 'deliverable').exists():
            print(f"SKIP_DELIVERABLE: {name}")
            continue
        if git_dir.exists():
            # FAIL-CLOSED GATE (2026-07-10, mirrored from worktree_backstop_sweep.py):
            # deletion eligibility requires an affirmatively verified origin remote AND a
            # resolvable default ref. No remote, or a remote whose origin/HEAD/main/master
            # can't be resolved, means we can never prove the content is safely pushed/landed
            # elsewhere — protect, always, on any ambiguity.
            if not _has_origin_remote(wdir):
                print(f"SKIP_NO_REMOTE: {name} (no origin remote configured)")
                continue
            if _default_ref(wdir) is None:
                print(f"SKIP_NO_REMOTE: {name} (origin/HEAD not resolvable)")
                continue

            ahead = git_commits_ahead(wdir)
            if ahead is AHEAD_UNKNOWN:
                print(f"SKIP_NO_REMOTE: {name} (commits-ahead check failed — cannot verify, protecting)")
                continue
            if ahead > 0:
                if _content_landed(wdir):
                    print(f"LANDED_SQUASH: {name} | ahead={ahead} (content already on default branch, proceeding)")
                else:
                    print(f"SKIP_AHEAD_COMMITS: {name} | ahead={ahead}")
                    continue
        # age check
        try:
            mtime = wdir.stat().st_mtime
        except Exception:
            mtime = now
        age_days = (now - mtime) / 86400
        if age_days < args.days:
            print(f"SKIP_RECENT: {name} | age_days={age_days:.1f}")
            continue
        candidates.append((name, age_days, git_dir.exists()))

    if not candidates:
        print("no-stranded-candidates-found")
        return 0

    for name, age, has_git in candidates:
        path = dev / name
        if args.dry_run:
            print(f"DRY-CANDIDATE: {name} | age_days={age:.1f} | has_git={has_git}")
        else:
            print(f"PRUNING: {name} | age_days={age:.1f} | has_git={has_git}")
            try:
                # double-check before destructive action (re-check remote, ahead, and
                # content-landed too, in case state changed between the scan pass and now).
                # Any error/ambiguity here aborts — never falls through to deletion.
                if has_git:
                    if not _has_origin_remote(path) or _default_ref(path) is None:
                        print(f"ABORT_PRUNE_NO_REMOTE: {name}")
                        continue
                    ahead_final = git_commits_ahead(path)
                    if ahead_final is AHEAD_UNKNOWN:
                        print(f"ABORT_PRUNE_UNKNOWN_AHEAD: {name}")
                        continue
                    if ahead_final > 0 and not _content_landed(path):
                        print(f"ABORT_PRUNE_AHEAD: {name}")
                        continue
                # final safety: only remove if not a symlink and path is under ~/dev
                if str(path).startswith(str(dev)) and path.is_dir():
                    # use shutil.rmtree
                    import shutil
                    shutil.rmtree(path)
                    print(f"REMOVED: {name}")
                else:
                    print(f"FAILED_SAFETY: {name} | path check")
            except Exception as e:
                print(f"ERROR_REMOVING: {name} | {e}")

    return 0

if __name__ == '__main__':
    sys.exit(main())
