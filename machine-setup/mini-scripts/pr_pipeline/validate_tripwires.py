#!/usr/bin/env python3
"""validate_tripwires.py — deterministic, zero-LLM gate for a PR/worktree diff.

The cheap first line of the validator. Catches the failure CLASSES that a
build/test gate cannot see and that an LLM should never be paid to find with a
regex — exactly the ones that passed every gate on 2026-06-18:
  - secret_scan          a real secret committed (AKIA…, ghp_…, sk_live_…, keys)
  - auth_on_dispatch     an endpoint that dispatches a workflow / writes to
                         protected main behind a cookie-SHAPE check, not real auth
                         (the sync-wistia.ts class)
  - governance           a diff that does something a repo rule forbids
                         (seed: programmatic in-review -> approved flips)
  - rls_on_new_table     CREATE TABLE public.* with no ENABLE ROW LEVEL SECURITY
  - rendered_content_leak  MDX/JSX (import…from "@/", <FaqBlock>, items={[) added
                         to a PLAIN .md content file -> renders as literal junk on
                         the live page even though it builds + returns 200 (the
                         jdmbuysell PR #390 class, 2026-06-21)
  - ci_green_on_base     base branch (main) is red right now (don't merge onto red)
  - hygiene              node_modules / binaries / large blobs in the diff

Output (JSON):
  {"pass": bool, "tier": "...", "surfaces": [...], "findings": [{check,severity,file,detail}]}
pass == (no finding of severity "high"). "medium" findings warn but don't fail.

Usage:
  validate_tripwires.py --repo owner/repo --pr 123
  validate_tripwires.py --diff /tmp/x.diff [--repo owner/repo]   # repo enables ci_green
  git diff origin/main... | validate_tripwires.py
"""
import argparse
import json
import os
import re
import sys

if __package__:
    from . import risk_classify
    from . import validator_common as vc
    from . import validator_verdict as vv
else:
    import risk_classify
    import validator_common as vc
    import validator_verdict as vv

POLICY_PATH = os.path.expanduser("~/.hermes/scripts/validator_policy.json")

# --- secret_scan ------------------------------------------------------------

_SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_live", re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("generic_assigned_secret", re.compile(
        r"\b(api[_-]?key|secret|access[_-]?token|client[_-]?secret|password|passwd)\b"
        r"\s*[:=]\s*['\"][^'\"]{12,}['\"]", re.I)),
]
# Things that look like a secret assignment but are NOT (env refs / placeholders).
_SECRET_FALSE_POSITIVE = re.compile(
    r"process\.env|os\.environ|import\.meta\.env|getenv|\$\{|<[^>]+>|"
    r"xxx+|your[_-]|example|changeme|placeholder|dummy|redact|\*\*\*|"
    r"secret\s*[:=]\s*['\"]?(true|false|null|none)['\"]?", re.I)


def check_secrets(files):
    out = []
    for fd in files:
        for ln in fd.get("added", []):
            if _SECRET_FALSE_POSITIVE.search(ln):
                continue
            for name, pat in _SECRET_PATTERNS:
                if pat.search(ln):
                    out.append({"check": "secret_scan", "severity": "high",
                                "file": fd.get("path"),
                                "detail": f"possible {name} in an added line"})
                    break
    return out


# --- auth_on_dispatch -------------------------------------------------------

_DISPATCH = re.compile(
    r"\b(repository_dispatch|workflow_dispatch|createWorkflowDispatch|/dispatches\b|"
    r"gh\s+workflow\s+run|actions/workflows/.+/dispatches)\b", re.I)
# Genuine CALLER authentication only. Deliberately EXCLUDES getToken()/token
# reads — those are usually a SERVER-SIDE service credential (e.g. a dispatch
# token), NOT a check that the caller is who they claim. Conflating "uses a
# token somewhere" with "validated the caller" is exactly the sync-wistia.ts
# trap (2026-06-18): it read GITHUB_DISPATCH_TOKEN but authed the caller by a
# cookie regex. Caller-auth verbs that actually validate identity:
_REAL_AUTH = re.compile(
    r"\b(verifyJwt|verifyToken|verifySession|jwt\.verify|decodeToken|introspect|"
    r"authenticate|requireUser|assertAuth|session\.user|currentUser|checkAuth|"
    r"hasScope|getServerSession|auth\.protect)\b", re.I)
