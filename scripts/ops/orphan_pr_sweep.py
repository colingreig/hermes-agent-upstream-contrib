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

THIRD GAP — committed-but-NEVER-PUSHED branches (86e2d6vjj): the remote sweep above scans
`git ls-remote` only, so a branch whose commit was made on a mini worktree but never pushed to
origin is completely invisible to it — it has no remote ref to list. This is a real strand
mode: task 86e250d61's commit bd65dda sat unpushed on a mini worktree and stayed "in review"
for ~13h because nothing was watching the local side. The `--check-unpushed` pass closes it by
enumerating LOCAL branches (across the current repo's worktrees AND the mini's shared bare
mirrors under ``~/.hermes/bare/*.git``), and flagging any that are ahead of base but whose tip
commit is not present on origin.

  Design note (push-reliability approach; deliberately conservative — 86e2d6vjj was filed
  "needs design judgment, do NOT auto-assign"): detection defaults to REPORT-ONLY and returns a
  non-zero exit so a cron surfaces it, rather than auto-pushing. Auto-push is gated behind the
  explicit ``--push-unpushed`` flag because the correct push remote is topology-dependent — on
  the mini's deploy checkout ``origin`` tracks the NousResearch upstream, while per-task
  worktrees push to the fork — so a blind ``git push origin`` from the wrong context could push
  to the wrong place or no-op. Detection is universally correct; the push is not, so the push is
  opt-in and the human/executor owns the remote choice until that topology is unified.

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
  python3 scripts/ops/orphan_pr_sweep.py --check-unpushed [--push-unpushed]

Env:
  CLICKUP_API_TOKEN — optional; enables the needs-validation cross-reference. Without it the
  script still does the git/gh orphan sweep and just skips the ClickUp cross-reference (logged,
  not fatal).
  HERMES_BARE_ROOT — optional; directory holding the mini's shared bare mirrors
  (``<owner>__<repo>.git``) scanned by --check-unpushed. Defaults to ``~/.hermes/bare``. A
  missing directory is simply skipped (the current repo's worktrees are always scanned).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request

DEFAULT_PREFIXES = ("agent/", "hermes/")
NEEDS_VALIDATION_TAG = "needs-validation"
CLICKUP_TEAM_ID = "9017245888"  # same team id as scripts/clickup_poll_gate.py
DEFAULT_BARE_ROOT = os.path.join(os.path.expanduser("~"), ".hermes", "bare")


def _run(cmd, **kwargs):
    """subprocess.run wrapper: capture output, never raise on non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _remote_branches(prefixes, remote="origin"):
    """Return `remote` branch names (no refs/heads/ prefix) matching any of `prefixes`.

    `remote` is configurable because the mini's deploy checkout uses ``origin`` for the
    NousResearch upstream and ``fork`` for colingreig/hermes-agent — where Hermes branches and
    PRs actually live (86e2d6vjj). Cron-wiring MUST pass ``--remote fork`` there or the sweep
    silently scans the wrong remote and finds nothing."""
    proc = _run(["git", "ls-remote", "--heads", remote])
    if proc.returncode != 0:
        print(f"[orphan-sweep] git ls-remote {remote} failed: {proc.stderr.strip()}", file=sys.stderr)
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


def _ahead_of_base(branch, base, remote="origin"):
    """True iff `branch` has commits not on `base` (best-effort; False on any git error
    so a single malformed ref never crashes the whole sweep). Uses the `remote`'s tracking
    refs (``<remote>/<base>``..``<remote>/<branch>``), so the caller must have fetched it."""
    proc = _run(["git", "rev-list", "--count", f"{remote}/{base}..{remote}/{branch}"])
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


def _gh(args, gh_repo=None):
    """Build a `gh` argv, injecting ``-R <owner/repo>`` when `gh_repo` is set so the call
    targets the fork explicitly rather than whatever gh infers from the checkout's remotes
    (which is ``origin`` = the NousResearch upstream on the mini — the wrong repo)."""
    cmd = ["gh"] + list(args)
    if gh_repo:
        cmd += ["-R", gh_repo]
    return cmd


def _existing_pr(branch, gh_repo=None):
    """Return the PR number for `branch` (open OR closed/merged — any of those means
    "not orphaned"), or None if no PR references it at all."""
    proc = _run(
        _gh(
            [
                "pr", "list",
                "--head", branch,
                "--state", "all",
                "--json", "number,state",
                "--limit", "1",
            ],
            gh_repo,
        )
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


def _last_commit_subject_body(ref):
    proc = _run(["git", "log", "-1", "--pretty=%s%x00%b", ref])
    if proc.returncode != 0 or "\x00" not in proc.stdout:
        return f"chore: recover orphaned branch {ref}", ""
    subject, _, body = proc.stdout.partition("\x00")
    return subject.strip() or f"chore: recover orphaned branch {ref}", body.strip()


def _create_pr(branch, base, dry_run, remote="origin", gh_repo=None):
    title, body = _last_commit_subject_body(f"{remote}/{branch}")
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
        _gh(
            [
                "pr", "create",
                "--head", branch,
                "--base", base,
                "--title", title,
                "--body", full_body,
            ],
            gh_repo,
        )
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


def _git(gitdir, *args):
    """Run git, optionally against an explicit --git-dir (for bare mirrors). Never raises."""
    cmd = ["git"]
    if gitdir:
        cmd += ["--git-dir", gitdir]
    return _run(cmd + list(args))


def _git_contexts(bare_root):
    """Yield (label, gitdir) git contexts to scan for local branches.

    Always yields the current working repo (gitdir=None → git uses discovery). Then yields
    every bare mirror under `bare_root` (the mini's shared ``~/.hermes/bare/<owner>__<repo>.git``
    worktree source). A branch committed in a per-task worktree lives in the mirror's
    refs/heads, so scanning the CWD repo alone would miss it on the mini.
    """
    yield ("cwd", None)
    if not bare_root or not os.path.isdir(bare_root):
        return
    try:
        for name in sorted(os.listdir(bare_root)):
            path = os.path.join(bare_root, name)
            # A bare repo dir ends in .git and has a HEAD file; be lenient and let git decide.
            if name.endswith(".git") and os.path.isdir(path):
                yield (name, path)
    except OSError as e:
        print(f"[orphan-sweep] could not list bare root {bare_root}: {e!r}", file=sys.stderr)


def _local_branch_tips(gitdir, prefixes):
    """Return {branch_name: tip_sha} for local refs/heads matching any prefix."""
    proc = _git(gitdir, "for-each-ref", "--format=%(refname:short)%00%(objectname)", "refs/heads/")
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        name, _, sha = line.partition("\x00")
        name, sha = name.strip(), sha.strip()
        if name and sha and any(name.startswith(p) for p in prefixes):
            out[name] = sha
    return out


def _on_origin(gitdir, branch, tip_sha, remote="origin"):
    """True iff `remote` already has `branch` at `tip_sha` (i.e. the local commits are pushed).
    Any error → False (treat as 'not confirmed on remote' so we report rather than silently
    swallow — detection fails loud, not closed)."""
    proc = _git(gitdir, "ls-remote", "--heads", remote, branch)
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        remote_sha = line.split("\t")[0].strip()
        if remote_sha == tip_sha:
            return True
    return False


def _ahead_of_base_local(gitdir, branch, base, remote="origin"):
    """Count commits on local `branch` not reachable from base. Prefers <remote>/<base>, falls
    back to a local <base> ref. Returns 0 on any error (so a missing base never crashes)."""
    for base_ref in (f"{remote}/{base}", base):
        proc = _git(gitdir, "rev-list", "--count", f"{base_ref}..{branch}")
        if proc.returncode == 0:
            try:
                return int(proc.stdout.strip())
            except ValueError:
                return 0
    return 0


def check_unpushed(base="main", prefixes=DEFAULT_PREFIXES, bare_root=None, push=False,
                   dry_run=False, remote="origin", gh_repo=None):
    """Detect committed-but-never-pushed local branches (86e2d6vjj gap 3).

    Returns 0 when nothing is stranded, 1 when at least one unpushed-ahead branch is found
    (so a cron treats it as an actionable condition). With `push=True` (opt-in), pushes each
    stranded branch to `remote` and opens its PR; otherwise it is report-only.
    """
    bare_root = bare_root if bare_root is not None else DEFAULT_BARE_ROOT
    stranded = []  # (label, gitdir, branch, sha, ahead)
    seen = set()   # de-dupe a branch that appears in multiple contexts (same name+sha)
    for label, gitdir in _git_contexts(bare_root):
        for branch, sha in _local_branch_tips(gitdir, prefixes).items():
            key = (branch, sha)
            if key in seen:
                continue
            if _on_origin(gitdir, branch, sha, remote):
                seen.add(key)
                continue
            ahead = _ahead_of_base_local(gitdir, branch, base, remote)
            if ahead <= 0:
                continue
            seen.add(key)
            stranded.append((label, gitdir, branch, sha, ahead))

    if not stranded:
        print("[orphan-sweep] check-unpushed: no committed-but-unpushed branches found")
        return 0

    print(
        f"[orphan-sweep] check-unpushed: {len(stranded)} committed-but-UNPUSHED branch(es) "
        f"ahead of {base} (invisible to the remote sweep):"
    )
    handled = 0
    for label, gitdir, branch, sha, ahead in stranded:
        print(
            f"[orphan-sweep]   {branch} ({sha[:12]}, +{ahead} commit(s) vs {base}) "
            f"in [{label}] — pushed=NO"
        )
        if not push:
            continue
        if dry_run:
            print(f"[orphan-sweep]   DRY-RUN would push+PR {branch}")
            handled += 1
            continue
        pushp = _git(gitdir, "push", "-u", remote, f"{branch}:{branch}")
        if pushp.returncode != 0:
            print(
                f"[orphan-sweep]   push failed for {branch}: {pushp.stderr.strip()}",
                file=sys.stderr,
            )
            continue
        print(f"[orphan-sweep]   pushed {branch} to {remote}")
        # After push, <remote>/<branch> exists; reuse the standard PR-create path.
        if _create_pr(branch, base, dry_run=False, remote=remote, gh_repo=gh_repo):
            handled += 1

    if push and handled:
        print(f"[orphan-sweep] check-unpushed: pushed/PR'd {handled}/{len(stranded)} branch(es)")
    # Non-zero: stranded branches are an actionable condition even after a push attempt.
    return 1


def sweep(base="main", prefixes=DEFAULT_PREFIXES, dry_run=False, remote="origin", gh_repo=None):
    branches = _remote_branches(prefixes, remote)
    if not branches:
        print(f"[orphan-sweep] no agent/* or hermes/* branches on {remote} — nothing to do")
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
        if not _ahead_of_base(branch, base, remote):
            skipped += 1
            continue
        pr_number = _existing_pr(branch, gh_repo)
        if pr_number is not None:
            skipped += 1
            continue
        print(f"[orphan-sweep] orphan candidate: {branch} (ahead of {base}, no PR)")
        if not _create_pr(branch, base, dry_run, remote=remote, gh_repo=gh_repo):
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
    parser.add_argument(
        "--check-unpushed",
        action="store_true",
        help="Detect committed-but-never-pushed local branches across worktrees/bare mirrors "
        "(86e2d6vjj gap 3). Report-only unless --push-unpushed is given; exits 1 if any found.",
    )
    parser.add_argument(
        "--push-unpushed",
        action="store_true",
        help="With --check-unpushed: actually push each stranded branch to origin and open its "
        "PR (opt-in; the correct push remote is topology-dependent — see module docstring).",
    )
    parser.add_argument(
        "--bare-root",
        default=None,
        help=f"Directory of bare mirrors scanned by --check-unpushed (default: {DEFAULT_BARE_ROOT} "
        "or $HERMES_BARE_ROOT).",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Git remote holding Hermes branches/PRs. On the mini deploy checkout this MUST be "
        "'fork' (origin there is the NousResearch upstream). Default: origin.",
    )
    parser.add_argument(
        "--gh-repo",
        default=None,
        help="owner/repo passed to `gh -R` so PR calls target the fork explicitly rather than "
        "gh's inference from remotes (e.g. colingreig/hermes-agent). Default: gh's inference.",
    )
    args = parser.parse_args()
    prefixes = tuple(args.prefixes) if args.prefixes else DEFAULT_PREFIXES
    if args.check_unpushed:
        bare_root = args.bare_root or os.environ.get("HERMES_BARE_ROOT") or DEFAULT_BARE_ROOT
        rc_unpushed = check_unpushed(
            base=args.base,
            prefixes=prefixes,
            bare_root=bare_root,
            push=args.push_unpushed,
            dry_run=args.dry_run,
            remote=args.remote,
            gh_repo=args.gh_repo,
        )
        # Run the remote sweep too so a single cron invocation covers both surfaces.
        rc_remote = sweep(
            base=args.base, prefixes=prefixes, dry_run=args.dry_run,
            remote=args.remote, gh_repo=args.gh_repo,
        )
        return rc_remote or rc_unpushed
    return sweep(
        base=args.base, prefixes=prefixes, dry_run=args.dry_run,
        remote=args.remote, gh_repo=args.gh_repo,
    )


if __name__ == "__main__":
    sys.exit(main())
