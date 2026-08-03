#!/usr/bin/env python3
"""manual_platform_handoff.py — executor handoff policy for platform=manual repos.

THE GAP THIS FILLS
------------------
`ignite-ship` classifies some repos as `PLATFORM=manual` / `DEPLOY_ON_PUSH=false`
— `hermes-agent` (and its contrib fork) is the canonical example: production is a
launchd service on a physical Mac mini, deployed by the release-cut script, never
by merging to `main` (see `docs/deploy/hermes-agent-ignite-ship-exemption.md`).

On 2026-08-03 two governed executor runs (PR #304, PR #306) finished real,
CI-green work and then parked their ClickUp tasks in `in progress` with an
"ignite- BLOCKED HANDOFF" comment, because the executor treated "I could not
deploy" as "I could not finish". On a manual-platform repo the executor can
*never* deploy — the deploy is operator/poller gated — so that handoff stalls
forever and the validator never sees finished work.

THE POLICY (this module is its enforcement)
-------------------------------------------
For a repo whose deploy platform is `manual`, the executor's complete deliverable
is a **CI-green PR**, not a deploy. Such a task belongs in **`in review` with a
review packet** that says plainly that deploy is operator/poller gated, so the
validator judges the PR now and the post-cut behavior after the next release cut.

This actor is the deterministic, zero-LLM enforcement of that policy — the same
shape as `closeout_actor.py`, which exists because `autonomous_merge.py` merges
but never advances the task. The two are disjoint by construction:

    closeout_actor        MERGED PR + validator PASS   -> in review
    manual_platform_handoff   OPEN CI-green PR on a manual-platform repo -> in review

SAFETY (why this cannot flip work out from under a running executor)
--------------------------------------------------------------------
Every one of these must hold before a single write happens:

1. The repo is classified `manual` AND is on `~/.hermes/allowed-repos.txt`.
   A missing allowlist yields an empty repo set — fail-closed, not fail-open.
2. The PR is OPEN and its body/branch links exactly one ClickUp task id.
3. CI is green: at least one check, none failing, none pending.
4. CI settled at least `--min-idle-minutes` ago (default 10).
5. `claim_store` reports NO live claim on the task — a live claim means an
   executor still owns the task and we never touch it.
6. The task is in an advanceable in-flight status (`in progress` / `to do`).
   Anything already review-class or complete-class is a silent idempotent no-op.
7. The newest `ignite-validate:` marker on the task is not FAIL/BLOCK.

Writes are ordered **packet first, then status flip**: the acceptance contract is
"In Review *with* a review packet", so a task must never appear in the validator's
queue without its packet. If the packet fails to post, the flip is skipped and the
task stays exactly where it was. After the flip we do a ClickUp read-after-write
confirmation through `report_activity_journal.confirm_transition`.

The status write goes through the GUARDED `clickup.mjs status` path, which
re-enforces G1 (Hermes never sets `complete`), G2 (never advance over a standing
FAIL) and G3 (no fabricated authority in comments). No `CLICKUP_ALLOW_*` override
is ever set.

`sweep()` never raises. Every skip is logged with its reason.

CLI:
  manual_platform_handoff.py --dry-run          # report, touch nothing
  manual_platform_handoff.py                    # live
  manual_platform_handoff.py --repo owner/name  # restrict to one repo
  manual_platform_handoff.py --task TASKID      # restrict to one task
  manual_platform_handoff.py --list-repos       # print the manual-platform set
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_BIN_DIR = os.path.expanduser("~/.hermes/bin")
PY = os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11")
CLICKUP_MJS = os.path.expanduser("~/dev/ignite-skills-live/skills/clickup/clickup.mjs")
POST_CLICKUP_COMMENT = os.path.expanduser("~/.hermes/scripts/post_clickup_comment.py")
ALLOWLIST_PATH = os.path.expanduser("~/.hermes/allowed-repos.txt")
REPO_ALIASES_PATH = os.path.expanduser("~/.hermes/config/repo-aliases.json")

# `ignite-ship`'s platform classifier is the single source of truth for which
# repos are PLATFORM=manual. It lives in the ignite-skills checkout the fleet
# already refreshes every 3h. We read it when present.
PLATFORM_HINTS_PATH = os.path.expanduser(
    "~/dev/ignite-skills-live/skills/ignite-ship/references/platform-hints.json"
)
# Pinned floor for when the hints file is missing/unreadable on this host. These
# repos are manual by construction (documented in
# docs/deploy/hermes-agent-ignite-ship-exemption.md) and the policy must hold
# even if the skills checkout has not been pulled yet. Bare repo names: the hints
# file is keyed by bare name, and the contrib fork shares hermes-agent's runtime.
BASELINE_MANUAL_REPOS = frozenset({
    "hermes-agent",
    "hermes-agent-upstream-contrib",
})

TERMINAL_STATUS = "in review"
ADVANCEABLE = {"in progress", "in-progress", "to do", "to-do"}

GH_TIMEOUT = 90
NODE_TIMEOUT = 90
PR_LIST_LIMIT = 50
DEFAULT_MIN_IDLE_MINUTES = 10

# CI conclusions that do not block a handoff. NEUTRAL/SKIPPED are non-verdicts,
# not passes we are inventing.
_CI_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_CI_PENDING = {"", "PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"}

# A ClickUp v2 task id is a short lowercase alnum token. Only matched in anchored
# contexts (a clickup.com URL, an explicit "ClickUp task" label, or an
# agent/ignite branch prefix) so ordinary prose can never masquerade as one.
_TASK_TOKEN = r"[a-z0-9]{6,16}"
_TASK_PATTERNS = (
    re.compile(r"clickup\.com/t/(" + _TASK_TOKEN + r")", re.IGNORECASE),
    re.compile(r"[Cc]lick[Uu]p task:?\s*\[?(" + _TASK_TOKEN + r")\]?", re.IGNORECASE),
    re.compile(r"[Tt]ask[ _-]?[Ii][Dd]:?\s*\[?(" + _TASK_TOKEN + r")\]?"),
)
_BRANCH_PATTERNS = (
    re.compile(r"(?:^|/)agent/(" + _TASK_TOKEN + r")$", re.IGNORECASE),
    re.compile(r"(?:^|/)ignite-\s*(" + _TASK_TOKEN + r")(?:[-/_]|$)", re.IGNORECASE),
)


class HandoffError(RuntimeError):
    """A handoff precondition could not be evaluated."""


# --------------------------------------------------------------------------
# Optional mini-only collaborators. This module must import cleanly in a bare
# checkout (CI runs its tests), so every mini-only dependency is lazy and
# degrades to an explicit, logged no-op rather than an ImportError.
# --------------------------------------------------------------------------

def _optional(name):
    if SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, SCRIPTS_DIR)
    try:
        return __import__(name)
    except Exception:
        return None


@contextlib.contextmanager
def _null_lock(task_id):
    yield True


def _lock_factory(task_lock=None):
    """Return a `task_id -> context manager yielding acquired: bool`."""
    module = task_lock if task_lock is not None else _optional("task_action_lock")
    if module is not None and hasattr(module, "task_lock"):
        return module.task_lock
    return _null_lock


def _shim_env():
    env = dict(os.environ)
    env["PATH"] = HERMES_BIN_DIR + os.pathsep + env.get("PATH", "")
    return env


# --------------------------------------------------------------------------
# Platform classification
# --------------------------------------------------------------------------

def _bare_name(repo):
    return str(repo or "").strip().rstrip("/").split("/")[-1].lower()


def load_manual_platform_repos(hints_path=PLATFORM_HINTS_PATH,
                               baseline=BASELINE_MANUAL_REPOS):
    """Bare repo names whose deploy platform is `manual`.

    ignite-ship's `platform-hints.json` is authoritative when readable; the
    pinned baseline is a floor, never an override, so a hint that reclassifies a
    baseline repo away from `manual` still cannot silently disable the policy for
    a repo that has no deploy pipeline at all.
    """
    names = {_bare_name(n) for n in baseline if _bare_name(n)}
    try:
        with open(hints_path, encoding="utf-8") as fh:
            hints = json.load(fh)
    except Exception:
        return frozenset(names)
    entries = hints.get("repos") if isinstance(hints, dict) and "repos" in hints else hints
    if not isinstance(entries, dict):
        return frozenset(names)
    for key, value in entries.items():
        platform = ""
        if isinstance(value, str):
            platform = value
        elif isinstance(value, dict):
            platform = value.get("platform") or value.get("PLATFORM") or ""
        if str(platform).strip().lower() == "manual":
            bare = _bare_name(key)
            if bare:
                names.add(bare)
    return frozenset(names)


def _expand_repo_aliases(names, path=REPO_ALIASES_PATH):
    """Same tolerant behavior as closeout_actor._expand_repo_aliases."""
    try:
        with open(path, encoding="utf-8") as fh:
            groups = json.load(fh).get("aliases") or []
    except Exception:
        return set(names)
    expanded = set(names)
    for group in groups:
        if isinstance(group, list) and expanded.intersection(group):
            expanded.update(group)
    return expanded


def load_allowlist(path=ALLOWLIST_PATH):
    """owner/repo entries Hermes may act on. Absent file => empty (fail-closed)."""
    result = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s and not s.startswith("#"):
                    result.add(s)
    except OSError:
        return set()
    return _expand_repo_aliases(result)


def manual_platform_targets(allowlist=None, manual_names=None):
    """The allowlisted owner/repo values whose platform is `manual`, sorted."""
    allow = load_allowlist() if allowlist is None else set(allowlist)
    manual = load_manual_platform_repos() if manual_names is None else set(manual_names)
    return sorted(r for r in allow if _bare_name(r) in manual)


# --------------------------------------------------------------------------
# Pure decision helpers (no network) — these carry the policy and are the
# surface the tests exercise directly.
# --------------------------------------------------------------------------

def extract_task_id(body="", head_ref="", title=""):
    """The single ClickUp task id this PR is for, or "" when ambiguous/absent.

    Ambiguity is refused, not guessed: if the PR references two different task
    ids we return "" and the caller skips, because handing the wrong task to
    review is worse than leaving it in progress.
    """
    found = []
    for text in (body or "", title or ""):
        for pattern in _TASK_PATTERNS:
            found.extend(m.lower() for m in pattern.findall(text))
    if not found:
        for pattern in _BRANCH_PATTERNS:
            found.extend(m.lower() for m in pattern.findall(head_ref or ""))
    unique = sorted(set(found))
    if len(unique) != 1:
        return ""
    return unique[0]


def _parse_ts(value):
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def summarize_ci(rollup):
    """Reduce a `gh` statusCheckRollup to {state, total, failing, pending, settled_at}.

    state is one of "green" / "pending" / "failing" / "none". "none" (no checks
    at all) is deliberately NOT green: a repo with no CI gives us no evidence the
    work is sound, and this actor only ever advances on evidence.
    """
    failing, pending, settled = [], [], None
    total = 0
    for entry in rollup or []:
        if not isinstance(entry, dict):
            continue
        total += 1
        name = entry.get("name") or entry.get("context") or "check"
        if entry.get("__typename") == "StatusContext" or "state" in entry:
            state = str(entry.get("state") or "").upper()
            conclusion = state
            done = state not in _CI_PENDING
            when = entry.get("createdAt") or entry.get("startedAt")
        else:
            status = str(entry.get("status") or "").upper()
            conclusion = str(entry.get("conclusion") or "").upper()
            done = status == "COMPLETED"
            when = entry.get("completedAt") or entry.get("startedAt")
        if not done or conclusion in _CI_PENDING:
            pending.append(name)
            continue
        if conclusion not in _CI_OK:
            failing.append(name)
            continue
        stamp = _parse_ts(when)
        if stamp and (settled is None or stamp > settled):
            settled = stamp
    if total == 0:
        state = "none"
    elif failing:
        state = "failing"
    elif pending:
        state = "pending"
    else:
        state = "green"
    return {"state": state, "total": total, "failing": sorted(failing),
            "pending": sorted(pending), "settled_at": settled}


def evaluate(pr, task, *, ci, claim_live, now=None,
             min_idle_seconds=DEFAULT_MIN_IDLE_MINUTES * 60,
             latest_validate_verdict=None):
    """Decide one PR/task pair. Returns (action, detail).

    action is "handoff" | "skip" | "blocked". Pure: every input is already
    fetched by the caller, so the whole policy is unit-testable offline.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    state = str(pr.get("state") or "").upper()
    if state and state != "OPEN":
        return "skip", f"PR state is {state}, not OPEN (merged PRs are closeout_actor's job)"

    if ci["state"] == "failing":
        return "skip", f"CI failing: {', '.join(ci['failing'][:5])}"
    if ci["state"] == "pending":
        return "skip", f"CI still running: {', '.join(ci['pending'][:5])}"
    if ci["state"] == "none":
        return "skip", "PR has no CI checks — no evidence to hand to review"

    settled = ci.get("settled_at")
    if settled is None:
        return "skip", "CI green but no completion timestamp — cannot prove the run settled"
    idle = (now - settled).total_seconds()
    if idle < min_idle_seconds:
        return "skip", (f"CI settled {int(idle)}s ago, under the "
                        f"{int(min_idle_seconds)}s idle floor — executor may still be working")

    if claim_live:
        return "skip", "task has a live executor claim — leaving it to its owner"

    status = str(((task or {}).get("status") or {}).get("status") or "").strip().lower()
    if not status:
        return "skip", "task status unreadable"
    if status not in ADVANCEABLE:
        return "skip", f"status '{status}' is not an advanceable in-flight status"

    if latest_validate_verdict:
        return "blocked", (f"newest ignite-validate marker is "
                           f"{str(latest_validate_verdict).upper()}; refusing handoff")

    return "handoff", (f"platform=manual, CI green ({ci['total']} checks, settled "
                       f"{settled.isoformat()}), no live claim, status '{status}' "
                       f"-> '{TERMINAL_STATUS}'")