# the dangerous tell: deciding auth by the SHAPE/PRESENCE of a cookie string.
# Matches BOTH argument orders: `cookie.includes('x=')` and the regex-literal
# form `/x=/.test(cookie)` (sync-wistia.ts used the latter).
_COOKIE_SHAPE_AUTH = re.compile(
    r"cookie[^\n]{0,80}?(\.includes\(|\.test\(|\.match\(|indexOf\(|=~)|"
    r"\.(includes|test|match|indexOf)\(\s*(request\.)?cookie", re.I)


def check_auth_on_dispatch(files):
    out = []
    for fd in files:
        path = fd.get("path", "")
        txt = fd.added_text
        is_handler = bool(re.search(r"/api/|\.(t|j)sx?$", path, re.I))
        if not is_handler or not _DISPATCH.search(txt):
            continue
        has_real_auth = bool(_REAL_AUTH.search(txt))
        cookie_shape = bool(_COOKIE_SHAPE_AUTH.search(txt))
        if cookie_shape and not has_real_auth:
            out.append({"check": "auth_on_dispatch", "severity": "high",
                        "file": path,
                        "detail": "endpoint dispatches a workflow / writes to "
                                  "protected main but gates on a cookie SHAPE/"
                                  "presence check, not token validation"})
        elif not has_real_auth:
            out.append({"check": "auth_on_dispatch", "severity": "high",
                        "file": path,
                        "detail": "endpoint dispatches a workflow / writes to "
                                  "protected main with no recognizable auth "
                                  "validation in the diff"})
    return out


# --- governance -------------------------------------------------------------

# Built-in seed rules; merged with validator_policy.json (per-repo + "*").
_SEED_POLICY = {
    "*": [
        {"id": "auto-approve-translation",
         "pattern": r"translationStatus\s*[:=]\s*['\"]approved['\"]",
         "severity": "high",
         "message": "programmatically setting translationStatus='approved' — "
                    "in-review->approved must be a human step (translation runbook)"},
    ],
}


def _load_policy():
    policy = {k: list(v) for k, v in _SEED_POLICY.items()}
    try:
        with open(POLICY_PATH, encoding="utf-8") as f:
            extra = json.load(f)
        for repo, rules in (extra or {}).items():
            policy.setdefault(repo, []).extend(rules)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[tripwires] policy load error: {e!r}", file=sys.stderr)
    return policy


def check_governance(files, repo):
    policy = _load_policy()
    rules = list(policy.get("*", []))
    if repo:
        rules += policy.get(repo, [])
    compiled = []
    for r in rules:
        try:
            compiled.append((re.compile(r["pattern"], re.I), r))
        except Exception:
            continue
    out = []
    for fd in files:
        for ln in fd.get("added", []):
            for pat, r in compiled:
                if pat.search(ln):
                    out.append({"check": "governance", "severity": r.get("severity", "high"),
                                "file": fd.get("path"),
                                "detail": r.get("message", r.get("id", "policy violation"))})
    return out


# --- rls_on_new_table -------------------------------------------------------

_CREATE_PUBLIC_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?[\"`]?(\w+)", re.I)
_ENABLE_RLS = re.compile(r"enable\s+row\s+level\s+security", re.I)


def check_rls(files):
    out = []
    for fd in files:
        path = fd.get("path", "")
        if not re.search(r"\.sql$|/migrations?/|/supabase/", path, re.I):
            continue
        txt = fd.added_text
        tables = _CREATE_PUBLIC_TABLE.findall(txt)
        if not tables:
            continue
        if not _ENABLE_RLS.search(txt):
            out.append({"check": "rls_on_new_table", "severity": "high",
                        "file": path,
                        "detail": f"CREATE TABLE ({', '.join(tables[:5])}) with no "
                                  "ENABLE ROW LEVEL SECURITY in the same diff"})
    return out


# --- ci_green_on_base -------------------------------------------------------

CI_BASE_FIX_EXEMPT_PATH = os.path.expanduser(
    "~/.hermes/scripts/ci_base_fix_exempt.json")


