#!/usr/bin/env python3
"""validate_pr.py — one-shot PR validation orchestrator (the skill's workhorse).

Given a PR, runs the full risk-gated pipeline and records a verdict the merge gate
will read:

  1. fetch the PR diff + head SHA (gh, via validator_common)
  2. deterministic tripwires (validate_tripwires) — zero LLM
       any HIGH finding        -> BLOCK immediately (no LLM spend needed)
  3. risk tier (risk_classify)
       low + tripwires clean    -> PASS (no panel)
       medium/high              -> adversarial panel (validator_panel), Claude on high
  4. write the verdict (validator_verdict) keyed {repo#pr}

Prints a human-readable summary to stderr and the verdict JSON to stdout. Exit 0
on PASS, 1 on BLOCK, 2 on operational error (couldn't fetch diff).

Usage:
  validate_pr.py --repo owner/repo --pr 123 --task 86e1xxxx [--shadow] [--no-panel]
"""
import argparse
import json
import os
import re
import subprocess
import sys

if __package__:
    from . import risk_classify
    from . import validate_tripwires as vt
    from . import validator_common as vc
    from . import validator_image_authenticity as via
    from . import validator_model
    from . import validator_panel
    from . import validator_verdict
else:
    import risk_classify
    import validate_tripwires as vt
    import validator_common as vc
    import validator_image_authenticity as via
    import validator_model
    import validator_panel
    import validator_verdict


def _log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Content-quality lens — D1 fix (S2, 2026-06-23)
#
# WHY: tier=="low" used to auto-PASS with zero content check. Bad-but-safe
# prose PRs shipped untouched (the "Driven" failure mode). This block adds a
# real content-quality lens for prose-dominant PRs on the would-be PASS-low
# path. Rubric source: references/content-quality-lens-rubric.md.
# Reversibility: remove _is_prose_dominant, _run_content_lens, and the
# elif-content-lens branch in validate() (restore the original elif tier=="low").
# ---------------------------------------------------------------------------

# Content-collection path patterns (case-insensitive) — see rubric §1 rule 2.
_CONTENT_PATH_RE = re.compile(
    r"(?i)(^|/)(blog|posts|content|articles|reviews?|copy|editorial|"
    r"news|_posts|src/content|pages/.*\.(mdx|md))(/|$|\.)"
)
# File extensions that count as prose for the 70% heuristic.
_PROSE_EXTS = {".md", ".mdx", ".astro"}

HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
HERMES_TIMEOUT = 240

_CONTENT_LENS_PROMPT = """You are a content-quality reviewer for an autonomous merge gate.
Your job: score the added prose against the rubric below, then emit a verdict.

STYLE RULE: If Colin must read twice, rewrite. Put the answer first, use concrete nouns,
and keep the prose direct. If a sentence only repeats the brief, cut it.

RUBRIC (score added lines only — the +lines in the diff):
Q1 ON-VOICE: Matches the house guide's tone/register/POV; or (no guide) reads as a specific, confident human-edited piece.
Q2 SPECIFIC NOT GENERIC: Concrete claims, named entities, real specifics, numbers. NOT: "In today's fast-paced world…", "plays a crucial role", generic filler.
Q3 FACTUALLY GROUNDED: Claims are checkable; no fabricated stats, invented quotes, or self-contradiction.
Q4 VERDICT-FIRST (only if guide/type requires it): lede delivers the answer in first ~2 paragraphs.
Q5 NO AI-TELLS: No dense AI-tell patterns ("It's worth noting", "In conclusion", "Let's dive in", "Furthermore/Moreover" stacking, "In the realm of", "Whether you're X or Y", listicle-of-platitudes).
Q6 MEETS TASK INTENT: Delivers what was asked (topic, angle, audience, shape).

HOUSE VOICE GUIDE:
{voice_guide}

ADDED PROSE (from the diff +lines):
{prose}

BLOCK DISCIPLINE: BLOCK only on a CLEAR, evidence-backed failure you can quote verbatim from the prose.
A single borderline phrase is PASS (with a note). BLOCK on a PATTERN (e.g. Q5 across many paragraphs, Q3 fabrication, Q2 wholesale filler, Q6 wrong deliverable).
If you cannot quote evidence, PASS.

Reason in at most 4 lines, then end with EXACTLY:
VERDICT: PASS
or
VERDICT: BLOCK — <one-line reason with criterion id e.g. Q2+Q5>
"""

