"""Tests for hermes_cli.pr_safety_gate — the automated recovery-PR CI gate.

Covers the pure ``check_recovery_pr`` / ``is_recovery_pr`` logic that the
``pr-safety-gate.yml`` CI workflow invokes via ``python -m
hermes_cli.pr_safety_gate``. These are the same three scenarios called out
in the task: (a) a normal PR is not flagged, (b) a recovery PR whose
description matches its diff is not flagged, (c) a recovery PR whose
description doesn't match its diff IS flagged.
"""

from __future__ import annotations

import subprocess

from hermes_cli import pr_safety_gate as psg


# ---------------------------------------------------------------------------
# is_recovery_pr detection
# ---------------------------------------------------------------------------

def test_is_recovery_pr_false_for_ordinary_pr():
    assert not psg.is_recovery_pr(
        branch_name="feature/add-checkout-flow",
        pr_title="Add checkout flow validation",
        pr_description="This PR adds validation to the checkout flow.",
    )


def test_is_recovery_pr_true_via_branch_name_pattern():
    assert psg.is_recovery_pr(branch_name="recover/checkout-fix")
    assert psg.is_recovery_pr(branch_name="recovery/lost-worktree")
    assert psg.is_recovery_pr(branch_name="salvage/orphaned-branch")


def test_is_recovery_pr_true_via_title():
    assert psg.is_recovery_pr(pr_title="Recovery: restore lost checkout changes")
    assert psg.is_recovery_pr(pr_title="Salvage stranded worktree changes")


def test_is_recovery_pr_true_via_description():
    assert psg.is_recovery_pr(
        pr_description="This is a recovery PR for a stranded worktree."
    )


def test_is_recovery_pr_false_when_no_signals_present():
    assert not psg.is_recovery_pr(
        branch_name="fix/typo", pr_title="Fix typo in README", pr_description="Small fix.",
    )


# ---------------------------------------------------------------------------
# Regression: bare "recovery" as domain vocabulary must NOT classify a PR as
# a recovery/stranded-worktree PR. PR #318 and #321 were both false-blocked
# on 2026-08-03 by the old bare-word match and needed a body reword +
# close/reopen to merge -- bodies below are the verbatim false-positive
# fragments (recovered via the GitHub content-edit history) that triggered
# the old detector.
# ---------------------------------------------------------------------------

def test_is_recovery_pr_false_for_alarm_recovery_domain_vocabulary():
    """PR #318: 'recovery' describes an alarm routing-decision variant
    (recovery-sent / auto-resolved), not a git-worktree recovery."""
    assert not psg.is_recovery_pr(
        branch_name="ignite-86e2ku0a7-fleet-alarm-forensics",
        pr_title="ignite- 86e2ku0a7: make fleet-outcome alarm storms triageable and self-explaining",
        pr_description=(
            "`route_alarm` now persists a human `reason` for **every** "
            "routing decision (sent, deduped, cutover-suppressed, "
            "new-finding-pending, recovery-*, delivery-failed), not just a "
            "bare action. Slack alerts carry the incident id, when it "
            "opened, how long it has been open, which alert number this "
            "is, why it fired now, and the triage paths. Recovery reports "
            "incident duration and alert count."
        ),
    )


def test_is_recovery_pr_false_for_lease_recovery_domain_vocabulary():
    """PR #321: 'recovery' describes the production write-lease's
    recovered-state/fence-loss-recovery mechanism, not a stranded worktree."""
    assert not psg.is_recovery_pr(
        branch_name="ignite-86e2kmq8u-single-writer-lifecycle-gate",
        pr_title=(
            "ignite- 86e2kmq8u: single-writer mutation control — close the "
            "state-db lifecycle registry gate"
        ),
        pr_description=(
            "Governed destructive writers acquire the fenced production "
            "write lease (monotone fencing tokens, CAS heartbeats, "
            "`mutation_guard` held across each durable filesystem/DB "
            "commit, immutable fence-loss and recovery receipts). "
            "**Lifecycle states** are the lease ledger's: `active → "
            "released | recovered | expired`, with evidence-backed "
            "recovery only (expiry alone never permits takeover) and "
            "immutable receipts for fence loss and recovery."
        ),
    )