def _ci_base_fix_exempt(repo, pr, path=CI_BASE_FIX_EXEMPT_PATH):
    """True iff (repo, pr) is on the explicit, audited base-fixing-PR exemption
    list — a PR that REPAIRS the red base CI and therefore cannot pass
    ci_green_on_base pre-merge (the 'fix can't self-green' deadlock).

    Fail-safe: missing/corrupt file, pr is None, or no matching entry => NOT
    exempt (the full block stands). An exemption is granted ONLY by an explicit
    entry a human/caretaker deliberately wrote into the file."""
    if not repo or pr is None:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return False
    entries = d.get("exempt", {}) if isinstance(d, dict) else {}
    return isinstance(entries, dict) and f"{repo}#{pr}" in entries


def check_ci_green(repo, pr=None):
    if not repo:
        return []
    try:
        import ci_health_watch
        # MERGE-gate view: only branch-gating events (push/PR/merge_group) count.
        # A scheduled/monitoring smoke red on main (e.g. a prod-targeting E2E
        # smoke) reflects prod health, not mergeability — it must NOT deadlock
        # merges. The monitoring cron still sees the full picture for alerts.
        red = ci_health_watch._red_workflows(repo, gating_only=True)
    except Exception as e:
        print(f"[tripwires] ci_green check skipped: {e!r}", file=sys.stderr)
        return []
    if not red:
        return []
    wfs = ", ".join(sorted(red.keys()))
    if _ci_base_fix_exempt(repo, pr):
        # Audited exemption: this PR is the one that REPAIRS the red base. Emit a
        # non-blocking (info) finding so the exemption is VISIBLE in the verdict
        # output — never silently dropped.
        return [{"check": "ci_green_on_base", "severity": "info", "file": "(base/main)",
                 "detail": f"base RED ({wfs}) but {repo}#{pr} is an AUDITED base-fixing "
                           f"exemption (ci_base_fix_exempt.json) — not blocking"}]
    return [{"check": "ci_green_on_base", "severity": "high", "file": "(base/main)",
             "detail": f"base branch is RED now ({wfs}); do not merge onto red main"}]


# --- rendered_content_leak --------------------------------------------------
#
# WHY THIS EXISTS (jdmbuysell PR #390 / task 86e1z9xx2, 2026-06-21):
#   A blog post was added to a PLAIN-Markdown (.md) Astro collection but written
#   as if it were MDX — it carried `import YouTubeEmbed from "@/..."` lines and
#   `<FaqBlock id="faq" items={[ ... ]}/>` JSX. Astro .md does NOT process
#   imports or JSX, so the live page rendered those statements as LITERAL TEXT.
#   The PR built fine, returned HTTP 200, and the LLM "verified-live" lens passed
#   it — because nothing deterministically checked that the RENDERED body wasn't
#   garbage. The content also used smart/curly quotes inside the import lines.
#
# WHAT THIS CHECKS (deterministic, zero-LLM):
#   Added content lines in *.md / *.mdx files under a content/ dir for literal
#   "unrendered-syntax leak" markers. In a .md file ANY of these markers is junk
#   the reader will see verbatim; in .mdx they're legitimate, so we only flag a
#   .md/.markdown file. The marker list is a small constant — extend as needed.
#
# Reversibility: delete this block + the check_rendered_content() call in run().

# Literal markers that mean "MDX/JSX syntax leaked into plain Markdown".
_RENDER_LEAK_MARKERS = [
    'import ',          # only counted when paired with a from "@/ import (below)
    'from "@/',         # MDX component import: import X from "@/components/..."
    "from '@/",         # single-quote variant
    "<FaqBlock",        # bare JSX component the .md renderer won't expand
    "<YouTubeEmbed",
    "items={[",         # JSX expression attribute (e.g. <FaqBlock items={[...]} />
]
# Smart/curly quotes & dashes that, inside an MDX import/JSX line, both leak AND
# break parsing even in a real .mdx file. Flagged only on import/JSX-shaped lines.
_SMART_PUNCT = re.compile(r"[“”‘’]")  # " " ' '
# Files this check applies to: Markdown content under a content/ directory.
_MD_CONTENT_PATH = re.compile(r"(^|/)content/.*\.(md|markdown)$", re.I)
_MDX_CONTENT_PATH = re.compile(r"(^|/)content/.*\.mdx$", re.I)
_IMPORT_OR_JSX = re.compile(r'^\s*(import\s|<[A-Z]\w+)')


