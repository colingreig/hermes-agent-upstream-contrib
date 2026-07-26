#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

# The flat Mini entrypoint has a flat authority sibling; package imports bind
# the canonical package module instead so identity/store classes never split.
if __package__:
    from . import validator_verdict
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import validator_verdict

VERDICT_PREFIX = "ignite-validate:"
FRESH_VERDICT_WINDOW = timedelta(hours=24)
STALE_WINDOW = timedelta(hours=6)
DEFAULT_ALLOWLIST = Path.home() / ".hermes/allowed-repos.txt"
DEFAULT_REPORT_PATH = Path.home() / ".hermes/scripts/.pr_wake_last_run.json"
DEFAULT_MERGE_SCRIPT = Path.home() / ".hermes/scripts/autonomous_merge.py"
ALERT_LOG_PATH = Path("/tmp/pr_wake_alerts.log")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def load_allowlist(path: Path) -> list[str]:
    repos: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        repos.append(line)
    return repos


def verdict_timestamp_for_pr(repo: str, pr_number: int, head_sha: str) -> datetime | None:
    """Look up when a PR last got a verdict, from the store the validator
    actually writes to (the fenced SQLite TrustStore ledger).

    BUG THIS REPLACES (2026-07-22): this function used to scan GitHub PR/issue
    comments for a literal `"ignite-validate:"` prefix (see git history /
    latest_verdict_at()). But the hermes-pr-validate skill never posts that
    marker as a GitHub comment — it writes to the authoritative SQLite verdict
    ledger, not a PR comment. So the comment scan always returned
    None, every PR looked permanently unverdicted, and the whole pipeline
    (wake / merge / conflict-routing / staleness alert) never cleared its
    backlog. Read the real store instead.

    Only a verdict for the PR's CURRENT head SHA counts as fresh — a verdict
    against a superseded head is treated the same as no verdict at all, so a
    new push correctly re-triggers a wake.
    """
    # ``verdict_for`` is intentionally SQLite-only.  A missing/corrupt ledger
    # cannot fall back to the retired JSON dictionary: treating it as absent
    # wakes a new fenced validation instead of trusting an unfenced record.
    record = validator_verdict.verdict_for(repo, pr_number, head_sha=head_sha)
    if not record:
        return None
    recorded_head = (record.get("head_sha") or "").strip()
    if head_sha and recorded_head and recorded_head != head_sha:
        return None
    return parse_time(record.get("ts"))


def latest_verdict_at(comments: list[dict[str, Any]]) -> datetime | None:
    """Deprecated: GitHub PR/issue comments never carry the ignite-validate:
    marker in practice (see verdict_timestamp_for_pr). Kept only so any
    external caller still passing comment lists in doesn't hard-crash; prefer
    verdict_timestamp_for_pr for anything new."""
    latest: datetime | None = None
    for comment in comments:
        body = (comment.get("body") or "").strip()
        if VERDICT_PREFIX not in body.lower():
            continue
        created_at = parse_time(comment.get("created_at"))
        if created_at and (latest is None or created_at > latest):
            latest = created_at
    return latest


def is_mergeable(details: dict[str, Any]) -> bool:
    mergeable = details.get("mergeable")
    if mergeable is True:
        return True
    if mergeable is False:
        return False
    return details.get("mergeable_state") in {"clean", "unstable", "has_hooks"}


def lacks_fresh_verdict(verdict_at: datetime | None, now: datetime) -> bool:
    return verdict_at is None or now - verdict_at >= FRESH_VERDICT_WINDOW


def stale_without_verdict(created_at: datetime, verdict_at: datetime | None, now: datetime) -> bool:
    if now - created_at < STALE_WINDOW:
        return False
    return verdict_at is None or now - verdict_at >= STALE_WINDOW


def notify(message: str, alert_log_path: Path = ALERT_LOG_PATH) -> None:
    print(message)
    alert_log_path.parent.mkdir(parents=True, exist_ok=True)
    with alert_log_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    payload = json.dumps({"text": message}).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


