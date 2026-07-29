#!/usr/bin/env python3
"""
clickup_review_sla.py — "ready for review" SLA sweep (cheap, deterministic, zero-LLM).

The handoff gap this closes (2026-06-14): Hermes correctly parks tasks it has
finished but is not allowed to auto-merge (not `low-risk`, or no CI) in a review
status with the PR open — then NOTHING chases that handoff. Task 86e1vrh4t sat
in `ready for review` for ~2 days with a finished PR and no nudge. This sweep
makes the handoff loud: any task sitting in a review status past the SLA gets a
ClickUp comment pinging Colin, with the PR link.

How staleness is measured: NOT `date_updated` (that bumps on any edit, incl. the
poll-gate auto-assign pass) but a FIRST-SEEN-IN-REVIEW timestamp this script
records in its own state file. So the SLA clock is decoupled from every other
writer — assigning the task does not reset it.

Idempotency: each task is nudged once when it first crosses the SLA, then
re-nudged at most every RENUDGE_HOURS so a genuinely stuck review escalates
without spamming. A marker string in the comment body is a second guard that
survives state-file loss.

Schedule: daily (08:00 PT) via the clickup-review-sla cron job.
Delivery: ClickUp comment per stale task (Colin's choice 2026-06-14). Digest to
stdout for the cron log.

Default mode is DRY RUN. Promote to live only after Colin signs off on a dry-run
digest (same rollout discipline as the groomer).

Env:
  CLICKUP_API_TOKEN              required (Doppler-injected into the gateway env)
  CLICKUP_REVIEW_SLA_DRY_RUN     "1" (default) = dry-run; "0" = live
  CLICKUP_REVIEW_SLA_HOURS       SLA threshold in hours (default "24")
  CLICKUP_REVIEW_SLA_RENUDGE_HOURS  re-nudge interval in hours (default "72")
  CLICKUP_REVIEW_SLA_ASSIGNEE    ClickUp user id to @-mention (default Colin 168143285)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import slack_msg_builder as smb

TEAM_ID = "9017245888"

# Status NAMES (lowercased) that mean "finished by the agent, awaiting a human".
# Matched by name because ClickUp gives review states the generic "custom" type,
# and the exact label varies per list ("ready for review" vs "review" vs "qa").
REVIEW_STATUSES = {
    "ready for review", "review", "in review", "for review",
    "qa", "qa review", "needs review",
}

# "Worked By" custom field — the poller stamps this = Hermes on claim. We ONLY
# nudge tasks Hermes actually worked; otherwise the sweep would spam the ~80
# legacy human task-board items that also live in a review status. (Verified
# 2026-06-14: 86 tasks in review workspace-wide, only ~1 worked by Hermes.)
WORKED_BY_FIELD_ID = "2bf5c958-ca2a-4f6b-bab5-25693b98b1f1"
HERMES_OPTION_ID = "36c0d22d-3128-42b3-94d2-0d6072d2c0ea"
HERMES_OPTION_ORDERINDEX = 1  # dropdown value comes back as the orderindex on list endpoints

SLA_HOURS = float(os.environ.get("CLICKUP_REVIEW_SLA_HOURS", "24"))
RENUDGE_HOURS = float(os.environ.get("CLICKUP_REVIEW_SLA_RENUDGE_HOURS", "72"))
DRY_RUN = os.environ.get("CLICKUP_REVIEW_SLA_DRY_RUN", "1") != "0"
ASSIGNEE_ID = os.environ.get("CLICKUP_REVIEW_SLA_ASSIGNEE", "168143285")  # Colin

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_HERE, ".review_sla_state.json")

MARKER = "⏳ hermes-review-sla"  # idempotency guard inside the comment body

# The executor (SKILL.md step E) prefixes its escalation comment with this marker
# when autonomy hit a HARD stop (prod reverted, or deploy infra broke) — those get
# an immediate Slack DM to Colin, not just a routine 24h ClickUp nudge.
HARDSTOP_MARKER = "🚨 hermes-hardstop"

# The executor stamps this when it finishes a task but parks it on an OPERATOR
# DECISION it cannot make autonomously (brand/design/strategy/deploy-identity).
# Unlike a routine review handoff, this is BLOCKING progress — so it gets an
# immediate Slack DM (once), bypassing the SLA wait, but a calmer message than a
# hard-stop. Emitted via SKILL.md ("park for decision" rule) or
# closeout_from_continuation.py --decision.
DECISION_MARKER = "🟡 hermes-decision"
# Concise operator-summary block the executor is instructed to embed inside the
# 🟡 hermes-decision comment. When BOTH delimiters are present we forward ONLY
# the text strictly between them to Slack (the Q/a/b/c/Rec block Colin wants),
# instead of the verbose technical comment body. Delimiters are VERBATIM.
OPSUM_START = "<<<OPERATOR-SUMMARY>>>"
OPSUM_END = "<<<END-OPERATOR-SUMMARY>>>"


def _extract_operator_summary(body: str) -> str | None:
    """Return the text strictly BETWEEN the OPERATOR-SUMMARY delimiters, stripped.
    Defensive: returns None if `body` is falsy, if either delimiter is missing,
    if they appear out of order, or if the slice is empty after stripping — in
    every such case the caller falls back to the full (trimmed) body so nothing
    regresses for older parks that lack the block."""
    if not body:
        return None
    start = body.find(OPSUM_START)
    if start == -1:
        return None
    end = body.find(OPSUM_END, start + len(OPSUM_START))
    if end == -1:
        return None
    inner = body[start + len(OPSUM_START):end].strip()
    return inner or None
# Where Slack escalations go. NOTE: `hermes send` resolves a Slack CHAT id
# (C…/D…) or a configured name, NOT a user id (U…). The original value
# `slack:UN4CQ1EGG` was Colin's USER id → `hermes send` returned "Could not
# resolve … on slack" and EVERY Slack escalation (hard-stops included) silently
# failed.
# 2026-06-14→15: routed to the **#hermes channel** (C0BA8S6JF4J) because the DM
# was assumed unwatched. 2026-06-19: Colin's explicit call — route decision/stuck
# alerts DIRECT to his DM (D0BA2PM9CFM) ONLY, so a blocking decision pings him
# personally instead of sitting in a channel he doesn't monitor. Verified the
# bare D-id resolves via `hermes send --to slack:D0BA2PM9CFM` (exit 0, 2026-06-19).
# List targets: `hermes send --list slack`.
SLACK_TARGET = os.environ.get("CLICKUP_REVIEW_SLA_SLACK", "slack:D0BA2PM9CFM")  # Colin DM (per 2026-06-19); override via env if reverting to slack:hermes
SLACK_MENTION = os.environ.get("CLICKUP_REVIEW_SLA_SLACK_MENTION", "<@UN4CQ1EGG>")  # Colin's Slack user id, for the @-ping in-channel

# Decision-park lifecycle: the executor stamps DECISION_MARKER AND tags the task
# `agent-review` (the "waiting on Colin" queue). This sweep polls agent-review
# tasks hourly; once the operator replies in the Slack decision thread, the
# gateway round-trip hook (05-slack-decision-thread-hook) records that reply as a
# ClickUp comment prefixed with OPERATOR_REPLY_PREFIX below. This sweep then swaps
# agent-review → agent-ready so the poll gate re-claims it and the executor
# applies the decision. Closes the loop the notify tier opened.
AGENT_REVIEW_TAG = "agent-review"
READY_TAG = "agent-ready"
# Operator hard-fence (2026-06-19, Colin). A task tagged agent-avoid must never
# be re-armed by this sweep — even if it sits in agent-review and the operator
# replied, the avoid tag wins and we leave it parked. Mirrors the poll gate's
# top-priority exclusion so the two selection paths agree.
AVOID_TAG = "agent-avoid"
# Markers that identify an AGENT-authored comment. Retained for other call sites.
AGENT_COMMENT_MARKERS = ("🤖", "✅", "🔧", DECISION_MARKER, HARDSTOP_MARKER, MARKER)
# An operator reply is recognized by an EXPLICIT prefix, NOT by the absence of
# agent markers. Reason (2026-06-19 incident, 86e1yxn5e): every Hermes comment is
# authored via Colin's ClickUp token, so author cannot distinguish agent vs
# operator, and the agent routinely posts marker-free comments (e.g. "Colin's
# decisions LOCKED ..."). The old "non-agent comment newer than park" heuristic
# mistook those for an operator reply, un-parked the task, and the poll gate
# re-woke it — an infinite operator-blocked loop. The round-trip hook ALWAYS
# prefixes a genuine Slack reply with this exact string, so match on it.
OPERATOR_REPLY_PREFIX = "Operator (via Slack):"

# ----- Validation-blocked self-heal (close the BLOCKED-PR dead-end loop) -------
# Gap found 2026-06-20: when the validator BLOCKs a PR it tags the task
# `validation-blocked` (+ keeps `needs-validation`) "so the executor/SLA can pick
# it up" — but NOTHING consumed that tag. The executor only claims `agent-ready`;
# this sweep only re-armed decision-parks (agent-review). So a red PR sat tagged
# forever: re-seen each tick, validator dedups to SILENT, never fixed, never
# escalated — a permanent dead-end that staleness-sweep then nagged Colin about.
# This phase closes it: for a task whose BLOCK is still UNADDRESSED (the verdict's
# head_sha == the PR's live head), re-arm it to `agent-ready` so the executor
# fixes its own red PR (the BLOCK findings comment is the fix brief). The executor
# pushes a new commit → head changes → validator re-validates → PASS lands or a
# fresh BLOCK re-arms again. Capped at MAX_FIX_ATTEMPTS so a PR that genuinely
# can't be fixed escalates to Colin ONCE instead of looping. Attempt ledger is
# hermes_validate_ops' .hermes_validate_state.json (shared with the validator).
VALIDATION_BLOCKED_TAG = "validation-blocked"
NEEDS_VALIDATION_TAG = "needs-validation"
MAX_FIX_ATTEMPTS = int(os.environ.get("CLICKUP_REVIEW_SLA_MAX_FIX_ATTEMPTS", "3"))
# Marker so a human/operator can spot an exhausted-attempts escalation in-thread.
VALIDATION_EXHAUSTED_TAG = "validation-needs-human"
# 86e29q8qd/86e2eu8a4 (no-measurement failure class, Audit M6): ignite-validate
# ESCALATEs a structurally-unmeasurable/external-blocked objective to Needs Human
# on the FIRST fail (no MAX_FIX_ATTEMPTS wait — see ignite-validate SKILL.md
# §3d/Step 4) and tags it `needs-human`. Without this guard the re-queue below
# would still re-arm it to agent-ready on attempt 1, re-entering the exact loop
# the escalation exists to stop.
NEEDS_HUMAN_TAG = "needs-human"

# ----- Partial-brief capture (self-heal the iteration-cap brief-loss loop) -----
# When an executor cron run dies at the iteration cap (max_iterations_reached),
# its handoff brief ("what's done / what's left / exact file:line edits") is
# written ONLY to the cron output .md file — which the next FRESH executor
# session never reads (it reads task description + comments + brain). So the next
# run re-investigates from scratch, re-burns the budget, and can loop forever
# without shipping. Observed twice in one hour 2026-06-14 (WS2 86e1w1eh0 apply
# path, WS3 86e1w1ehb). The skill tells the agent to post a milestone before the
# cap, but it doesn't reliably honour it (it runs out of budget first). This
# phase is the safety net OUTSIDE the agent: scan recent executor outputs, and
# for any PARTIAL run, lift its Response/handoff brief into the claimed task's
# ClickUp thread so the next run resumes instead of restarting.
EXECUTOR_JOB_ID = os.environ.get("CLICKUP_EXECUTOR_JOB_ID", "62714b869845")
EXECUTOR_OUTPUT_DIR = os.path.join(
    os.path.dirname(_HERE), "cron", "output", EXECUTOR_JOB_ID
)
PARTIAL_MARKERS = (
    "max_iterations_reached",
    "PARTIAL — iteration budget hit",
    "BUDGET EXHAUSTED",
)
# Shared marker for BOTH the manual relay and this auto-capture — so dedup works
# across both and the resume phase classifies the brief as an agent comment.
CONTINUATION_MARKER = "🤖 CONTINUATION HANDOFF"
# Only look at outputs newer than this many minutes (the cron runs hourly; a 90m
# window covers the gap with margin without re-reading the whole archive).
BRIEF_CAPTURE_WINDOW_S = float(os.environ.get("CLICKUP_BRIEF_WINDOW_MIN", "90")) * 60



def _send_slack(text: str, return_ts: bool = False):
    """Escalate to Colin's Slack via `hermes send` (reuses gateway Slack creds,
    no LLM). Best-effort: a failure must not break the sweep.

    Delivery self-check: we always ask `hermes send` for JSON and require a
    `success` payload with a real `message_id`. That catches the silent no-op
    class where the CLI exits 0 but never records a delivered Slack message.

    Return contract:
      - return_ts=False → True on success, False on any failure
      - return_ts=True  → Slack ts/message_id string on success, None on failure

    In DRY_RUN we never post, so there is no real ts to return."""
    if DRY_RUN:
        return None if return_ts else True
    try:
        argv = ["hermes", "send", "--to", SLACK_TARGET, "--json", text]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            print(f"  ! slack send failed: {out.stderr.strip()[:200]}", file=sys.stderr)
            return None if return_ts else False
        try:
            payload = json.loads(out.stdout or "{}")
        except Exception as e:
            print(f"  ! slack send --json parse error: {e}", file=sys.stderr)
            return None if return_ts else False
        if not isinstance(payload, dict) or not payload.get("success"):
            print(
                f"  ! slack send self-check failed: {str(payload)[:200]}",
                file=sys.stderr,
            )
            return None if return_ts else False
        mid = payload.get("message_id")
        if not mid:
            print("  ! slack send self-check failed: missing message_id", file=sys.stderr)
            return None if return_ts else False
        return str(mid) if return_ts else True
    except Exception as e:
        print(f"  ! slack send error: {e}", file=sys.stderr)
        return None if return_ts else False


# ----- HTTP helpers (subprocess + curl — same proven pattern as the groomer) --

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


# ----- Helpers ----------------------------------------------------------------

def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def _worked_by_hermes(task: dict) -> bool:
    """True only if the task's "Worked By" custom field is set to Hermes. This is
    the scope gate: we never nudge tasks a human parked in review."""
    for f in (task.get("custom_fields") or []):
        if f.get("id") != WORKED_BY_FIELD_ID:
            continue
        v = f.get("value")
        return str(v) in {str(HERMES_OPTION_ORDERINDEX), HERMES_OPTION_ID}
    return False


def _scan_review_tasks() -> list[dict]:
    """Paginate the whole team for open, Hermes-worked tasks in a review status.

    subtasks=true is REQUIRED — agent-worked tasks are frequently children of an
    initiative parent (same lesson the poll-gate learned v3). The Worked-By gate
    keeps the sweep to Hermes's own parked work, not the human task board."""
    out: list[dict] = []
    page = 0
    while True:
        path = (
            f"/team/{TEAM_ID}/task"
            f"?page={page}&subtasks=true&include_closed=false"
        )
        data = _curl("GET", path)
        tasks = data.get("tasks") or []
        for t in tasks:
            name = ((t.get("status") or {}).get("status") or "").strip().lower()
            stype = ((t.get("status") or {}).get("type") or "").strip().lower()
            if stype in {"closed", "done"}:
                continue
            if name in REVIEW_STATUSES and _worked_by_hermes(t):
                out.append(t)
        if data.get("last_page", True) or not tasks:
            break
        page += 1
        if page > 50:
            break
    return out