def check_rendered_content(files):
    """BLOCK when added Markdown content contains unrendered MDX/JSX leak markers.

    A .md/.markdown file in a content/ collection that contains `import ... from
    "@/..."`, `<FaqBlock>`, `<YouTubeEmbed>`, or `items={[` will render those as
    literal text on the live page. That's a HARD block (the page is broken even
    though it builds + returns 200). For .mdx we only flag smart/curly quotes on
    an import/JSX line (those break the MDX parser / leak regardless)."""
    out = []
    for fd in files:
        path = fd.get("path", "")
        is_md = bool(_MD_CONTENT_PATH.search(path))
        is_mdx = bool(_MDX_CONTENT_PATH.search(path))
        if not (is_md or is_mdx):
            continue
        for ln in fd.get("added", []):
            hits = []
            if is_md:
                # `import ` alone is noisy; only count it when the line is also a
                # `from "@/` component import (the real MDX-in-md tell).
                if 'from "@/' in ln or "from '@/" in ln:
                    hits.append('import-from-@/')
                for marker in ("<FaqBlock", "<YouTubeEmbed", "items={["):
                    if marker in ln:
                        hits.append(marker)
            # smart-quote-in-import/JSX applies to BOTH .md and .mdx
            if _IMPORT_OR_JSX.match(ln) and _SMART_PUNCT.search(ln):
                hits.append("smart-quote-in-import/JSX")
            if hits:
                out.append({"check": "rendered_content_leak", "severity": "high",
                            "file": path,
                            "detail": "unrendered MDX/JSX in a Markdown content "
                                      f"file (renders as literal text): {hits[0]} "
                                      f"in {ln.strip()[:80]!r}"})
                break  # one finding per file is enough to block
    return out


# --- blog_imagery -----------------------------------------------------------
#
# WHY THIS EXISTS (jdmbuysell PR #391 / task 86e1z9xx2, 2026-06-21):
#   A "fix-forward" PR was meant to REPLACE a single scraped dealer image with a
#   generated, interspersed image SET. Instead it referenced the SAME scraped
#   image URL 4× (hero + 3 inline) with fabricated scene captions ("driving
#   through coastal BC forest roads", "parked at a lookout at sunset", ...) that
#   one static photo cannot depict. It built fine, the LLM panel PASSed it, and
#   the deterministic merge sweep landed it on the live site — because nothing
#   deterministically checked image reuse or scraped-source hotlinks. Binding
#   policy (operator, 2026-06-21): jdmbuysell imagery must be GENERATED/owned,
#   distinct per slot, NEVER scraped from a dealer/marketplace listing.
#
# WHAT THIS CHECKS (deterministic, zero-LLM) on added image refs in a Markdown
# content file (markdown `![](url)` + frontmatter `url: "..."` image values):
#   1. REUSE — the same image URL appears 2+ times (one image masquerading as a
#      "set"). HIGH.
#   2. SCRAPED-SOURCE HOTLINK — an `imagedelivery.net/.../uploads-` dealer-upload
#      URL (the Cloudflare-Images dealer-listing scrape signature). HIGH.
#
# Scope: any */content/*.md|markdown|mdx file. Generic enough to be correct in
# any repo (reusing one image as a "set" / hotlinking a scraped listing image is
# wrong anywhere); the dealer signature is jdmbuysell-specific but harmless
# elsewhere. Reversibility: delete this block + the check_blog_imagery() call.

_IMG_MD = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")        # ![alt](URL ...)
_IMG_FM = re.compile(r'^\s*url:\s*["\']([^"\']+)["\']')   # frontmatter  url: "URL"
_IMG_EXT = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|/|$)", re.I)
_SCRAPED_IMG = re.compile(r"imagedelivery\.net/[^)\s\"']*/uploads-", re.I)
_ANY_MD_CONTENT_PATH = re.compile(r"(^|/)content/.*\.(md|markdown|mdx)$", re.I)