def test_check_recovery_pr_not_flagged_for_alarm_recovery_domain_vocabulary():
    """End-to-end (a): the PR #318 false positive must not block merge --
    not classified as a recovery PR at all, so no diff cross-check runs."""
    result = psg.check_recovery_pr(
        branch_name="ignite-86e2ku0a7-fleet-alarm-forensics",
        pr_title="ignite- 86e2ku0a7: make fleet-outcome alarm storms triageable and self-explaining",
        pr_description=(
            "`route_alarm` now persists a human `reason` for **every** "
            "routing decision (sent, deduped, cutover-suppressed, "
            "new-finding-pending, recovery-*, delivery-failed), not just a "
            "bare action."
        ),
        diff_stat_text=(
            "hermes_cli/fleet.py | 20 ++++\n"
            "machine-setup/mini-scripts/fleet_outcome_probe.py | 80 +++++\n"
            "2 files changed, 100 insertions(+)"
        ),
    )
    assert result.is_recovery_pr is False
    assert result.mismatch is None
    assert result.blocked is False


def test_check_recovery_pr_not_flagged_for_lease_recovery_domain_vocabulary():
    """End-to-end (b): the PR #321 false positive must not block merge."""
    result = psg.check_recovery_pr(
        branch_name="ignite-86e2kmq8u-single-writer-lifecycle-gate",
        pr_title=(
            "ignite- 86e2kmq8u: single-writer mutation control — close the "
            "state-db lifecycle registry gate"
        ),
        pr_description=(
            "Governed destructive writers acquire the fenced production "
            "write lease, with evidence-backed recovery only (expiry "
            "alone never permits takeover) and immutable receipts for "
            "fence loss and recovery."
        ),
        diff_stat_text=(
            "state_db_lifecycle.py | 60 +++++\n"
            "tests/test_state_db_lifecycle.py | 40 +++\n"
            "2 files changed, 100 insertions(+)"
        ),
    )
    assert result.is_recovery_pr is False
    assert result.mismatch is None
    assert result.blocked is False


