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
    content_landed(path, fetch_cache=None)         -> bool
    content_landed_check(path, fetch_cache=None)   -> LandedCheck (landed, fetch_failed, via)
    has_write_tree()                 -> bool  (repo-independent feature probe)
    HAS_WRITE_TREE                   -> bool, computed once at import time

2026-08-01 (mini fetch-failure triage, 86e2k...): two follow-on hardenings to
content_landed(), both fail-closed like everything else here:
  - `fetch_cache` (optional dict, caller-owned) lets a caller memoize fetch success/failure
    per resolved remote URL across multiple content_landed() calls in one run — e.g. the
    backstop sweep's early gate and its late re-check for the same worktree, or several
    linked worktrees sharing one bare mirror's remote. Passing None (default) preserves the
    original always-fetch-fresh behavior exactly.
  - content_landed_check() exposes WHY a result came back False: fetch_failed=True means the
    remote refresh itself failed (network/auth/etc — refs may be stale, cannot verify
    anything), vs fetch_failed=False + landed=False meaning the refresh succeeded but no
    landing proof was found. Callers that want to log/count these differently (e.g.
    SKIP_FETCH_FAILED vs SKIP_AHEAD_COMMITS) should use content_landed_check(); plain
    content_landed() remains a bool-only convenience wrapper for existing callers.
  - A second landing proof (`via="pr_merged"`) covers "merge-tree decay": once the default
    branch moves far enough past an old squash/rebase merge, byte-for-byte tree equality can
    stop proving landing even though the branch genuinely landed. When tree-equality is
    inconclusive, this asks GitHub directly via `gh pr list --head <branch> --state merged`
    whether the branch has a merged PR, resolving the repo strictly from `origin` and
    requiring the returned PR's head branch to match exactly. Any gh error, timeout,
    unparseable output, unresolvable origin URL, or detached HEAD fails closed (not landed).
"""
import json
import re
import subprocess
from pathlib import Path
from typing import NamedTuple


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


class LandedCheck(NamedTuple):
    """Result of content_landed_check(). `landed` is the actual verdict; `fetch_failed`
    distinguishes "could not even refresh the remote" from "refreshed fine, found no landing
    proof", so callers can log/count those differently while both still resolve to
    landed=False. `via` names which proof succeeded ("ancestor" | "squash_tree" |
    "pr_merged" | "" when not landed)."""
    landed: bool
    fetch_failed: bool
    via: str


_NOT_LANDED = LandedCheck(False, False, "")
_FETCH_FAILED = LandedCheck(False, True, "")


def _cache_key_for_remote(remote: str, path: Path):
    """Resolve `remote`'s URL for use as a fetch-memoization key, or None if it can't be
    resolved — callers must not memoize under an ambiguous/missing key."""
    proc = _git(["remote", "get-url", remote], path)
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _fetch_remote(remote: str, path: Path, fetch_cache) -> bool:
    """Fetch `remote` for `path`; True on success. When `fetch_cache` (a caller-owned dict)
    is given, memoize success/failure by the remote's resolved URL so repeated calls against
    the same remote within a run (e.g. a sweep's early gate + late re-check for the same
    worktree, or several linked worktrees/prefetched bare mirrors sharing one URL) skip the
    redundant network fetch. fetch_cache=None (default) always fetches fresh — the original,
    unmemoized behavior — so callers that don't opt in are unaffected."""
    cache_key = None
    if fetch_cache is not None:
        cache_key = _cache_key_for_remote(remote, path)
        if cache_key is not None and cache_key in fetch_cache:
            return fetch_cache[cache_key]

    proc = _git(["fetch", remote, "--quiet"], path, timeout=60)
    ok = proc is not None and proc.returncode == 0
    if fetch_cache is not None and cache_key is not None:
        fetch_cache[cache_key] = ok
    return ok


def _run_gh(args, timeout=30):
    """Thin, non-throwing `gh` invocation seam (mirrors _git's contract) so callers/tests can
    monkeypatch this single function instead of real subprocess/gh. Returns None on ANY
    failure (missing binary, timeout, etc) — callers must treat None as "cannot verify"."""
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception:
        return None