def _extract_pr_urls(text: str) -> list[str]:
    if not text:
        return []
    return sorted(set(re.findall(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text)))


def _comments(task_id: str) -> list[dict]:
    try:
        return _curl("GET", f"/task/{task_id}/comment").get("comments", []) or []
    except Exception:
        return []


def _post_comment(task_id: str, text: str) -> bool:
    try:
        data = _curl("POST", f"/task/{task_id}/comment", {"comment_text": text})
        return bool(data.get("id"))
    except Exception as e:
        print(f"  ! comment failed on {task_id}: {e}", file=sys.stderr)
        return False


def _scan_agent_review_tasks() -> list[dict]:
    """Tasks tagged agent-review — Hermes parked them awaiting an operator decision."""
    out: list[dict] = []
    page = 0
    while True:
        path = (f"/team/{TEAM_ID}/task?page={page}&subtasks=true"
                f"&include_closed=false&tags%5B%5D={AGENT_REVIEW_TAG}")
        try:
            data = _curl("GET", path)
        except Exception as e:
            print(f"  ! agent-review scan failed: {e}", file=sys.stderr)
            break
        tasks = data.get("tasks") or []
        out.extend(tasks)
        if data.get("last_page", True) or not tasks:
            break
        page += 1
        if page > 50:
            break
    return out


