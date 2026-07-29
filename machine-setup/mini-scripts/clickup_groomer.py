#!/usr/bin/env python3
"""clickup_groomer.py — retired ClickUp Groomer cron script.

This file is kept for historical reference only. The weekly clickup-groomer job
was deleted from jobs.json on 2026-06-15 after the bulk auto-tagging incident,
so the docstring must reflect reality: the cron is retired and must not be
re-registered here.

Delivery used to be stdout -> cron job -> slack:UN4CQ1EGG (Colin).

Env:
  CLICKUP_API_TOKEN       required (Doppler-injected)
  CLICKUP_GROOMER_DRY_RUN "1" (default) = dry-run; "0" = live
  CLICKUP_GROOMER_STALE_DAYS  default "14"
  CLICKUP_GROOMER_LISTS   comma-sep list IDs (default: the three starter lists)

Reads from ClickUp via curl; writes via curl. subprocess.run + curl pattern
is used throughout to avoid the write_file/urllib env-var truncation pitfall
documented in the clickup-queue-poller skill.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ----- Config ----------------------------------------------------------------

DEFAULT_LISTS = [
    ("901714171122", "SEO agent"),
    ("901714176776", "jdm.com Scraper v4"),
    ("901714465284", "AI Dev Assistant"),
]

TEAM_ID = "9017245888"
GROOMER_TAG = "groomer-comment"  # not a real tag, just a marker prefix in comments
STALE_DAYS = int(os.environ.get("CLICKUP_GROOMER_STALE_DAYS", "14"))

DRY_RUN = os.environ.get("CLICKUP_GROOMER_DRY_RUN", "1") != "0"

_env_lists = os.environ.get("CLICKUP_GROOMER_LISTS", "").strip()
if _env_lists:
    LISTS = [(lid.strip(), f"list-{lid.strip()}") for lid in _env_lists.split(",") if lid.strip()]
else:
    LISTS = DEFAULT_LISTS

# Terminal status NAMES, used only to RECOGNIZE an already-closed task when
# classifying drift (pass_a). The groomer never WRITES a terminal status —
# Hermes never self-completes (2026-06-22 completion-guardrails policy); it flags
# drift only and leaves the close to the validator / Colin.
TERMINAL_FALLBACKS = ("complete", "closed", "done")

# ----- HTTP helpers (subprocess + curl) --------------------------------------

def _token() -> str:
    value = None
    try:
        from agent import lazy_secret_resolver
        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None
    if not value:
        value = os.environ.get("CLICKUP_API_TOKEN", "")
    t = (value or "").strip()
    if not t:
        print("ERROR: CLICKUP_API_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    return t


def _curl(method: str, path: str, body: dict | None = None) -> dict:
    """Run a single ClickUp API call via curl. Returns parsed JSON."""
    url = f"https://api.clickup.com/api/v2{path}"
    args = [
        "curl", "-s", "-S", "-X", method,
        "-H", f"Authorization: {_token()}",
        "-H", "Content-Type: application/json",
        url,
    ]
    if body is not None:
        args.extend(["-d", json.dumps(body)])
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed: {proc.stderr.strip()[:200]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"non-JSON response: {proc.stdout[:200]}") from e


def _get_tasks(list_id: str, include_closed: bool = True) -> list[dict]:
    """Fetch all tasks in a list (handles pagination)."""
    tasks: list[dict] = []
    page = 0
    while True:
        data = _curl("GET", f"/list/{list_id}/task?page={page}&include_closed={'true' if include_closed else 'false'}&subtasks=false")
        tasks.extend(data.get("tasks", []))
        if data.get("last_page", True):
            break
        page += 1
        if page > 50:  # safety
            break
    return tasks


def _get_task(task_id: str) -> dict:
    return _curl("GET", f"/task/{task_id}")


def _get_comments(task_id: str) -> list[dict]:
    data = _curl("GET", f"/task/{task_id}/comment")
    return data.get("comments", []) or []


def _put_status(task_id: str, status: str) -> None:
    # NEUTERED 2026-06-22 (Hermes completion guardrails). This was dead code (defined,
    # never called) but its presence + the "we set tasks to complete on auto-close"
    # comment above made it a latent G1 bypass. The groomer only FLAGS drift; it must
    # never write a terminal status. If auto-close is ever genuinely wanted, route it
    # through clickup_status_guard.assert_status_allowed (which blocks complete) and the
    # validator handshake — do not restore a raw status PUT here.
    raise RuntimeError(
        "clickup_groomer._put_status is disabled: Hermes never writes a terminal status "
        "(2026-06-22 completion-guardrails policy). The groomer flags drift only."
    )


def _post_comment(task_id: str, text: str) -> dict:
    return _curl("POST", f"/task/{task_id}/comment", {"comment_text": text})


# ----- Helpers ---------------------------------------------------------------

def _ms_to_dt(ms) -> datetime | None:
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _age_days(ts) -> float | None:
    dt = _ms_to_dt(ts)
    if not dt:
        return None
    return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 86400.0


def _extract_pr_urls(text: str) -> list[str]:
    if not text:
        return []
    # github.com/owner/repo/pull/N
    return list(set(re.findall(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text)))


def _extract_branch_hint(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"agent/(\S+?)(?=[\s,)]|$)", text)
    return m.group(0) if m else None


def _has_groomer_comment(comments: list[dict], finding: str) -> bool:
    """Has the groomer already posted this finding on this task?"""
    needle = f"🧹 groomer:{finding}"
    for c in comments:
        if needle in (c.get("comment_text") or ""):
            return True
    return False


def _is_terminal_status(status: str) -> bool:
    return (status or "").strip().lower() in TERMINAL_FALLBACKS


def _looks_unclaimed(status: str) -> bool:
    """A task is unclaimed if it's still in an open non-progress state."""
    s = (status or "").strip().lower()
    return s in {"to do", "backlog", "open", "deferred"}