def check_blog_imagery(files):
    """BLOCK when a Markdown content file reuses one image URL across multiple
    slots, or hotlinks a scraped dealer-listing image. Enforces the
    distinct/generated/owned imagery policy deterministically."""
    out = []
    for fd in files:
        path = fd.get("path", "")
        if not _ANY_MD_CONTENT_PATH.search(path):
            continue
        urls = []
        for ln in fd.get("added", []):
            for u in _IMG_MD.findall(ln):
                urls.append(u)
            m = _IMG_FM.search(ln)
            if m and (_IMG_EXT.search(m.group(1)) or "imagedelivery.net" in m.group(1)):
                urls.append(m.group(1))
        # 2. scraped-source hotlink (dealer-listing upload signature)
        scraped = [u for u in urls if _SCRAPED_IMG.search(u)]
        if scraped:
            out.append({"check": "blog_imagery_scraped", "severity": "high",
                        "file": path,
                        "detail": "scraped dealer-listing image hotlinked "
                                  "(imagedelivery.net/.../uploads-); imagery must "
                                  f"be generated/owned: {scraped[0][:90]!r}"})
        # 1. reuse — same URL in 2+ slots (one image faked as a set)
        dupes = {u for u in urls if urls.count(u) >= 2}
        if dupes:
            d = sorted(dupes)[0]
            out.append({"check": "blog_imagery_reuse", "severity": "high",
                        "file": path,
                        "detail": f"image URL reused {urls.count(d)}× in one post "
                                  "(needs a distinct image per slot, not one image "
                                  f"repeated): {d[:90]!r}"})
    return out


# --- blog_imagery_count (Driven series: >3 distinct owned images) -----------
#
# WHY THIS EXISTS (operator policy, 2026-06-22):
#   The reuse/scraped tripwires above stop ONE image masquerading as a set and
#   stop scraped dealer hotlinks — but they do NOT require a post to actually
#   CARRY a real interspersed image set. PR #391's deeper complaint was a Driven
#   review that shipped with too few genuine in-body images. Binding policy:
#   jdmbuysell "Driven" review posts must reference MORE THAN 3 (i.e. >=4)
#   DISTINCT generated/owned images (hero + >=3 inline), all under the owned
#   /images/driven/ asset dir. Scraped URLs never count toward the quota.
#
# WHAT THIS CHECKS (deterministic, zero-LLM), only when the diff is AUTHORING a
# Driven post — i.e. a content .md/.mdx whose added lines either (a) make it a
# new file, (b) add the featured_image hero `url:` pointing at /images/driven/,
# or (c) add the `- "Driven"` category marker. That "authoring" gate keeps an
# incidental one-line text edit to an existing Driven post from being blocked
# for not re-listing 4 images, while still catching a freshly authored Driven
# post that ships with zero/too-few owned images.
#
# Scope: the `Driven` category + /images/driven/ signature is jdmbuysell-Driven
# specific and harmless elsewhere (other repos won't carry it). Reversibility:
# delete this block + the check_blog_imagery_count() call in run().

DRIVEN_MIN_DISTINCT_IMAGES = 4  # operator: ">3 images for driven" => minimum 4
_DRIVEN_IMG = re.compile(r"/images/driven/", re.I)
_DRIVEN_CATEGORY = re.compile(r'^\s*-\s*["\']?Driven["\']?\s*$')
_HERO_DRIVEN_URL = re.compile(r'^\s*url:\s*["\'][^"\']*?/images/driven/[^"\']+["\']', re.I)


