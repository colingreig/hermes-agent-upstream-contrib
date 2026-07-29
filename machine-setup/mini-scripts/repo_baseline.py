#!/usr/bin/env python3
"""
repo_baseline.py — establish the origin/main baseline for a repo BEFORE deciding
that code is "missing".

WHY (2026-06-17, task 86e1xvdmj): the executor's shared checkout of
colingreig/ignite-digital-engine was parked on a stale branch
(`hermes/dedup-publish-route-helpers`, 891 commits behind main) that predates the
`lib/aoe/` subsystem. The executor saw `lib/aoe/outcomes/snapshot.ts` absent in
its WORKING TREE and escalated "code missing / unpushed" — an A/B operator
decision — without ever fetching or checking origin/main, where the file is fully
present. "Absent from my working tree" != "missing code." This script makes the
baseline check deterministic so that misread can't recur.

USAGE
  python3 repo_baseline.py status <repo_path> [--need <relpath> ...]

It fetches origin (read-only: NO checkout, NO merge, NO reset — only updates
remote-tracking refs), then reports:
  - current branch, and how far BEHIND/ahead of origin/<default-branch> it is
  - for each --need path: present on the baseline? present in the working tree?
    with the verdict you should act on.

The CALLER decides:
  - need present on baseline but ABSENT in tree  -> you are on a STALE BRANCH;
    branch/checkout off origin/<default>, proceed. This is a SELF-HEAL, never an
    operator escalation.
  - need ABSENT on baseline too                  -> genuine gap; escalate.
Exit 0 on a successful report; 2 on usage / not-a-repo.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

# All per-task worktrees live UNDER one root so they can't fragment ~/Projects
# and so GC has a single target. Never scatter sibling dirs next to the clone.
WORKTREE_ROOT = os.path.expanduser("~/.hermes/worktrees")


def _git(repo, *args, timeout=90):
    r = subprocess.run(["git", "-C", repo, *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def cmd_status(a):
    repo = os.path.expanduser(a.repo_path)
    if _git(repo, "rev-parse", "--git-dir")[0] != 0:
        print(f"NOT A GIT REPO: {repo}", file=sys.stderr)
        return 2

    fcode, _, ferr = _git(repo, "fetch", "origin", "--quiet")
    if fcode != 0:
        print(f"WARNING: git fetch origin failed ({ferr[:120]}); "
              "baseline reflects last-known remote refs", file=sys.stderr)

    # origin's default branch (falls back to main)
    c, sym, _ = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    default = sym.rsplit("/", 1)[1] if c == 0 and sym else "main"
    base = f"origin/{default}"

    _, cur, _ = _git(repo, "branch", "--show-current")
    behind = ahead = "?"
    c, counts, _ = _git(repo, "rev-list", "--left-right", "--count", f"{base}...HEAD")
    if c == 0 and len(counts.split()) == 2:
        behind, ahead = counts.split()

    print(f"repo: {repo}")
    print(f"current_branch: {cur or '(detached)'}")
    print(f"baseline: {base}")
    print(f"behind_baseline: {behind} | ahead: {ahead}")
    if behind not in ("0", "?"):
        print(f"NOTE: working tree is {behind} commits behind {base} — branch your "
              f"work off {base}, not the current branch.")

    genuine_gap = False
    for need in (a.need or []):
        on_base = _git(repo, "cat-file", "-e", f"{base}:{need}")[0] == 0
        in_tree = os.path.exists(os.path.join(repo, need))
        if on_base and not in_tree:
            verdict = (f"present on {base}, ABSENT in tree -> STALE BRANCH: "
                       f"branch/checkout off {base}; SELF-HEAL, do NOT escalate")
        elif in_tree:
            verdict = "present in working tree -> OK"
        elif not on_base:
            verdict = f"ABSENT on {base} too -> GENUINE GAP: escalate"
            genuine_gap = True
        else:
            verdict = "unknown"
        print(f"need '{need}': on_baseline={on_base} in_tree={in_tree} -> {verdict}")

    if a.need and not genuine_gap:
        print("RESULT: every needed path is reachable from the baseline — "
              "no missing-code escalation is warranted.")
    return 0


def _default_base(repo):
    c, sym, _ = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    default = sym.rsplit("/", 1)[1] if c == 0 and sym else "main"
    return f"origin/{default}"


def _worktree_for_branch(repo, branch):
    """Return the path of an existing worktree checked out on `branch`, or None."""
    c, out, _ = _git(repo, "worktree", "list", "--porcelain")
    if c != 0:
        return None
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return None


def cmd_prep(a):
    """ENFORCE a clean baseline: put the task in its OWN worktree branched off
    origin/<default>, so it can NEVER inherit a stale branch the shared clone is
    parked on (the 86e1xvdmj bug: shared clone sat 891 commits behind main). This
    is deterministic — the executor must NOT work the shared clone's current
    checkout. Idempotent: reuses the task's existing agent/<id> worktree on a
    continuation rather than re-creating it.

    Prints `WORKTREE=<path>` — cd there and work. tirith-safe: only `git fetch`
    and `git worktree add` (no reset --hard, no checkout of the shared clone)."""
    repo = os.path.abspath(os.path.expanduser(a.repo_path))
    if _git(repo, "rev-parse", "--git-dir")[0] != 0:
        print(f"NOT A GIT REPO: {repo}", file=sys.stderr)
        return 2

    fcode, _, ferr = _git(repo, "fetch", "origin", "--quiet")
    if fcode != 0:
        print(f"WARNING: git fetch origin failed ({ferr[:120]})", file=sys.stderr)

    base = a.base or _default_base(repo)
    branch = f"agent/{a.task_id}"

    # Continuation: reuse the existing per-task worktree (it was branched off the
    # baseline when first created — do NOT clobber its in-progress work).
    existing = _worktree_for_branch(repo, branch)
    if existing:
        _git(existing, "fetch", "origin", "--quiet")
        c, counts, _ = _git(existing, "rev-list", "--left-right", "--count", f"{base}...HEAD")
        behind = counts.split()[0] if c == 0 and counts else "?"
        print(f"reused existing worktree (continuation) on {branch}")
        print(f"WORKTREE={existing}")
        print(f"branch: {branch} | base: {base} | behind_base: {behind}")
        return 0

    os.makedirs(WORKTREE_ROOT, exist_ok=True)
    worktree_path = os.path.join(WORKTREE_ROOT, f"{os.path.basename(repo)}-{a.task_id}")
    branch_exists = _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")[0] == 0
    if branch_exists:
        rc, _, err = _git(repo, "worktree", "add", worktree_path, branch)
    else:
        rc, _, err = _git(repo, "worktree", "add", "-b", branch, worktree_path, base)
    if rc != 0:
        print(f"prep: `git worktree add` failed: {err[:200]}", file=sys.stderr)
        print("If the path exists from a dead run, remove it: "
              f"`python3 repo_baseline.py cleanup {repo} {a.task_id}`", file=sys.stderr)
        return 1

    print(f"created clean worktree off {base}")
    print(f"WORKTREE={worktree_path}")
    print(f"branch: {branch} | base: {base} | behind_base: 0")
    print(f"NEXT: cd {worktree_path} && work here — NOT the shared clone at {repo}.")
    print(f"On closeout, reclaim it: `python3 repo_baseline.py cleanup {repo} {a.task_id}`")
    return 0


def _branch_merged(repo, branch, base):
    """True if `branch` is fully contained in `base` (its work has shipped)."""
    return _git(repo, "merge-base", "--is-ancestor", branch, base)[0] == 0


def cmd_cleanup(a):
    """Reclaim ONE finished task's worktree + branch. Call at closeout. Refuses to
    delete UNMERGED work unless --force (so a not-yet-shipped continuation isn't
    lost). The remote `agent/<id>` branch is deleted by the PR merge, not here."""
    repo = os.path.abspath(os.path.expanduser(a.repo_path))
    branch = f"agent/{a.task_id}"
    base = a.base or _default_base(repo)
    _git(repo, "fetch", "origin", "--quiet")
    wt = _worktree_for_branch(repo, branch)
    merged = _branch_merged(repo, branch, base)
    if not merged and not a.force:
        # unmerged: check for unpushed/uncommitted before refusing
        print(f"cleanup: {branch} is NOT merged into {base}. Refusing to delete "
              "(use --force if you're sure the work is abandoned). Worktree: "
              f"{wt or '(none)'}", file=sys.stderr)
        return 1
    if wt:
        rc, _, err = _git(repo, "worktree", "remove", "--force", wt)
        print(f"worktree removed: {wt}" if rc == 0 else f"worktree remove failed: {err[:160]}")
    rc, _, err = _git(repo, "branch", "-D", branch)
    if rc == 0:
        print(f"branch deleted: {branch}")
    _git(repo, "worktree", "prune")
    return 0


def _worktree_dirty(path):
    """True if the worktree has uncommitted changes (never auto-remove those)."""
    return bool(_git(path, "status", "--porcelain")[1].strip())


def cmd_gc(a):
    """Deterministic janitor — keeps worktree sprawl bounded WITHOUT ever losing
    live work. Removes an agent/* worktree only when ALL hold: (1) its branch is
    merged into the baseline (work shipped / nothing unique to lose), (2) no
    uncommitted changes, and (3) it's been idle >= --max-age-days (default 3) —
    so a FRESHLY-prepped worktree (empty branch == ancestor of base, but recent)
    is KEPT, not mistaken for shipped-and-stale. Unmerged or dirty or recent
    worktrees are REPORTED, never auto-deleted. Meant to run on a schedule (e.g.
    the poll-gate), not by the executor."""
    repo = os.path.abspath(os.path.expanduser(a.repo_path))
    if _git(repo, "rev-parse", "--git-dir")[0] != 0:
        print(f"NOT A GIT REPO: {repo}", file=sys.stderr)
        return 2
    _git(repo, "fetch", "origin", "--quiet")
    _git(repo, "remote", "prune", "origin")
    _git(repo, "worktree", "prune")  # drop refs to manually-deleted dirs
    base = a.base or _default_base(repo)
    max_age = float(a.max_age_days)
    now = time.time()

    c, out, _ = _git(repo, "worktree", "list", "--porcelain")
    pairs, path, branch = [], None, None
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "")
        elif line == "":
            if path and branch and branch.startswith("agent/"):
                pairs.append((path, branch))
            path = branch = None

    main_wt = os.path.realpath(repo)
    removed, kept = [], []
    for path, branch in pairs:
        if os.path.realpath(path) == main_wt:
            # NEVER remove the primary checkout, even if it's on an agent/* branch
            kept.append(f"{branch}[primary]")
            continue
        try:
            idle_days = (now - os.path.getmtime(path)) / 86400.0
        except OSError:
            idle_days = 0.0
        merged = _branch_merged(repo, branch, base)
        dirty = _worktree_dirty(path)
        if merged and not dirty and idle_days >= max_age:
            _git(repo, "worktree", "remove", "--force", path)
            _git(repo, "branch", "-D", branch)
            removed.append(branch)
        else:
            why = ("dirty" if dirty else "unmerged" if not merged
                   else f"recent({idle_days:.1f}d<{max_age})")
            kept.append(f"{branch}[{why}]")

    # sweep orphaned dirs under WORKTREE_ROOT that are no longer git worktrees
    orphans = []
    if os.path.isdir(WORKTREE_ROOT):
        tracked = {p for p, _ in pairs}
        for name in os.listdir(WORKTREE_ROOT):
            full = os.path.join(WORKTREE_ROOT, name)
            if os.path.isdir(full) and full not in tracked and \
               _git(full, "rev-parse", "--git-dir")[0] != 0:
                shutil.rmtree(full, ignore_errors=True)
                orphans.append(name)
    print(f"gc {repo}: removed {len(removed)} shipped+stale worktree(s)"
          f"{' '+str(removed) if removed else ''}; kept {len(kept)}"
          f"{' '+str(kept) if kept else ''}; swept {len(orphans)} orphan dir(s)")
    return 0


def main():
    p = argparse.ArgumentParser(description="Repo origin/main baseline + clean-worktree prep.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="fetch origin + report behind-ness and --need paths")
    s.add_argument("repo_path")
    s.add_argument("--need", action="append",
                   help="a repo-relative path the task needs; repeatable")
    s.set_defaults(fn=cmd_status)

    pr = sub.add_parser("prep", help="ENFORCE a clean worktree branched off origin/<default> for a task")
    pr.add_argument("repo_path")
    pr.add_argument("task_id")
    pr.add_argument("--base", help="override baseline ref (default origin/<default-branch>)")
    pr.set_defaults(fn=cmd_prep)

    cl = sub.add_parser("cleanup", help="reclaim ONE finished task's worktree+branch (call at closeout)")
    cl.add_argument("repo_path")
    cl.add_argument("task_id")
    cl.add_argument("--base", help="override baseline ref")
    cl.add_argument("--force", action="store_true", help="delete even if unmerged (abandoned work)")
    cl.set_defaults(fn=cmd_cleanup)

    gc = sub.add_parser("gc", help="janitor: remove shipped+stale agent/* worktrees, prune dangling (run on a schedule)")
    gc.add_argument("repo_path")
    gc.add_argument("--base", help="override baseline ref")
    gc.add_argument("--max-age-days", default=3,
                    help="only remove merged worktrees idle at least this long (default 3)")
    gc.set_defaults(fn=cmd_gc)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