# ----- Pass A: Reconcile -----------------------------------------------------

def pass_a(task: dict, comments: list[dict], list_terminal_status: str) -> dict:
    """Classify drift. Returns a finding dict."""
    name = task.get("name") or ""
    desc = task.get("description") or ""
    status = (task.get("status") or {}).get("status") or ""
    comment_texts = "\n".join(c.get("comment_text") or "" for c in comments)
    haystack = "\n".join([name, desc, comment_texts])

    pr_urls = _extract_pr_urls(haystack)
    last_comment_age = None
    if comments:
        last_comment_age = min((_age_days(c.get("date")) or 0) for c in comments)

    finding: dict[str, Any] = {
        "pass": "A",
        "task_id": task.get("id"),
        "name": name,
        "list": (task.get("list") or {}).get("name", "?"),
        "status": status,
        "verdict": "ok",
        "detail": "",
        "action": None,  # set if we'd take an action
    }

    # Stale: in progress, no activity
    if status.lower() == "in progress":
        if last_comment_age is not None and last_comment_age > STALE_DAYS:
            finding["verdict"] = "stale"
            finding["detail"] = (
                f"In progress with no comment activity for {last_comment_age:.0f} days "
                f"(threshold {STALE_DAYS}d)."
            )
            finding["action"] = "comment-flag"
            return finding

    # Has a PR but unmerged and task is in some non-terminal state
    if pr_urls and not _is_terminal_status(status):
        # We can't actually verify PR merge state from the ClickUp API alone
        # (would need gh), so we only flag here when the task is already
        # marked closed/done. That's the inverse drift case.
        pass

    # Marked done but evidence of live work
    if _is_terminal_status(status):
        if pr_urls:
            finding["verdict"] = "drift-done-with-pr"
            finding["detail"] = (
                f"Status is '{status}' but task mentions PR(s): {', '.join(pr_urls)}. "
                f"Verify PR is merged and deployed before treating as shipped."
            )
            finding["action"] = "comment-flag"
            return finding

    return finding


# ----- Pass B: Hygiene -------------------------------------------------------

def pass_b(task: dict) -> dict:
    """Surface hygiene proposals. No auto-writes in v1 except unambiguous tag add."""
    name = (task.get("name") or "").strip()
    desc = (task.get("description") or "").strip()
    tags = [t.get("name") for t in (task.get("tags") or [])]
    list_name = (task.get("list") or {}).get("name", "")

    finding: dict[str, Any] = {
        "pass": "B",
        "task_id": task.get("id"),
        "name": name,
        "list": list_name,
        "verdict": "ok",
        "proposals": [],
    }

    # Vague title: no verb-y / no description / very short
    if len(name) < 8 or name.lower() in {"fix", "bug", "task", "todo", "tbd", "wip"}:
        finding["verdict"] = "vague-title"
        finding["proposals"].append(f"Rename — current title is '{name}' (too vague or single-word).")

    # No description
    if not desc:
        finding["proposals"].append("Add a 1-2 line description (acceptance criteria).")
        if finding["verdict"] == "ok":
            finding["verdict"] = "missing-description"

    # Missing tag on jdm list (unambiguous auto-tag candidate)
    if "jdm" in list_name.lower() and "scraper" in list_name.lower() and "jdmbuysell" not in tags:
        finding["proposals"].append("Add tag 'jdmbuysell' (matches list context, no false positive).")
        finding["verdict"] = "missing-tag"

    return finding


# ----- Pass C: Brain ---------------------------------------------------------

