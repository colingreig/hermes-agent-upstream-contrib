#!/usr/bin/env python3
"""
clickup_status_guard.py — Python sibling of status_guard.mjs.

The agent's primary ClickUp client is the Node CLI (clickup.mjs), now guarded by
status_guard.mjs. This module gives the deterministic Hermes *cron* scripts that
write ClickUp status directly (in ~/.hermes/scripts/) the same G1–G3 chokepoint,
so the Python path can't reintroduce the failure class either.

DORMANT until deployed. To activate (see the ClickUp activation task):
  1. Copy this file to ~/.hermes/scripts/clickup_status_guard.py
  2. In clickup_groomer.py._put_status and clickup_triage_ops.py.cmd_status, call
     assert_status_allowed(status, comments=...) before the PUT.
  3. EXEMPT the validator's own writer (hermes_validate_ops.py.cmd_set_status):
     it legitimately owns the `complete` transition (the QA gate).
  4. Neuter clickup_groomer.py's dead `_put_status` (it is defined but never
     called; the "we set tasks to complete on auto-close" comment is stale).

Pure / network-free: callers pass in the task's comments; this module decides.
Mirrors the policy and message text of status_guard.mjs — keep the two in sync.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable

COLIN_USER_ID = "168143285"  # Hermes posts AS this id — not a discriminator (see G3).

COMPLETE_WORDS = {"complete", "completed", "closed", "done", "archived", "archive"}
REVIEW_WORDS = {
    "ready for review", "in review", "review", "for review",
    "qa", "qa review", "needs review", "ready for qa", "in qa",
}
NEGATIVE_VERDICTS = re.compile(r"^(fail|failed|block|blocked|reject|rejected)$", re.I)
VALIDATE_MARKER = re.compile(r"ignite-validate:\s*([a-z]+)", re.I)

BANNED_AUTH_PATTERNS = [
    re.compile(r"\bper\s+colin\b", re.I),
    re.compile(r"\bcolin\s+(approved|accepted|signed?[\s-]*off|confirmed|authoriz\w*|ok'?d|okayed|gave\s+the\s+(go|ok|green))", re.I),
    re.compile(r"\b(approved|accepted|authoriz\w*|signed?[\s-]*off|confirmed|cleared|greenlit)\s+by\s+colin\b", re.I),
    re.compile(r"\bcolin'?s\s+(approval|sign[\s-]*off|go[\s-]*ahead|ok\b|blessing|authoriz\w*|instruction)", re.I),
    re.compile(r"\bper\s+(the\s+)?(operator|owner)(?:'?s)?\s+(approval|sign[\s-]*off|instruction|go[\s-]*ahead)", re.I),
    re.compile(r"\bhuman\s+(approval|sign[\s-]*off)\s+(received|obtained|granted|confirmed)", re.I),
    re.compile(r"\bas\s+(approved|authoriz\w*|instructed)\s+by\s+colin\b", re.I),
]


class GuardError(Exception):
    """Raised when a write violates G1/G2/G3. Callers should refuse the write."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def is_complete_class(status: Any) -> bool:
    return _norm(status) in COMPLETE_WORDS


def is_review_class(status: Any) -> bool:
    return _norm(status) in REVIEW_WORDS


def is_advance_status(status: Any) -> bool:
    return is_review_class(status) or is_complete_class(status)


def comment_text(c: dict) -> str:
    """Tolerate ClickUp's two comment shapes (flat string / segment array)."""
    if not c:
        return ""
    t = c.get("comment_text")
    if isinstance(t, str) and t:
        return t
    seg = c.get("comment")
    if isinstance(seg, list):
        return "".join(s.get("text", "") for s in seg if isinstance(s, dict))
    if isinstance(seg, str):
        return seg
    return ""


def latest_validate_verdict(comments: Iterable[dict]) -> dict | None:
    """Return {'verdict','date','comment_id','text'} for the newest marker, or None."""
    marked = []
    for c in comments or []:
        m = VALIDATE_MARKER.search(comment_text(c))
        if not m:
            continue
        try:
            date = int(c.get("date") or c.get("date_created") or 0)
        except (TypeError, ValueError):
            date = 0
        marked.append({"verdict": m.group(1).lower(), "date": date,
                       "comment_id": c.get("id"), "text": comment_text(c)})
    if not marked:
        return None
    marked.sort(key=lambda x: x["date"], reverse=True)
    return marked[0]