def check_blog_imagery_count(files):
    """BLOCK a Driven post authored/updated with <4 distinct owned images.

    Fires only when the diff is AUTHORING a Driven content file (new file, or
    adds the /images/driven/ hero `url:`, or adds the `- "Driven"` category).
    Counts DISTINCT owned (/images/driven/) image URLs in added refs; scraped
    URLs are excluded (and blocked separately). Enforces the operator's
    '>3 distinct generated images per Driven post' policy deterministically."""
    out = []
    for fd in files:
        path = fd.get("path", "")
        if not _ANY_MD_CONTENT_PATH.search(path):
            continue
        added = fd.get("added", [])
        authoring = (
            bool(fd.get("is_new_file"))
            or any(_HERO_DRIVEN_URL.search(ln) for ln in added)
            or any(_DRIVEN_CATEGORY.search(ln) for ln in added)
        )
        if not authoring:
            continue
        # Confirm this is actually a Driven piece (category marker present in the
        # added lines, OR it references the owned Driven image dir). Guards a new
        # NON-Driven post from being subject to the Driven quota.
        is_driven = (
            any(_DRIVEN_CATEGORY.search(ln) for ln in added)
            or any(_DRIVEN_IMG.search(ln) for ln in added)
        )
        if not is_driven:
            continue
        urls = []
        for ln in added:
            urls += _IMG_MD.findall(ln)
            m = _IMG_FM.search(ln)
            if m and (_IMG_EXT.search(m.group(1)) or "imagedelivery.net" in m.group(1)):
                urls.append(m.group(1))
        owned = {u for u in urls if _DRIVEN_IMG.search(u) and not _SCRAPED_IMG.search(u)}
        if len(owned) < DRIVEN_MIN_DISTINCT_IMAGES:
            out.append({"check": "blog_imagery_count", "severity": "high",
                        "file": path,
                        "detail": f"Driven post references only {len(owned)} distinct "
                                  f"owned image(s) under /images/driven/; policy requires "
                                  f">3 (>={DRIVEN_MIN_DISTINCT_IMAGES}) distinct generated "
                                  "images (hero + >=3 inline). Scraped URLs do not count."})
    return out


# --- hygiene ----------------------------------------------------------------

_BAD_PATH = re.compile(r"(^|/)(node_modules|\.venv|venv|__pycache__|dist|build)/", re.I)
_BINARY_EXT = re.compile(r"\.(zip|tar|gz|tgz|jar|exe|dll|so|dylib|bin|mp4|mov|"
                         r"woff2?|ttf|otf|psd|sketch)$", re.I)


def check_hygiene(files):
    out = []
    for fd in files:
        path = fd.get("path", "")
        if _BAD_PATH.search(path):
            out.append({"check": "hygiene", "severity": "medium", "file": path,
                        "detail": "build/vendor artifact committed (node_modules/dist/etc.)"})
        elif _BINARY_EXT.search(path) and fd.get("is_new_file"):
            out.append({"check": "hygiene", "severity": "medium", "file": path,
                        "detail": "binary/large asset added to the diff"})
    return out


# --- orchestration ----------------------------------------------------------

def run(diff_text: str, repo: str = "", pr=None, expected_repo: str = "") -> dict:
    files = vc.parse_unified_diff(diff_text)
    risk = risk_classify.classify(diff_text)
    findings = []
    actual_repo = vv.canonical_repo(repo)
    target_repo = vv.canonical_repo(expected_repo or repo)
    if actual_repo and target_repo and actual_repo != target_repo:
        findings.append({"check": "wrong-repo", "severity": "high", "file": "(repo identity)",
                         "detail": f"expected {target_repo} but validator was pointed at {actual_repo}"})
    findings += check_secrets(files)
    findings += check_auth_on_dispatch(files)
    findings += check_governance(files, repo)
    findings += check_rls(files)
    findings += check_rendered_content(files)
    findings += check_blog_imagery(files)
    findings += check_blog_imagery_count(files)
    findings += check_ci_green(repo, pr)
    findings += check_hygiene(files)
    passed = not any(f["severity"] == "high" for f in findings)
    return {"pass": passed, "tier": risk["tier"], "surfaces": risk["surfaces"],
            "findings": findings}


def main():
    p = argparse.ArgumentParser(description="Deterministic PR tripwire checks.")
    vc.add_source_args(p)
    p.add_argument("--expected-repo", default="")
    args = p.parse_args()
    diff_text, src = vc.load_diff_from_args(args)
    if not diff_text.strip():
        print(json.dumps({"pass": True, "tier": "low", "surfaces": [],
                          "findings": [], "note": f"empty diff ({src})"}))
        return 0
    result = run(diff_text, repo=getattr(args, "repo", "") or "",
                 pr=getattr(args, "pr", None),
                 expected_repo=getattr(args, "expected_repo", "") or "")
    print(json.dumps(result, indent=2))
    # exit 1 when blocked, 0 when clean — lets shell callers gate on $?
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