_VERDICT_RE = re.compile(r"VERDICT\s*:\s*(.+)$", re.IGNORECASE)


def _parse_content_verdict(out: str):
    """Extract (verdict, reason) from content lens output. Fail-open: any
    parse/model error -> ('PASS', 'content-lens-parse-error (fail-open)')."""
    for line in reversed((out or "").splitlines()):
        m = _VERDICT_RE.search(line)
        if not m:
            continue
        body = m.group(1).strip().strip("*`_ ").strip()
        if body.upper().startswith("PASS"):
            return "PASS", ""
        if body.upper().startswith("BLOCK"):
            reason = body[5:].lstrip(" —-:*`").strip() or "content quality blocked"
            return "BLOCK", reason
    return "PASS", "content-lens-parse-error (fail-open)"


def _is_prose_dominant(diff_text: str):
    """Return (is_prose_dominant: bool, prose_lines: str, prose_pct: float).

    Computes the fraction of added lines in .md/.mdx/.astro files under a
    content-collection path. Returns (True, prose, pct) if ≥70% of added
    lines meet that test. Fail-open: any exception returns (False, '', 0.0)
    so a parse error never triggers the content lens accidentally."""
    try:
        files = vc.parse_unified_diff(diff_text)
        total_added = 0
        prose_added = 0
        prose_lines_buf = []
        for f in files:
            path = f.get("path", "")
            added = f.get("added", [])
            ext = os.path.splitext(path)[1].lower()
            total_added += len(added)
            if ext in _PROSE_EXTS and _CONTENT_PATH_RE.search(path):
                prose_added += len(added)
                prose_lines_buf.extend(added)
        if total_added == 0:
            return False, "", 0.0
        pct = prose_added / total_added
        return pct >= 0.70, "\n".join(prose_lines_buf[:3000]), pct
    except Exception:
        return False, "", 0.0


def _fetch_voice_guide(repo: str) -> str:
    """Best-effort fetch of the repo's editorial voice guide (rubric §2).
    Returns '' on any error — the lens uses the generic rubric when absent."""
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/git/trees/HEAD?recursive=1",
             "--jq", ".tree[].path | select(test(\"(?i)(voice|style|editorial|tone|brand-?voice).*\\\\.md$\"))"],
            capture_output=True, text=True, timeout=30)
        guide_path = (r.stdout.strip().splitlines() or [""])[0].strip()
        if not guide_path:
            return ""
        r2 = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{guide_path}",
             "--jq", ".content"],
            capture_output=True, text=True, timeout=30)
        if r2.returncode != 0 or not r2.stdout.strip():
            return ""
        import base64
        raw = base64.b64decode(r2.stdout.strip()).decode("utf-8", "replace")
        return raw[:7000]
    except Exception:
        return ""


