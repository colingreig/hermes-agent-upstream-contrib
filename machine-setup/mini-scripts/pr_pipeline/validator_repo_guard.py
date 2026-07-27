#!/usr/bin/env python3
"""
validator_repo_guard.py — repository-identity guard for the hermes-pr-validate
cron job (id 5a76e290811d, `~/.hermes/cron/jobs.json`).

=============================================================================
RC1 (2026-07-26, morning) — workdir sanity guard
=============================================================================
That job's `workdir` was hardcoded to `/Users/colingreig/dev/thermal`, but the
job is chartered (by name and config) to validate the hermes-agent ClickUp
board. ignite-validate's Step 1 resolves the ClickUp board from the session's
cwd, so every hermes-agent task picked up under that misconfigured workdir got
a resolved "board repo" of `thermal` instead of `hermes-agent`. Fixed at the
source by repointing `workdir` in jobs.json; this script is defense-in-depth
(`--expect-repo` mode) so a *future* workdir misconfiguration aborts the pass
instead of quietly reproducing the same spurious-FAIL failure mode.

=============================================================================
RC2 (2026-07-26, afternoon) — the RENAME-ALIAS false mismatch
=============================================================================
Repointing the workdir did NOT stop the spurious `class=wrong-repo` FAILs,
because the deeper defect is that repo identity was compared as a NAME STRING.

On 2026-07-23 `colingreig/hermes-agent` was RENAMED to
`colingreig/hermes-agent-upstream-contrib`. GitHub redirects the old name, so
BOTH names address one single repository (id 1267898035, node R_kgDOS5KWsw).
But the mini now holds both spellings simultaneously:

  * `/Users/colingreig/dev/hermes-agent` -> origin
    https://github.com/colingreig/hermes-agent.git                      (OLD name)
  * `~/.hermes/bare/colingreig__hermes-agent-upstream-contrib.git`
    and every `wt-new` worktree cut from it                             (NEW name)
  * ClickUp Execution Briefs carrying a literal `Target repo: hermes-agent`
                                                                        (OLD name)
  * Handoff PR / CI URLs produced from those worktrees                  (NEW name)

`ignite-state/scripts/target-repo.mjs` compares `basename(nameWithOwner)`
against the declared `Target repo:` basename and hard-fails on disagreement,
and ignite-validate's SKILL.md then converts that (plus any PR/CI URL whose
owner/name text differs from the resolved target) into an automatic
`class=wrong-repo` FAIL. Result: correct, reviewed, merged hermes-agent work
was FAILed purely because two spellings of ONE repo were string-compared.
Confirmed spurious and manually voided: 86e2gh04e, 86e2gdmfk, 86e25xww8,
86e2f7ukm.

Fix: resolve every repo reference to a CANONICAL GITHUB IDENTITY (the repo's
immutable `node_id`, obtained via `gh api repos/<owner>/<name>`, which FOLLOWS
renames) and compare canonical identities, never name strings. This is
deliberately generic: the next rename resolves itself with no code change and
no alias-list edit. An alias file exists only as a cold-cache offline last
resort (see REPO ALIAS FILE below).

Genuinely-different repositories still resolve to different node ids and are
still caught, e.g. `colingreig/brain` (R_kgDOS4oHNQ) vs
`colingreig/hermes-agent-upstream-contrib` (R_kgDOS5KWsw). Confirmed genuine
wrong-repo FAILs that must keep failing: 86e2eu4fp (hermes-ops-scripts),
86e264vgu (brain PR #4), 86e1z37du (ignite-marketplace).

DEFINITE-404 INFERENCE. The mini's GitHub App token (hermes-dev-assistant[bot])
is installed per repository, so some real repos 404 for it — e.g.
`colingreig/hermes-ops-scripts`, the genuine wrong-repo in 86e2eu4fp. Treating
"unresolvable" as UNRESOLVED there would let a genuine wrong-repo escape as a
skip. But a 404 is still decisive in one direction: GitHub's rename redirect
and App installation access are BOTH keyed on the repository id, so if the
same credential resolves the target LIVE and then 404s on the evidence slug,
the evidence slug cannot be a rename alias of the target — an alias would have
redirected to the repo the token just read. That yields DIFFERENT without ever
learning what the evidence repo is. A non-404 `gh` failure (network, auth,
rate limit, timeout) proves nothing and still yields UNRESOLVED.

=============================================================================
RC3 (2026-07-26, evening) — alias file was consulted BEFORE live gh
=============================================================================
`compare_refs()` originally checked the static alias file before ever calling
`gh`, as a network short-circuit. That inverted the documented precedence: the
alias file (a hand-maintained, editable, can-go-stale file) could answer SAME
even when live `gh` would have said DIFFERENT, which defeats the entire point
of resolving identity live instead of by name string — a rename pair that is
one repo TODAY could diverge into two real repos LATER, and a leftover alias
entry would then silently authorize merging work validated against the wrong
one. Fixed by moving the alias check to run only after live `gh` (and the
identity cache) have already been tried and failed to resolve one or both
sides — a true offline last resort, never a way to override a live answer.

=============================================================================
FAIL-CLOSED RULE (both modes)
=============================================================================
When repo identity cannot be established WITH CONFIDENCE, the validator must
SKIP/ABORT the task and write NO verdict — never a `wrong-repo` FAIL. A
spurious FAIL kicks real reviewed work back to `to do` and burns multi-day
executor rework loops; a skip costs one hourly cycle. Exit code 3 always means
"stop, write nothing". Only exit 4 (`--compare`) authorizes a wrong-repo FAIL.

Usage:
  # Mode A — is this session's checkout the repo this cron is chartered for?
  validator_repo_guard.py --expect-repo <repo> [--workdir PATH]

  # Mode B — is the evidence (PR/CI URL, owner/name, bare name) the SAME
  #          repository as the task's resolved target?
  validator_repo_guard.py --compare <target-ref> <evidence-ref> \
      [--default-owner colingreig]

  # Mode C — print one ref's canonical identity (debugging)
  validator_repo_guard.py --identity <ref> [--default-owner colingreig]

Exit codes:
  0 = SAME / OK          -> safe to proceed
  3 = ABORT (skip)       -> identity unresolvable OR chartered-repo mismatch.
                            Caller MUST abort the pass and MUST NOT write a
                            FAIL verdict or change any task status.
  4 = DIFFERENT          -> canonical identities confirmed distinct. This is a
                            genuine wrong-repo; caller MAY write the
                            `class=wrong-repo` FAIL. (`--compare` only.)
  2 = usage error

REPO ALIAS FILE (discoverable, documented, last-resort only)
  Path:  ~/.hermes/config/repo-aliases.json   (override: $HERMES_REPO_ALIASES)
  Shape: {"aliases": [["colingreig/old-name", "colingreig/new-name"], ...]}
  Each inner list is one equivalence group of `owner/name` spellings that
  address a single repository. Consulted ONLY AFTER live `gh` (and the
  identity cache) has failed to establish a canonical identity for one or
  both sides -- never before, and never in place of a live/cached lookup
  that already succeeded. That ordering is deliberate (fixed 2026-07-26,
  RC3 below): a stale or wrong alias entry must not be able to override a
  live GitHub answer. It only helps when the mini is offline with a cold
  identity cache. Prefer letting `gh` follow the redirect — add entries
  here only for a repo `gh` can no longer resolve at all (deleted/transferred).

IDENTITY CACHE
  Path:  ~/.hermes/state/repo_identity_cache.json
         (override: $HERMES_REPO_IDENTITY_CACHE)
  ref -> {node_id, full_name, ts}. TTL 7 days; STALE ENTRIES ARE SERVED when
  `gh` fails, so a transient network/auth blip degrades to "use last known
  identity" rather than to a fleet-wide skip.

Compatibility note: the mini's bare `python3` on PATH is 3.9 (no PEP 604 `X |
Y` type syntax at runtime); `from __future__ import annotations` keeps this
importable/runnable there without requiring callers to use the 3.11 venv.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_OWNER = "colingreig"
CACHE_TTL_SECONDS = 7 * 24 * 3600
GH_TIMEOUT_SECONDS = 25

# ignite-ship's shared checkout-identity helper. Reused (never reimplemented)
# because it already handles linked `git worktree` checkouts and Conductor
# workspaces, where `basename($PWD)` is the workspace name, not the repo name.
REPO_IDENTITY_SH_GLOB = (
    "{home}/.claude/plugins/cache/ignite-skills/ignite/*/skills/"
    "ignite-ship/scripts/repo-identity.sh"
)


# ---------------------------------------------------------------------------
# Reference parsing
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(r"[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$")
_GITHUB_URL_RE = re.compile(
    r"(?:^|//|@)github\.com[:/]+([A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?(?:/|$)"
)
_SLUG_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)$"
)
_BARE_NAME_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_repo_ref(ref, default_owner=DEFAULT_OWNER):
    """Normalize any repo reference to `owner/name`, or None if unparseable.

    Accepts a full GitHub URL of ANY shape the validator sees in handoff
    evidence -- repo URL, `/pull/<n>`, `/actions/runs/<n>`, `/commit/<sha>`,
    `/compare/...` -- plus `owner/name`, a git remote URL, and a bare repo
    name (which takes `default_owner`)."""
    if ref is None:
        return None
    ref = str(ref).strip().rstrip("/")
    if not ref:
        return None

    match = _GITHUB_URL_RE.search(ref)
    if match:
        return "{0}/{1}".format(match.group(1), match.group(2))

    match = _SLUG_RE.match(ref)
    if match:
        return "{0}/{1}".format(match.group(1), match.group(2))

    if "://" in ref or ref.startswith("git@") or ref.endswith(".git"):
        match = _REMOTE_RE.search(ref)
        if match:
            return "{0}/{1}".format(match.group(1), match.group(2))
        return None

    if _BARE_NAME_RE.match(ref):
        return "{0}/{1}".format(default_owner, ref)

    return None


# ---------------------------------------------------------------------------
# Checkout -> reference
# ---------------------------------------------------------------------------


def _repo_identity_helper():
    """Newest available ignite-ship/scripts/repo-identity.sh, or None."""
    override = os.environ.get("REPO_IDENTITY_SH")
    if override and os.path.isfile(override):
        return override
    matches = sorted(
        glob.glob(REPO_IDENTITY_SH_GLOB.format(home=os.path.expanduser("~")))
    )
    return matches[-1] if matches else None


def workdir_origin_url(workdir):
    """Raw `origin` URL for `workdir`, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", workdir, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def workdir_repo_name(workdir):
    """Return the GitHub repo basename for `workdir`'s `origin` remote, or
    None if `workdir` is not a readable git checkout with a parseable GitHub
    origin URL.

    Delegates to ignite-ship's `repo-identity.sh --name` when that helper is
    present (correct under linked worktrees / Conductor workspaces), and falls
    back to parsing `git remote get-url origin` directly. The helper is only
    trusted when the checkout really does have an origin remote: its
    no-origin fallback returns a DIRECTORY name, which is not a repo identity
    and must stay a None here so the caller fails closed."""
    remote = workdir_origin_url(workdir)
    if not remote:
        return None

    helper = _repo_identity_helper()
    if helper:
        try:
            result = subprocess.run(
                ["bash", helper, "--name", workdir],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass  # fall through to the direct parse

    match = _REMOTE_RE.search(remote)
    return match.group(2) if match else None


def workdir_repo_ref(workdir, default_owner=DEFAULT_OWNER):
    """`owner/name` for `workdir`, or None. Owner comes from origin; name comes
    from `repo-identity.sh` when available (worktree-safe)."""
    name = workdir_repo_name(workdir)
    if not name:
        return None
    owner = default_owner
    remote = workdir_origin_url(workdir)
    if remote:
        match = _REMOTE_RE.search(remote)
        if match:
            owner = match.group(1)
    return "{0}/{1}".format(owner, name)


# ---------------------------------------------------------------------------
# Canonical identity (rename-following)
# ---------------------------------------------------------------------------


def _cache_path():
    return os.environ.get(
        "HERMES_REPO_IDENTITY_CACHE",
        os.path.expanduser("~/.hermes/state/repo_identity_cache.json"),
    )


def _alias_path():
    return os.environ.get(
        "HERMES_REPO_ALIASES",
        os.path.expanduser("~/.hermes/config/repo-aliases.json"),
    )


def _load_cache():
    try:
        with open(_cache_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache):
    path = _cache_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        pass  # cache is an optimization; never fatal


GH_OK = "ok"
GH_NOT_FOUND = "not_found"     # definite HTTP 404 from a working `gh`
GH_ERROR = "error"             # network/auth/rate-limit/timeout — says nothing
GH_DISABLED = "disabled"       # HERMES_REPO_GUARD_NO_NETWORK=1

_HTTP_404_RE = re.compile(r'HTTP 404|"status"\s*:\s*"404"')


def _gh_lookup(slug):
    """Ask GitHub for `slug`'s canonical identity. `gh` follows the rename
    redirect, so an OLD name returns the CURRENT full_name and the SAME
    immutable node_id.

    Returns (status, identity_or_None). The status matters: a definite 404 is
    *evidence*, whereas a transport/auth failure is the absence of evidence and
    must never be read as "different repository"."""
    if os.environ.get("HERMES_REPO_GUARD_NO_NETWORK") == "1":
        return (GH_DISABLED, None)
    try:
        result = subprocess.run(
            ["gh", "api", "repos/" + slug,
             "--jq", "{node_id: .node_id, id: .id, full_name: .full_name}"],
            capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS,
        )
    except Exception:
        return (GH_ERROR, None)
    if result.returncode != 0:
        combined = (result.stdout or "") + (result.stderr or "")
        if _HTTP_404_RE.search(combined):
            return (GH_NOT_FOUND, None)
        return (GH_ERROR, None)
    try:
        payload = json.loads(result.stdout.strip())
    except Exception:
        return (GH_ERROR, None)
    if not isinstance(payload, dict) or not payload.get("node_id"):
        return (GH_ERROR, None)
    return (GH_OK, {
        "node_id": payload["node_id"],
        "full_name": payload.get("full_name") or slug,
        "source": "gh",
    })


def canonical_identity_ex(ref, default_owner=DEFAULT_OWNER, cache=None):
    """Resolve `ref` to (identity_or_None, gh_status).

    Order: fresh cache -> live `gh` (rename-following, authoritative) ->
    STALE cache (gh unreachable) -> nothing. `gh_status` describes the live
    lookup only; it is None when a fresh cache hit meant no call was made."""
    slug = parse_repo_ref(ref, default_owner)
    if not slug:
        return (None, None)

    owned_cache = cache is None
    if owned_cache:
        cache = _load_cache()

    entry = cache.get(slug)
    now = time.time()
    if isinstance(entry, dict) and entry.get("node_id"):
        try:
            age = now - float(entry.get("ts") or 0)
        except (TypeError, ValueError):
            age = CACHE_TTL_SECONDS + 1
        if age < CACHE_TTL_SECONDS:
            return ({"node_id": entry["node_id"],
                     "full_name": entry.get("full_name") or slug,
                     "source": "cache"}, None)

    status, fresh = _gh_lookup(slug)
    if fresh:
        cache[slug] = {"node_id": fresh["node_id"],
                       "full_name": fresh["full_name"], "ts": now}
        # Also key the canonical spelling so the reverse lookup is a cache hit.
        cache[fresh["full_name"]] = dict(cache[slug])
        if owned_cache:
            _save_cache(cache)
        return (fresh, status)

    # gh failed -- serve stale rather than forcing a fleet-wide skip.
    if isinstance(entry, dict) and entry.get("node_id"):
        return ({"node_id": entry["node_id"],
                 "full_name": entry.get("full_name") or slug,
                 "source": "cache-stale"}, status)
    return (None, status)


def canonical_identity(ref, default_owner=DEFAULT_OWNER, cache=None):
    """Resolve `ref` to {'node_id','full_name','source'}, or None if it cannot
    be established with confidence."""
    return canonical_identity_ex(ref, default_owner, cache)[0]


def _alias_groups():
    try:
        with open(_alias_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return []
    groups = data.get("aliases") if isinstance(data, dict) else data
    out = []
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, list):
                out.append(set(str(item).strip().lower() for item in group))
    return out


def _alias_equivalent(slug_a, slug_b):
    low_a, low_b = slug_a.lower(), slug_b.lower()
    for group in _alias_groups():
        if low_a in group and low_b in group:
            return True
    return False


# ---------------------------------------------------------------------------
# The comparison the validator actually needs
# ---------------------------------------------------------------------------

SAME = "same"
DIFFERENT = "different"
UNRESOLVED = "unresolved"


def compare_refs(ref_a, ref_b, default_owner=DEFAULT_OWNER):
    """Compare two repo references by CANONICAL identity.

    Returns (verdict, detail) where verdict is SAME / DIFFERENT / UNRESOLVED.
    UNRESOLVED is the fail-closed outcome: the caller must skip, not FAIL."""
    slug_a = parse_repo_ref(ref_a, default_owner)
    slug_b = parse_repo_ref(ref_b, default_owner)
    if not slug_a or not slug_b:
        bad = ref_a if not slug_a else ref_b
        return (UNRESOLVED,
                "cannot parse a GitHub owner/repository out of {0!r}; refusing "
                "to judge repository identity".format(bad))

    # Cheapest possible answer: identical spelling needs no network at all.
    if slug_a.lower() == slug_b.lower():
        return (SAME, "both references are {0}".format(slug_a))

    # Live gh (rename-following, authoritative) first, falling back to the
    # fresh/stale identity cache only when gh itself cannot answer. The
    # static alias file is intentionally NOT consulted here: it must never
    # get a chance to override an identity that gh or the cache actually
    # established (see the offline-last-resort fallback at the bottom of
    # this function).
    cache = _load_cache()
    ident_a, status_a = canonical_identity_ex(slug_a, default_owner, cache)
    ident_b, status_b = canonical_identity_ex(slug_b, default_owner, cache)
    _save_cache(cache)

    # Definite-404 inference. GitHub's rename redirect is keyed on the repo id,
    # and a GitHub App installation grants access by repo id too. So if `gh`
    # resolved slug_a LIVE in this same run (proving the token works right now)
    # and then returned a definite HTTP 404 for slug_b, slug_b cannot be a
    # rename alias of slug_a -- an alias would have redirected to the very repo
    # the token just read. That is enough to justify a wrong-repo verdict even
    # though we never learn what slug_b actually is (it may be private to
    # another installation, e.g. colingreig/hermes-ops-scripts on the mini's
    # hermes-dev-assistant[bot] token). A non-404 gh failure proves nothing and
    # still falls through to UNRESOLVED.
    for known_slug, known_ident, known_status, missing_slug, missing_status in (
        (slug_a, ident_a, status_a, slug_b, status_b),
        (slug_b, ident_b, status_b, slug_a, status_a),
    ):
        if not known_ident or missing_status != GH_NOT_FOUND:
            continue
        # `known_status` is None when a fresh cache hit meant no call was made;
        # re-probe live so the 404 is judged against a credential we have just
        # seen working, never against a stale cache entry.
        if known_status != GH_OK and _gh_lookup(known_slug)[0] != GH_OK:
            continue
        return (DIFFERENT,
                "{0} resolves live to {1} (node {2}), while {3} returns HTTP "
                "404 to the same working credential -- a rename alias of {1} "
                "would redirect to it, so {3} is a different repository "
                "(its own identity is not visible to this token)".format(
                    known_slug, known_ident["full_name"],
                    known_ident["node_id"], missing_slug))

    if ident_a and ident_b:
        if ident_a["node_id"] == ident_b["node_id"]:
            return (SAME,
                    "{0} and {1} both resolve to canonical repository {2} "
                    "(node {3}) -- same repository under different names "
                    "(rename alias), not a wrong-repo".format(
                        slug_a, slug_b, ident_a["full_name"],
                        ident_a["node_id"]))

        return (DIFFERENT,
                "{0} resolves to {1} (node {2}) but {3} resolves to {4} "
                "(node {5}) -- genuinely different repositories".format(
                    slug_a, ident_a["full_name"], ident_a["node_id"],
                    slug_b, ident_b["full_name"], ident_b["node_id"]))

    # Neither live gh nor the identity cache could establish one or both
    # sides with confidence. Documented OFFLINE LAST RESORT: fall back to
    # the static alias file. This branch is unreachable whenever gh/cache
    # already resolved both sides above, so a stale or wrong alias entry
    # can never override a successful live lookup -- it can only stand in
    # for one that genuinely could not be made.
    if _alias_equivalent(slug_a, slug_b):
        return (SAME,
                "{0} and {1} are declared equivalent in {2} (gh/cache could "
                "not resolve one or both sides live)".format(
                    slug_a, slug_b, _alias_path()))

    unresolved = slug_a if not ident_a else slug_b
    return (UNRESOLVED,
            "canonical identity for {0} could not be established (gh "
            "lookup failed and no cached/alias identity is available); "
            "comparing {1} vs {2} by NAME STRING is exactly the bug this "
            "guard exists to prevent".format(unresolved, slug_a, slug_b))


# ---------------------------------------------------------------------------
# Mode A -- chartered-workdir guard (RC1), now alias-aware
# ---------------------------------------------------------------------------


def repo_mismatch_reason(expect_repo, workdir, default_owner=DEFAULT_OWNER):
    """Pure guard. Returns a human-readable abort reason if `workdir` is not a
    readable git checkout of `expect_repo`, else None (safe to proceed).

    Alias-aware: a checkout whose origin is a RENAMED spelling of
    `expect_repo` (hermes-agent vs hermes-agent-upstream-contrib) is a MATCH,
    resolved via canonical GitHub identity rather than a name string.

    Fails CLOSED: an unresolvable workdir (missing, not a git repo, no GitHub
    origin) or an unresolvable identity is treated as an abort rather than a
    pass. Every reason mentions both names so the operator sees the pair."""
    actual = workdir_repo_name(workdir)
    if not actual:
        return (
            "cannot validate: repo mismatch — workdir '{0}' is not a "
            "readable git checkout with a GitHub origin (expected "
            "'{1}')".format(workdir, expect_repo)
        )
    if actual == expect_repo:
        return None

    actual_ref = workdir_repo_ref(workdir, default_owner) or actual
    verdict, detail = compare_refs(expect_repo, actual_ref, default_owner)
    if verdict == SAME:
        return None
    if verdict == DIFFERENT:
        return (
            "cannot validate: repo mismatch — workdir '{0}' resolves to repo "
            "'{1}', but this validator run is chartered for '{2}' ({3})".format(
                workdir, actual, expect_repo, detail)
        )
    return (
        "cannot validate: repo mismatch — workdir '{0}' names repo '{1}' and "
        "this run is chartered for '{2}', but their canonical identities could "
        "not be established, so they cannot be proven the same OR different "
        "({3})".format(workdir, actual, expect_repo, detail)
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Repository-identity guard for ignite-validate.")
    parser.add_argument("--expect-repo",
                        help="repo basename this validator run is chartered "
                             "to validate (mode A)")
    parser.add_argument("--workdir", default=os.getcwd(),
                        help="checkout to verify (default: cwd)")
    parser.add_argument("--compare", nargs=2, metavar=("TARGET", "EVIDENCE"),
                        help="compare two repo references by canonical "
                             "identity (mode B)")
    parser.add_argument("--identity", metavar="REF",
                        help="print one reference's canonical identity (mode C)")
    parser.add_argument("--default-owner", default=DEFAULT_OWNER,
                        help="owner assumed for bare repo names "
                             "(default: %(default)s)")
    args = parser.parse_args()

    modes = [bool(args.expect_repo), bool(args.compare), bool(args.identity)]
    if sum(1 for mode in modes if mode) != 1:
        parser.error("choose exactly one of --expect-repo / --compare / --identity")

    if args.identity:
        ident = canonical_identity(args.identity, args.default_owner)
        if not ident:
            print("ABORT: cannot resolve canonical identity for {0!r} — do NOT "
                  "emit a wrong-repo verdict from an unresolved identity"
                  .format(args.identity))
            return 3
        print("{0}\t{1}\t{2}".format(
            ident["full_name"], ident["node_id"], ident["source"]))
        return 0

    if args.compare:
        target, evidence = args.compare
        verdict, detail = compare_refs(target, evidence, args.default_owner)
        if verdict == SAME:
            print("SAME: {0}".format(detail))
            return 0
        if verdict == DIFFERENT:
            print("DIFFERENT: {0}".format(detail))
            return 4
        print("ABORT: {0}. SKIP this task — write no verdict, change no status."
              .format(detail))
        return 3

    reason = repo_mismatch_reason(args.expect_repo, args.workdir,
                                  args.default_owner)
    if reason:
        print("ABORT: {0}".format(reason))
        return 3
    print("OK: workdir '{0}' is a valid '{1}' checkout".format(
        args.workdir, args.expect_repo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