def build_review_packet(repo, pr, *, ci, task_id, blocked_marker=False,
                        pr_title="", now=None):
    """The review packet that must accompany every manual-platform handoff.

    Deliberately states what the validator can judge NOW versus what can only be
    judged after the operator/poller release cut, so a validator never treats
    "not deployed yet" as incomplete work. Wording avoids any claim of delegated
    human authority (G3)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    settled = ci.get("settled_at")
    pr_url = f"https://github.com/{repo}/pull/{pr}"
    lines = [
        "ignite- REVIEW HANDOFF (platform=manual)",
        "",
        f"Outcome: {repo}#{pr} is open and CI-green; task {task_id} moved to "
        f"'{TERMINAL_STATUS}'.",
        f"PR: {pr_url}" + (f" — {pr_title.strip()[:120]}" if pr_title else ""),
        f"CI: {ci['total']} check(s) green"
        + (f", settled {settled.isoformat()}" if settled else ""),
        "",
        "Why this is a complete executor deliverable:",
        f"- `ignite-ship` classifies {repo} as PLATFORM=manual / DEPLOY_ON_PUSH=false.",
        "- There is no pipeline the executor can drive: production runs from a",
        "  frozen release snapshot on the Mac mini and only changes when the",
        "  release cut repoints `runtime-current`.",
        "- Deploy is therefore operator/poller gated, not part of this task's",
        "  executor scope. A CI-green PR is the whole deliverable.",
        "",
        "What to validate now:",
        "- The PR diff against the task's acceptance criteria.",
        "- Tests added/changed and the green CI run linked above.",
        "",
        "What to validate after the next release cut:",
        "- Post-cut runtime behavior on the mini (the change is inert until then).",
        "- Reference: docs/deploy/hermes-agent-ignite-ship-exemption.md and",
        "  scripts/mini-release-cut.sh.",
        "",
        f"Handoff source: manual_platform_handoff.py at {now.isoformat()}",
    ]
    if blocked_marker:
        lines.insert(
            5,
            "Note: an earlier run left a BLOCKED HANDOFF comment on this task for "
            "an undeployable-platform reason; this packet supersedes it.",
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# I/O adapters (thin, replaceable in tests)
# --------------------------------------------------------------------------

def _gh_pr_list(repo, limit=PR_LIST_LIMIT):
    fields = "number,title,body,headRefName,state,updatedAt,statusCheckRollup"
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--limit", str(limit), "--json", fields],
            capture_output=True, text=True, timeout=GH_TIMEOUT, env=_shim_env())
        if r.returncode != 0:
            raise HandoffError(f"gh pr list rc={r.returncode}: {r.stderr.strip()[:160]}")
        return json.loads(r.stdout or "[]")
    except HandoffError:
        raise
    except Exception as exc:
        raise HandoffError(f"gh pr list error: {exc!r}") from exc


def _cu_json(args):
    try:
        r = subprocess.run([CLICKUP_MJS, *args], capture_output=True, text=True,
                           timeout=NODE_TIMEOUT, env=_shim_env())
        if r.returncode != 0:
            raise HandoffError(f"clickup.mjs {args[0]} rc={r.returncode}: "
                               f"{(r.stderr or '').strip()[:160]}")
        out = (r.stdout or "").strip()
        if not out:
            raise HandoffError(f"clickup.mjs {args[0]} returned no output")
        return json.loads(out)
    except HandoffError:
        raise
    except Exception as exc:
        raise HandoffError(f"clickup.mjs {args[0]} error: {exc!r}") from exc


def _fetch_task(task_id):
    return _cu_json(["task", task_id, "--json"])


def _fetch_comments(task_id):
    try:
        data = _cu_json(["comments", task_id, "--json"])
    except HandoffError:
        return []
    if isinstance(data, dict):
        return data.get("comments") or []
    return data if isinstance(data, list) else []


def _set_status(task_id, status):
    try:
        r = subprocess.run([CLICKUP_MJS, "status", task_id, status],
                           capture_output=True, text=True, timeout=NODE_TIMEOUT,
                           env=_shim_env())
        out = ((r.stdout or "") + (r.stderr or "")).strip()[:600]
        return r.returncode == 0, out
    except Exception as exc:
        return False, f"clickup.mjs status error: {exc!r}"


def _post_comment(task_id, body):
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                      prefix="manual_platform_handoff_",
                                      delete=False, encoding="utf-8")
    try:
        tmp.write(body)
        tmp.close()
        r = subprocess.run([PY, POST_CLICKUP_COMMENT, task_id, tmp.name],
                           capture_output=True, text=True, timeout=NODE_TIMEOUT,
                           env=_shim_env())
        out = ((r.stdout or "") + (r.stderr or "")).strip()[:600]
        return r.returncode == 0, out
    except Exception as exc:
        return False, f"guarded comment error: {exc!r}"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)


def _claim_is_live(task_id):
    """True when an executor still owns the task. Unknown => True (fail-closed):
    if we cannot prove the task is unowned we do not touch it."""
    module = _optional("claim_store")
    if module is None or not hasattr(module, "is_claimed"):
        return True
    try:
        return bool(module.is_claimed(task_id))
    except Exception:
        return True


def _negative_validate_verdict(comments):
    """The newest ignite-validate marker when it is FAIL/BLOCK, else ""."""
    guard = _optional("clickup_status_guard")
    if guard is None or not hasattr(guard, "latest_validate_verdict"):
        return ""
    try:
        latest = guard.latest_validate_verdict(comments)
        if latest and guard.NEGATIVE_VERDICTS.match(latest["verdict"]):
            return latest["verdict"]
    except Exception:
        return ""
    return ""


def _has_blocked_handoff_marker(comments):
    for comment in comments or []:
        text = ""
        if isinstance(comment, dict):
            text = str(comment.get("comment_text") or comment.get("text") or "")
        elif isinstance(comment, str):
            text = comment
        if "BLOCKED HANDOFF" in text.upper():
            return True
    return False


def _confirm(task_id):
    journal = _optional("report_activity_journal")
    if journal is None:
        return {"status": "UNKNOWN", "confirmed": False,
                "error": "report_activity_journal unavailable"}
    try:
        return journal.confirm_transition(
            kind="review_handoff",
            task_id=task_id,
            source="manual-platform-handoff",
            expected_status=TERMINAL_STATUS,
            run_id=os.environ.get("HERMES_EXECUTOR_RUN_ID") or None,
            execution_id=os.environ.get("HERMES_EXECUTION_ID") or None,
            fetch_task=_fetch_task,
        )
    except Exception as exc:
        return {"status": "UNKNOWN", "confirmed": False, "error": repr(exc)}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _do_handoff(repo, pr, task_id, packet):
    """Post the review packet, then flip. Packet first is deliberate: a task must
    never reach the validator's queue in review WITHOUT its packet."""
    ok, out = _post_comment(task_id, packet)
    if not ok:
        return False, f"review packet failed to post; status left untouched: {out}"
    ok, out = _set_status(task_id, TERMINAL_STATUS)
    if not ok:
        return False, f"status flip refused/failed after packet posted: {out}"
    confirmation = _confirm(task_id)
    if not confirmation.get("confirmed"):
        return False, ("status writer returned success but ClickUp read-after-write "
                       f"could not verify '{TERMINAL_STATUS}': "
                       f"{confirmation.get('error', 'unknown')}")
    note = ""
    if confirmation.get("status") != "ok":
        note = (f"; report activity health UNKNOWN: "
                f"{confirmation.get('error', 'append failed')}")
    return True, f"handed off to '{TERMINAL_STATUS}' with review packet" + note


