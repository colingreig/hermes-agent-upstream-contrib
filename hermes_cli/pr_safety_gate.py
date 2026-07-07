"""Automated pre-merge gate for recovery/stranded-worktree PRs.

Grew out of the same incident as ``hermes_cli/content_gate.py``: the blog
post that shipped live with literal placeholder content ("Placeholder
model" / "TBC") was merged via a recovery PR (a PR created to rescue work
from a stranded/abandoned git worktree) whose description didn't match its
actual diff. Nobody caught the mismatch before merge, because the only
check that existed was a manual, reviewer-invoked checklist item in
``skills/github/github-code-review/SKILL.md`` — it never actually blocked
anything.

This module is the automated version of that check. It reuses the exact
mismatch heuristic already implemented as
:func:`hermes_cli.content_gate.flag_recovery_pr_mismatch` (Check 3) and
wires it into an actual chokepoint: a required GitHub Actions status check
(see ``.github/workflows/pr-safety-gate.yml``) that runs on every
``pull_request`` event and fails the job when a mismatch is detected. A
failing required check blocks merge via branch protection — this is a
structural gate, not a comment or a checklist line.

The CLI entry point (``python -m hermes_cli.pr_safety_gate``) is the piece
CI actually invokes. It is a thin wrapper: take the PR title/body/branch
name as CLI args (the calling workflow passes these from
``github.event.pull_request.*`` — see ``.github/workflows/ci.yml``'s
``pr-safety-gate`` job), compute the diff stat locally via ``git diff
--stat`` against the merge-base with the target branch (same technique as
``.github/workflows/history-check.yml``, no ``gh``/API auth required),
call :func:`check_recovery_pr`, print the result, and exit non-zero on a
mismatch. The core logic lives in :func:`check_recovery_pr` so it is
independently unit-testable without any git or GitHub access at all.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from typing import NamedTuple, Optional

from hermes_cli.content_gate import flag_recovery_pr_mismatch

# Additional recovery-PR signals beyond the description text itself —
# branch name and PR title patterns. A recovery PR is often titled/branched
# distinctively even when the body text doesn't use the word "recovery"
# verbatim (e.g. `recover/checkout-fix` or "Salvage stranded worktree
# changes"). These are OR'd with the description-text signal already
# implemented in ``flag_recovery_pr_mismatch`` so detection doesn't depend
# solely on body wording.
_RECOVERY_BRANCH_RE = re.compile(r"^(recover|recovery|salvage)/", re.IGNORECASE)
_RECOVERY_TITLE_RE = re.compile(r"\b(recovery|stranded|salvage)\b", re.IGNORECASE)

# Self-referential exemption: a PR that touches the gate's own implementation
# (this file, or content_gate.py's flag_recovery_pr_mismatch it wraps) will
# inevitably discuss "recovery"/"stranded worktree" in its title/description
# -- it IS the feature that talks about that -- which would otherwise always
# false-positive the title/description signal against itself (observed live:
# PR #14, whose description explains this very check, got flagged as a
# mismatched recovery PR for naming `config.platforms`/`config.yaml` in
# unrelated prose). The branch-name pattern is a genuine structural signal a
# real recovery PR could still carry even while touching these files, so it
# still overrides this exemption.
_GATE_OWN_FILES = ("hermes_cli/pr_safety_gate.py", "hermes_cli/content_gate.py")


class RecoveryPrCheckResult(NamedTuple):
    """Outcome of :func:`check_recovery_pr`."""

    is_recovery_pr: bool
    mismatch: Optional[str]

    @property
    def blocked(self) -> bool:
        """True when this PR must not be auto-merged."""
        return self.mismatch is not None


def is_recovery_pr(*, branch_name: str = "", pr_title: str = "", pr_description: str = "") -> bool:
    """Return True if any signal marks this as a recovery/stranded-worktree PR.

    Checks, in order: branch name pattern (``recover/*`` / ``recovery/*`` /
    ``salvage/*``), recovery/stranded/salvage language in the title, and
    recovery/stranded-worktree language in the description (the same
    pattern :func:`hermes_cli.content_gate.flag_recovery_pr_mismatch`
    matches internally). Any one hit is sufficient.
    """
    if branch_name and _RECOVERY_BRANCH_RE.search(branch_name.strip()):
        return True
    if pr_title and _RECOVERY_TITLE_RE.search(pr_title):
        return True
    if pr_description and _RECOVERY_TITLE_RE.search(pr_description):
        return True
    return False


def check_recovery_pr(
    *,
    branch_name: str = "",
    pr_title: str = "",
    pr_description: str = "",
    diff_stat_text: str = "",
) -> RecoveryPrCheckResult:
    """Full recovery-PR safety check: detect + cross-check description vs diff.

    Pure function, no I/O — the CLI wrapper (:func:`main`) is responsible
    for fetching ``pr_title``/``pr_description``/``diff_stat_text`` via
    ``gh``/the GitHub API before calling this.

    Returns a :class:`RecoveryPrCheckResult`. When ``is_recovery_pr`` is
    False, ``mismatch`` is always ``None`` (the check doesn't apply to
    ordinary PRs). When ``is_recovery_pr`` is True, ``mismatch`` is a
    human-readable warning string if the description doesn't match the
    diff, or ``None`` if everything lines up.
    """
    branch_signals_recovery = bool(branch_name and _RECOVERY_BRANCH_RE.search(branch_name.strip()))
    touches_gate_own_code = any(f in (diff_stat_text or "") for f in _GATE_OWN_FILES)
    if touches_gate_own_code and not branch_signals_recovery:
        return RecoveryPrCheckResult(is_recovery_pr=False, mismatch=None)

    recovery = is_recovery_pr(
        branch_name=branch_name, pr_title=pr_title, pr_description=pr_description,
    )
    if not recovery:
        return RecoveryPrCheckResult(is_recovery_pr=False, mismatch=None)

    # flag_recovery_pr_mismatch only activates on recovery language in the
    # description text itself. If this PR was flagged as a recovery PR via
    # branch name or title alone (description has no recovery wording),
    # synthesize a minimal description prefix so the mismatch heuristic
    # still runs against the real description content.
    effective_description = pr_description
    if not _RECOVERY_TITLE_RE.search(pr_description or ""):
        effective_description = f"Recovery PR. {pr_description or ''}"

    mismatch = flag_recovery_pr_mismatch(effective_description, diff_stat_text)
    return RecoveryPrCheckResult(is_recovery_pr=True, mismatch=mismatch)


# ---------------------------------------------------------------------------
# CLI entry point — the actual CI step invocation.
# ---------------------------------------------------------------------------

def _git_diff_stat(base_ref: str) -> str:
    """Return ``git diff --stat`` of HEAD against the merge-base with
    ``base_ref``. Falls back to a plain ``git diff --stat`` against
    ``base_ref`` directly if the merge-base lookup fails (shallow clone,
    detached history, etc.) — non-fatal, mirrors
    ``content_gate.diff_changed_files``'s fallback style.
    """
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", base_ref, "HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        merge_base = base_ref

    proc = subprocess.run(
        ["git", "diff", "--stat", f"{merge_base}...HEAD" if merge_base != base_ref else base_ref],
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout if proc.returncode == 0 else ""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hermes_cli.pr_safety_gate",
        description=(
            "Recovery/stranded-worktree PR description-vs-diff safety gate. "
            "Fails (exit 1) when a recovery PR's description doesn't match "
            "its actual diff — intended as a required CI status check. "
            "Run from inside the PR's checkout with full history "
            "(fetch-depth: 0)."
        ),
    )
    parser.add_argument("--branch", default="", help="PR head branch name (github.head_ref)")
    parser.add_argument("--title", default="", help="PR title (github.event.pull_request.title)")
    parser.add_argument("--body", default="", help="PR description/body (github.event.pull_request.body)")
    parser.add_argument(
        "--base-ref", default="origin/main",
        help="Base ref to diff against (default: origin/main)",
    )
    args = parser.parse_args(argv)

    diff_stat = _git_diff_stat(args.base_ref)

    result = check_recovery_pr(
        branch_name=args.branch, pr_title=args.title, pr_description=args.body,
        diff_stat_text=diff_stat,
    )

    if not result.is_recovery_pr:
        print("pr-safety-gate: not a recovery/stranded-worktree PR — check does not apply.")
        return 0

    if result.mismatch:
        print(f"::error::pr-safety-gate: {result.mismatch}")
        print(
            "This PR was detected as a recovery/stranded-worktree PR and its "
            "description does not match its diff. Merge is blocked until a "
            "human confirms the extra/missing scope is expected and updates "
            "the description (or the diff) accordingly."
        )
        return 1

    print("pr-safety-gate: recovery PR detected — description matches diff. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
