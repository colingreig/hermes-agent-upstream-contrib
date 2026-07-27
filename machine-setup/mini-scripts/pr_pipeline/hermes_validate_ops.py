#!/usr/bin/env python3
"""
hermes_validate_ops.py — sanctioned operations helper for the Universal AI Review
Daemon (hermes-validate skill).

Companion to clickup_triage_ops.py.  This module covers the WRITE-side ops that
the validator is allowed to perform: post comments, flip task status, add/remove
tags, track fix-attempt counts, merge PRs (allowlist-gated), and escalate to Colin
via Slack when a task can't be fixed autonomously.

Pure-logic functions (repo_allowed, load_allowlist, get/inc/reset_attempts,
escalation_message) are side-effect-free and fully importable for unit testing.
Network ops live in CLI subcommand handlers.

DRY_RUN env var: when set, WRITE ops print what they WOULD do and skip the real
call.  READ ops (get) always execute.

All ops read CLICKUP_API_TOKEN from env (Doppler-injected).  Slack escalation uses
`hermes send --to <SLACK_ESCALATION_CHANNEL>` (same pattern as clickup_review_sla.py).

Usage:
  hermes_validate_ops.py get <task_id>
  hermes_validate_ops.py comment <task_id> --body <file>
  hermes_validate_ops.py set-status <task_id> <status>
  hermes_validate_ops.py add-tag <task_id> <tag>
  hermes_validate_ops.py rm-tag <task_id> <tag>
  hermes_validate_ops.py attempts <get|inc|reset> <task_id>
  hermes_validate_ops.py merge-pr <repo> <pr_number> [--squash]
  hermes_validate_ops.py escalate <task_id> --reason <file>
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from . import slack_msg_builder as smb
    from . import validator_verdict
else:
    import slack_msg_builder as smb
    import validator_verdict

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
API = "https://api.clickup.com/api/v2"

ATTEMPTS_PATH = os.path.expanduser("~/.hermes/scripts/.hermes_validate_state.json")
ALLOWLIST_PATH = os.path.expanduser("~/.hermes/allowed-repos.txt")
# Same offline rename-equivalence file validator_repo_guard.py uses (its RC2
# header). A repo string here is expanded to every alias spelling before a
# membership check, so an allowlist entry written under one name (e.g. the
# pre-rename "colingreig/hermes-agent") still matches a caller that supplies
# the post-rename name (or vice versa) without editing the allowlist itself.
REPO_ALIASES_PATH = os.path.expanduser("~/.hermes/config/repo-aliases.json")

# Slack target: `hermes send` resolves channel names or C… IDs.
# The hermes channel (@hermes + @-mention Colin) is the pattern that actually
# delivers — see clickup_review_sla.py lines 87-95 for the full rationale.
# Colin's user id for @-mention: UN4CQ1EGG (confirmed working 2026-06-14).
# Override via env; set SLACK_ESCALATION_CHANNEL to e.g. "slack:hermes".
SLACK_ESCALATION_CHANNEL = os.environ.get("SLACK_ESCALATION_CHANNEL", "slack:hermes")
SLACK_MENTION = "<@UN4CQ1EGG>"

DRY_RUN = bool(os.environ.get("DRY_RUN"))
# Activation switch (Task: autonomous-merge activation). Default is ACTIVATED
# (False, not shadow) unless HERMES_MERGE_SHADOW is explicitly truthy — the
# SAME env-derived helper autonomous_merge._shadow(), merge_guard._shadow(),
# and the verdict writer's "shadow" stamp all call
# (validator_verdict.merge_shadow_active()), so none of them can disagree
# about which mode the pipeline is in. Read once at import time: this module
# runs as a fresh process per invocation, same as the DRY_RUN pattern above.
VALIDATE_SHADOW = validator_verdict.merge_shadow_active()

# ---------------------------------------------------------------------------
# Pure logic — importable, side-effect-free
# ---------------------------------------------------------------------------

def repo_allowed(repo: str, allowed_set: set) -> bool:
    """Return True iff repo is in the allowlist set."""
    return repo in allowed_set


def _expand_repo_aliases(names: set, path: str = REPO_ALIASES_PATH) -> set:
    """Rename-tolerant expansion: for each known alias group (one GitHub repo
    that has been reached under >1 owner/name spelling), if ANY spelling is
    already in ``names``, add every spelling in that group. A missing/corrupt
    alias file is tolerated exactly like a missing allowlist file — returns
    ``names`` unchanged, never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            groups = json.load(f).get("aliases") or []
    except Exception:
        return names
    expanded = set(names)
    for group in groups:
        if isinstance(group, list) and expanded.intersection(group):
            expanded.update(group)
    return expanded


