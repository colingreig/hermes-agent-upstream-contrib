"""Pre-publish content safety gates for the Kanban dispatch chokepoint.

Grew out of an incident where an AI-dispatched content task shipped a blog
post to production containing literal unfilled placeholder text
("Placeholder model" / "TBC" rows) and a leaked internal note ("will be
refreshed from the task 1 dataset before publish"). Nothing scanned the
generated content for placeholder markers before merge, and a sibling task
providing the real dataset was still open when the task shipped — no
dependency gate caught it.

This module provides the checks wired into
:func:`hermes_cli.kanban_db.complete_task` and
:func:`plugins.kanban.dashboard.plugin_api._set_status_direct` (the review
transition):

* :func:`scan_text_for_placeholders` / :func:`scan_paths_for_placeholders` /
  :func:`diff_changed_files` — Check 1, pre-publish placeholder/stub content
  scan.
* :func:`open_parent_summaries` — Check 2, open-dependency gate.
* :func:`requires_human_signoff` / :func:`has_non_bot_comment` — Check 4,
  human sign-off enforcement (see :func:`hermes_cli.kanban_db.complete_task`
  for how these combine into the actual gate).

It also provides :func:`flag_recovery_pr_mismatch` (Check 3), a pure
heuristic used by :mod:`hermes_cli.pr_safety_gate` (the automated CI gate)
and human/review-agent workflows (see
``skills/github/github-code-review/SKILL.md``) to catch stranded-worktree
recovery PRs whose description doesn't match their actual diff.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Iterable, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check 1: placeholder / stub content scan
# ---------------------------------------------------------------------------

# Kept tight and anchored on the specific phrases from the incident plus a
# couple of generic high-confidence markers — false positives on legitimate
# prose are worse than missing an edge case, so we do NOT match single
# common words like "placeholder" or "todo" in isolation (a bare "TODO:" or
# "placeholder" is extremely common in ordinary code comments/docstrings —
# see the false-positive audit that added this scoping). Marker phrases are
# multi-word or otherwise distinctive to the unfilled-content-shipped-live
# failure mode this module exists to catch.
PLACEHOLDER_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("literal TBC marker", re.compile(r"\bTBC\b", re.IGNORECASE)),
    (
        "placeholder stand-in value",
        re.compile(
            r"\bplaceholder\s+(?:model|text|value|data|row|copy|content|number|price|name)\b"
            r"|\[placeholder\]|\"placeholder\"|'placeholder'",
            re.IGNORECASE,
        ),
    ),
    ("lorem ipsum filler text", re.compile(r"\blorem ipsum\b", re.IGNORECASE)),
    ("'will be refreshed' internal note", re.compile(r"\bwill be refreshed\b", re.IGNORECASE)),
    (
        "'replace after/once task' internal note",
        re.compile(r"\breplace (?:after|once) task\b", re.IGNORECASE),
    ),
    ("'publish-ready placeholder' internal note", re.compile(r"\bpublish-ready placeholder\b", re.IGNORECASE)),
    ("'not the final numbers' internal note", re.compile(r"\bnot the final numbers\b", re.IGNORECASE)),
    (
        "'TODO: fill in' unfilled-content note",
        re.compile(r"\bTODO:\s*fill in\b", re.IGNORECASE),
    ),
    ("unrendered template token {{...}}", re.compile(r"\{\{[^}]*\}\}")),
    ("unrendered template token <PLACEHOLDER>", re.compile(r"<PLACEHOLDER>", re.IGNORECASE)),
]

# The scan applies only to content-typical file extensions — Hermes workers
# routinely touch source files (.py/.ts/.js/...) as part of ordinary
# engineering tasks, and those files legitimately contain words like
# "placeholder" or "TODO" in comments/docstrings at a high base rate (see
# the false-positive audit). The incident this module guards against was a
# rendered content artifact (a blog post), not source code, so scope the
# scan to the file types that actually carry publish-facing prose.
CONTENT_FILE_EXTENSIONS = frozenset({".md", ".mdx", ".html", ".htm", ".txt"})


def scan_text_for_placeholders(text: str) -> list[str]:
    """Return the list of marker descriptions that matched ``text``.

    Empty list means the text is clean. Anchored on the tight,
    high-confidence marker set in :data:`PLACEHOLDER_MARKERS`.
    """
    if not text:
        return []
    hits: list[str] = []
    for description, pattern in PLACEHOLDER_MARKERS:
        if pattern.search(text):
            hits.append(description)
    return hits


def scan_paths_for_placeholders(paths: Iterable[Path]) -> dict[str, list[str]]:
    """Scan each file in ``paths`` for placeholder markers.

    Only files with an extension in :data:`CONTENT_FILE_EXTENSIONS` are
    scanned — ordinary source files are skipped so routine engineering
    tasks (which legitimately contain words like "placeholder" or "TODO"
    in comments/docstrings) don't trip this gate. Binary or unreadable
    content files are skipped silently. Returns ``{path_str: [markers]}``
    for files with hits only — clean files are omitted entirely.
    """
    results: dict[str, list[str]] = {}
    for path in paths:
        if path.suffix.lower() not in CONTENT_FILE_EXTENSIONS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary file or unreadable (permissions, race with a
            # deletion, etc.) — not our concern here, skip silently.
            continue
        hits = scan_text_for_placeholders(text)
        if hits:
            results[str(path)] = hits
    return results


def diff_changed_files(workspace_path: str, base_ref: str = "origin/main") -> list[Path]:
    """Return absolute paths of files changed in ``workspace_path``'s git repo.

    Tries a merge-base diff against ``base_ref`` first (``git diff
    --name-only base_ref...HEAD``); if that fails (no remote configured,
    shallow clone, detached scratch repo, etc.) falls back to a working
    tree vs last commit diff (``git diff --name-only HEAD``). Non-fatal on
    any subprocess error — logs and returns ``[]``. Only returns paths that
    currently exist on disk (deleted files are skipped).
    """
    root = Path(workspace_path)

    def _run(args: list[str]) -> Optional[list[str]]:
        try:
            proc = subprocess.run(
                ["git", "-C", workspace_path, *args],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log.warning("diff_changed_files: git %s failed: %s", args, exc)
            return None
        if proc.returncode != 0:
            return None
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    lines = _run(["diff", "--name-only", f"{base_ref}...HEAD"])
    if lines is None:
        lines = _run(["diff", "--name-only", "HEAD"])
    if lines is None:
        return []

    out: list[Path] = []
    for rel in lines:
        p = (root / rel).resolve()
        if p.exists():
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Check 2: open-dependency gate
# ---------------------------------------------------------------------------

_OPEN_STATUSES_EXCLUDED = {"done", "archived"}


def open_parent_summaries(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """Return summaries of every parent of ``task_id`` that is not done/archived.

    Reuses the ``task_links`` schema (parent_id/child_id). Mirrors the
    query shape used by ``plugin_api._parents_blocking_ready`` so behavior
    stays consistent across the dashboard's ``ready``-transition gate and
    the completion-time dependency gate here.
    """
    rows = conn.execute(
        "SELECT t.id AS id, t.title AS title, t.status AS status "
        "FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ? AND t.status NOT IN ('done', 'archived') "
        "ORDER BY t.id",
        (task_id,),
    ).fetchall()
    return [
        {"id": r["id"], "title": r["title"], "status": r["status"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Check 3: recovery/stranded-worktree PR description-vs-diff integrity
# ---------------------------------------------------------------------------

_RECOVERY_SIGNAL_RE = re.compile(
    r"\b(recovery|stranded worktree|stranded-worktree|recovered branch)\b",
    re.IGNORECASE,
)

# File-path-looking tokens: backtick-quoted, or bare tokens containing a
# '/' and a file extension.
_BACKTICK_PATH_RE = re.compile(r"`([^`\s]+\.[A-Za-z0-9]+)`")
_BARE_PATH_RE = re.compile(r"\b([\w\-./]+/[\w\-.]+\.[A-Za-z0-9]+)\b")


def _extract_named_paths(description: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for pattern in (_BACKTICK_PATH_RE, _BARE_PATH_RE):
        for m in pattern.finditer(description):
            token = m.group(1).strip()
            if token and token not in seen:
                seen.add(token)
                names.append(token)
    return names


def _diff_stat_files(diff_stat_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_stat_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # `git diff --stat` / `gh pr diff --stat` lines look like:
        #   path/to/file.py | 12 +++++++---
        # and the final summary line ("N files changed...") has no '|'.
        if "|" not in line:
            continue
        files.append(line.split("|", 1)[0].strip())
    return files


def flag_recovery_pr_mismatch(pr_description: str, diff_stat_text: str) -> Optional[str]:
    """Flag a mismatch between a recovery PR's claimed changes and its diff.

    Pure function, no I/O. If ``pr_description`` (case-insensitive)
    contains recovery/stranded-worktree signal phrases, extract any
    file-path-looking tokens mentioned in the description and compare
    (loose substring match) against the files listed in ``diff_stat_text``
    (the output of ``git diff --stat`` or ``gh pr diff --stat``, one file
    per line).

    Returns a human-readable warning string when:

    * the description names specific files that don't appear anywhere in
      the diff stat, OR
    * the description claims a narrow/limited change but the diff touches
      3x+ as many files as named.

    Returns ``None`` when the description carries no recovery/stranded
    language (not applicable) or when everything lines up.
    """
    if not pr_description or not _RECOVERY_SIGNAL_RE.search(pr_description):
        return None

    named_paths = _extract_named_paths(pr_description)
    diff_files = _diff_stat_files(diff_stat_text or "")

    missing = [
        p for p in named_paths
        if not any(p in f or f in p for f in diff_files)
    ]
    if missing:
        return (
            "Recovery/stranded-worktree PR description names file(s) not "
            f"found in the diff: {', '.join(missing)}. The diff touches: "
            f"{', '.join(diff_files) if diff_files else '(no files)'}. "
            "Hold — do not merge without explicit human confirmation."
        )

    if named_paths and diff_files and len(diff_files) >= 3 * len(named_paths):
        return (
            f"Recovery/stranded-worktree PR description names only "
            f"{len(named_paths)} file(s) ({', '.join(named_paths)}) but the "
            f"diff touches {len(diff_files)} files. The description may "
            "understate the scope of this recovery. Hold — do not merge "
            "without explicit human confirmation."
        )

    return None


# ---------------------------------------------------------------------------
# Check 4: human sign-off enforcement
# ---------------------------------------------------------------------------

# Grew out of the same incident: the task's acceptance criteria explicitly
# required Colin's sign-off, but the task was marked complete off the back
# of AI-authored comments alone — no real human ever looked at it. Anchored
# on the specific phrases that show up in ClickUp/kanban acceptance-criteria
# text when a task genuinely needs a human in the loop; kept tight for the
# same false-positive-avoidance reason as PLACEHOLDER_MARKERS above.
HUMAN_SIGNOFF_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"\bcolin sign[- ]?off\b", re.IGNORECASE),
    re.compile(r"\brequires colin\b", re.IGNORECASE),
    re.compile(r"\bhuman sign[- ]?off\b", re.IGNORECASE),
    re.compile(r"\bexplicit sign[- ]?off\b", re.IGNORECASE),
    re.compile(r"\bneeds colin'?s? approval\b", re.IGNORECASE),
    re.compile(r"\bcolin'?s? (?:explicit )?approval\b", re.IGNORECASE),
]

# Bot-authored comments in this repo's convention are prefixed "ignite-"
# (e.g. "ignite- claiming:", "ignite- done:") — see the ClickUp comment
# threads referenced in the task that added this gate. Matched
# case-insensitively against the comment body after stripping leading
# whitespace.
_BOT_COMMENT_PREFIX = "ignite-"


def requires_human_signoff(text: str) -> bool:
    """Return True if ``text`` (a task's body/acceptance-criteria text)
    contains an explicit human-sign-off requirement.

    Empty list means the text is clean. See :data:`HUMAN_SIGNOFF_MARKERS`
    for the exact phrases matched.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in HUMAN_SIGNOFF_MARKERS)


def is_bot_comment(comment_body: str) -> bool:
    """Return True if ``comment_body`` is bot-authored by this repo's
    convention (starts with the ``ignite-`` marker prefix, e.g.
    ``"ignite- claiming: ..."`` / ``"ignite- done: ..."``).
    """
    if not comment_body:
        return False
    return comment_body.strip().lower().startswith(_BOT_COMMENT_PREFIX)


def has_non_bot_comment(comment_bodies: Iterable[str]) -> bool:
    """Return True if at least one comment in ``comment_bodies`` is NOT
    bot-authored (see :func:`is_bot_comment`).

    Used by the human-sign-off gate to distinguish a real human comment
    from an agent narrating its own progress — a task requiring explicit
    sign-off must not be satisfiable by bot comments alone.
    """
    return any(not is_bot_comment(body) for body in comment_bodies)
