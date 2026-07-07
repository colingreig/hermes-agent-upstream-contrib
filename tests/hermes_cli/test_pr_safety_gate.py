"""Tests for hermes_cli.pr_safety_gate — the automated recovery-PR CI gate.

Covers the pure ``check_recovery_pr`` / ``is_recovery_pr`` logic that the
``pr-safety-gate.yml`` CI workflow invokes via ``python -m
hermes_cli.pr_safety_gate``. These are the same three scenarios called out
in the task: (a) a normal PR is not flagged, (b) a recovery PR whose
description matches its diff is not flagged, (c) a recovery PR whose
description doesn't match its diff IS flagged.
"""

from __future__ import annotations

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