def _is_agent_comment(text: str) -> bool:
    return any(m in (text or "") for m in AGENT_COMMENT_MARKERS)


def _tag(method: str, tid: str, tag: str) -> bool:
    try:
        _curl(method, f"/task/{tid}/tag/{tag}")
        return True
    except Exception as e:
        print(f"  ! {method} tag {tag} on {tid} failed: {e}", file=sys.stderr)
        return False


def _resumable_task(tid: str) -> tuple[bool, str]:
    """True only if the task is still in a state the poll gate would re-claim —
    i.e. tagged `agent-ready` and NOT already parked in a review status or
    closed/done. Guards against posting a stale partial brief onto a task that a
    later run already shipped or parked (the SLA nudge handles review-status tasks
    separately). Returns (resumable, reason)."""
    try:
        t = _curl("GET", f"/task/{tid}")
    except Exception as e:
        return False, f"task fetch failed: {e}"
    st = (t.get("status") or {})
    name = (st.get("status") or "").strip().lower()
    stype = (st.get("type") or "").strip().lower()
    tags = {(tag.get("name") or "").lower() for tag in (t.get("tags") or [])}
    if stype in {"closed", "done"}:
        return False, f"status '{name}' is terminal"
    if name in REVIEW_STATUSES:
        return False, f"parked in review status '{name}'"
    if READY_TAG not in tags:
        return False, f"no '{READY_TAG}' tag (won't be re-claimed)"
    return True, name


def _extract_claimed_task_id(resp: str) -> str | None:
    """Pull the task id the partial run actually WORKED out of its handoff brief.
    A brief also lists sibling task ids (the queue-snapshot enumeration at the top),
    so "first id" mis-routes — verified 2026-06-15: a WS2 brief that listed WS3
    first in its snapshot got captured onto WS3. Strategy: (1) trust an explicit
    "picked / claim pick" adjacency marker; (2) else the worked task is the one the
    brief is ABOUT, so take the most-frequently-mentioned id (siblings appear once
    in the snapshot; the worked task is referenced throughout)."""
    strong = (
        r"[Pp]icked\s*\**\s*`?(86[0-9a-z]{5,})`?",                       # "Picked **86..**" / "Picked `86..`"
        r"`?(86[0-9a-z]{5,})`?[^\n]{0,30}?\((?:picked|claim pick)",       # "`86..` — WS2 (picked per claim..."
        r"[Cc]laim pick[^\n]{0,15}?`?(86[0-9a-z]{5,})`?",                 # "claim pick: 86.."
    )
    for pat in strong:
        m = re.search(pat, resp)
        if m:
            return m.group(1)
    ids = re.findall(r"86[0-9a-z]{5,}", resp)
    if not ids:
        return None
    # most frequent; tiebreak = earliest mention
    return max(set(ids), key=lambda k: (ids.count(k), -ids.index(k)))


def _format_captured_brief(resp: str) -> str:
    """Wrap the run's Response section as a continuation-handoff comment."""
    brief = resp.split("## Response", 1)[-1].lstrip("\n ")
    if len(brief) > 6000:
        brief = brief[:6000] + "\n\n…(truncated — see full cron output on the mini)…"
    return (
        f"{CONTINUATION_MARKER} (auto-captured from cron output — the prior executor "
        "run hit the iteration cap before it could write back). RESUME from this; do "
        "NOT re-investigate from scratch. Status is/should be `in progress` = a "
        "continuation.\n\n---\n" + brief
    )


