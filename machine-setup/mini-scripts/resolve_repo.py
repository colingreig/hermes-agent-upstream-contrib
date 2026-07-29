#!/usr/bin/env python3
"""
resolve_repo.py — turn a repo name from a ClickUp task body into a real repo
BEFORE declaring it "missing" (BLOCKED-ON-EXTERNAL).

WHY (2026-06-17, 86e1xvdce/dmj/dgk): three tasks said `Repo:
ignitemarketing.com-client` and the executor hard-failed them to a re-claiming
escalation loop. That repo was NOT missing — it is the Ignite Workbench codebase,
renamed to colingreig/ignite-digital-engine. A rename shares no tokens with the
old name, so fuzzy matching can't bridge it; the fix is an explicit ALIAS MAP
(safe auto-resolve) plus a fuzzy step that only SURFACES candidates for the
escalation menu (never auto-picks — jdmbuysell-v2/v3/v4 are distinct repos one
char apart, so a blind fuzzy auto-resolve would silently work the wrong repo).

Resolution order:
  1. ALIAS  — ~/.hermes/repo-aliases.txt exact (case-insensitive) hit -> auto-resolve.
  2. EXACT  — name matches a disk clone, the allowlist, or `gh repo list` (either
              org), case-insensitive -> auto-resolve.
  3. FUZZY  — close matches become CANDIDATES for the escalation comment; NOT an
              auto-resolve. Exit 3 so the caller posts a "pick one" menu instead
              of a dead-end "missing repo".
  4. NONE   — exit 3 with empty candidates -> genuine BLOCKED-ON-EXTERNAL.

Usage:
  python3 resolve_repo.py "ignitemarketing.com-client"
  -> prints JSON: {"input","resolved":"owner/repo"|null,"via","clone_path","candidates":[...]}
  Exit 0 = resolved (auto), 3 = needs operator/menu, 2 = usage error.

Env: HOME, optional CLONE_ROOT (default ~/dev). gh must be authed for the org probe.
"""
import json
import os
import subprocess
import sys
from difflib import get_close_matches

HOME = os.path.expanduser("~")
ALLOWLIST = os.path.join(HOME, ".hermes", "allowed-repos.txt")
ALIASES = os.path.join(HOME, ".hermes", "repo-aliases.txt")
CLONE_ROOT = os.environ.get("CLONE_ROOT", os.path.join(HOME, "dev"))
ORGS = ("colingreig", "ignitemarketing")


def _load_aliases():
    out = {}
    try:
        with open(ALIASES) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return out


def _load_allowlist():
    try:
        with open(ALLOWLIST) as f:
            return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def _gh_repos():
    """[owner/name] across both orgs; [] if gh is unavailable."""
    out = []
    for org in ORGS:
        try:
            r = subprocess.run(["gh", "repo", "list", org, "--limit", "300",
                                "--json", "nameWithOwner"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                out += [x["nameWithOwner"] for x in json.loads(r.stdout or "[]")]
        except Exception:
            continue
    return out


def _basename(full):
    return full.split("/", 1)[1] if "/" in full else full


_canon_cache = {}


def _canonicalize(owner_repo):
    """Resolve owner/repo through the GitHub API's own full_name, so a repo
    reached via a stale alias/allowlist entry (pointing at an OLD name that
    GitHub silently redirects) collapses to the SAME string as one reached
    directly by its current name. Without this, two different input strings
    that both land on the identical repo look like two different repos to
    any caller doing repo-identity comparison (e.g. the validator's
    cross-repo sweep deduping verdicts) — this produced duplicate BLOCK
    verdicts on task 86e25xnhq when "ignite-workbench" (direct) and
    "ignite-digital-engine" (a now-stale alias for the same repo, after it
    was renamed a second time) were treated as distinct. Fails open (returns
    the input unchanged) on any error — never block resolution on this.
    """
    if owner_repo in _canon_cache:
        return _canon_cache[owner_repo]
    canonical = owner_repo
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}", "--jq", ".full_name"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            canonical = r.stdout.strip()
    except Exception:
        pass
    _canon_cache[owner_repo] = canonical
    return canonical


def resolve(name):
    q = name.strip()
    ql = q.lower()
    aliases = _load_aliases()
    allow = _load_allowlist()
    # known universe of owner/repo (allowlist is authoritative; gh fills gaps)
    universe = list(dict.fromkeys(allow + _gh_repos()))
    by_base = {_basename(u).lower(): u for u in universe}

    def result(resolved, via, candidates):
        if resolved:
            resolved = _canonicalize(resolved)
        path = os.path.join(CLONE_ROOT, _basename(resolved)) if resolved else ""
        # clone_path is just CLONE_ROOT/<basename> — it is NOT proof the clone exists.
        # The executor MUST clone from remote_url when clone_exists is false; pointing a
        # worktree at a non-existent clone_path is the 2026-06-23 86e1z37du wasted-tick bug.
        clone_exists = bool(resolved) and os.path.isdir(os.path.join(path, ".git"))
        remote_url = f"https://github.com/{resolved}.git" if resolved else ""
        return {"input": q, "resolved": resolved, "via": via,
                "clone_path": path, "clone_exists": clone_exists,
                "remote_url": remote_url, "candidates": candidates}

    # 1. ALIAS
    if ql in aliases:
        return 0, result(aliases[ql], "alias", [])

    # 2. EXACT (disk clone, allowlist, or gh — case-insensitive on the basename)
    if os.path.isdir(os.path.join(CLONE_ROOT, q)) and ql in by_base:
        return 0, result(by_base[ql], "disk", [])
    if ql in by_base:
        return 0, result(by_base[ql], "exact", [])
    #    also accept an exact owner/repo the caller already fully-qualified
    if q in universe:
        return 0, result(q, "exact", [])

    # 3. FUZZY — surface candidates only; do NOT auto-resolve
    bases = list(by_base.keys())
    close = get_close_matches(ql, bases, n=5, cutoff=0.6)
    candidates = [by_base[c] for c in close]
    return 3, result(None, "fuzzy-candidates" if candidates else "none", candidates)


def main():
    if len(sys.argv) != 2 or not sys.argv[1].strip():
        print('usage: resolve_repo.py "<repo-name-from-task-body>"', file=sys.stderr)
        sys.exit(2)
    code, res = resolve(sys.argv[1])
    print(json.dumps(res, indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
