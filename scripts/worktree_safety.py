#!/usr/bin/env python3
"""worktree_safety.py — single source of truth for worktree deletion-safety predicates.

Extracted (2026-07-10) from worktree_backstop_sweep.py so both the age-based backstop
sweep and the merge-aware cleaner (`cleanup_hermes_state.py`) share ONE hardened
implementation instead of drifting copies. This module makes NO destructive decisions
itself — it only answers safety questions. Callers own the actual removal logic and
must still apply their own gates (claim checks, deliverable checks, age, etc.) in
addition to what's here.

FAIL CLOSED, always: every predicate here is designed so that any error, timeout,
ambiguity, or unparseable git output resolves toward "not provably safe to delete" —
never toward "safe to delete". This is what protects things like an unpushed, no-remote
clone (e.g. ignite-86e251a3e) from being swept just because a check errored out and was
misread as a green light. If you extend this module, preserve that invariant.

Public API:
    _git(args, cwd, timeout=30)      -> subprocess.CompletedProcess | None
    is_dirty(path)                   -> bool
    has_origin_remote(path)          -> bool
    default_ref(path)                -> str | None
    AHEAD_UNKNOWN                    -> sentinel (None) for "cannot verify ahead-count"
    commits_ahead(path)              -> int | AHEAD_UNKNOWN
    content_landed(path)             -> bool
    has_write_tree()                 -> bool  (repo-independent feature probe)
    HAS_WRITE_TREE                   -> bool, computed once at import time
"""
import subprocess
from pathlib import Path


def _git(args, cwd, timeout=30):
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None


AHEAD_UNKNOWN = None  # sentinel: git error / unparseable output — caller must NOT treat as 0


def commits_ahead(path: Path):
    """Return the commit count HEAD is ahead of origin/HEAD, or AHEAD_UNKNOWN (None) if the
    check could not be performed reliably. FAILS CLOSED (2026-07-10 hardening): a git error,
    non-zero exit, or unparseable output must never be silently read as ahead=0 — that reading
    is what let an unpushed, no-remote clone (ignite-86e251a3e) look deletion-safe. Callers
    must treat AHEAD_UNKNOWN as "cannot verify" -> protect, never as "0 -> safe"."""
    proc = _git(["rev-list", "--count", "origin/HEAD..HEAD"], path)
    if proc is None or proc.returncode != 0:
        return AHEAD_UNKNOWN
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return AHEAD_UNKNOWN


def has_origin_remote(path: Path) -> bool:
    """True only if `origin` is configured with a non-empty URL. No remote means there is
    nowhere the content could have been safely pushed to — deletion must never proceed
    without this being affirmatively true."""
    proc = _git(["remote", "get-url", "origin"], path)
    return bool(proc is not None and proc.returncode == 0 and proc.stdout.strip())


def is_dirty(path: Path) -> bool:
    proc = _git(["status", "--porcelain"], path)
    return bool(proc is not None and proc.returncode == 0 and proc.stdout.strip())


def has_write_tree() -> bool:
    """Repo-independent feature probe: `git merge-tree -h` prints usage (exit 0) on any
    git new enough to support the modern `--write-tree` mode, even outside a repo. Safe
    to call once at import time — read-only, no cwd/repo requirement."""
    try:
        proc = subprocess.run(
            ["git", "merge-tree", "-h"], capture_output=True, text=True, timeout=10, check=False,
        )
        return "--write-tree" in (proc.stdout + proc.stderr)
    except Exception:
        return False


HAS_WRITE_TREE = has_write_tree()


def _resolve_ref_for_remote(remote: str, path: Path):
    """Try to resolve <remote>/HEAD -> <remote>/main -> <remote>/master for the given
    remote name. Returns the ref string, or None if none of the candidates resolve."""
    proc = _git(["rev-parse", "--abbrev-ref", f"{remote}/HEAD"], path)
    if proc is not None and proc.returncode == 0:
        ref = proc.stdout.strip()
        if ref and ref != "HEAD":
            return ref
    for candidate in (f"{remote}/main", f"{remote}/master"):
        proc = _git(["rev-parse", "--verify", "--quiet", candidate], path)
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return candidate
    return None


def default_ref(path: Path):
    """Resolve the repo's default branch ref (e.g. 'origin/main'), or None if it can't be
    determined. Returning None is the safe direction — callers treat it as NOT landed.

    Split-remote topology (2026-07-10): some checkouts (e.g. hermes-agent on the mini)
    have `origin` pointed at an unrelated upstream (NousResearch) while the real working
    remote is `fork` (the colingreig fork actually pushed to). When a `fork` remote is
    configured, prefer fork/HEAD -> fork/main -> fork/master; only if none of those
    resolve does this fall through to the original origin/HEAD -> origin/main ->
    origin/master behavior, unchanged."""
    remotes_proc = _git(["remote"], path)
    if (
        remotes_proc is not None
        and remotes_proc.returncode == 0
        and "fork" in remotes_proc.stdout.split()
    ):
        fork_ref = _resolve_ref_for_remote("fork", path)
        if fork_ref:
            return fork_ref

    return _resolve_ref_for_remote("origin", path)


def content_landed(path: Path) -> bool:
    """Return True ONLY when the worktree's HEAD contributes NOTHING new relative to the
    default branch — i.e. it's safe to delete even though `origin/HEAD..HEAD` shows commits
    ahead (which happens after a squash/rebase merge rewrites history). Conservative by
    design: any error, ambiguity, or missing default ref returns False (worktree survives).
    A false "not landed" is fine; a false "landed" is forbidden."""
    # Best-effort refresh so a stale local `origin/HEAD`/`origin/main` doesn't cause a false
    # NOT-landed for something that merged since the last fetch. Never let failure here be
    # fatal, and never let it flip a result toward "landed" — worst case it's a no-op.
    _git(["fetch", "origin", "--quiet"], path, timeout=60)

    ref = default_ref(path)
    if not ref:
        return False

    # (a) Normal (non-squash) merge: HEAD is already an ancestor of the default branch.
    proc = _git(["merge-base", "--is-ancestor", "HEAD", ref], path)
    if proc is not None and proc.returncode == 0:
        return True

    # (b) Squash/rebase merge: merging HEAD into the default branch would be a content
    # no-op — the merge's resulting tree is byte-identical to the default branch's tree.
    if HAS_WRITE_TREE:
        proc = _git(["merge-tree", "--write-tree", ref, "HEAD"], path, timeout=60)
        if proc is None or proc.returncode != 0:
            return False
        out_lines = proc.stdout.strip().splitlines()
        merged_tree = out_lines[0].strip() if out_lines else ""
        base_tree_proc = _git(["rev-parse", f"{ref}^{{tree}}"], path)
        if base_tree_proc is None or base_tree_proc.returncode != 0:
            return False
        base_tree = base_tree_proc.stdout.strip()
        return bool(merged_tree) and bool(base_tree) and merged_tree == base_tree
    else:
        # Fallback for git too old for `merge-tree --write-tree`: a weaker but still-safe
        # identical-tree check (misses some legitimate squash cases, never a false positive).
        proc = _git(["diff", "--quiet", ref, "HEAD"], path)
        return proc is not None and proc.returncode == 0