def _capture_partial_briefs(now: float, state: dict) -> tuple[list[str], list[str]]:
    """Safety net for the iteration-cap brief-loss loop. For each recent executor
    output that ended PARTIAL and whose claimed task has no handoff brief in its
    thread yet, post the brief so the next run resumes. Idempotent via filename
    state + a per-task marker check. Best-effort — never crash the cron."""
    captured, skipped = [], []
    processed = set(state.get("captured_briefs", []))
    if not os.path.isdir(EXECUTOR_OUTPUT_DIR):
        return captured, skipped
    cutoff = now - BRIEF_CAPTURE_WINDOW_S

    # Gather unprocessed PARTIAL outputs in the window, grouped by claimed task.
    # When a task has several partial runs (it died, was re-claimed, died again),
    # only the NEWEST brief is worth posting — earlier ones are stale or wrong
    # (verified 2026-06-14: WS3 86e1w1ehb's 22:05 run corrected its 21:34 chunk
    # attribution). Post the latest; mark the older ones processed so they're never
    # posted later.
    by_task: dict[str, list] = {}
    for fn in os.listdir(EXECUTOR_OUTPUT_DIR):
        if not fn.endswith(".md") or fn in processed:
            continue
        fp = os.path.join(EXECUTOR_OUTPUT_DIR, fn)
        try:
            mtime = os.path.getmtime(fp)
            if mtime < cutoff:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        idx = text.find("## Response")
        resp = text[idx:] if idx >= 0 else ""
        if not any(m in resp for m in PARTIAL_MARKERS):
            processed.add(fn)  # complete/normal run — never revisit
            continue
        tid = _extract_claimed_task_id(resp)
        if not tid:
            skipped.append(f"{fn} — PARTIAL but no task id parsed")
            processed.add(fn)
            continue
        by_task.setdefault(tid, []).append((mtime, fn, resp))

    for tid, runs in by_task.items():
        runs.sort()  # ascending by mtime
        newest_mtime, newest_fn, newest_resp = runs[-1]
        older_fns = [fn for _, fn, _ in runs[:-1]]
        # Dedup: a handoff brief already in the thread at/after the NEWEST run?
        # (covers the manual relay and a prior pass of this phase.)
        already = any(
            CONTINUATION_MARKER in (c.get("comment_text") or "")
            and (float(c.get("date") or 0) / 1000.0) >= newest_mtime - 300
            for c in _comments(tid)
        )
        if already:
            skipped.append(f"`{tid}` ({newest_fn}) — brief already in thread")
            processed.update(older_fns)
            processed.add(newest_fn)
            continue
        resumable, reason = _resumable_task(tid)
        if not resumable:
            # A later run shipped/parked it, or it won't be re-claimed — the brief
            # is moot. Mark processed so we don't re-check it every hour.
            skipped.append(f"`{tid}` ({newest_fn}) — not resumable ({reason})")
            processed.update(older_fns)
            processed.add(newest_fn)
            continue
        if DRY_RUN:
            captured.append(f"`{tid}` ({newest_fn}) — WOULD post captured brief")
            processed.update(older_fns)  # older are stale regardless
            continue  # don't mark newest processed; the live run should post it
        if _post_comment(tid, _format_captured_brief(newest_resp)):
            captured.append(f"`{tid}` ({newest_fn}) — captured brief posted")
            processed.update(older_fns)
            processed.add(newest_fn)
        else:
            skipped.append(f"`{tid}` ({newest_fn}) — post FAILED")
            processed.update(older_fns)
    state["captured_briefs"] = sorted(processed)[-300:]  # bound growth
    return captured, skipped


def _latest_decision_body(comments: list[dict]) -> str | None:
    """Return the Slack body for the NEWEST 🟡 hermes-decision park comment.

    Preferred: if the comment embeds an OPERATOR-SUMMARY block (both delimiters
    present), forward ONLY the concise text between the delimiters (the Q/a/b/c/Rec
    block Colin wants). Fallback (older parks, or executor omitted the block):
    keep the prior behavior — strip the bare marker line and forward the rest of
    the verbose body verbatim, trimmed."""
    newest_ts, newest_body = 0.0, None
    for c in comments:
        body = c.get("comment_text") or ""
        if DECISION_MARKER not in body:
            continue
        ts = float(c.get("date") or 0) / 1000.0
        if ts >= newest_ts:
            newest_ts, newest_body = ts, body
    if newest_body is None:
        return None
    # Preferred: the concise operator-summary block, when the executor embedded it.
    summary = _extract_operator_summary(newest_body)
    if summary:
        return summary
    # Fallback: strip any line that is just the marker (the executor stamps it on
    # its own line / at the start); keep everything else, trimmed — but HARD-CAP
    # the length. Without an OPERATOR-SUMMARY block the raw park body can be a
    # page-long wall of text in Slack (2026-06-19 incident). Forward only the head
    # and point to ClickUp for the rest.
    lines = newest_body.splitlines()
    kept = [ln for ln in lines if ln.strip() != DECISION_MARKER and ln.strip()]
    body = "\n".join(kept).strip()
    MAX = 900
    if len(body) > MAX:
        head = body[:MAX].rsplit("\n", 1)[0].rstrip()
        body = head + "\n\n… (truncated — full decision context in the ClickUp task)"
    return body