def sweep(dry_run=False, only_repo=None, only_task=None,
          min_idle_minutes=DEFAULT_MIN_IDLE_MINUTES, task_lock=None,
          targets=None, now=None):
    """Sweep every manual-platform repo. Returns result dicts. Never raises."""
    results = []
    lock_for = _lock_factory(task_lock)
    min_idle_seconds = max(0, int(min_idle_minutes)) * 60
    try:
        repos = manual_platform_targets() if targets is None else list(targets)
        if only_repo:
            repos = [r for r in repos if r == only_repo]
            if not repos:
                results.append({"repo": only_repo, "pr": None, "task_id": only_task or "",
                                "action": "skip",
                                "detail": "repo is not an allowlisted manual-platform repo"})
                return results
        for repo in repos:
            try:
                prs = _gh_pr_list(repo)
            except HandoffError as exc:
                results.append({"repo": repo, "pr": None, "task_id": "",
                                "action": "error", "detail": str(exc)})
                continue
            for pr in prs:
                number = pr.get("number")
                rec = {"repo": repo, "pr": number, "task_id": "", "action": "skip",
                       "detail": ""}
                try:
                    task_id = extract_task_id(pr.get("body") or "",
                                              pr.get("headRefName") or "",
                                              pr.get("title") or "")
                    if only_task and task_id != only_task:
                        continue
                    rec["task_id"] = task_id
                    if not task_id:
                        rec["detail"] = "no single ClickUp task id in PR body/branch/title"
                        results.append(rec)
                        continue
                    ci = summarize_ci(pr.get("statusCheckRollup"))
                    with lock_for(task_id) as locked:
                        if not locked:
                            rec["detail"] = "task lock busy — another actor owns this task"
                            results.append(rec)
                            continue
                        try:
                            task = _fetch_task(task_id)
                        except HandoffError as exc:
                            rec["action"] = "error"
                            rec["detail"] = f"could not read task: {exc}"
                            results.append(rec)
                            continue
                        comments = _fetch_comments(task_id)
                        action, detail = evaluate(
                            pr, task, ci=ci,
                            claim_live=_claim_is_live(task_id),
                            now=now, min_idle_seconds=min_idle_seconds,
                            latest_validate_verdict=_negative_validate_verdict(comments),
                        )
                        rec["action"] = action
                        rec["detail"] = detail
                        if action == "handoff" and not dry_run:
                            packet = build_review_packet(
                                repo, number, ci=ci, task_id=task_id,
                                blocked_marker=_has_blocked_handoff_marker(comments),
                                pr_title=pr.get("title") or "", now=now)
                            ok, msg = _do_handoff(repo, number, task_id, packet)
                            rec["handoff_ok"] = ok
                            rec["handoff_out"] = msg
                        results.append(rec)
                except Exception as exc:  # one bad PR never stops the sweep
                    rec["action"] = "error"
                    rec["detail"] = repr(exc)
                    results.append(rec)
    except Exception as exc:
        results.append({"repo": "*", "pr": None, "task_id": "", "action": "error",
                        "detail": f"sweep fatal: {exc!r}"})
    return results