def _run_content_lens(repo: str, diff_text: str):
    """Run the content-quality lens on a prose-dominant PR.

    Returns a lens result dict: {lens, verdict, reason, model, prose_pct}.
    Fail-open on ALL errors: any exception or model failure returns PASS.
    D3: content lens MUST use an Anthropic model (different vendor from the
    OpenCode/gpt-5 writer). Uses the "medium" chain from validator_model but
    prefers the Anthropic haiku entry; falls through to the chain on failure.
    """
    is_prose, prose_text, prose_pct = _is_prose_dominant(diff_text)
    if not is_prose:
        return {"lens": "content-quality", "verdict": "PASS",
                "reason": "not prose-dominant (fail-open)", "prose_pct": prose_pct}

    _log(f"[content-lens] prose-dominant ({prose_pct:.0%}) — running content-quality lens")
    voice_guide = _fetch_voice_guide(repo)
    prompt = _CONTENT_LENS_PROMPT.format(
        voice_guide=voice_guide or "NO GUIDE — use generic rubric above.",
        prose=prose_text or "(no prose lines extracted)",
    )

    # D3: Use Anthropic haiku (different vendor from gpt-5 writer) for
    # self-grading avoidance. Build a chain that puts Anthropic first.
    # Fall back through the normal medium chain on any error.
    anthropic_chain = [e for e in validator_model.resolve_chain("medium")
                       if "anthropic" in e.get("provider", "").lower()]
    medium_chain = validator_model.resolve_chain("medium")
    chain = anthropic_chain + [e for e in medium_chain if e not in anthropic_chain]

    if not chain:
        _log("[content-lens] no model chain — PASS (fail-open)")
        return {"lens": "content-quality", "verdict": "PASS",
                "reason": "no model chain (fail-open)", "prose_pct": prose_pct}

    for entry in chain:
        model = entry.get("model", "")
        provider = entry.get("provider", "")
        cmd = [HERMES_BIN, "-z", prompt]
        if model:
            cmd += ["-m", model]
        if provider:
            cmd += ["--provider", provider]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=HERMES_TIMEOUT)
            out = (r.stdout or "") + "\n" + (r.stderr or "")
            if "VERDICT:" not in out and r.returncode != 0:
                _log(f"[content-lens] model {entry.get('label')} failed rc={r.returncode}; trying next")
                continue
            verdict, reason = _parse_content_verdict(out)
            _log(f"[content-lens] {entry.get('label')} -> {verdict} {reason}")
            return {"lens": "content-quality", "verdict": verdict,
                    "reason": reason, "model": entry.get("label", model),
                    "prose_pct": prose_pct}
        except Exception as exc:
            _log(f"[content-lens] model {entry.get('label')} exception: {exc!r}; trying next")
            continue

    # Every chain entry failed -> fail-open PASS
    _log("[content-lens] entire chain failed — PASS (fail-open)")
    return {"lens": "content-quality", "verdict": "PASS",
            "reason": "all models unavailable (fail-open)", "prose_pct": prose_pct}