def _post_decision_threads(state: dict, now: float) -> tuple[list[str], list[str]]:
    """TAG-DRIVEN decision-thread front-end. For each `agent-review`-tagged task
    (NOT review-status-gated — a recent sweep saw 0 review tasks but 6 agent-review
    tasks awaiting) that has a 🟡 hermes-decision park comment AND no
    decision_thread_ts recorded yet, post ONE rich threaded Slack message and
    record its ts so the gateway inbound hook can map a reply in that thread back
    to this task. Idempotent via the decision_thread_ts guard. Best-effort.

    Replaces the old review-loop decision ping (which only fired for REVIEW_STATUSES
    tasks). The gateway hook turns a thread reply into the operator ClickUp comment
    that `_resume_agent_review` already consumes as the apply trigger."""
    posted, skipped = [], []
    # DM channel id from SLACK_TARGET ("slack:<id>"); used so the inbound hook can
    # match channel + thread_ts. Falls back to None if the target isn't slack:.
    decision_channel = (
        SLACK_TARGET.split(":", 1)[1] if ":" in SLACK_TARGET else None
    )
    for t in _scan_agent_review_tasks():
        tid = t.get("id")
        if not tid:
            continue
        name = (t.get("name") or "")[:120]
        rec = state.get(tid)
        if isinstance(rec, dict) and rec.get("decision_thread_ts"):
            skipped.append(f"`{tid}` {name[:60]} — thread already posted")
            continue
        comments = _comments(tid)
        body = _latest_decision_body(comments)
        if not body:
            # Fallback: agent-review tag present but no 🟡 decision comment —
            # the task is stranded with no parse-able context. Still ping Colin
            # so it doesn't silently sit forever. Use the re-nudge interval to
            # avoid spam. State key "no_park_nudge_ts" is separate from the main
            # path's "decision_thread_ts" so both guards are independent.
            # (Gap closed 2026-06-27: 86e1z0ucg + 86e1vv88r sat agent-review for
            # days with 0 Slack pings because the 🟡-gated path skipped them.)
            rec_d = state.get(tid) if isinstance(state.get(tid), dict) else {}
            no_park_ts = rec_d.get("no_park_nudge_ts", 0)
            if (now - no_park_ts) >= RENUDGE_HOURS * 3600:
                url = t.get("url") or f"https://app.clickup.com/t/{tid}"
                msg = smb.build_alert_message(
                    "⚠️",
                    "Task needs your attention.",
                    facts=[
                        f"{name}",
                        url,
                        "Tagged agent-review but no 🟡 decision comment is present.",
                    ],
                    next_step="Reply here with a decision or add a 🟡 ClickUp comment.",
                    max_words=60,
                )
                if DRY_RUN:
                    posted.append(f"`{tid}` {name[:60]} — WOULD send fallback nudge (no 🟡)")
                else:
                    ok = _send_slack(msg)
                    if ok:
                        if not isinstance(state.get(tid), dict):
                            state[tid] = {"first_seen_ts": now, "last_nudge_ts": 0,
                                          "status_name": "", "slack_notified_ts": 0,
                                          "decision_notified_ts": 0}
                        state[tid]["no_park_nudge_ts"] = now
                        posted.append(f"`{tid}` {name[:60]} — fallback Slack nudge sent (no 🟡)")
                    else:
                        skipped.append(f"`{tid}` {name[:60]} — fallback Slack nudge FAILED")
            else:
                skipped.append(f"`{tid}` {name[:60]} — no 🟡 park comment (re-nudge not due)")
            continue
        if len(body) > 3500:
            body = body[:3500] + "\n…(truncated — see the full ClickUp comment)…"
        url = t.get("url") or f"https://app.clickup.com/t/{tid}"
        msg = (
            "🟡 *Hermes needs a decision — this task is stuck and I can't proceed "
            "autonomously.*\n"
            f"*{name}*\n"
            f"{url}\n"
            "\n"
            f"{body}\n"
            "\n"
            "↩️ *Reply in this thread* with your choice (e.g. \"b\") or ask a question."
        )
        if DRY_RUN:
            posted.append(f"`{tid}` {name[:60]} — WOULD post decision thread")
            continue
        ts = _send_slack(msg, return_ts=True)
        if ts:
            # Create the rec if absent, mirroring the review-loop rec shape.
            if not isinstance(rec, dict) or "first_seen_ts" not in rec:
                rec = {"first_seen_ts": now, "last_nudge_ts": 0,
                       "status_name": "", "slack_notified_ts": 0,
                       "decision_notified_ts": 0}
                state[tid] = rec
            rec["decision_thread_ts"] = ts
            rec["decision_channel"] = decision_channel
            rec["decision_notified_ts"] = now
            posted.append(f"`{tid}` {name[:60]} — decision thread posted (ts={ts})")
        else:
            skipped.append(f"`{tid}` {name[:60]} — Slack post FAILED")
    return posted, skipped


def _resume_agent_review(now: float) -> tuple[list[str], list[str]]:
    """Swap agent-review → agent-ready once the operator has replied to a decision
    park, so the poll gate re-claims it. Reply = a non-agent comment newer than the
    🟡 decision-park comment. Idempotent: the swap drops it from the next scan."""
    resumed, waiting = [], []
    for t in _scan_agent_review_tasks():
        tid = t.get("id")
        name = (t.get("name") or "")[:90]
        # Operator hard-fence: agent-avoid wins over a pending operator reply.
        # Never re-arm a fenced task — leave it parked until the tag is removed.
        if any((tg.get("name") or "").lower() == AVOID_TAG
               for tg in (t.get("tags") or [])):
            waiting.append(f"`{tid}` {name} — agent-avoid set, not re-arming")
            continue
        comments = _comments(tid)
        park_ts = 0.0
        for c in comments:
            if DECISION_MARKER in (c.get("comment_text") or ""):
                park_ts = max(park_ts, float(c.get("date") or 0) / 1000.0)
        # An operator reply = a comment newer than the park that carries the
        # explicit OPERATOR_REPLY_PREFIX written by the Slack round-trip hook.
        # Do NOT use "absence of agent markers" — the agent posts marker-free
        # comments too, which caused the 86e1yxn5e un-park loop (2026-06-19).
        replied = bool(park_ts) and any(
            (float(c.get("date") or 0) / 1000.0) > park_ts
            and OPERATOR_REPLY_PREFIX in (c.get("comment_text") or "")
            for c in comments
        )
        if not replied:
            tail = "" if park_ts else " (no 🟡 park comment — left as-is)"
            waiting.append(f"`{tid}` {name} — awaiting operator reply{tail}")
            continue
        if DRY_RUN:
            resumed.append(f"`{tid}` {name} — WOULD re-activate (operator replied).")
            continue
        if _tag("POST", tid, READY_TAG) and _tag("DELETE", tid, AGENT_REVIEW_TAG):
            _post_comment(tid, "🤖 Operator replied — re-activating (agent-review → "
                               "agent-ready). The executor will apply your decision "
                               "on the next poll.")
            resumed.append(f"`{tid}` {name} — RE-ACTIVATED (operator replied).")
        else:
            resumed.append(f"`{tid}` {name} — re-activate FAILED (tag swap).")
    return resumed, waiting


def _scan_validation_blocked_tasks() -> list[dict]:
    """Tasks tagged validation-blocked — the validator BLOCKed their PR."""
    out: list[dict] = []
    page = 0
    while True:
        path = (f"/team/{TEAM_ID}/task?page={page}&subtasks=true"
                f"&include_closed=false&tags%5B%5D={VALIDATION_BLOCKED_TAG}")
        try:
            data = _curl("GET", path)
        except Exception as e:
            print(f"  ! validation-blocked scan failed: {e}", file=sys.stderr)
            break
        tasks = data.get("tasks") or []
        out.extend(tasks)
        if data.get("last_page", True) or not tasks:
            break
        page += 1
        if page > 50:
            break
    return out


