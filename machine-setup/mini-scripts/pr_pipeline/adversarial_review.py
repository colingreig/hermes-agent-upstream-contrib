#!/usr/bin/env python3
"""adversarial_review.py — the adversarial scrutiny pass for in-review /
needs-validation tasks, run before a validator verdict can be finalized as
PASS (ClickUp 86e2k3qe2).

Hermes's PR pipeline already built strong, well-tested machinery for each of
the three failure classes an adversarial reviewer must actively hunt for —
but that machinery was either duplicated with a weaker/buggy variant, or only
wired in at MERGE time rather than at VALIDATE time. This module is the single
explicit, testable place these three checks live, and it reuses the existing
authoritative implementations instead of re-deriving them:

  - wrong_repo       validate_tripwires.run() used to compare `repo` and
                     `expected_repo` as LOWERCASED NAME STRINGS. That is
                     exactly the RC2 bug validator_repo_guard.py was built to
                     fix (`colingreig/hermes-agent` renamed to
                     `.../hermes-agent-upstream-contrib` on 2026-07-23 — both
                     spellings address ONE repository, but a naive string
                     compare reads them as different and manufactures a
                     spurious `class=wrong-repo` FAIL against real, merged
                     work — see validator_repo_guard.py's docstring, RC2/RC3,
                     and the manually-voided FAILs 86e2gh04e/86e2gdmfk/
                     86e25xww8/86e2f7ukm). check_wrong_repo() delegates to
                     validator_repo_guard.compare_refs() (canonical GitHub
                     node_id, rename-following, alias-aware, fail-closed on
                     UNRESOLVED) so this call path can never reintroduce that
                     bug, and still catches a GENUINELY different repo.

  - stale_evidence   a diff/verdict pulled for one commit is worthless proof
                     once the PR has moved to a new head — the review is FOR
                     a snapshot that no longer exists. validate_pr.py already
                     enforces this for its OWN read (fail-closed BLOCK on
                     head-sha mismatch right after fetching the diff); this
                     module exposes the same check as a standalone, reusable,
                     independently testable finding so any OTHER adversarial
                     reader (a human re-reading a stored verdict, a future
                     caller) gets the same guarantee instead of having to
                     re-derive it.

  - missing_ci       validate_tripwires.check_ci_green() only ever asked "is
                     the BASE branch (main) red right now?" — it says nothing
                     about the PR's OWN head commit. Nothing stopped a
                     validator PASS being recorded for a PR whose own CI never
                     ran, is still pending, or is actively failing; that gap
                     is only closed later, at MERGE time, by
                     autonomous_merge._merge_readiness(). check_missing_ci()
                     reuses that exact function (autonomous_merge.pr_state(),
                     the same gating-vs-non-gating classification the merge
                     gate itself trusts) so the validate-time and merge-time
                     views of "is CI green" can never disagree, and so a
                     stored PASS comment/marker is honest about CI state
                     *before* a task is advanced toward completion — not just
                     before the PR is merged.

FAIL-CLOSED POLICY (mirrors validator_repo_guard's documented rule): an
INCONCLUSIVE check (network error, unresolvable identity, no PR context)
never manufactures a fabricated HIGH/BLOCK against real work, and never
silently says nothing either — it degrades to a "medium" (visible, WARN,
non-blocking) finding. Only a POSITIVELY CONFIRMED problem — genuinely
different repo, confirmed stale head, a real failing gating check — is
"high" (blocking).

Usage (library, the integration point — see validate_pr.py / validate_tripwires.py):
    from . import adversarial_review as ar
    findings = ar.check_wrong_repo(repo, expected_repo)
    findings += ar.check_missing_ci(repo, pr)

Usage (CLI, manual adversarial spot-check of an open PR):
    adversarial_review.py --repo owner/repo --pr 123 [--expected-repo X] \\
        [--recorded-head SHA]
"""
from __future__ import annotations

import argparse
import json
import sys

if __package__:
    from . import validator_common as vc
    from . import validator_repo_guard as vrg
else:
    import validator_common as vc
    import validator_repo_guard as vrg


# ---------------------------------------------------------------------------
# wrong-repo
# ---------------------------------------------------------------------------


def check_wrong_repo(repo, expected_repo, default_owner=vrg.DEFAULT_OWNER):
    """Return findings iff `repo` and `expected_repo` are CONFIRMED different
    repositories by canonical GitHub identity. Never fires on a rename alias
    (e.g. hermes-agent vs hermes-agent-upstream-contrib) and never fires when
    either side is blank (nothing to compare)."""
    findings = []
    if not repo or not expected_repo:
        return findings
    # Cheapest possible answer: identical spelling needs no network/identity
    # lookup at all, and short-circuits before compare_refs's own fast path.
    if str(repo).strip().lower() == str(expected_repo).strip().lower():
        return findings
    verdict, detail = vrg.compare_refs(expected_repo, repo, default_owner)
    if verdict == vrg.DIFFERENT:
        findings.append({
            "check": "wrong-repo", "severity": "high", "file": "(repo identity)",
            "detail": detail,
        })
    elif verdict == vrg.UNRESOLVED:
        # Fail-closed per validator_repo_guard's FAIL-CLOSED RULE: an
        # unresolvable identity must SKIP the wrong-repo verdict, never
        # manufacture a spurious BLOCK. Still surfaced (medium/visible) so a
        # human or ignite-validate sees "could not confirm" instead of
        # silence — a genuine wrong-repo case can't hide behind an outage.
        findings.append({
            "check": "wrong-repo", "severity": "medium", "file": "(repo identity)",
            "detail": f"cannot confirm repo identity (fail-open, non-blocking): {detail}",
        })
    # verdict == SAME -> no finding: confirmed rename alias or identical repo.
    return findings