def validate(
    repo,
    pr,
    task="",
    shadow=True,
    allow_panel=True,
    expected_repo="",
    *,
    trusted_identity=None,
    trust_store_path=None,
):
    """Run the vendored validator inside the SQLite trust boundary.

    Identity/lease acquisition happens before the diff is read, so overlapping
    wakes cannot execute concurrent validators for the same immutable candidate.
    This deployment is shadow-only: ``ReviewRunner`` plans but never executes
    PR code, and the existing deterministic/panel result is finalized through
    the live SQLite fencing token.
    """
    try:
        identity = trusted_identity or validator_verdict.resolve_shadow_identity(
            repo, pr, task_id=task, expected_repo=expected_repo
        )
        session = validator_verdict.begin_shadow_review(identity, path=trust_store_path)
    except validator_verdict.VerdictBusyError as exc:
        _log(f"[validate_pr] {repo}#{pr} validation already fenced by another worker: {exc}")
        return 2, {"verdict": "BLOCK", "reason": str(exc), "fenced": True}
    except validator_verdict.VerdictStoreError as exc:
        _log(f"[validate_pr] {repo}#{pr} cannot establish trusted SQLite identity: {exc}")
        return 2, {"verdict": "BLOCK", "reason": str(exc), "fenced": False}

    if session.existing is not None:
        existing = validator_verdict.verdict_for(
            repo, pr, path=trust_store_path, head_sha=identity.head_sha
        )
        if existing is None:
            return 2, {"verdict": "BLOCK", "reason": "immutable SQLite verdict could not be read"}
        _log(f"[validate_pr] {repo}#{pr} already has immutable fenced verdict for this identity")
        return (0 if existing.get("verdict") == "PASS" else 1), existing

    # Once the lease exists, every exit path must release that exact fencing
    # token.  Panel/tripwire integrations are allowed to fail, but they must
    # never leave a 15-minute busy lease that prevents an immediate retry.
    try:
        diff = vc.fetch_pr_diff(repo, pr)
        if not diff.strip():
            _log(f"[validate_pr] empty/failed diff for {repo}#{pr} — cannot validate")
            return 2, {"verdict": "BLOCK", "tier": "unknown",
                       "reason": "could not fetch PR diff (fail-closed)"}
        head = vc.pr_head_sha(repo, pr)
        if head != identity.head_sha:
            _log(f"[validate_pr] {repo}#{pr} head moved after lease acquisition; refusing stale validation")
            return 2, {"verdict": "BLOCK", "tier": "unknown",
                       "reason": "PR head changed after immutable identity acquisition (fail-closed)"}

        content_result = None   # set in the tier=="low" branch; None elsewhere
        tw = vt.run(diff, repo=repo, expected_repo=expected_repo)
        # image authenticity: a vision BACKSTOP over added /images/driven/ vehicle
        # photos (executor self-QC is the primary catch — skills/vehicle-image-qc).
        # Deterministic findings here join the tripwire findings so a HIGH (confident
        # AI-fake) flows through the same early-BLOCK + fail-closed override below; it
        # is biased to REAL and fails OPEN (medium/warn) on any vision error, so it
        # never bricks a non-vehicle or transient-error PR. No-op when the diff
        # touches no driven images. Reversibility: delete via import + this block.
        img = via.run(diff, repo=repo, head=head, pr=pr)
        det_findings = list(tw["findings"]) + list(img.get("findings", []))
        high_findings = [f for f in det_findings if f["severity"] == "high"]
        tier = tw["tier"]

        if high_findings:
            verdict, model_used = "BLOCK", "tripwires+vision"
            panel = None
            _log(f"[validate_pr] {repo}#{pr} BLOCK on {len(high_findings)} deterministic "
                 "finding(s): "
                 + "; ".join(f"{f['check']}:{f['file']}" for f in high_findings))
        elif tier == "low":
            # D1 fix (S2, 2026-06-23): run the content-quality lens for
            # prose-dominant PRs instead of auto-PASS-low. Fail-open: on any
            # lens error the returned verdict is PASS so safe small fixes still
            # ship. The lens is the only LLM spend for low-tier content PRs.
            content_result = _run_content_lens(repo, diff)
            panel = None
            if content_result.get("verdict") == "BLOCK":
                verdict = "BLOCK"
                model_used = "content-quality-lens:" + content_result.get("model", "")
                _log(f"[validate_pr] {repo}#{pr} BLOCK (content-quality lens): "
                     f"{content_result.get('reason','')}")
            else:
                verdict = "PASS"
                model_used = ("tripwires+content-quality-lens"
                              if content_result.get("prose_pct", 0) >= 0.70
                              else "tripwires")
                _log(f"[validate_pr] {repo}#{pr} PASS (low risk, "
                     f"prose_pct={content_result.get('prose_pct', 0):.0%})")
        elif allow_panel:
            panel = validator_panel.run(diff, tier, tripwires=tw["findings"])
            verdict = panel["verdict"]
            model_used = panel.get("model_used", "")
            _log(f"[validate_pr] {repo}#{pr} panel({tier}) -> {verdict} "
                 f"[{model_used}] " + json.dumps(panel.get("lenses", [])))
        else:
            # panel suppressed (e.g. shadow smoke test) but risk >= medium:
            # deterministic-clean but unreviewed -> conservative BLOCK for high,
            # PASS-with-warning for medium. NOTE: a panel-suppressed run is
            # ALWAYS finalized as a shadow verdict (force_shadow below), so
            # this PASS is never merge-eligible.
            verdict = "BLOCK" if tier == "high" else "PASS"
            model_used, panel = "tripwires(no-panel)", None
            _log(f"[validate_pr] {repo}#{pr} {verdict} (tier={tier}, panel suppressed)")

        findings = list(det_findings)
        if panel:
            for l in panel.get("lenses", []):
                if l.get("verdict") == "BLOCK":
                    findings.append({"check": f"panel:{l['lens']}", "severity": "high",
                                     "file": "(review)", "detail": l.get("reason", "")})
        # D1 fix: record content-lens result in findings either way (PASS or BLOCK)
        # so the verdict store always has a content-quality entry for prose-dominant PRs.
        if content_result is not None:
            cl = content_result
            sev = "high" if cl.get("verdict") == "BLOCK" else "info"
            findings.append({
                "check": "content-quality-lens",
                "severity": sev,
                "file": "(prose content)",
                "detail": cl.get("reason", "") or f"prose_pct={cl.get('prose_pct',0):.0%} PASS",
            })

        # FAIL-CLOSED OVERRIDE (Caretaker fix, 2026-06-21, behavior #2):
        # Any HIGH-severity tripwire/deterministic finding forces a non-PASS verdict
        # REGARDLESS of what the LLM panel (or any other layer) concluded. The early
        # `if high_findings:` branch above usually catches this before the panel runs,
        # but this is the single authoritative guard right before the verdict is
        # persisted — so a HIGH deterministic finding can never reach the store as a
        # PASS. Reversibility: delete this block.
        high_now = [f for f in findings if f.get("severity") == "high"]
        if verdict == "PASS" and high_now:
            _log(f"[validate_pr] {repo}#{pr} FAIL-CLOSED: downgrading PASS->BLOCK on "
                 f"{len(high_now)} HIGH finding(s): "
                 + "; ".join(f"{f.get('check')}:{f.get('file')}" for f in high_now[:5]))
            verdict = "BLOCK"
            model_used = (model_used + "+failclosed").strip("+")

        result = {
            "repo": repo, "pr": pr, "task_id": task,
            "verdict": verdict, "tier": tier, "head_sha": head,
            "expected_repo": expected_repo or repo,
            "model_used": model_used, "shadow": shadow,
            "tripwire_findings": tw["findings"],
            "image_findings": img.get("findings", []),
            "panel": panel, "findings": findings,
        }

        # Commit through the active SQLite fencing token. There is deliberately no
        # JSON write or JSON fallback: a failed finalization leaves no mergeable
        # verdict and the outer finally releases only this worker's lease.
        #
        # validator_review=True marks this as the validator's own fenced review
        # flow — the ONLY caller allowed to finalize a merge-eligible
        # (non-shadow) verdict, and only while its lease is provably live.
        # force_shadow: a caller-requested --shadow run OR a panel-suppressed
        # (--no-panel) run must ALWAYS produce a shadow verdict — a zero-LLM
        # smoke test can never mint a merge-eligible PASS, regardless of tier
        # or env.
        kept, immutable = validator_verdict.finalize_shadow_review(session, {
            "verdict": verdict, "tier": tier, "task_id": task,
            "head_sha": identity.head_sha, "expected_repo": identity.canonical_repo,
            "model_used": model_used, "findings": findings, "shadow": shadow,
            "ts": validator_verdict._now_iso(),
        }, validator_review=True, force_shadow=bool(shadow) or not allow_panel)
        result["shadow"] = kept.get("shadow", True)
        if immutable:
            _log(f"[validate_pr] {repo}#{pr} immutable terminal verdict held for "
                 f"head {kept.get('head_sha','')[:8]}; this result was ignored (fail-closed)")
            verdict = kept.get("verdict", verdict)
            result["verdict"] = verdict
            result["immutable_verdict"] = True

        return (0 if verdict == "PASS" else 1), result
    except Exception as exc:
        _log(f"[validate_pr] {repo}#{pr} validation crashed after lease acquisition: {exc!r}")
        return 2, {"verdict": "BLOCK", "tier": "unknown", "fenced": False,
                   "reason": "unexpected validator failure after lease acquisition (fail-closed)"}
    finally:
        validator_verdict.abort_shadow_review(session)


def main():
    p = argparse.ArgumentParser(description="One-shot PR validation orchestrator.")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--task", default="", help="ClickUp task id (for the verdict record)")
    p.add_argument("--expected-repo", default="", help="resolved target owner/repo")
    p.add_argument("--shadow", action="store_true",
                   help="mark verdict as shadow (recorded, human still merges)")
    p.add_argument("--no-panel", action="store_true",
                   help="skip the LLM panel (deterministic-only smoke test)")
    args = p.parse_args()
    rc, result = validate(args.repo, args.pr, task=args.task,
                          shadow=args.shadow, allow_panel=not args.no_panel,
                          expected_repo=args.expected_repo)
    print(json.dumps(result, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