class GitHubClient:
    def _run(self, args: list[str]) -> Any:
        completed = subprocess.run(args, capture_output=True, text=True, check=True)
        return json.loads(completed.stdout or "null")

    def list_open_prs(self, repo: str) -> list[dict[str, Any]]:
        return self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,url,createdAt",
            ]
        )

    def pr_details(self, repo: str, pr_number: int) -> dict[str, Any]:
        return self._run(["gh", "api", f"repos/{repo}/pulls/{pr_number}"])

    def pr_comments(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        # No longer consulted for verdict freshness (see verdict_timestamp_for_pr) —
        # kept for any caller that still wants raw comments for other purposes.
        return self._run(["gh", "api", f"repos/{repo}/issues/{pr_number}/comments?per_page=100"])


class CommandRunner:
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True)


@dataclass
class PullRequestState:
    repo: str
    number: int
    url: str
    created_at: datetime
    mergeable: bool
    mergeable_state: str
    latest_verdict_at: datetime | None


def scan_repos(
    repos: list[str],
    now: datetime,
    gh: GitHubClient,
    errors: list[str],
) -> tuple[list[PullRequestState], int]:
    states: list[PullRequestState] = []
    total_open_mergeable = 0
    for repo in repos:
        try:
            prs = gh.list_open_prs(repo)
        except Exception as exc:  # pragma: no cover - defensive for cron runs
            errors.append(f"failed to list PRs for {repo}: {exc}")
            continue
        for pr in prs:
            number = int(pr["number"])
            try:
                details = gh.pr_details(repo, number)
            except Exception as exc:  # pragma: no cover - defensive for cron runs
                errors.append(f"failed to inspect {repo}#{number}: {exc}")
                continue
            created_at = parse_time(pr.get("createdAt"))
            if created_at is None:
                errors.append(f"missing createdAt for {repo}#{number}")
                continue
            mergeable = is_mergeable(details)
            if mergeable:
                total_open_mergeable += 1
            head_sha = (details.get("head") or {}).get("sha") or ""
            states.append(
                PullRequestState(
                    repo=repo,
                    number=number,
                    url=pr["url"],
                    created_at=created_at,
                    mergeable=mergeable,
                    mergeable_state=details.get("mergeable_state") or "unknown",
                    latest_verdict_at=verdict_timestamp_for_pr(repo, number, head_sha),
                )
            )
    return states, total_open_mergeable


def validator_status(output: str) -> str:
    normalized = output.upper()
    if "CONFLICTING" in normalized:
        return "CONFLICTING"
    if "PASS" in normalized:
        return "PASS"
    return "UNKNOWN"


def build_rework_body(pr_state: PullRequestState, task_id: str) -> str:
    return (
        f"Hermes PR wake sweep flagged {pr_state.repo}#{pr_state.number} for rework. "
        f"PR: {pr_state.url} . Merge state: {pr_state.mergeable_state}. "
        f"Recommended action: rework in executor lane. Source task: {task_id}."
    )