def _origin_repo_slug(path: Path):
    """Best-effort 'owner/repo' parsed from origin's remote URL (https or ssh form), or None
    if it can't be resolved/parsed — callers must treat that as "cannot verify", never guess."""
    proc = _git(["remote", "get-url", "origin"], path)
    if proc is None or proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    if not url:
        return None
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _merged_pr_landed(path: Path) -> bool:
    """Second landing proof (2026-08-01): ask GitHub directly whether HEAD's branch has a
    merged PR, for use when tree-equality can't prove landing (merge-tree decay — the default
    branch has moved past the point where the old squash/rebase merge's tree still matches
    byte-for-byte). Fails closed on ANY ambiguity: unresolvable/non-origin repo, detached
    HEAD, gh error/timeout/non-zero exit, unparseable JSON, or no exact head-branch match in
    a merged PR all return False. Repo is resolved strictly from `origin` (never `fork`) per
    the guard that only origin's PRs count as landing proof."""
    repo_slug = _origin_repo_slug(path)
    if not repo_slug:
        return False
    branch_proc = _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if branch_proc is None or branch_proc.returncode != 0:
        return False
    branch = branch_proc.stdout.strip()
    if not branch or branch == "HEAD":
        return False  # detached HEAD — no branch identity to check against gh

    proc = _run_gh(
        [
            "pr", "list",
            "--repo", repo_slug,
            "--head", branch,
            "--state", "merged",
            "--json", "number,mergedAt,headRefName",
        ],
        timeout=30,
    )
    if proc is None or proc.returncode != 0:
        return False
    try:
        rows = json.loads(proc.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict)
        and row.get("headRefName") == branch
        and row.get("mergedAt")
        for row in rows
    )


def content_landed(path: Path, fetch_cache=None) -> bool:
    """Bool-only convenience wrapper around content_landed_check() for callers that don't
    need to distinguish WHY a result is False. See content_landed_check() for the full
    contract; behavior and fail-closed guarantees are identical."""
    return content_landed_check(path, fetch_cache=fetch_cache).landed


def content_landed_check(path: Path, fetch_cache=None) -> LandedCheck:
    """Return LandedCheck(landed=True, ...) ONLY when the worktree's HEAD contributes NOTHING
    new relative to the default branch — i.e. it's safe to delete even though
    `origin/HEAD..HEAD` shows commits ahead (which happens after a squash/rebase merge
    rewrites history). Conservative by design: any error, ambiguity, or missing default ref
    returns landed=False (worktree survives). A false "not landed" is fine; a false "landed"
    is forbidden.

    `fetch_cache`: optional caller-owned dict for cross-call fetch memoization — see
    _fetch_remote(). None (default) always fetches fresh, matching the original behavior.
    """
    # Best-effort refresh so a stale local `origin/HEAD`/`origin/main` doesn't cause a false
    # NOT-landed for something that merged since the last fetch. The fetch commands remain
    # non-throwing via _git(), but a failed refresh means local refs may be stale enough to
    # create a false "landed" through ancestry or tree equality, so fail closed — and is
    # reported as fetch_failed=True (distinct from a clean refresh that just found nothing)
    # so callers can log/count the two situations differently.
    if not _fetch_remote("origin", path, fetch_cache):
        return _FETCH_FAILED
    remotes_proc = _git(["remote"], path)
    if (
        remotes_proc is not None
        and remotes_proc.returncode == 0
        and "fork" in remotes_proc.stdout.split()
    ):
        if not _fetch_remote("fork", path, fetch_cache):
            return _FETCH_FAILED

    ref = default_ref(path)
    if not ref:
        return _NOT_LANDED

    # (a) Normal (non-squash) merge: HEAD is already an ancestor of the default branch.
    # Shallow clones may not contain enough ancestry for this check; when git cannot prove
    # ancestry, conservatively fall through to the existing squash/tree-equality path.
    proc = _git(["merge-base", "--is-ancestor", "HEAD", ref], path)
    if proc is not None and proc.returncode == 0:
        return LandedCheck(True, False, "ancestor")

    # (b) Squash/rebase merge: merging HEAD into the default branch would be a content
    # no-op — the merge's resulting tree is byte-identical to the default branch's tree.
    if HAS_WRITE_TREE:
        proc = _git(["merge-tree", "--write-tree", ref, "HEAD"], path, timeout=60)
        if proc is not None and proc.returncode == 0:
            out_lines = proc.stdout.strip().splitlines()
            merged_tree = out_lines[0].strip() if out_lines else ""
            base_tree_proc = _git(["rev-parse", f"{ref}^{{tree}}"], path)
            if base_tree_proc is not None and base_tree_proc.returncode == 0:
                base_tree = base_tree_proc.stdout.strip()
                if merged_tree and base_tree and merged_tree == base_tree:
                    return LandedCheck(True, False, "squash_tree")
        # merge-tree errored, or the trees didn't match, or the base-tree lookup failed:
        # inconclusive, not a hard "not landed" — fall through to the merged-PR proof below
        # instead of failing closed immediately (this is the "merge-tree decay" case: once
        # the default branch has moved on, byte-identical trees stop proving an old
        # squash/rebase merge landed even though it genuinely did).
    else:
        # Fallback for git too old for `merge-tree --write-tree`: a weaker but still-safe
        # identical-tree check (misses some legitimate squash cases, never a false positive).
        proc = _git(["diff", "--quiet", ref, "HEAD"], path)
        if proc is not None and proc.returncode == 0:
            return LandedCheck(True, False, "squash_tree")

    # (c) Second landing proof: tree-equality was inconclusive — ask GitHub whether the
    # branch has a merged PR before giving up. Still fails closed on any gh ambiguity.
    if _merged_pr_landed(path):
        return LandedCheck(True, False, "pr_merged")

    return _NOT_LANDED