# ---------------------------------------------------------------------------
# stale evidence
# ---------------------------------------------------------------------------


def check_stale_evidence(repo, pr, recorded_head_sha="", live_head_sha=""):
    """Return findings iff the evidence being reviewed (a diff/verdict pulled
    for `recorded_head_sha`) no longer matches the PR's live head — the
    reviewed snapshot has been superseded by a newer commit, so a PASS minted
    against it says nothing about the code that will actually ship.

    `live_head_sha` is a value the caller already resolved (e.g. via
    validator_common.pr_head_sha) — this function does no network I/O itself,
    so it stays cheap and trivially testable."""
    findings = []
    if not recorded_head_sha:
        return findings  # nothing recorded yet to compare against — not this check's job
    if not live_head_sha:
        findings.append({
            "check": "stale-evidence", "severity": "medium", "file": "(head sha)",
            "detail": f"could not read live head for {repo}#{pr}; cannot confirm "
                      "evidence currency (fail-open, non-blocking)",
        })
        return findings
    if recorded_head_sha != live_head_sha:
        findings.append({
            "check": "stale-evidence", "severity": "high", "file": "(head sha)",
            "detail": f"evidence was produced for {recorded_head_sha[:12]} but "
                      f"{repo}#{pr}'s live head is now {live_head_sha[:12]} — "
                      "re-validate against the current commit",
        })
    return findings


# ---------------------------------------------------------------------------
# missing CI (the PR's OWN head — not the base-branch check validate_tripwires
# already runs)
# ---------------------------------------------------------------------------


def check_missing_ci(repo, pr):
    """Return findings about the PR's OWN head commit CI status.

    HIGH (blocking) when a real gating check is FAILING on the PR's head.
    MEDIUM (visible, non-blocking) when no gating check has gone green yet —
    CI may simply still be running, and validate-time is often EARLIER than
    merge-time, so this must not hard-block a freshly-opened PR. The merge
    gate (autonomous_merge._merge_readiness) still hard-blocks on this exact
    condition before anything actually merges — this check's job is only to
    make the gap VISIBLE at validate time, not to duplicate the merge gate's
    stricter bar.
    """
    findings = []
    if not repo or not pr:
        return findings
    try:
        if __package__:
            from . import autonomous_merge as am
        else:
            import autonomous_merge as am
    except Exception as exc:
        findings.append({
            "check": "missing-ci", "severity": "medium", "file": "(ci)",
            "detail": f"could not load merge-gate CI classifier (fail-open, "
                      f"non-blocking): {exc!r}",
        })
        return findings

    info, err = am.pr_state(repo, pr)
    if err:
        findings.append({
            "check": "missing-ci", "severity": "medium", "file": "(ci)",
            "detail": f"could not read PR head CI status (fail-open, "
                      f"non-blocking): {err}",
        })
        return findings

    if info.get("failing"):
        findings.append({
            "check": "missing-ci", "severity": "high", "file": "(ci)",
            "detail": f"{repo}#{pr}'s own head has FAILING gating check(s): "
                      + ", ".join(info["failing"][:5]),
        })
        return findings

    if not info.get("gating_green"):
        pending = info.get("pending") or []
        detail = (f"{repo}#{pr}'s own head has NO green gating CI check yet"
                  + (f" (pending: {', '.join(pending[:5])})" if pending
                     else " (none configured or none completed)"))
        findings.append({"check": "missing-ci", "severity": "medium",
                         "file": "(ci)", "detail": detail})
    return findings


# ---------------------------------------------------------------------------
# combined pass
# ---------------------------------------------------------------------------


def run(repo, pr=None, expected_repo="", recorded_head_sha="", live_head_sha=""):
    """Run the full adversarial pass. Returns {"pass": bool, "findings": [...]}.
    `pass` is False iff any finding is "high" — mirrors validate_tripwires'
    own pass/fail contract so callers can treat the two result shapes
    identically."""
    findings = list(check_wrong_repo(repo, expected_repo))
    if pr:
        findings += check_stale_evidence(repo, pr, recorded_head_sha, live_head_sha)
        findings += check_missing_ci(repo, pr)
    passed = not any(f.get("severity") == "high" for f in findings)
    return {"pass": passed, "findings": findings}


def main():
    p = argparse.ArgumentParser(
        description="Adversarial review pass: wrong-repo, stale-evidence, missing-CI.")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--pr", type=int, help="PR number (enables stale-evidence + missing-ci)")
    p.add_argument("--expected-repo", default="", help="repo this task is chartered against")
    p.add_argument("--recorded-head", default="", help="the head SHA the evidence was produced for")
    args = p.parse_args()

    live_head = vc.pr_head_sha(args.repo, args.pr) if args.pr else ""
    result = run(args.repo, args.pr, expected_repo=args.expected_repo,
                 recorded_head_sha=args.recorded_head, live_head_sha=live_head)
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