def test_check_recovery_pr_genuine_recovery_still_blocked_despite_domain_wording():
    """A GENUINE recovery PR (structural branch marker) whose description
    also happens to use 'recovery' loosely must still be classified and
    cross-checked -- the branch pattern is the primary, unweakened signal.
    Regression guard: tightening the description-text match must not
    weaken detection when a real structural marker is present.
    """
    result = psg.check_recovery_pr(
        branch_name="recover/lost-work",
        pr_title="Recover lost work from stranded worktree",
        pr_description=(
            "Recovery PR for a stranded worktree. Restores `src/a.py`."
        ),
        diff_stat_text=(
            "src/unrelated.py | 5 +++--\n"
            "1 file changed, 3 insertions(+), 2 deletions(-)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is not None
    assert result.blocked is True


# ---------------------------------------------------------------------------
# check_recovery_pr — the three required scenarios
# ---------------------------------------------------------------------------

def test_check_recovery_pr_normal_pr_not_flagged():
    """(a) A normal (non-recovery) PR is not flagged."""
    result = psg.check_recovery_pr(
        branch_name="feature/new-widget",
        pr_title="Add new widget component",
        pr_description="Adds a new widget component with tests.",
        diff_stat_text=(
            "src/widget.py | 40 ++++++++++++++++++++++++++++++++++++++++\n"
            "1 file changed, 40 insertions(+)"
        ),
    )
    assert result.is_recovery_pr is False
    assert result.mismatch is None
    assert result.blocked is False


def test_check_recovery_pr_recovery_pr_matching_diff_not_flagged():
    """(b) A recovery PR whose description matches its diff is not flagged."""
    result = psg.check_recovery_pr(
        branch_name="recover/checkout-fix",
        pr_title="Recovery PR: restore checkout worktree",
        pr_description=(
            "Recovery PR for a stranded worktree. Restores `src/checkout.py` "
            "and `src/orders/summary.py`."
        ),
        diff_stat_text=(
            "src/checkout.py | 12 +++++++---\n"
            "src/orders/summary.py | 5 +++--\n"
            "2 files changed, 12 insertions(+), 5 deletions(-)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is None
    assert result.blocked is False


def test_check_recovery_pr_recovery_pr_mismatched_diff_is_flagged():
    """(c) A recovery PR whose description doesn't match its diff IS flagged.

    Description claims the recovery restores the blog post's "final
    numbers" in one file, but the diff shows a completely different file
    changed — exactly the shape of mismatch that let the original
    incident's recovery PR (whose description didn't match its diff) sail
    through unchecked.
    """
    result = psg.check_recovery_pr(
        branch_name="recover/blog-post",
        pr_title="Recovery: restore blog post final numbers",
        pr_description=(
            "Recovery PR for a stranded worktree — restores the final "
            "numbers in `content/blog/q3-report.md`."
        ),
        diff_stat_text=(
            "content/blog/unrelated-draft.md | 8 ++++----\n"
            "1 file changed, 4 insertions(+), 4 deletions(-)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is not None
    assert result.blocked is True


def test_check_recovery_pr_flags_named_file_missing_from_diff():
    """(c) variant — description names a file the diff never touches."""
    result = psg.check_recovery_pr(
        branch_name="recover/checkout-fix",
        pr_title="Recovery PR",
        pr_description=(
            "Recovery PR for a stranded worktree. Restores "
            "`src/orders/checkout.py`."
        ),
        diff_stat_text=(
            "src/unrelated/other.py | 5 +++--\n"
            "1 file changed, 3 insertions(+), 2 deletions(-)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is not None
    assert "checkout.py" in result.mismatch
    assert result.blocked is True


def test_check_recovery_pr_flags_scope_understatement():
    """(c) variant — description understates scope vs a much larger diff."""
    result = psg.check_recovery_pr(
        branch_name="",
        pr_title="Stranded worktree recovery",
        pr_description="Stranded worktree recovery — just recovers `src/a.py`.",
        diff_stat_text=(
            "src/a.py | 5 +++--\n"
            "src/b.py | 3 +--\n"
            "src/c.py | 2 +-\n"
            "src/d.py | 1 +\n"
            "4 files changed, 8 insertions(+), 3 deletions(-)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is not None
    assert result.blocked is True


def test_check_recovery_pr_detected_via_branch_alone_still_cross_checks_diff():
    """Recovery detected purely via branch name (no recovery wording in the
    description) should still cross-check the diff, not silently no-op."""
    result = psg.check_recovery_pr(
        branch_name="salvage/lost-work",
        pr_title="Restore lost work",
        pr_description="Restores `src/only_named.py`.",
        diff_stat_text=(
            "src/only_named.py | 2 +-\n"
            "src/extra_one.py | 3 +-\n"
            "src/extra_two.py | 4 +-\n"
            "src/extra_three.py | 5 +-\n"
            "4 files changed, 12 insertions(+), 2 deletions(-)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is not None


def test_check_recovery_pr_self_referential_exemption_not_flagged():
    """A PR that touches the gate's own implementation and merely discusses
    recovery PRs as a feature (not a genuine stranded-worktree recovery)
    must not be flagged, even though its description reads exactly like a
    real recovery PR's would. Regression for PR #14, which was falsely
    blocked because its description explained this very check."""
    result = psg.check_recovery_pr(
        branch_name="ignite-cycle-20260706-131700",
        pr_title="Cycle batch: Slack config fallback + recovery-PR safety gate",
        pr_description=(
            "Automates the recovery/stranded-worktree PR description/diff "
            "mismatch check (a recovery PR is a PR created to rescue work "
            "from a stranded/abandoned git worktree) into a required CI "
            "check. Also fixes `config.platforms` handling."
        ),
        diff_stat_text=(
            "hermes_cli/pr_safety_gate.py | 40 ++++++++++\n"
            "hermes_cli/content_gate.py | 20 +++++\n"
            "tools/send_message_tool.py | 42 +++++++++\n"
            "3 files changed, 102 insertions(+)"
        ),
    )
    assert result.is_recovery_pr is False
    assert result.mismatch is None
    assert result.blocked is False


def test_check_recovery_pr_branch_name_overrides_self_referential_exemption():
    """A genuine recovery/salvage branch that happens to also touch the
    gate's own files must still be checked -- the branch-name signal is
    stronger than the self-referential exemption."""
    result = psg.check_recovery_pr(
        branch_name="salvage/gate-fix",
        pr_title="Salvage stranded worktree changes",
        pr_description="Restores `src/only_named.py`.",
        diff_stat_text=(
            "hermes_cli/pr_safety_gate.py | 5 +-\n"
            "src/only_named.py | 2 +-\n"
            "src/extra.py | 3 +-\n"
            "3 files changed, 10 insertions(+)"
        ),
    )
    assert result.is_recovery_pr is True
    assert result.mismatch is not None


# ---------------------------------------------------------------------------
# _git_diff_stat — local git fallback behavior
# ---------------------------------------------------------------------------

def test_git_diff_stat_non_fatal_when_not_a_git_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No git repo here at all — should not raise, just return "".
    assert psg._git_diff_stat("origin/main") == ""


def _init_repo_with_long_path(tmp_path):
    """Build a throwaway repo whose one changed file has a path long enough
    that ``git diff --stat`` abbreviates it with a leading ``...``.
    """
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "base")
    # Two files: git sizes --stat's name column against the widest path and
    # elides anything past the budget, so the deeper path is the one that gets
    # abbreviated. A single short-path fixture would not reproduce the bug.
    relpath = (
        "machine-setup/mini-scripts/pr_pipeline/deeply/nested/"
        "test_a_very_long_supplementary_path_name_here.py"
    )
    for rel in (relpath, "tests/agent/test_credential_pool_no_entries.py"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# added\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "add long path")
    return relpath


def test_git_diff_stat_includes_full_paths_that_stat_abbreviates(tmp_path, monkeypatch):
    """``git diff --stat`` elides long paths with a leading ``...`` to fit its
    column budget, which made ``flag_recovery_pr_mismatch`` treat an
    accurately-described file as missing from the diff and false-block the PR.
    The full path must appear in the returned text.
    """
    relpath = _init_repo_with_long_path(tmp_path)
    monkeypatch.chdir(tmp_path)

    # Guard: the bug only reproduces when --stat actually abbreviates. If a
    # future git stops eliding this path, this assertion tells us the fixture
    # went stale rather than silently passing for the wrong reason.
    stat_only = subprocess.run(
        ["git", "diff", "--stat", "main~1...HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert relpath not in stat_only
    assert "..." in stat_only

    text = psg._git_diff_stat("main~1")
    assert relpath in text


def test_matching_recovery_pr_with_long_path_is_not_blocked(tmp_path, monkeypatch):
    """End-to-end: a recovery PR that accurately names a long-path file must
    exit 0. Before the fix this exited 1 purely because of --stat truncation.
    """
    relpath = _init_repo_with_long_path(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = psg.main([
        "--branch", "recover/lost-work",
        "--title", "Recovery PR",
        "--body", f"Recovery PR for stranded worktree, restores `{relpath}`.",
        "--base-ref", "main~1",
    ])
    assert rc == 0


# ---------------------------------------------------------------------------
# CLI entry point — exit code behavior
# ---------------------------------------------------------------------------

def test_main_exits_zero_for_non_recovery_pr(monkeypatch):
    monkeypatch.setattr(psg, "_git_diff_stat", lambda base_ref: "src/a.py | 1 +\n1 file changed, 1 insertion(+)")
    rc = psg.main([
        "--branch", "feature/x", "--title", "Add feature x", "--body", "Adds feature x.",
    ])
    assert rc == 0


def test_main_exits_nonzero_for_mismatched_recovery_pr(monkeypatch):
    monkeypatch.setattr(
        psg, "_git_diff_stat",
        lambda base_ref: "src/unrelated.py | 1 +\n1 file changed, 1 insertion(+)",
    )
    rc = psg.main([
        "--branch", "recover/lost-work",
        "--title", "Recovery PR",
        "--body", "Recovery PR for stranded worktree, restores `src/named.py`.",
    ])
    assert rc == 1


def test_main_exits_zero_for_matching_recovery_pr(monkeypatch):
    monkeypatch.setattr(
        psg, "_git_diff_stat",
        lambda base_ref: "src/named.py | 1 +\n1 file changed, 1 insertion(+)",
    )
    rc = psg.main([
        "--branch", "recover/lost-work",
        "--title", "Recovery PR",
        "--body", "Recovery PR for stranded worktree, restores `src/named.py`.",
    ])
    assert rc == 0
