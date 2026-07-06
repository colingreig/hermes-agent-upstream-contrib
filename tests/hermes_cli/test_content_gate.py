"""Tests for hermes_cli.content_gate — the content safety gates."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import content_gate as cg
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "kanban@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Kanban Test"], check=True, capture_output=True, text=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# scan_text_for_placeholders
# ---------------------------------------------------------------------------

def test_scan_text_for_placeholders_detects_incident_markers():
    text = "Author: Placeholder model\nStatus: TBC\nNote: will be refreshed from the task 1 dataset before publish."
    hits = cg.scan_text_for_placeholders(text)
    assert hits
    assert any("placeholder" in h.lower() for h in hits)
    assert any("tbc" in h.lower() for h in hits)
    assert any("refreshed" in h.lower() for h in hits)


def test_scan_text_for_placeholders_detects_lorem_ipsum_and_template_tokens():
    assert cg.scan_text_for_placeholders("Lorem ipsum dolor sit amet")
    assert cg.scan_text_for_placeholders("Hello {{customer_name}}, welcome.")
    assert cg.scan_text_for_placeholders("Ship to <PLACEHOLDER> address")
    assert cg.scan_text_for_placeholders("TODO: fill in the real numbers")


def test_scan_text_for_placeholders_clean_text_returns_empty():
    text = (
        "This is a normal blog post about electric vehicle maintenance. "
        "Regular oil changes are not required, but tire rotations matter."
    )
    assert cg.scan_text_for_placeholders(text) == []


def test_scan_text_for_placeholders_does_not_flag_incidental_word_forms():
    # "please" contains "plea" but not the word "placeholder"; guard
    # against overly broad substring matching that would cause false
    # positives on legitimate prose.
    assert cg.scan_text_for_placeholders("Please review the attached document.") == []


def test_scan_text_for_placeholders_empty_string():
    assert cg.scan_text_for_placeholders("") == []


# ---------------------------------------------------------------------------
# scan_paths_for_placeholders
# ---------------------------------------------------------------------------

def test_scan_paths_for_placeholders_reports_only_files_with_hits(tmp_path):
    clean = tmp_path / "clean.md"
    clean.write_text("A perfectly normal article about widgets.\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text("Model: Placeholder model\nTBC\n", encoding="utf-8")

    results = cg.scan_paths_for_placeholders([clean, dirty])

    assert str(clean) not in results
    assert str(dirty) in results
    assert results[str(dirty)]


def test_scan_paths_for_placeholders_skips_binary_and_missing_files(tmp_path):
    binary = tmp_path / "image.bin"
    binary.write_bytes(b"\x00\x01\xff\xfe\x00TBC\x00")
    missing = tmp_path / "does-not-exist.md"

    # Should not raise despite unreadable/binary/missing files.
    results = cg.scan_paths_for_placeholders([binary, missing])
    assert results == {}


# ---------------------------------------------------------------------------
# diff_changed_files
# ---------------------------------------------------------------------------

def test_diff_changed_files_falls_back_to_working_tree_diff_when_no_remote(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    # No origin/main configured — the merge-base diff must fail and the
    # function must fall back to `git diff --name-only HEAD` without
    # raising.
    (repo / "new_file.md").write_text("some new content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "new_file.md"], check=True, capture_output=True, text=True)

    changed = cg.diff_changed_files(str(repo))

    assert (repo / "new_file.md").resolve() in changed


def test_diff_changed_files_skips_deleted_files(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "README.md").unlink()

    changed = cg.diff_changed_files(str(repo))

    assert all(p.exists() for p in changed)
    assert (repo / "README.md").resolve() not in changed


def test_diff_changed_files_non_fatal_on_non_git_directory(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    # Should not raise — just return an empty list.
    assert cg.diff_changed_files(str(not_a_repo)) == []


# ---------------------------------------------------------------------------
# open_parent_summaries
# ---------------------------------------------------------------------------

def test_open_parent_summaries_returns_only_non_terminal_parents(kanban_home):
    with kb.connect() as conn:
        parent_open = kb.create_task(conn, title="dataset task")
        parent_done = kb.create_task(conn, title="already done parent")
        child = kb.create_task(
            conn, title="blog post", parents=[parent_open, parent_done],
        )
        kb.complete_task(conn, parent_done, result="ok")

        summaries = cg.open_parent_summaries(conn, child)

    assert len(summaries) == 1
    assert summaries[0]["id"] == parent_open
    assert summaries[0]["title"] == "dataset task"
    assert summaries[0]["status"] not in {"done", "archived"}


def test_open_parent_summaries_empty_when_no_parents_or_all_done(kanban_home):
    with kb.connect() as conn:
        parent_done = kb.create_task(conn, title="parent")
        child = kb.create_task(conn, title="child", parents=[parent_done])
        kb.complete_task(conn, parent_done, result="ok")

        assert cg.open_parent_summaries(conn, child) == []

        lonely = kb.create_task(conn, title="no parents")
        assert cg.open_parent_summaries(conn, lonely) == []


# ---------------------------------------------------------------------------
# flag_recovery_pr_mismatch
# ---------------------------------------------------------------------------

def test_flag_recovery_pr_mismatch_not_applicable_without_recovery_language():
    description = "This PR adds a new feature to the checkout flow."
    diff_stat = "src/checkout.py | 20 ++++++++++----\n1 file changed, 16 insertions(+), 4 deletions(-)"
    assert cg.flag_recovery_pr_mismatch(description, diff_stat) is None


def test_flag_recovery_pr_mismatch_flags_named_file_missing_from_diff():
    description = (
        "Recovery PR for a stranded worktree. This restores changes to "
        "`src/orders/checkout.py` that were lost when the worktree was abandoned."
    )
    diff_stat = "src/unrelated/other.py | 5 +++--\n1 file changed, 3 insertions(+), 2 deletions(-)"
    warning = cg.flag_recovery_pr_mismatch(description, diff_stat)
    assert warning is not None
    assert "checkout.py" in warning


def test_flag_recovery_pr_mismatch_flags_scope_understatement():
    description = (
        "Stranded worktree recovery — just recovers `src/a.py`."
    )
    diff_stat = (
        "src/a.py | 5 +++--\n"
        "src/b.py | 3 +--\n"
        "src/c.py | 2 +-\n"
        "src/d.py | 1 +\n"
        "4 files changed, 8 insertions(+), 3 deletions(-)"
    )
    warning = cg.flag_recovery_pr_mismatch(description, diff_stat)
    assert warning is not None


def test_flag_recovery_pr_mismatch_none_when_everything_lines_up():
    description = (
        "Recovery PR for stranded worktree — restores `src/a.py` and `src/b.py`."
    )
    diff_stat = (
        "src/a.py | 5 +++--\n"
        "src/b.py | 3 +--\n"
        "2 files changed, 6 insertions(+), 2 deletions(-)"
    )
    assert cg.flag_recovery_pr_mismatch(description, diff_stat) is None


# ---------------------------------------------------------------------------
# requires_human_signoff / is_bot_comment / has_non_bot_comment
# ---------------------------------------------------------------------------

def test_requires_human_signoff_detects_marker_phrases():
    assert cg.requires_human_signoff("This task requires Colin sign-off before closing.")
    assert cg.requires_human_signoff("Needs Colin's approval before shipping.")
    assert cg.requires_human_signoff("Explicit sign-off required from a human.")
    assert cg.requires_human_signoff("This requires Colin to review.")


def test_requires_human_signoff_false_for_ordinary_ac_text():
    text = "Acceptance criteria: all tests pass and the endpoint returns 200."
    assert cg.requires_human_signoff(text) is False


def test_requires_human_signoff_empty_string():
    assert cg.requires_human_signoff("") is False


def test_is_bot_comment_detects_ignite_prefix():
    assert cg.is_bot_comment("ignite- claiming: starting work on this now")
    assert cg.is_bot_comment("ignite- done: shipped and tested")
    assert cg.is_bot_comment("  ignite- claiming: leading whitespace")
    assert cg.is_bot_comment("IGNITE- DONE: case insensitive")


def test_is_bot_comment_false_for_human_comment():
    assert cg.is_bot_comment("Looks good, approved.") is False
    assert cg.is_bot_comment("") is False


def test_has_non_bot_comment_true_when_at_least_one_human_comment():
    comments = ["ignite- claiming: starting", "Looks good, approved.", "ignite- done: shipped"]
    assert cg.has_non_bot_comment(comments) is True


def test_has_non_bot_comment_false_when_all_bot():
    comments = ["ignite- claiming: starting", "ignite- done: shipped"]
    assert cg.has_non_bot_comment(comments) is False


def test_has_non_bot_comment_false_for_empty_list():
    assert cg.has_non_bot_comment([]) is False
