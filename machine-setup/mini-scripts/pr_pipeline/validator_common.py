#!/usr/bin/env python3
"""validator_common.py — shared, zero-LLM helpers for the Hermes PR validator.

Used by risk_classify.py and validate_tripwires.py so the diff parser and the
gh-fetch path exist in exactly ONE place. Pure stdlib + the `gh` CLI. No network
beyond `gh`. Importable and side-effect-free (functions only).

A "diff" here is a unified diff (the output of `git diff` / `gh pr diff`). We parse
it into per-file records carrying the ADDED lines (what the PR introduces) — that
is what every downstream check reasons about. Removed/context lines are kept too
for the few checks that need surrounding context.
"""
import json
import os
import re
import subprocess
import sys

GH_TIMEOUT = 60

# --- diff parsing -----------------------------------------------------------

_DIFF_GIT = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_NEW_FILE = re.compile(r"^new file mode ")
_PLUS_FILE = re.compile(r"^\+\+\+ b/(.+)$")


class FileDiff(dict):
    """dict with keys: path, added (list[str]), removed (list[str]),
    is_new_file (bool). added_text / removed_text are joined convenience views."""

    @property
    def added_text(self) -> str:
        return "\n".join(self.get("added", []))

    @property
    def removed_text(self) -> str:
        return "\n".join(self.get("removed", []))


def parse_unified_diff(text: str):
    """Parse a unified diff into a list[FileDiff]. Robust to `gh pr diff` and
    plain `git diff` output. Lines starting with '+' (not '+++') are additions;
    '-' (not '---') are removals."""
    files = []
    cur = None
    for line in (text or "").splitlines():
        m = _DIFF_GIT.match(line)
        if m:
            if cur is not None:
                files.append(cur)
            cur = FileDiff(path=m.group(2), added=[], removed=[], is_new_file=False)
            continue
        if cur is None:
            continue
        if _NEW_FILE.match(line):
            cur["is_new_file"] = True
            continue
        pm = _PLUS_FILE.match(line)
        if pm:
            # canonical path (handles renames); ignore /dev/null
            if pm.group(1) != "/dev/null":
                cur["path"] = pm.group(1)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            cur["added"].append(line[1:])
        elif line.startswith("-"):
            cur["removed"].append(line[1:])
    if cur is not None:
        files.append(cur)
    return files


# --- gh fetch ---------------------------------------------------------------

def fetch_pr_diff(repo: str, pr: int) -> str:
    """Return the unified diff for a PR via `gh pr diff`. '' on error.

    When gh pr diff returns an HTTP 406 (>300-file limit), falls back to a
    per-file summary built from the PR's file list (gh pr view --json files).
    This is NOT the full diff, but it is enough to run tripwires and risk
    classification — the caller gets a non-empty diff-like text so the PR
    does NOT get a permanent fail-closed BLOCK. The returned text carries a
    sentinel comment so downstream callers know it is a summary, not a full
    diff. Reversibility: replace this function with the original one-liner.
    """
    try:
        r = subprocess.run(
            ["gh", "pr", "diff", str(pr), "--repo", repo],
            capture_output=True, text=True, timeout=GH_TIMEOUT)
        if r.returncode == 0:
            return r.stdout
        stderr_low = r.stderr.lower()
        is_406 = ("406" in r.stderr or "too many files" in stderr_low
                  or "exceeds" in stderr_low)
        if not is_406:
            print(f"[validator] gh pr diff {repo}#{pr} rc={r.returncode} "
                  f"{r.stderr.strip()[:160]}", file=sys.stderr)
            return ""
        # 406 / >300-file path: build a summary diff from the file list
        print(f"[validator] gh pr diff {repo}#{pr} 406 (>300 files) — "
              "falling back to file-list summary", file=sys.stderr)
        return _fetch_pr_diff_summary(repo, pr)
    except Exception as e:
        print(f"[validator] gh pr diff error: {e!r}", file=sys.stderr)
        return ""


def _fetch_pr_diff_summary(repo: str, pr: int) -> str:
    """Fallback for >300-file PRs: build a pseudo-diff from `gh pr view --json
    files`. Returns a text that parse_unified_diff can parse (file headers only,
    no line-level adds/removes) plus a header comment marking it as a summary.
    Risk classification and tripwires still run; the panel sees the truncation
    note. Produces a BLOCK only on real risk signals, not on file-count alone.
    Returns '' on any gh error (callers treat '' as unresolvable)."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo,
             "--json", "files,title,body"],
            capture_output=True, text=True, timeout=GH_TIMEOUT)
        if r.returncode != 0:
            print(f"[validator] file-list fallback: gh pr view rc={r.returncode} "
                  f"{r.stderr.strip()[:120]}", file=sys.stderr)
            return ""
        data = json.loads(r.stdout) if r.stdout.strip() else {}
        files = data.get("files") or []
        if not files:
            return ""
        lines = [
            "# VALIDATOR-SUMMARY: full diff unavailable (>300 files); "
            "file list only — tripwires and risk classification run on paths, "
            "panel sees this note.\n",
            f"# PR: {repo}#{pr}  title={data.get('title','')[:120]}\n",
        ]
        for f in files:
            path = f.get("path", "")
            adds = f.get("additions", 0)
            dels = f.get("deletions", 0)
            lines.append(f"diff --git a/{path} b/{path}\n")
            lines.append(f"+++ b/{path}\n")
            # Emit a synthetic +/- header line so line-based tripwires see the
            # path; no real content lines since we don't have them.
            if adds:
                lines.append(f"+# {adds} line(s) added\n")
            if dels:
                lines.append(f"-# {dels} line(s) removed\n")
        return "".join(lines)
    except Exception as e:
        print(f"[validator] file-list fallback error: {e!r}", file=sys.stderr)
        return ""


def pr_head_sha(repo: str, pr: int) -> str:
    """Return the PR head commit SHA via `gh pr view`. '' on error."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo,
             "--json", "headRefOid", "-q", ".headRefOid"],
            capture_output=True, text=True, timeout=GH_TIMEOUT)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def load_diff_from_args(args):
    """Resolve a diff from CLI args supporting three sources, in order:
      --diff <file>  |  --repo <owner/repo> --pr <n>  |  stdin
    Returns (diff_text, source_label)."""
    if getattr(args, "diff", None):
        with open(os.path.expanduser(args.diff), encoding="utf-8") as f:
            return f.read(), f"file:{args.diff}"
    if getattr(args, "repo", None) and getattr(args, "pr", None):
        return fetch_pr_diff(args.repo, args.pr), f"{args.repo}#{args.pr}"
    if not sys.stdin.isatty():
        return sys.stdin.read(), "stdin"
    return "", "(none)"


def add_source_args(parser):
    """Attach the standard diff-source flags to an argparse parser."""
    parser.add_argument("--diff", help="path to a unified diff file")
    parser.add_argument("--repo", help="owner/repo (with --pr, fetch via gh)")
    parser.add_argument("--pr", type=int, help="PR number (with --repo)")