def print_results(results, dry_run=False, prefix="manual-handoff"):
    handed = 0
    would = 0
    for rec in results:
        action = rec.get("action")
        if action == "handoff":
            if dry_run:
                tag, would = "WOULD-HANDOFF", would + 1
            elif rec.get("handoff_ok"):
                tag, handed = "HANDED-OFF", handed + 1
            else:
                tag = "HANDOFF-FAILED"
        elif action == "blocked":
            tag = "BLOCKED"
        elif action == "error":
            tag = "ERROR"
        else:
            tag = "SKIP"
            # keep cron output quiet: only surface skips that reached a task
            if not rec.get("task_id"):
                continue
        print(f"[{prefix}] {tag:15} {rec.get('repo')}#{rec.get('pr')} "
              f"task={rec.get('task_id') or '-'} — {rec.get('detail')}")
        if rec.get("handoff_out") and not rec.get("handoff_ok"):
            print(f"            {rec['handoff_out']}")
    return handed, would


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Hand CI-green PRs on platform=manual repos to review.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would hand off; touch nothing")
    parser.add_argument("--repo", help="restrict to one owner/repo")
    parser.add_argument("--task", help="restrict to one ClickUp task id")
    parser.add_argument("--min-idle-minutes", type=int, default=DEFAULT_MIN_IDLE_MINUTES,
                        help="minimum minutes since CI settled (default: %(default)s)")
    parser.add_argument("--list-repos", action="store_true",
                        help="print the allowlisted manual-platform repos and exit")
    args = parser.parse_args(argv)

    if args.list_repos:
        for repo in manual_platform_targets():
            print(repo)
        return 0

    results = sweep(dry_run=args.dry_run, only_repo=args.repo, only_task=args.task,
                    min_idle_minutes=args.min_idle_minutes)
    handed, would = print_results(results, dry_run=args.dry_run)
    print(json.dumps({"handed_off": handed, "would_hand_off": would,
                      "total": len(results)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
