#!/usr/bin/env python3
"""Read-safe PR-pipeline helpers used by the Mini review cron jobs.

The ``backlog-status`` command is intentionally a recount only: it never
wakes a validator, validates a PR, merges, or writes ClickUp.  It exists so
operators can capture factual before/after backlog counts around another
pipeline action without an unbounded scan hiding in a cron log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from pr_wake_and_sweep import (
    ALERT_LOG_PATH,
    DEFAULT_ALLOWLIST,
    DEFAULT_MERGE_SCRIPT,
    DEFAULT_REPORT_PATH,
    GitHubClient,
    PullRequestState,
    build_rework_body,
    lacks_fresh_verdict,
    load_allowlist,
    notify,
    run_sweep,
    scan_repos,
    stale_without_verdict,
    utcnow,
)

SCRIPTS_DIR = Path.home() / ".hermes/scripts"
PYTHON = Path.home() / ".hermes/runtime-current/venv/bin/python3.11"
CLICKUP_OPS = SCRIPTS_DIR / "hermes_validate_ops.py"
JOBS_PATH = Path.home() / ".hermes/cron/jobs.json"
STATE_PATH = SCRIPTS_DIR / ".pr_pipeline_state.json"
VALIDATOR_JOB_NAME = "hermes-pr-validate"
WAKE_COOLDOWN_S = 20 * 60
DEFAULT_BACKLOG_REPO_TIMEOUT_S = 20.0
DEFAULT_BACKLOG_MAX_REPOS = 100
DEFAULT_CONFLICT_COMMENT_PAGE_CAP = 10
_TASK_RE = re.compile(r"clickup\.com/t/([a-z0-9]+)", re.IGNORECASE)
_TASK_MARKER_RE = re.compile(r"[Cc]lick[Uu]p task \[?([a-z0-9]+)\]?", re.IGNORECASE)
_TASK_ID_RE = re.compile(r"\b(86e[0-9a-z]{5,8})\b", re.IGNORECASE)


def _load_state() -> dict[str, Any]:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _extract_clickup_task_id(text: str | None) -> str:
    if not text:
        return ""
    for pattern in (_TASK_RE, _TASK_MARKER_RE, _TASK_ID_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _task_comment(task_id: str, body: str) -> bool:
    if not task_id:
        return False
    tmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".md", encoding="utf-8")
    try:
        tmp.write(body.rstrip() + "\n")
        tmp.close()
        result = subprocess.run(
            [str(PYTHON), str(CLICKUP_OPS), "comment", task_id, "--body", tmp.name],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[pr-pipeline] clickup comment failed for {task_id}: {exc}", file=sys.stderr)
        return False
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _task_tag(task_id: str, action: str, tag: str) -> bool:
    try:
        result = subprocess.run(
            [str(PYTHON), str(CLICKUP_OPS), action, task_id, tag],
            capture_output=True, text=True, timeout=60,
        )
        return bool(task_id) and result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[pr-pipeline] clickup {action} failed for {task_id}: {exc}", file=sys.stderr)
        return False


def _set_status(task_id: str, status: str) -> bool:
    try:
        result = subprocess.run(
            [str(PYTHON), str(CLICKUP_OPS), "set-status", task_id, status],
            capture_output=True, text=True, timeout=60,
        )
        return bool(task_id) and result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[pr-pipeline] clickup set-status failed for {task_id}: {exc}", file=sys.stderr)
        return False


def _validator_job_id() -> str:
    try:
        data = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for job in data.get("jobs", []):
        if job.get("name") == VALIDATOR_JOB_NAME:
            return str(job.get("id") or "")
    return ""


def _wake_validator(reason: str) -> bool:
    job_id = _validator_job_id()
    if not job_id:
        print(f"[pr-pipeline] {VALIDATOR_JOB_NAME!r} not found in jobs.json", file=sys.stderr)
        return False
    try:
        result = subprocess.run(
            [shutil.which("hermes") or str(Path.home() / ".local/bin/hermes"), "cron", "run", job_id],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[pr-pipeline] validator wake failed ({reason}): {exc}", file=sys.stderr)
        return False
    if result.returncode:
        print(f"[pr-pipeline] validator wake failed ({reason}): {(result.stderr or result.stdout).strip()[:240]}", file=sys.stderr)
        return False
    return True


def wake_validator_if_needed(allowlist: list[str], dry_run: bool = False) -> tuple[int, list[dict[str, Any]]]:
    """Preserve the event-driven wake entry point used by the cron shim."""
    now, errors, gh = utcnow(), [], GitHubClient()
    states, _ = scan_repos(allowlist, now, gh, errors)
    candidates = [state for state in states if lacks_fresh_verdict(state.latest_verdict_at, now)]
    details = [{"repo": state.repo, "pr": state.number, "url": state.url,
                "merge_state": state.mergeable_state,
                "age_hours": round((now - state.created_at).total_seconds() / 3600.0, 2)} for state in candidates]
    if not candidates or dry_run:
        return len(candidates), details
    state = _load_state()
    if now.timestamp() - float(state.get("last_validator_wake_ts", 0) or 0) < WAKE_COOLDOWN_S:
        return len(candidates), details
    if _wake_validator(f"{len(candidates)} candidate(s) without fresh verdict"):
        state["last_validator_wake_ts"] = now.timestamp()
        _save_state(state)
    return len(candidates), details


def _clickup_token() -> str:
    try:
        from agent import lazy_secret_resolver
        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None
    return (value or os.environ.get("CLICKUP_API_TOKEN", "")).strip()


def _task_comment_page(task_id: str, start: str | None = None, start_id: str | None = None) -> dict[str, Any] | None:
    """Read one bounded ClickUp task-comment page, or ``None`` on error."""
    token = _clickup_token()
    if not task_id or not token:
        return None
    # ClickUp's initial request must not carry an empty cursor.  Once paging
    # begins both cursor values are required so a repeated timestamp cannot
    # skip or repeat comments.
    query = urllib.parse.urlencode({"start": start, "start_id": start_id}) if start and start_id else ""
    request = urllib.request.Request(
        f"https://api.clickup.com/api/v2/task/{task_id}/comment" + (f"?{query}" if query else ""),
        headers={"Authorization": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"[pr-pipeline] cannot read ClickUp comments for {task_id}: {exc}", file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def _conflict_route_marker(repo: str, pr_number: int, head_sha: str) -> str:
    return f"pr-pipeline-conflict-route:{repo}#{pr_number}:{head_sha or 'unknown'}"


def _comment_text(comment: dict[str, Any]) -> str:
    return str(comment.get("comment_text") or comment.get("text") or comment.get("comment") or "")


def _has_durable_conflict_route(task_id: str, repo: str, pr_number: int, head_sha: str) -> bool | None:
    """Return whether a route marker exists; ``None`` is intentionally
    conservative (unknown), so callers must not repost a duplicate marker.

    ClickUp pages comments with a ``start``/``start_id`` cursor.  The page cap
    makes a pathological task bounded; inability to reach the end is unknown,
    not evidence that no marker exists.
    """
    marker = _conflict_route_marker(repo, pr_number, head_sha)
    start: str | None = None
    start_id: str | None = None
    for _ in range(DEFAULT_CONFLICT_COMMENT_PAGE_CAP):
        payload = _task_comment_page(task_id, start, start_id)
        if payload is None:
            return None
        comments = payload.get("comments")
        if not isinstance(comments, list):
            return None
        if any(marker in _comment_text(comment) for comment in comments if isinstance(comment, dict)):
            return True
        if payload.get("last_page") is True or not comments:
            return False
        cursor = comments[-1] if isinstance(comments[-1], dict) else {}
        next_start = cursor.get("date")
        next_start_id = cursor.get("id")
        if next_start is None or not next_start_id:
            return None
        start, start_id = str(next_start), str(next_start_id)
    print(f"[pr-pipeline] comment page cap reached for {task_id}; refusing duplicate route", file=sys.stderr)
    return None


def _run_bounded(operation: Callable[[], Any], timeout_s: float) -> Any:
    """Run one local scan with a hard Unix alarm; subprocesses are reaped by
    ``subprocess.run`` when the interrupt escapes.  Mini is Unix-only."""
    if timeout_s <= 0:
        raise ValueError("repo timeout must be greater than zero")
    if not hasattr(signal, "setitimer"):
        return operation()
    def expired(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"timed out after {timeout_s:g}s")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def count_unverdicted_mergeable_backlog(
    allowlist: list[str], *, repo_timeout_s: float = DEFAULT_BACKLOG_REPO_TIMEOUT_S,
    max_repos: int = DEFAULT_BACKLOG_MAX_REPOS, phase: str = "status",
    progress: Callable[[str], None] | None = None,
    scan: Callable[[list[str], Any, Any, list[str]], tuple[list[PullRequestState], int]] = scan_repos,
    github_factory: Callable[[], GitHubClient] = GitHubClient,
) -> dict[str, Any]:
    """Return a bounded, read-only count of open mergeable PRs with no verdict.

    ``phase`` is informational (normally ``before`` or ``after``) so callers
    can take two factual recounts around an independent action without this
    function ever triggering that action.
    """
    if max_repos <= 0:
        raise ValueError("max repos must be greater than zero")
    selected = list(allowlist)[:max_repos]
    emit = progress or (lambda message: print(message, file=sys.stderr, flush=True))
    details: list[dict[str, Any]] = []
    errors: list[str] = []
    completed_repos = timed_out_repos = failed_repos = 0
    gh = github_factory()
    now = utcnow()
    for index, repo in enumerate(selected, start=1):
        repo_errors: list[str] = []
        try:
            states, _ = _run_bounded(lambda: scan([repo], now, gh, repo_errors), repo_timeout_s)
        except TimeoutError as exc:
            timed_out_repos += 1
            errors.append(f"{repo}: {exc}")
            emit(f"[pr-pipeline] backlog {phase} progress {index}/{len(selected)} repo={repo} timed_out")
            continue
        except Exception as exc:
            failed_repos += 1
            errors.append(f"{repo}: {exc}")
            emit(f"[pr-pipeline] backlog {phase} progress {index}/{len(selected)} repo={repo} failed")
            continue
        errors.extend(repo_errors)
        completed_repos += 1
        matches = [state for state in states if state.mergeable and lacks_fresh_verdict(state.latest_verdict_at, now)]
        details.extend({"repo": state.repo, "pr": state.number, "url": state.url, "merge_state": state.mergeable_state} for state in matches)
        emit(f"[pr-pipeline] backlog {phase} progress {index}/{len(selected)} repo={repo} unverdicted_mergeable={len(matches)}")
    repos_capped = len(allowlist) > len(selected)
    complete = not repos_capped and completed_repos == len(selected) and not errors
    result = {"phase": phase, "count": len(details), "details": details, "errors": errors,
              "repos_attempted": len(selected), "repos_scanned": completed_repos,
              "repos_completed": completed_repos, "repos_timed_out": timed_out_repos,
              "repos_failed": failed_repos, "repos_total": len(allowlist), "repos_capped": repos_capped,
              "complete": complete,
              "repo_timeout_s": repo_timeout_s}
    emit(f"[pr-pipeline] backlog {phase} final count={result['count']} complete={str(complete).lower()} repos_completed={completed_repos} timed_out={timed_out_repos} failed={failed_repos} errors={len(errors)}")
    return result


def route_conflicting_prs_to_executor(allowlist: list[str], dry_run: bool = False) -> int:
    now, errors, gh = utcnow(), [], GitHubClient()
    states, _ = scan_repos(allowlist, now, gh, errors)
    routed, state = 0, _load_state()
    routed_conflicts = state.setdefault("routed_conflicts", {})
    for pr_state in states:
        if (pr_state.mergeable_state or "").upper() != "CONFLICTING":
            continue
        try:
            details = gh.pr_details(pr_state.repo, pr_state.number)
        except Exception as exc:
            print(f"[pr-pipeline] failed to inspect {pr_state.repo}#{pr_state.number}: {exc}", file=sys.stderr)
            continue
        task_id = _extract_clickup_task_id(details.get("body")) or _extract_clickup_task_id(details.get("title")) or _extract_clickup_task_id((details.get("head") or {}).get("ref"))
        if not task_id:
            continue
        head_sha = (details.get("head") or {}).get("sha") or details.get("headRefOid") or ""
        key = f"{pr_state.repo}#{pr_state.number}"
        last = routed_conflicts.get(key, {})
        if last.get("head_sha") == head_sha and last.get("task_id") == task_id:
            continue
        durable_route = _has_durable_conflict_route(task_id, pr_state.repo, pr_state.number, head_sha)
        if durable_route is True:
            routed_conflicts[key] = {"head_sha": head_sha, "task_id": task_id, "updated_at": now.isoformat()}
            if not dry_run:
                _save_state(state)
            continue
        if durable_route is None:
            print(f"[pr-pipeline] durable route evidence unknown for {key}; refusing duplicate route", file=sys.stderr)
            continue
        comment = build_rework_body(pr_state, task_id) + "\n\n" + _conflict_route_marker(pr_state.repo, pr_state.number, head_sha)
        if dry_run:
            print(f"[pr-pipeline] DRY_RUN would route conflict {key} -> {task_id}")
            routed += 1
            continue
        # The durable marker is the completion record.  Never write it until
        # all other route mutations have succeeded; otherwise a later run
        # could mistake a partial route for a completed one and skip recovery.
        ok = _task_tag(task_id, "add-tag", "agent-ready") and _task_tag(task_id, "rm-tag", "needs-validation") and _set_status(task_id, "to do") and _task_comment(task_id, comment)
        if ok:
            routed += 1
            routed_conflicts[key] = {"head_sha": head_sha, "task_id": task_id, "updated_at": now.isoformat()}
            _save_state(state)
    return routed


def check_staleness_and_alert(allowlist: list[str], dry_run: bool = False) -> list[dict[str, Any]]:
    """Keep the legacy stale-alert entry point for existing cron callers."""
    now, errors, gh = utcnow(), [], GitHubClient()
    states, _ = scan_repos(allowlist, now, gh, errors)
    alerted: list[dict[str, Any]] = []
    state = _load_state()
    prior = state.setdefault("stale_alerts", {})
    for pr_state in states:
        if not stale_without_verdict(pr_state.created_at, pr_state.latest_verdict_at, now):
            continue
        key = f"{pr_state.repo}#{pr_state.number}"
        item = {"repo": pr_state.repo, "pr": pr_state.number, "url": pr_state.url,
                "merge_state": pr_state.mergeable_state,
                "age_hours": round((now - pr_state.created_at).total_seconds() / 3600.0, 2)}
        if prior.get(key, {}).get("age_hours") == item["age_hours"] and prior[key].get("merge_state") == item["merge_state"]:
            continue
        if not dry_run:
            notify(f"stale-pr-alert repo={item['repo']} pr={item['pr']} url={item['url']} age_hours={item['age_hours']:.1f}", alert_log_path=ALERT_LOG_PATH)
        prior[key] = {"age_hours": item["age_hours"], "merge_state": item["merge_state"], "updated_at": now.isoformat()}
        alerted.append(item)
    if alerted and not dry_run:
        _save_state(state)
    return alerted


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print("Usage: pr_pipeline_improvements.py {backlog-status|wake-validator|route-conflicts|alert-stale|sweep} [options]")
        return 0
    command = argv.pop(0)
    if command in {"backlog-status", "count-unverdicted"}:
        import argparse
        parser = argparse.ArgumentParser(prog="pr_pipeline_improvements.py backlog-status")
        parser.add_argument("--repo-timeout", type=float, default=DEFAULT_BACKLOG_REPO_TIMEOUT_S)
        parser.add_argument("--max-repos", type=int, default=DEFAULT_BACKLOG_MAX_REPOS)
        parser.add_argument("--phase", choices=("before", "after", "status"), default="status")
        args = parser.parse_args(argv)
        report = count_unverdicted_mergeable_backlog(load_allowlist(DEFAULT_ALLOWLIST), repo_timeout_s=args.repo_timeout, max_repos=args.max_repos, phase=args.phase)
        print(json.dumps(report, indent=2))
        return 0
    if command == "route-conflicts":
        print(json.dumps({"routed": route_conflicting_prs_to_executor(load_allowlist(DEFAULT_ALLOWLIST), dry_run="--dry-run" in argv)}, indent=2))
        return 0
    if command == "wake-validator":
        count, details = wake_validator_if_needed(load_allowlist(DEFAULT_ALLOWLIST), dry_run="--dry-run" in argv)
        print(json.dumps({"candidates": count, "details": details}, indent=2))
        return 0
    if command == "alert-stale":
        print(json.dumps({"alerted": check_staleness_and_alert(load_allowlist(DEFAULT_ALLOWLIST), dry_run="--dry-run" in argv)}, indent=2))
        return 0
    if command == "sweep":
        report = run_sweep(repos=load_allowlist(DEFAULT_ALLOWLIST), task_id=os.environ.get("CLICKUP_TASK_ID"),
                           dry_run="--dry-run" in argv, test_alert="--test-alert" in argv,
                           report_path=DEFAULT_REPORT_PATH, alert_log_path=ALERT_LOG_PATH,
                           merge_script=DEFAULT_MERGE_SCRIPT)
        print(json.dumps(report, indent=2))
        return 0 if report.get("ok") else 1
    print(f"Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