def run_sweep(
    repos: list[str],
    task_id: str | None,
    dry_run: bool,
    test_alert: bool,
    report_path: Path,
    alert_log_path: Path,
    merge_script: Path,
    gh: GitHubClient | None = None,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    gh = gh or GitHubClient()
    runner = runner or CommandRunner()

    merged: list[str] = []
    rework: list[dict[str, Any]] = []
    alerts: list[str] = []
    errors: list[str] = []

    states, total_open_mergeable_before = scan_repos(repos, now, gh, errors)
    candidates = [
        state
        for state in states
        if state.mergeable and lacks_fresh_verdict(state.latest_verdict_at, now)
    ]
    before_count = len(candidates)

    for state in states:
        if stale_without_verdict(state.created_at, state.latest_verdict_at, now):
            message = (
                f"stale-pr-alert repo={state.repo} pr={state.number} url={state.url} "
                f"age_hours={(now - state.created_at).total_seconds() / 3600:.1f}"
            )
            notify(message, alert_log_path=alert_log_path)
            alerts.append(message)

    if test_alert:
        test_message = "stale-pr-alert test-mode simulated alert"
        notify(test_message, alert_log_path=alert_log_path)
        alerts.append(test_message)

    if dry_run:
        after_count = 0
        total_open_mergeable_after = max(total_open_mergeable_before - before_count, 0)
    else:
        for state in candidates:
            started_at = utcnow().isoformat()
            validate_command = os.environ.get("HERMES_PR_VALIDATE_CMD")
            validate_args = (
                [validate_command, "--repo", state.repo, "--pr", str(state.number)]
                if validate_command
                else ["hermes-pr-validate", "--repo", state.repo, "--pr", str(state.number)]
            )
            validation = runner.run(validate_args)
            ended_at = utcnow().isoformat()
            combined_output = "\n".join(
                item for item in [validation.stdout.strip(), validation.stderr.strip()] if item
            )
            status = validator_status(combined_output)

            if validation.returncode != 0 and status == "UNKNOWN":
                errors.append(
                    f"validator failed for {state.repo}#{state.number} start={started_at} end={ended_at}: {combined_output}"
                )
                continue

            if status == "PASS":
                merge_result = runner.run(
                    [
                        "python3",
                        str(merge_script),
                        "--repo",
                        state.repo,
                        "--pr",
                        str(state.number),
                    ]
                )
                if merge_result.returncode == 0:
                    merged.append(state.url)
                else:
                    errors.append(
                        f"merge failed for {state.repo}#{state.number}: {(merge_result.stderr or merge_result.stdout).strip()}"
                    )
            elif status == "CONFLICTING":
                if not task_id:
                    errors.append(f"missing task id for rework routing on {state.repo}#{state.number}")
                    continue
                body = build_rework_body(state, task_id)
                body_file = Path(report_path.parent) / f"{task_id}-{state.repo.replace('/', '_')}-{state.number}-rework.md"
                body_file.write_text(body + "\n", encoding="utf-8")
                comment_result = runner.run(
                    [
                        # ~/.hermes/hermes-agent/venv no longer exists post the
                        # 2026-07-19 releases/+runtime-current deploy topology
                        # change; use the live symlink (see pr_pipeline_improvements.py).
                        os.path.expanduser("~/.hermes/runtime-current/venv/bin/python3.11"),
                        str(Path.home() / ".hermes/scripts/hermes_validate_ops.py"),
                        "comment",
                        task_id,
                        "--body",
                        str(body_file),
                    ]
                )
                if comment_result.returncode == 0:
                    rework.append(
                        {"repo": state.repo, "pr": state.number, "reason": state.mergeable_state}
                    )
                else:
                    errors.append(
                        f"clickup comment failed for {state.repo}#{state.number}: {(comment_result.stderr or comment_result.stdout).strip()}"
                    )
            else:
                errors.append(f"validator returned unknown status for {state.repo}#{state.number}: {combined_output}")

        rescanned, total_open_mergeable_after = scan_repos(repos, utcnow(), gh, errors)
        remaining = [
            state
            for state in rescanned
            if state.mergeable and lacks_fresh_verdict(state.latest_verdict_at, utcnow())
        ]
        after_count = len(remaining)
        for state in remaining:
            errors.append(
                f"remaining unverdicted mergeable PR {state.repo}#{state.number} state={state.mergeable_state}"
            )

    report = {
        "ok": not errors and after_count == 0,
        "before_count": before_count,
        "after_count": after_count,
        "total_open_mergeable_before": total_open_mergeable_before,
        "total_open_mergeable_after": total_open_mergeable_after,
        "merged": merged,
        "rework": rework,
        "alerts": alerts,
        "errors": errors,
        "dry_run": dry_run,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wake validator and sweep open PR backlog.")
    parser.add_argument("--task-id", default=os.environ.get("CLICKUP_TASK_ID"))
    parser.add_argument("--allowlist-path", default=str(DEFAULT_ALLOWLIST))
    parser.add_argument("--report-path", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--merge-script", default=str(DEFAULT_MERGE_SCRIPT))
    parser.add_argument("--alert-log-path", default=str(ALERT_LOG_PATH))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-alert", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    allowlist_path = Path(args.allowlist_path).expanduser()
    try:
        repos = load_allowlist(allowlist_path)
    except FileNotFoundError:
        report = {
            "ok": False,
            "before_count": 0,
            "after_count": 0,
            "merged": [],
            "rework": [],
            "alerts": [],
            "errors": [f"allowlist file not found: {allowlist_path}"],
        }
        print(json.dumps(report, indent=2))
        return 1

    report = run_sweep(
        repos=repos,
        task_id=args.task_id,
        dry_run=args.dry_run,
        test_alert=args.test_alert,
        report_path=Path(args.report_path).expanduser(),
        alert_log_path=Path(args.alert_log_path),
        merge_script=Path(args.merge_script).expanduser(),
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