def find_banned_auth_claims(text: str) -> list[str]:
    if not text:
        return []
    hits = []
    for pat in BANNED_AUTH_PATTERNS:
        m = pat.search(text)
        if m:
            hits.append(m.group(0).strip())
    return hits


def _flag(name: str) -> bool:
    return os.environ.get(name, "") in ("1", "true", "yes", "on", "True", "YES")


def assert_status_allowed(status: str, comments: Iterable[dict] | None = None) -> None:
    """G1 + G2. Raise GuardError if the transition is forbidden.

    Pass `comments` (the task's comment list) so G2 can read the validator verdict;
    G2 is only checked when advancing to a review/complete status.
    """
    if is_complete_class(status) and not _flag("CLICKUP_ALLOW_COMPLETE"):
        raise GuardError(
            "G1",
            f'Hermes must NEVER set ClickUp status to a complete-class status ("{status}"). '
            'Terminal Hermes status is "in review"; "complete" is owned by ignite-validate / '
            "Colin (2026-06-22 policy). [override: CLICKUP_ALLOW_COMPLETE=1]",
        )
    if is_advance_status(status) and not _flag("CLICKUP_ALLOW_FAIL_OVERRIDE"):
        v = latest_validate_verdict(comments or [])
        if v and NEGATIVE_VERDICTS.match(v["verdict"]):
            raise GuardError(
                "G2",
                f'latest ignite-validate marker is "{v["verdict"].upper()}" '
                f'(comment {v["comment_id"]}); refusing to advance to "{status}" over a '
                "standing FAIL/BLOCK. Re-pick from to do and let the validator re-run. "
                "[override: CLICKUP_ALLOW_FAIL_OVERRIDE=1]",
            )


def assert_comment_allowed(text: str) -> None:
    """G3. Raise GuardError if the comment fabricates human authorization."""
    if _flag("CLICKUP_ALLOW_AUTH_CLAIM"):
        return
    hits = find_banned_auth_claims(text)
    if hits:
        raise GuardError(
            "G3",
            "comment asserts human authorization (" + ", ".join(f'"{h}"' for h in hits) +
            "). Hermes posts via Colin's PAT and cannot prove a sign-off — never fabricate "
            '"Closed per Colin". [override: CLICKUP_ALLOW_AUTH_CLAIM=1]',
        )


if __name__ == "__main__":
    # Tiny self-check mirroring the .mjs test cases.
    fails = 0

    def check(cond, label):
        global fails
        if not cond:
            fails += 1
            print(f"  ✗ {label}")
        else:
            print(f"  ✓ {label}")

    try:
        assert_status_allowed("complete")
        check(False, "G1 blocks complete")
    except GuardError as e:
        check(e.code == "G1", "G1 blocks complete")

    try:
        assert_status_allowed("in review", [
            {"id": "f", "date": "1", "comment_text": "ignite-validate: FAIL"},
            {"id": "s", "date": "2", "comment_text": "🤖 ALREADY-SHIPPED closeout"},
        ])
        check(False, "G2 blocks advance over FAIL")
    except GuardError as e:
        check(e.code == "G2", "G2 blocks advance over FAIL")

    check(assert_status_allowed("in review", [
        {"id": "f", "date": "1", "comment_text": "ignite-validate: FAIL"},
        {"id": "p", "date": "2", "comment_text": "ignite-validate: PASS"},
    ]) is None, "G2 clears on newer PASS")

    check(assert_status_allowed("in progress", [
        {"id": "f", "date": "1", "comment_text": "ignite-validate: FAIL"}]) is None,
        "G2 does not gate non-advance (in progress)")

    try:
        assert_comment_allowed("Closed per Colin.")
        check(False, "G3 blocks 'per Colin'")
    except GuardError as e:
        check(e.code == "G3", "G3 blocks 'per Colin'")

    check(assert_comment_allowed("Shipped PR; assigned to Colin for review.") is None,
          "G3 allows benign Colin mention")

    print("ALL PASS" if not fails else f"{fails} FAILED")
    raise SystemExit(1 if fails else 0)