def _gh_pr_state(repo: str, pr: str) -> tuple[str, str]:
    """(STATE, head_sha) for a PR via gh; ('', '') on any failure (caller skips)."""
    try:
        r = subprocess.run(
            ["gh", "pr", "view", str(pr), "--repo", repo,
             "--json", "state,headRefOid"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return "", ""
        d = json.loads(r.stdout or "{}")
        return (d.get("state") or "").upper(), d.get("headRefOid") or ""
    except Exception as e:
        print(f"  ! gh pr view {repo}#{pr} failed: {e}", file=sys.stderr)
        return "", ""


def _latest_block_for_task(tid: str):
    """Most-recent BLOCK entry for this task in the validator verdict store.
    Returns (repo, pr, head_sha) or None. Store is keyed 'owner/repo#pr'."""
    try:
        sys.path.insert(0, _HERE)
        import validator_verdict
        store = validator_verdict.load_verdicts()
    except Exception as e:
        print(f"  ! verdict store load failed: {e}", file=sys.stderr)
        return None
    best = None  # (ts, repo, pr, head)
    for key, v in (store or {}).items():
        if not isinstance(v, dict) or "#" not in key:
            continue
        if v.get("task_id") != tid or v.get("verdict") != "BLOCK":
            continue
        repo, _, pr = key.rpartition("#")
        ts = v.get("ts") or ""
        if best is None or ts > best[0]:
            best = (ts, repo, pr, v.get("head_sha") or "")
    return None if best is None else (best[1], best[2], best[3])


_PR_URL_RE = re.compile(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")


def _pr_from_text(text: str):
    """Newest-looking (repo, pr) parsed from GitHub PR URLs in text, or None.
    Used as the fallback PR source when the verdict store has rotated out the
    BLOCK entry but the task still carries the validator's BLOCK comment + link."""
    if not text:
        return None
    matches = _PR_URL_RE.findall(text)
    if not matches:
        return None
    # Last match wins (comments are appended chronologically; the latest PR link
    # in the blob is the most recent reference).
    repo, pr = matches[-1]
    return repo, pr


def _set_status(tid: str, status: str) -> bool:
    try:
        _curl("PUT", f"/task/{tid}", {"status": status})
        return True
    except Exception as e:
        print(f"  ! set status {status} on {tid} failed: {e}", file=sys.stderr)
        return False


def _resume_validation_blocked(now: float) -> tuple[list[str], list[str]]:
    """Close the BLOCKED-PR dead-end. For each validation-blocked task whose BLOCK
    is still UNADDRESSED (verdict head_sha == the PR's live head), re-arm it to
    agent-ready (drop validation-blocked + needs-validation, set `in progress` to
    dodge the review-status re-claim trap) so the executor fixes its own red PR —
    the BLOCK findings comment is its fix brief. Capped at MAX_FIX_ATTEMPTS; on
    exhaustion, escalate to Colin ONCE and fence the task with
    validation-needs-human so it stops looping. Best-effort; never raises."""
    requeued, held = [], []
    try:
        sys.path.insert(0, _HERE)
        import hermes_validate_ops as hvo
    except Exception as e:
        print(f"  ! validation-blocked phase: import hermes_validate_ops failed: {e}",
              file=sys.stderr)
        return requeued, held
    for t in _scan_validation_blocked_tasks():
        tid = t.get("id")
        name = (t.get("name") or "")[:90]
        url = t.get("url") or ""
        tags = {(tg.get("name") or "").lower() for tg in (t.get("tags") or [])}
        if AVOID_TAG in tags:                       # operator hard-fence wins
            held.append(f"`{tid}` {name} — agent-avoid set, not re-arming")
            continue
        if NEEDS_HUMAN_TAG in tags:                 # no-measurement/external-blocked ESCALATE
            # Short-circuit at attempt 1, not MAX_FIX_ATTEMPTS: a structural
            # capability gap (no measurement infra / unreachable external
            # dependency) doesn't get less true on a retry, so ignite-validate
            # already escalated this on the FIRST fail (see failure-classes.md
            # `no-measurement`). Re-arming it here would silently undo that
            # escalation and re-enter the exact executor<->validator loop the
            # escalation exists to stop.
            held.append(f"`{tid}` {name} — needs-human set (no-measurement/external-blocked), not re-arming")
            continue
        # DB-publish lane neutralization (2026-06-27 rework): a DB-backed publish
        # task (Neon posts-row write, NO PR — durable marker at
        # ~/.hermes/deliverables/<tid>/publish_result.json) must NEVER enter the
        # PR-fix re-queue machinery below (it resolves a PR, re-arms to fix a red
        # PR, etc. — all PR-shaped). The DB lane is validated+closed by
        # db_closeout_actor (live-URL re-verify + guarded flip). If a DB task is
        # tagged validation-blocked, clear the stale PR-shaped tags and leave it to
        # the closeout actor; do NOT re-queue it as a PR fix.
        if os.path.isfile(os.path.expanduser(
                f"~/.hermes/deliverables/{tid}/publish_result.json")):
            if not DRY_RUN:
                _tag("DELETE", tid, VALIDATION_BLOCKED_TAG)
            held.append(f"`{tid}` {name} — DB-publish task (no PR); cleared validation-blocked, "
                        f"db_closeout_actor owns it")
            continue
        if VALIDATION_EXHAUSTED_TAG in tags:        # already escalated; don't loop
            held.append(f"`{tid}` {name} — exhausted, awaiting human")
            continue
        # Resolve the PR: prefer the verdict-store BLOCK entry (gives a head SHA to
        # detect a fix-in-flight); else fall back to the PR link in the task thread
        # (the store rotates / the validator goes SILENT once its BLOCK comment is
        # already posted, so the store is NOT a reliable "still blocked" signal).
        blk = _latest_block_for_task(tid)
        if blk:
            repo, pr, block_head = blk
        else:
            blob = (t.get("description") or "") + "\n" + \
                   "\n".join(c.get("comment_text") or "" for c in _comments(tid))
            parsed = _pr_from_text(blob)
            if not parsed:
                held.append(f"`{tid}` {name} — no PR resolvable (no store entry, no PR link), left for human")
                continue
            repo, pr = parsed
            block_head = ""
        state_, live_head = _gh_pr_state(repo, pr)
        if state_ in ("MERGED", "CLOSED"):          # block is moot — clean up
            if not DRY_RUN:
                _tag("DELETE", tid, VALIDATION_BLOCKED_TAG)
                _tag("DELETE", tid, NEEDS_VALIDATION_TAG)
                hvo.reset_attempts(tid)
            requeued.append(f"`{tid}` {name} — {repo}#{pr} {state_}; cleaned stale block tags")
            continue
        if not state_:
            held.append(f"`{tid}` {name} — {repo}#{pr} state unknown (gh failed), retry next tick")
            continue
        if live_head and block_head and live_head != block_head:
            held.append(f"`{tid}` {name} — fix pushed ({block_head[:7]}→{live_head[:7]}), awaiting re-validation")
            continue
        # BLOCK still unaddressed on the live head → the dead-end. Count + act.
        if DRY_RUN:
            attempts = hvo.get_attempts(tid) + 1
            requeued.append(f"`{tid}` {name} — WOULD re-queue to fix {repo}#{pr} (attempt {attempts}/{MAX_FIX_ATTEMPTS})")
            continue
        attempts = hvo.inc_attempts(tid)
        if attempts > MAX_FIX_ATTEMPTS:             # exhausted → escalate once, fence
            _tag("POST", tid, VALIDATION_EXHAUSTED_TAG)
            _tag("DELETE", tid, READY_TAG)
            _send_slack(f"🚨 *Validator BLOCK unresolved after {MAX_FIX_ATTEMPTS} fix "
                        f"attempts* — needs a human.\n{name}\n{repo}#{pr}\n{url}")
            _post_comment(tid, f"🚨 hermes-validate: BLOCK on {repo}#{pr} still unresolved "
                               f"after {MAX_FIX_ATTEMPTS} autonomous fix attempts. Tagged "
                               f"`{VALIDATION_EXHAUSTED_TAG}` and escalated to Colin. "
                               f"Auto-fix loop stopped.")
            held.append(f"`{tid}` {name} — EXHAUSTED ({MAX_FIX_ATTEMPTS}); escalated to Colin")
            continue
        ok = _tag("POST", tid, READY_TAG)
        _tag("DELETE", tid, VALIDATION_BLOCKED_TAG)
        _tag("DELETE", tid, NEEDS_VALIDATION_TAG)
        _set_status(tid, "in progress")             # dodge the re-claim trap
        if ok:
            _post_comment(tid, f"♻️ hermes-validate: re-queuing to fix the validator BLOCK "
                               f"on {repo}#{pr} (attempt {attempts}/{MAX_FIX_ATTEMPTS}). The "
                               f"validator findings above are your fix brief — push a fix and "
                               f"it will re-validate automatically.")
            requeued.append(f"`{tid}` {name} — RE-QUEUED to fix {repo}#{pr} (attempt {attempts}/{MAX_FIX_ATTEMPTS})")
        else:
            requeued.append(f"`{tid}` {name} — re-queue FAILED (tag add)")
    return requeued, held


def _hours_since(ts: float, now: float) -> float:
    return (now - ts) / 3600.0


def _prune_stale_records(state: dict, seen_ids: set) -> None:
    """Prune task records that have left the review statuses. Only prune
    per-task records (dicts with first_seen_ts) — NOT meta keys like
    `captured_briefs` (the partial-brief phase's processed-file ledger),
    which would otherwise be deleted every run because it isn't a task id.
    ALSO keep any rec carrying a `decision_thread_ts`: that rec maps a live
    Slack decision thread back to its task (agent-review-tagged, often NOT
    in a review status, so absent from seen_ids), and the gateway inbound
    hook needs it to round-trip the reply. _resume_agent_review drops the
    agent-review tag once the operator replies, so the rec is naturally
    retired then — pruning it here would break the loop.
    ALSO keep any rec carrying `no_park_nudge_ts`: that's the fallback-nudge
    timer set by _post_decision_threads for agent-review tasks with no 🟡
    park comment (also absent from seen_ids). Without this guard the record
    was deleted in the SAME run it was written, resetting the RENUDGE_HOURS
    clock to 0 every tick — the direct cause of 40 repeat ClickUp alerts +
    16 repeat sentinel-failure nudges in one 24h window (audit 2026-07-13,
    86e2abmmj). Mutates `state` in place."""
    for tid in list(state.keys()):
        rec = state.get(tid)
        if (
            isinstance(rec, dict)
            and "first_seen_ts" in rec
            and tid not in seen_ids
            and not rec.get("decision_thread_ts")
            and not rec.get("no_park_nudge_ts")
        ):
            del state[tid]


# ----- Main -------------------------------------------------------------------

def main() -> int:
    now = time.time()
    started = datetime.now(tz=timezone.utc).isoformat()
    state = _load_state()

    digest = [
        f"⏳ **Review-SLA sweep** — {started}",
        f"Mode: **{'DRY RUN' if DRY_RUN else 'LIVE'}** · SLA: {SLA_HOURS:.0f}h · "
        f"re-nudge: {RENUDGE_HOURS:.0f}h · delivery: SLA nudges via ClickUp comment; "
        f"decisions via Slack thread (reply round-trips back to ClickUp)",
        "",
    ]

    # Phase 0-pre: post a rich threaded Slack decision message for each
    # agent-review-tagged task that has a 🟡 park comment but no thread yet, and
    # record the thread ts in state. TAG-DRIVEN (not review-status-gated) so it
    # catches parks that aren't in a review status. Must run BEFORE the resume
    # phase. Best-effort — never crash the cron. Mutates `state` directly.
    try:
        decision_threads, decision_thread_skips = _post_decision_threads(state, now)
    except Exception as e:
        decision_threads, decision_thread_skips = [], []
        print(f"  ! decision-thread post phase error: {e}", file=sys.stderr)

    # Phase 0: resume decision-parked tasks the operator has now replied to
    # (agent-review → agent-ready). Independent of the review-status scan below;
    # queries by the agent-review tag. Best-effort — never crash the cron.
    try:
        resumed_review, awaiting_review = _resume_agent_review(now)
    except Exception as e:
        resumed_review, awaiting_review = [], []
        print(f"  ! agent-review resume phase error: {e}", file=sys.stderr)

    # Phase 0b: capture partial-run handoff briefs from the executor's cron output
    # into the claimed task's thread, so a run that died at the iteration cap
    # resumes instead of restarting (the brief-loss infinite-loop fix). Best-effort.
    try:
        captured_briefs, skipped_briefs = _capture_partial_briefs(now, state)
    except Exception as e:
        captured_briefs, skipped_briefs = [], []
        print(f"  ! partial-brief capture phase error: {e}", file=sys.stderr)

    # Phase 0d: re-queue BLOCKED PRs to the executor to fix themselves
    # (validation-blocked → agent-ready, capped at MAX_FIX_ATTEMPTS → escalate),
    # closing the BLOCKED-PR dead-end loop. TAG-DRIVEN. Best-effort.
    try:
        requeued_blocked, held_blocked = _resume_validation_blocked(now)
    except Exception as e:
        requeued_blocked, held_blocked = [], []
        print(f"  ! validation-blocked resume phase error: {e}", file=sys.stderr)


    try:
        review_tasks = _scan_review_tasks()
    except Exception as e:
        print("\n".join(digest))
        print(f"ERROR scanning workspace: {e}", file=sys.stderr)
        return 0  # never crash the cron

    seen_ids = set()
    nudged, waiting, fresh, hardstops = [], [], [], []

    for t in review_tasks:
        tid = t.get("id")
        seen_ids.add(tid)
        name = (t.get("name") or "")[:90]
        status = (t.get("status") or {}).get("status") or "?"
        url = t.get("url") or ""
        list_name = (t.get("list") or {}).get("name", "?")

        rec = state.get(tid)
        if not rec or rec.get("status_name") not in REVIEW_STATUSES:
            # First time we've seen this task in a review status — start its clock.
            rec = {"first_seen_ts": now, "last_nudge_ts": 0, "status_name": status.lower(),
                   "slack_notified_ts": 0, "decision_notified_ts": 0}
            state[tid] = rec

        # Fetch comments once: used for both hard-stop detection and PR-link extraction.
        comments = _comments(tid)
        comment_blob = "\n".join([t.get("description") or ""] + [c.get("comment_text") or "" for c in comments])
        pr_urls = _extract_pr_urls(comment_blob)
        pr_line = f" PR: {', '.join(pr_urls)}" if pr_urls else " (no PR link found in task)"

        # HARD STOP: executor reverted prod / deploy infra broke. Escalate to Slack
        # immediately (once), regardless of the 24h SLA — this is urgent.
        if HARDSTOP_MARKER in comment_blob and not rec.get("slack_notified_ts"):
            msg = smb.build_alert_message(
                "🚨",
                "Autonomous ship loop hard-stopped.",
                facts=[
                    name,
                    f"{url}{pr_line}",
                    "Hermes hit a deploy or verify failure it could not clear.",
                ],
                next_step="Check the latest ClickUp comment and unblock the task.",
                max_words=60,
            )
            if _send_slack(msg):
                rec["slack_notified_ts"] = now
                hardstops.append(f"`{tid}` {name} — Slack-escalated.{pr_line}")
            else:
                hardstops.append(f"`{tid}` {name} — Slack escalation FAILED.{pr_line}")
            continue  # don't also post a routine 24h nudge on a hard stop

        # DECISION NEEDED: now handled TAG-DRIVEN in _post_decision_threads()
        # (Phase 0-pre), which posts the rich Slack thread for EVERY agent-review
        # park — not just those that also happen to sit in a REVIEW_STATUS. The old
        # per-review-task ping block lived here and double-pinged tasks that were
        # both parked AND in a review status, while missing parks in other statuses
        # (the real bug). Intentionally removed; the hard-stop block above stays.

        age_h = _hours_since(rec["first_seen_ts"], now)
        if age_h < SLA_HOURS:
            fresh.append(f"`{tid}` {name} — in '{status}' {age_h:.0f}h (< SLA)")
            continue

        # Past SLA. Nudge if never nudged, or re-nudge interval elapsed.
        last_nudge = rec.get("last_nudge_ts", 0)
        due = (last_nudge == 0) or (_hours_since(last_nudge, now) >= RENUDGE_HOURS)
        if not due:
            waiting.append(
                f"`{tid}` {name} — in '{status}' {age_h:.0f}h, "
                f"last nudge {_hours_since(last_nudge, now):.0f}h ago (re-nudge not due)"
            )
            continue

        body = smb.build_alert_message(
            "⏳",
            f"{name} still needs review.",
            facts=[
                f"{status} for about {age_h / 24:.0f}d ({age_h:.0f}h).",
                pr_line.strip(),
                f"Hermes parked it here on purpose and is waiting on human review.",
            ],
            next_step=f"Review it or move it out of {status}.",
            max_words=60,
        )

        if DRY_RUN:
            nudged.append(f"`{tid}` {name} — WOULD nudge ({age_h:.0f}h).{pr_line}")
        else:
            ok = _post_comment(tid, body)
            if ok:
                rec["last_nudge_ts"] = now
                nudged.append(f"`{tid}` {name} — nudged ({age_h:.0f}h).{pr_line}")
            else:
                nudged.append(f"`{tid}` {name} — nudge FAILED ({age_h:.0f}h)")

    _prune_stale_records(state, seen_ids)

    if not DRY_RUN:
        _save_state(state)

    digest.append(f"### 📋 Partial briefs captured to ClickUp — {len(captured_briefs)}")
    digest.extend([f"- {x}" for x in captured_briefs] or ["- (none)"])
    if skipped_briefs:
        digest.append(f"### 📋 Partial-brief capture skipped — {len(skipped_briefs)}")
        digest.extend([f"- {x}" for x in skipped_briefs])
    digest.append(f"### ♻️ Validation-blocked RE-QUEUED to fix (capped {MAX_FIX_ATTEMPTS}) — {len(requeued_blocked)}")
    digest.extend([f"- {x}" for x in requeued_blocked] or ["- (none)"])
    if held_blocked:
        digest.append(f"### 🔴 Validation-blocked held (awaiting fix/human/re-validate) — {len(held_blocked)}")
        digest.extend([f"- {x}" for x in held_blocked])
    digest.append(f"### ♻️ Agent-review RE-ACTIVATED (operator replied) — {len(resumed_review)}")
    digest.extend([f"- {x}" for x in resumed_review] or ["- (none)"])
    digest.append(f"### 🟡 Agent-review awaiting operator reply — {len(awaiting_review)}")
    digest.extend([f"- {x}" for x in awaiting_review] or ["- (none)"])
    digest.append(f"### 🟡 Decision threads posted to Slack — {len(decision_threads)}")
    digest.extend([f"- {x}" for x in decision_threads] or ["- (none)"])
    if decision_thread_skips:
        digest.append(f"### 🟡 Decision-thread post skipped — {len(decision_thread_skips)}")
        digest.extend([f"- {x}" for x in decision_thread_skips])
    digest.append(f"### 🚨 Hard-stops Slack-escalated — {len(hardstops)}")
    digest.extend([f"- {x}" for x in hardstops] or ["- (none)"])
    digest.append("")
    digest.append(f"### Nudged ({'would' if DRY_RUN else 'did'}) — {len(nudged)}")
    digest.extend([f"- {x}" for x in nudged] or ["- (none)"])
    digest.append("")
    digest.append(f"### Past SLA but re-nudge not yet due — {len(waiting)}")
    digest.extend([f"- {x}" for x in waiting] or ["- (none)"])
    digest.append("")
    digest.append(f"### In review, still within SLA — {len(fresh)}")
    digest.extend([f"- {x}" for x in fresh] or ["- (none)"])
    digest.append("")
    digest.append("---")
    digest.append(
        f"_{len(review_tasks)} task(s) in a review status scanned. "
        f"State: {STATE_PATH}. Promote to live with CLICKUP_REVIEW_SLA_DRY_RUN=0._"
    )

    print("\n".join(digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