def load_allowlist(path: str = ALLOWLIST_PATH) -> set:
    """Parse the allowlist file.  One repo per line; skip blank lines and #
    comments.  Returns a set of 'owner/repo' strings, expanded to include any
    known rename-alias spellings (see _expand_repo_aliases)."""
    result: set = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    result.add(stripped)
    except FileNotFoundError:
        pass  # tolerate missing file — returns empty set
    return _expand_repo_aliases(result)


def _load_ledger(path: str) -> dict:
    """Load the attempts ledger JSON from path.  Returns {} on missing/corrupt."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_ledger(data: dict, path: str) -> None:
    """Atomically persist the ledger dict to path."""
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def get_attempts(task_id: str, path: str = ATTEMPTS_PATH) -> int:
    """Return the current attempt count for task_id (0 if unknown)."""
    return _load_ledger(path).get(task_id, 0)


def inc_attempts(task_id: str, path: str = ATTEMPTS_PATH) -> int:
    """Increment the attempt count for task_id, persist, and return the new value."""
    ledger = _load_ledger(path)
    ledger[task_id] = ledger.get(task_id, 0) + 1
    _save_ledger(ledger, path)
    return ledger[task_id]


def reset_attempts(task_id: str, path: str = ATTEMPTS_PATH) -> None:
    """Reset the attempt count for task_id to 0 and persist."""
    ledger = _load_ledger(path)
    ledger[task_id] = 0
    _save_ledger(ledger, path)


# Signals in a validator BLOCK reason that mean "the code may be fine — what's
# missing is a captured live RUN (or a credential only a human holds)", not a
# code defect. When the same lens blocks round after round and these phrases
# recur, more re-architecting won't clear it; an actual run will. This is the
# PressWhizz drain PR #37 failure mode (verified-live blocked 6 rounds while the
# executor kept restructuring the change instead of running the smoke).
_EVIDENCE_GAP_SIGNALS = (
    "verified-live", "verified live", "run output", "captured output",
    "shipped-blind", "shipped blind", "not demonstrate", "unexercised",
    "no test", "no run", "live smoke", "live-db", "live db", "actually exercised",
    "proof", "prove", "should work",
)


def classify_block_reason(reason: str) -> str:
    """Classify a persistent validator BLOCK as 'evidence-gap' or 'code-defect'.

    'evidence-gap' => the BLOCK is asking for proof the live path works (a
    captured run / live smoke / human-held credential), not for a code change.
    Re-architecting the change will not clear it; running the evidence will.
    Side-effect-free and importable for testing. Returns 'code-defect' by
    default (the conservative read — assume a real fix is needed)."""
    low = (reason or "").lower()
    return "evidence-gap" if any(s in low for s in _EVIDENCE_GAP_SIGNALS) else "code-defect"


def escalation_message(task_name: str, task_url: str, reason: str, attempts: int) -> str:
    """Build a plain-language escalation message for Colin.

    Rules (enforced by tests):
    - MUST contain task_name and task_url.
    - MUST NOT contain the substring "diff" (any case) or triple-backtick fences.
    - No code snippets.

    When the BLOCK looks like an evidence-gap (a live run / human credential is
    needed, not a code change), the message says so explicitly so a missing-
    evidence stall reads differently from a genuinely unfixable defect.
    """
    kind = classify_block_reason(reason)
    lead = (
        "This looks like an evidence gap: Hermes needs a live run or a human-held "
        "credential, not more code churn."
        if kind == "evidence-gap"
        else "This looks like a code issue that still needs a fix."
    )
    return smb.build_alert_message(
        "🚨",
        f"I couldn't finish \"{task_name}\".",
        facts=[
            f"I tried {attempts} time(s), but the blocker is still there.",
            lead,
        ],
        next_step=f"Review {task_url} and decide whether to keep going, pause, or change scope.",
        max_words=150,
    )


# ---------------------------------------------------------------------------
# Network helpers (ClickUp v2) — mirrors clickup_triage_ops.py exactly
# ---------------------------------------------------------------------------

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
        print("CLICKUP_API_TOKEN not set in env", file=sys.stderr)
        sys.exit(2)
    return t


def _req(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": _token(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}", file=sys.stderr)
        return e.code, None
    except Exception as e:
        print(f"request error: {e!r}", file=sys.stderr)
        return 0, None


def _get_task(task_id: str):
    """Fetch a task and return a normalized dict (or None on error)."""
    st, t = _req("GET", f"/task/{task_id}")
    if not t:
        return None
    return {
        "id": t.get("id"),
        "name": t.get("name"),
        "status": (t.get("status") or {}).get("status"),
        "priority": ((t.get("priority") or {}) or {}).get("priority"),
        "tags": [tg.get("name") for tg in (t.get("tags") or [])],
        "list": ((t.get("list") or {}) or {}).get("name"),
        "list_id": ((t.get("list") or {}) or {}).get("id"),
        "url": t.get("url"),
        "description": t.get("description") or t.get("text_content") or "",
    }


# ---------------------------------------------------------------------------
# CLI subcommand handlers
# ---------------------------------------------------------------------------

def cmd_get(a):
    t = _get_task(a.task_id)
    if not t:
        return 1
    print(json.dumps(t, indent=2))
    return 0


def cmd_comment(a):
    with open(a.body, encoding="utf-8") as f:
        text = f.read()
    if DRY_RUN:
        print(f"[DRY_RUN] would POST comment to task {a.task_id}:\n{text[:200]}")
        return 0
    st, d = _req("POST", f"/task/{a.task_id}/comment",
                 {"comment_text": text, "notify_all": False})
    if st and 200 <= st < 300:
        print(f"comment posted id={d.get('id')}")
        return 0
    return 1


def cmd_set_status(a):
    if DRY_RUN:
        print(f"[DRY_RUN] would PUT status='{a.status}' on task {a.task_id}")
        return 0
    st, d = _req("PUT", f"/task/{a.task_id}", {"status": a.status})
    if st and 200 <= st < 300:
        print(f"status set to '{a.status}'")
        return 0
    return 1


def cmd_add_tag(a):
    if DRY_RUN:
        print(f"[DRY_RUN] would POST tag '{a.tag}' to task {a.task_id}")
        return 0
    name = urllib.parse.quote(a.tag)
    st, _ = _req("POST", f"/task/{a.task_id}/tag/{name}")
    if st and 200 <= st < 300:
        print(f"tag '{a.tag}' added")
        return 0
    return 1


def cmd_rm_tag(a):
    if DRY_RUN:
        print(f"[DRY_RUN] would DELETE tag '{a.tag}' from task {a.task_id}")
        return 0
    name = urllib.parse.quote(a.tag)
    st, _ = _req("DELETE", f"/task/{a.task_id}/tag/{name}")
    if st and 200 <= st < 300:
        print(f"tag '{a.tag}' removed")
        return 0
    return 1


def cmd_attempts(a):
    op = a.operation
    if op == "get":
        print(get_attempts(a.task_id))
    elif op == "inc":
        print(inc_attempts(a.task_id))
    elif op == "reset":
        reset_attempts(a.task_id)
        print(get_attempts(a.task_id))
    else:
        print(f"unknown attempts operation: {op}", file=sys.stderr)
        return 2
    return 0


def cmd_merge_pr(a):
    """Merge a PR via `gh` after enforcing the gates."""
    # Shadow is a GLOBAL kill switch — check it FIRST, before any per-PR gate, so
    # it short-circuits unconditionally (matches merge_guard.py's ordering and is
    # robust regardless of whether a verdict exists). Was previously checked after
    # the verdict gate, so a no-verdict PR refused for the "wrong" reason.
    if VALIDATE_SHADOW:
        print(
            "[VALIDATE_SHADOW] merge REFUSED: validator is in shadow mode; a "
            "live merge while ClickUp writeback is muzzled would strand the task.",
            file=sys.stderr,
        )
        return 1

    allowlist = load_allowlist()
    if not repo_allowed(a.repo, allowlist):
        print(
            f"ERROR: repo '{a.repo}' is not in the allowlist "
            f"({ALLOWLIST_PATH}). Merge blocked.",
            file=sys.stderr,
        )
        return 1

    # Verdict gate: a fresh PASS verdict for THIS PR is required (same precondition
    # merge_guard.py enforces on the tool-call path). Default-deny.
    if validator_verdict is None:
        print("ERROR: verdict store unavailable; refusing merge (fail-closed).",
              file=sys.stderr)
        return 1
    # CI gate (defense-in-depth; mirrors autonomous_merge.evaluate so a DIRECT
    # `merge-pr` invocation cannot bypass it). A fresh PASS verdict is necessary
    # but NOT sufficient: never merge a PR with a failing/pending gating check,
    # and never merge one with NO green gating check at all (fail-closed — a repo
    # whose PRs run only non-gating checks (Vercel/preview/smoke) would otherwise
    # merge UNVERIFIED, violating the "CI green" guardrail). Single source of
    # truth: autonomous_merge._pr_state (same dir, no circular import).
    try:
        if __package__:
            from . import autonomous_merge as _am
        else:
            import autonomous_merge as _am
        ci_info, ci_err = _am._pr_state(a.repo, a.pr_number)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: CI gate could not evaluate checks ({e!r}); refusing merge "
              f"(fail-closed).", file=sys.stderr)
        return 1
    if ci_err or ci_info is None:
        print(f"ERROR: CI gate could not read PR checks ({ci_err}); refusing merge "
              f"(fail-closed).", file=sys.stderr)
        return 1
    # The authoritative SQLite verdict is candidate-bound.  Do not accept a
    # PASS until GitHub has supplied the exact current head it must match.
    ok, why = validator_verdict.is_pass_fresh(
        a.repo, a.pr_number, (ci_info.get("head") or "")
    )
    if not ok:
        print(f"ERROR: validator verdict gate refuses merge of {a.repo}#{a.pr_number}: "
              f"{why}", file=sys.stderr)
        return 1
    if ci_info.get("failing"):
        print(f"ERROR: gating checks FAILING on {a.repo}#{a.pr_number}: "
              f"{', '.join(ci_info['failing'][:5])}. Merge blocked.", file=sys.stderr)
        return 1
    if ci_info.get("pending"):
        print(f"ERROR: gating checks still PENDING on {a.repo}#{a.pr_number}: "
              f"{', '.join(ci_info['pending'][:5])}. Merge blocked.", file=sys.stderr)
        return 1
    if not ci_info.get("gating_green"):
        print(f"ERROR: NO green gating CI check on {a.repo}#{a.pr_number} — refusing "
              f"to merge unverified (fail-closed; configure a code-gate workflow on "
              f"pull_request). non-gating checks: "
              f"{', '.join(ci_info.get('ignored', [])[:5]) or 'none'}", file=sys.stderr)
        return 1

    merge_flag = "--squash" if a.squash else "--merge"
    cmd = ["gh", "pr", "merge", str(a.pr_number),
           "--repo", a.repo, merge_flag, "--delete-branch"]

    if DRY_RUN:
        print(f"[DRY_RUN] would run: {' '.join(cmd)}")
        return 0

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0:
            print(result.stderr.rstrip()[:400], file=sys.stderr)
        return result.returncode
    except Exception as e:
        print(f"gh merge error: {e!r}", file=sys.stderr)
        return 1


def cmd_override_block(a):
    """Refuse the retired JSON override path.

    A terminal SQLite verdict is immutable for its identity; changing it needs a
    new PR head and a new fenced review, not a direct state-file mutation.
    """
    print(
        "ERROR: override-block is retired. Immutable SQLite verdicts require a new "
        "head SHA and fenced re-validation (fail-closed).",
        file=sys.stderr,
    )
    return 1


def cmd_escalate(a):
    """Fetch task, build an escalation message, post to Colin's Slack."""
    with open(a.reason, encoding="utf-8") as f:
        reason_text = f.read().strip()

    # Fetch task name and URL
    task = _get_task(a.task_id)
    if not task:
        print(f"ERROR: could not fetch task {a.task_id}", file=sys.stderr)
        return 1

    task_name = task.get("name") or a.task_id
    task_url = task.get("url") or f"https://app.clickup.com/t/{a.task_id}"

    attempts = get_attempts(a.task_id)
    msg = escalation_message(
        task_name=task_name,
        task_url=task_url,
        reason=reason_text,
        attempts=attempts,
    )

    channel = SLACK_ESCALATION_CHANNEL
    if not channel:
        print(
            "ERROR: SLACK_ESCALATION_CHANNEL env var is not set. "
            "Set it to e.g. 'slack:hermes' to enable escalation.",
            file=sys.stderr,
        )
        return 2

    if DRY_RUN:
        print(f"[DRY_RUN] would send to {channel}:\n{msg}")
        return 0

    try:
        result = subprocess.run(
            ["hermes", "send", "--to", channel, "--quiet", msg],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"slack send failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        else:
            print(f"escalation sent to {channel}")
        return result.returncode
    except Exception as e:
        print(f"slack send error: {e!r}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Hermes validate ops — sanctioned write-side operations for the AI review daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    # get
    g = sub.add_parser("get", help="GET task; print JSON to stdout")
    g.add_argument("task_id"); g.set_defaults(fn=cmd_get)

    # comment
    cm = sub.add_parser("comment", help="POST a comment (body read from file)")
    cm.add_argument("task_id")
    cm.add_argument("--body", required=True, metavar="FILE",
                    help="path to file containing comment text")
    cm.set_defaults(fn=cmd_comment)

    # set-status
    ss = sub.add_parser("set-status", help="PUT task status")
    ss.add_argument("task_id"); ss.add_argument("status")
    ss.set_defaults(fn=cmd_set_status)

    # add-tag
    at = sub.add_parser("add-tag", help="POST add tag to task")
    at.add_argument("task_id"); at.add_argument("tag")
    at.set_defaults(fn=cmd_add_tag)

    # rm-tag
    rt = sub.add_parser("rm-tag", help="DELETE tag from task")
    rt.add_argument("task_id"); rt.add_argument("tag")
    rt.set_defaults(fn=cmd_rm_tag)

    # attempts
    att = sub.add_parser("attempts",
                         help="Manage fix-attempt ledger: get|inc|reset <task_id>")
    att.add_argument("operation", choices=["get", "inc", "reset"])
    att.add_argument("task_id")
    att.set_defaults(fn=cmd_attempts)

    # merge-pr
    mp = sub.add_parser("merge-pr",
                        help="Merge a PR via gh (allowlist-gated)")
    mp.add_argument("repo", help="owner/repo")
    mp.add_argument("pr_number", type=int)
    mp.add_argument("--squash", action="store_true",
                    help="squash merge (default: merge commit)")
    mp.set_defaults(fn=cmd_merge_pr)

    # Retained as an explicit fail-closed diagnostic for old operators. A
    # terminal TrustStore verdict is immutable and cannot be JSON-overridden.
    ob = sub.add_parser("override-block",
                        help="Retired: immutable SQLite verdicts cannot be overridden")
    ob.add_argument("spec", metavar="REPO#PR",
                    help="owner/repo#pr_number — e.g. ignitemarketing/site#42")
    ob.add_argument("--reason", default="", metavar="TEXT",
                    help="plain-language reason this was a false block (required for audit)")
    ob.set_defaults(fn=cmd_override_block)

    # escalate
    esc = sub.add_parser("escalate",
                         help="Fetch task, build escalation message, post to Slack")
    esc.add_argument("task_id")
    esc.add_argument("--reason", required=True, metavar="FILE",
                     help="path to file containing plain-language reason")
    esc.set_defaults(fn=cmd_escalate)

    args = p.parse_args()

    if not args.cmd:
        p.print_help()
        sys.exit(0)

    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