def pass_c(task: dict, comments: list[dict]) -> dict:
    """Summarize the actual outcome into a brain note. Read-only w.r.t. ClickUp."""
    name = task.get("name") or ""
    status = (task.get("status") or {}).get("status") or ""
    list_name = (task.get("list") or {}).get("name", "")
    url = task.get("url") or ""
    comment_count = len(comments)

    finding: dict[str, Any] = {
        "pass": "C",
        "task_id": task.get("id"),
        "name": name,
        "verdict": "would-write-note",
        "note_path": f"brain/clickup-outcomes/{task.get('id')}",
        "summary": (
            f"# {name}\n\n"
            f"- **List:** {list_name}\n"
            f"- **Status:** {status}\n"
            f"- **ClickUp:** {url}\n"
            f"- **Comments:** {comment_count}\n"
            f"- **Last activity:** {(_ms_to_dt(task.get('date_updated')) or '').isoformat() or 'unknown'}\n"
        ),
    }
    return finding


# ----- Main ------------------------------------------------------------------

def main() -> int:
    started = datetime.now(tz=timezone.utc).isoformat()
    digest_lines: list[str] = [
        f"🧹 **Groomer digest** — {started}",
        f"Mode: **{'DRY RUN' if DRY_RUN else 'LIVE'}** · Stale threshold: {STALE_DAYS}d",
        "",
    ]

    api_calls = 0
    auto_actions: list[str] = []
    proposals: list[str] = []
    would_close: list[str] = []
    stale_flags: list[str] = []
    drift_flags: list[str] = []
    brain_writes: list[str] = []

    for list_id, list_label in LISTS:
        digest_lines.append(f"## {list_label} (`{list_id}`)")
        try:
            tasks = _get_tasks(list_id)
            api_calls += 1
        except Exception as e:
            digest_lines.append(f"- (failed to list tasks: {e})")
            continue
        digest_lines.append(f"- {len(tasks)} tasks (incl. closed)")

        # Discover the list's terminal status name (so we don't say "complete"
        # if the list calls it "done" or "closed")
        list_terminal = "complete"
        try:
            list_meta = _curl("GET", f"/list/{list_id}")
            api_calls += 1
            for s in (list_meta.get("statuses") or []):
                if (s.get("type") or "").lower() == "closed":
                    list_terminal = s.get("status") or "complete"
                    break
        except Exception:
            pass

        for task in tasks:
            tid = task.get("id")
            try:
                comments = _get_comments(tid)
            except Exception as e:
                continue
            # 1 list-tasks call + 1 list-meta call + N comment calls
            api_calls += 1  # the per-task comments call (list-tasks counted outside)

            a = pass_a(task, comments, list_terminal)
            b = pass_b(task)
            c = pass_c(task, comments)

            # A
            if a["verdict"] == "stale":
                stale_flags.append(f"`{tid}` {a['name']} — {a['detail']}")
            elif a["verdict"] == "drift-done-with-pr":
                drift_flags.append(f"`{tid}` {a['name']} — {a['detail']}")

            # B
            if b["proposals"]:
                proposals.extend(
                    [f"  - [{b['list']}] `{b['task_id']}` **{b['name']}** — {p}"
                     for p in b["proposals"]]
                )

            # C: just count brain writes; never actually call write_note from cron
            brain_writes.append(c["note_path"])

    # Assemble
    digest_lines.append("")
    digest_lines.append(f"### Auto-actions taken ({'live' if not DRY_RUN else 'would take'})")
    if not DRY_RUN:
        digest_lines.extend([f"- {a}" for a in auto_actions] or ["- (none)"])
    else:
        digest_lines.append("- (none — dry run)")

    digest_lines.append("")
    digest_lines.append("### Drift flagged (86e1t59k7 class — done but unverified)")
    digest_lines.extend([f"- {d}" for d in drift_flags] or ["- (none)"])

    digest_lines.append("")
    digest_lines.append("### Stale tasks")
    digest_lines.extend([f"- {s}" for s in stale_flags] or ["- (none)"])

    digest_lines.append("")
    digest_lines.append("### Hygiene proposals (digest-only, never auto-applied)")
    digest_lines.extend(proposals or ["- (none)"])

    digest_lines.append("")
    digest_lines.append("### Brain writes planned (Pass C)")
    digest_lines.append(f"- {len(brain_writes)} task outcome note(s) queued")
    digest_lines.append(f"- Top 3 paths: {', '.join(f'`{p}`' for p in brain_writes[:3])}")

    digest_lines.append("")
    digest_lines.append("---")
    digest_lines.append(
        f"_API calls: {api_calls} this run (gate daily ceiling 250). "
        f"Reconciler-status: ACTIVE (still running 02:00). "
        f"First dry-run after install — promote to live only after Colin signs off on this output._"
    )

    print("\n".join(digest_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
