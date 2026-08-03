#!/usr/bin/env python3
"""validate_pr_live_trigger.py — deterministic live trigger for the fenced PR validator.

WHY THIS EXISTS (task 86e2m44np): autonomous_merge.sweep() only ever merges a
PR that already has a fresh non-shadow PASS row in the SQLite verdict ledger
(~/.hermes/scripts/.validator_trust.sqlite3), but nothing on the Mini ever
invoked validate_pr.py — the only PR-linked agent job (hermes-pr-validate)
runs the generic ignite-validate skill, which never references this pipeline.
The ledger therefore had ZERO finalizations ever and autonomous merge was
structurally unreachable. This module is the deterministic producer that
closes that gap: it enumerates allowlisted-repo PRs that are fully CI-green
and unheld, and runs validate_pr.validate() on each so a genuine fenced
verdict is finalized.

TRUST BOUNDARY (unchanged, deliberately): a merge-eligible (non-shadow)
verdict can only be finalized by validate_pr.validate()'s own fenced review
flow while HERMES_VALIDATOR_FINALIZE_TOKEN is present in the process env
(validator_verdict.finalize_shadow_review). This trigger runs as its own
dedicated no_agent cron job whose jobs.json entry declares that token in
required_environment_variables, so the scheduler resolves it per-job
(profile/lazy-1P) and injects it ONLY into this child process — the token
stays out of the gateway boot environment (it is in
reconcile_launchd_environment.FORBIDDEN_BOOT_REFERENCE_KEYS) and executor
subprocess scrubbing keeps stripping it everywhere else. No executor or agent
gains any new way to mint a verdict: this process IS the validator's review
flow, and everything it finalizes goes through the same lease-fenced,
fail-closed validate_pr pipeline (tripwires, risk tier, panel, fail-closed
overrides).

CANDIDATE DISCIPLINE (fail-safe): verdicts are immutable per PR head, so this
trigger must never validate early. Only OPEN, non-draft, unheld PR heads with
zero failing/pending GATING checks and at least one green gating check are
ever handed to the validator — validating before CI completes would mint an
immutable BLOCK (via check_missing_ci) for a head that was about to go green.
Everything else is skipped and retried on a later tick. LLM spend is bounded
by a per-tick validation cap.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

if __package__:
    from . import autonomous_merge
    from . import pr_pipeline_improvements
    from . import validate_pr
    from . import validator_verdict
else:  # pragma: no cover - flat deployment fallback, mirrors validate_pr.py
    import autonomous_merge
    import pr_pipeline_improvements
    import validate_pr
    import validator_verdict

FINALIZE_TOKEN_ENV = "HERMES_VALIDATOR_FINALIZE_TOKEN"
MAX_VALIDATIONS_ENV = "HERMES_VALIDATOR_TRIGGER_MAX"
DEFAULT_MAX_VALIDATIONS = 3
PR_LIST_LIMIT = 30
GH_TIMEOUT = 60
# Mirrors autonomous_merge._merge_readiness — a held PR is not a candidate.
HOLD_LABELS = {"hold-for-colin", "do-not-merge", "do-not-auto-merge", "hold"}


def _log(msg: str) -> None:
    print(f"[validator-trigger] {msg}", flush=True)


def _log_err(msg: str) -> None:
    print(f"[validator-trigger] {msg}", file=sys.stderr, flush=True)


def finalize_token_present() -> bool:
    return bool((os.environ.get(FINALIZE_TOKEN_ENV, "") or "").strip())


def max_validations() -> int:
    raw = (os.environ.get(MAX_VALIDATIONS_ENV, "") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_VALIDATIONS
    return value if value > 0 else DEFAULT_MAX_VALIDATIONS


def _list_open_prs(repo):
    """Return (prs, err). prs is a list of gh PR dicts; err set on fetch error."""
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--json", "number,title,body,headRefName",
             "--limit", str(PR_LIST_LIMIT)],
            capture_output=True, text=True, timeout=GH_TIMEOUT,
            env=autonomous_merge._shim_env())
    except Exception as exc:
        return None, f"gh pr list failed: {exc!r}"
    if r.returncode != 0:
        return None, f"gh pr list rc={r.returncode}: {(r.stderr or '').strip()[:160]}"
    try:
        prs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError as exc:
        return None, f"gh pr list returned malformed JSON: {exc}"
    return prs if isinstance(prs, list) else [], None


def candidate_skip_reason(info) -> str:
    """Return '' when a PR head is safe to validate live, else the skip reason.

    FAIL-SAFE: only a fully CI-green, unheld, open head is ever validated —
    the verdict store is immutable per head, so validating a head whose gating
    CI is failing or still pending would permanently BLOCK that head (via
    adversarial_review.check_missing_ci) even if CI went green minutes later.
    Reuses autonomous_merge.pr_state()'s gating classification so the
    validate-time and merge-time views of "is CI green" can never drift.
    """
    if (info.get("state") or "").upper() != "OPEN":
        return f"PR state is {info.get('state')} (not OPEN)"
    if info.get("draft"):
        return "PR is a draft (held)"
    held = [l for l in (info.get("labels") or []) if l.lower() in HOLD_LABELS]
    if held:
        return f"hold label(s): {', '.join(held)}"
    if (info.get("mergeable") or "").upper() == "CONFLICTING" or info.get("merge_state") == "DIRTY":
        return "PR has merge conflicts"
    if info.get("failing"):
        return f"gating checks failing: {', '.join(info['failing'][:3])}"
    if info.get("pending"):
        return f"gating checks pending: {', '.join(info['pending'][:3])}"
    if not info.get("gating_green"):
        return "no green gating check on head (fail-safe: not validating unverified code)"
    return ""


def scan_candidates(allowlist):
    """Enumerate validation candidates across the allowlist.

    Returns (candidates, skipped, errors). A candidate is a dict with
    repo/pr/head/task_id. Repo-alias spellings of the same repository are
    deduped by (pr_number, head_sha) — the lexicographically first spelling
    wins, so one PR never gets validated under two ledger subjects.
    """
    candidates, skipped, errors = [], [], []
    seen_heads = set()
    for repo in sorted(allowlist):
        prs, err = _list_open_prs(repo)
        if err:
            errors.append(f"{repo}: {err}")
            continue
        for pr_data in prs:
            number = pr_data.get("number")
            if not isinstance(number, int):
                continue
            info, perr = autonomous_merge.pr_state(repo, number)
            if perr:
                errors.append(f"{repo}#{number}: {perr}")
                continue
            head = info.get("head") or ""
            if not head:
                errors.append(f"{repo}#{number}: no head SHA")
                continue
            if (number, head) in seen_heads:
                continue  # alias spelling of an already-scanned repository
            seen_heads.add((number, head))
            reason = candidate_skip_reason(info)
            if reason:
                skipped.append({"repo": repo, "pr": number, "reason": reason})
                continue
            try:
                existing = validator_verdict.verdict_for(repo, number, head_sha=head)
            except validator_verdict.VerdictStoreError as exc:
                errors.append(f"{repo}#{number}: verdict store unreadable: {exc}")
                continue
            if existing is not None:
                skipped.append({
                    "repo": repo, "pr": number,
                    "reason": f"immutable verdict already recorded for head {head[:8]} "
                              f"({existing.get('verdict')})",
                })
                continue
            task_id = (
                pr_pipeline_improvements._extract_clickup_task_id(pr_data.get("body"))
                or pr_pipeline_improvements._extract_clickup_task_id(pr_data.get("title"))
                or pr_pipeline_improvements._extract_clickup_task_id(pr_data.get("headRefName"))
            )
            candidates.append({
                "repo": repo, "pr": number, "head": head, "task_id": task_id,
            })
    return candidates, skipped, errors


def run(dry_run: bool = False, cap: int | None = None):
    """One trigger tick. Returns a JSON-serializable summary dict."""
    summary = {
        "dry_run": bool(dry_run),
        "candidates": [],
        "skipped": [],
        "errors": [],
        "validated": [],
    }
    if not finalize_token_present():
        # The cron job declares the token in required_environment_variables so
        # the scheduler fails the run before this script even starts when it
        # cannot be resolved — this in-process check is belt-and-suspenders.
        # Running without it would silently downgrade every verdict to shadow
        # (never merge-eligible) while spending real LLM panel money: refuse
        # loudly instead so the fleet-outcome contract can alarm on it.
        _log_err("FINALIZE TOKEN ABSENT — refusing to run live validations "
                 "(verdicts would be silently shadow-only)")
        summary["errors"].append("finalize-token-absent")
        return 1, summary

    allowlist = autonomous_merge._load_allowlist()
    if not allowlist:
        _log("allowlist is empty — nothing to validate")
        return 0, summary

    candidates, skipped, errors = scan_candidates(allowlist)
    summary["candidates"] = candidates
    summary["skipped"] = skipped
    summary["errors"].extend(errors)
    for item in skipped:
        _log(f"skip {item['repo']}#{item['pr']}: {item['reason']}")
    for err in errors:
        _log_err(f"scan error (non-fatal): {err}")

    limit = cap if isinstance(cap, int) and cap > 0 else max_validations()
    to_validate = candidates[:limit]
    if len(candidates) > limit:
        _log(f"{len(candidates)} candidate(s); validating first {limit} "
             f"(cap, remainder next tick)")

    for item in to_validate:
        repo, pr, task_id = item["repo"], item["pr"], item["task_id"]
        if dry_run:
            _log(f"DRY_RUN would validate {repo}#{pr} head={item['head'][:8]} "
                 f"task={task_id or '-'}")
            continue
        _log(f"validating {repo}#{pr} head={item['head'][:8]} task={task_id or '-'}")
        try:
            rc, result = validate_pr.validate(
                repo, pr, task=task_id, shadow=False, allow_panel=True,
                expected_repo=repo)
        except Exception as exc:
            _log_err(f"{repo}#{pr} validation crashed (non-fatal): {exc!r}")
            summary["errors"].append(f"{repo}#{pr}: {exc!r}")
            continue
        outcome = {
            "repo": repo, "pr": pr, "rc": rc,
            "verdict": result.get("verdict"),
            "tier": result.get("tier"),
            "shadow": result.get("shadow"),
        }
        summary["validated"].append(outcome)
        _log(f"{repo}#{pr} -> {outcome['verdict']} tier={outcome['tier']} "
             f"shadow={outcome['shadow']} rc={rc}")

    return 0, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic live trigger for the fenced PR validator.")
    parser.add_argument("--dry-run", action="store_true",
                        help="enumerate candidates without validating")
    parser.add_argument("--max", type=int, default=0,
                        help=f"per-tick validation cap (default env "
                             f"{MAX_VALIDATIONS_ENV} or {DEFAULT_MAX_VALIDATIONS})")
    args = parser.parse_args(argv)
    dry_run = args.dry_run or bool(os.environ.get("DRY_RUN"))
    rc, summary = run(dry_run=dry_run, cap=args.max if args.max > 0 else None)
    print(json.dumps({
        "trigger": "validator-live-trigger",
        "candidates": len(summary["candidates"]),
        "validated": len(summary["validated"]),
        "skipped": len(summary["skipped"]),
        "errors": summary["errors"],
        "results": summary["validated"],
    }, indent=2))
    return rc


def safe_main() -> int:
    """Never let an operational failure break cron delivery."""
    try:
        return main()
    except Exception as exc:  # pragma: no cover - defensive cron wrapper
        _log_err(f"unexpected trigger error: {exc!r}")
        print(json.dumps({"trigger": "validator-live-trigger", "error": repr(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(safe_main())
