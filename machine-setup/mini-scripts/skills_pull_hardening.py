#!/usr/bin/env python3
"""Detect repeated skills-pull failures and optionally perform a safe auto-pull.

This script defaults to dry-run mode. Use --apply to allow writes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


EXIT_OK = 0
EXIT_APPLIED_SUCCESS = 2
EXIT_APPLIED_FAILED = 3
EXIT_PRECONDITION_FAILED = 4
EXIT_USAGE_ERROR = 5
EXIT_VERIFY_WOULD_APPLY = 10

DEFAULT_TASK_ID = "Hermes-skills-4-6"
DEFAULT_THRESHOLD = 3
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_LOG_PATH = Path.home() / ".hermes/logs/skills_pull.log"
DEFAULT_AUDIT_PATH = Path.home() / ".hermes/logs/skills_pull_hardening_audit.log"
DEFAULT_REPO_PATH = Path.home() / ".hermes/skills/anthropic-agent-skills"
DEFAULT_BACKUP_DIR = Path.home() / ".hermes/scripts/backups"
ALLOWLISTED_HOSTS = {"github.com", "gitlab.com"}
FAILURE_MARKERS = ("ERROR", "TRACEBACK", "FAILED")
SUCCESS_PATTERNS = (
    re.compile(r"\bexit(?:\s+code)?\s*[=:]\s*0\b", re.IGNORECASE),
    re.compile(r"\bstatus\s*[=:]\s*success\b", re.IGNORECASE),
    re.compile(r"\bskills-pull\b.*\bok\b", re.IGNORECASE),
)
FAILURE_PATTERNS = (
    re.compile(r"\bexit(?:\s+code)?\s*[=:]\s*([1-9][0-9]*)\b", re.IGNORECASE),
)
TIMESTAMP_PATTERNS = (
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
    re.compile(r"\[(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]"),
    re.compile(r"(?P<ts>\d{10}(?:\.\d+)?)"),
)


@dataclass
class Event:
    timestamp: datetime
    result: str
    line: str


@dataclass
class DetectionResult:
    triggered: bool
    consecutive_failures: int
    window_minutes: int
    threshold: int
    events_in_window: list[Event]
    reason: str


class HardeningError(Exception):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect consecutive skills-pull failures and optionally perform a safe "
            "fast-forward-only auto-pull of ~/.hermes/skills/anthropic-agent-skills. "
            "Dry-run is the default."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Allow writes and repo changes.")
    mode.add_argument("--dry-run", action="store_true", help="Explicitly enforce dry-run mode.")
    parser.add_argument("--verify", action="store_true", help="Dry-run check: 0=no-op, 10=would apply.")
    parser.add_argument("--selftest", action="store_true", help="Run lightweight local self-tests.")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="Consecutive failure threshold. Default: 3.")
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=DEFAULT_WINDOW_MINUTES,
        help="Sliding window in minutes for failure detection. Default: 60.",
    )
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH, help="skills-pull log path.")
    parser.add_argument("--audit-path", type=Path, default=DEFAULT_AUDIT_PATH, help="JSONL audit log path.")
    parser.add_argument("--repo-path", type=Path, default=DEFAULT_REPO_PATH, help="Target repository path.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="Backup tarball directory.")
    parser.add_argument("--task-id", default=DEFAULT_TASK_ID, help="ClickUp task id recorded in audit logs.")
    parser.add_argument(
        "--force-allow-missing",
        action="store_true",
        help="Required for operator-approved handling when the repo path is missing.",
    )
    parser.add_argument(
        "--force-allow-local-changes",
        action="store_true",
        help="Allow auto-pull when local changes exist. A backup tarball is always created first.",
    )
    return parser


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(line: str, fallback_epoch: float) -> datetime:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        raw = match.group("ts")
        if re.fullmatch(r"\d{10}(?:\.\d+)?", raw):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        cleaned = raw.replace("Z", "+00:00")
        if re.fullmatch(r".*[+-]\d{4}$", cleaned):
            cleaned = f"{cleaned[:-5]}{cleaned[-5:-2]}:{cleaned[-2:]}"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.fromtimestamp(fallback_epoch, tz=timezone.utc)


def classify_line(line: str) -> str | None:
    upper = line.upper()
    if any(marker in upper for marker in FAILURE_MARKERS):
        return "failure"
    for pattern in FAILURE_PATTERNS:
        if pattern.search(line):
            return "failure"
    for pattern in SUCCESS_PATTERNS:
        if pattern.search(line):
            return "success"
    return None


def load_events(log_path: Path) -> list[Event]:
    if not log_path.exists():
        raise HardeningError(f"Log path does not exist: {log_path}", EXIT_PRECONDITION_FAILED)
    if not log_path.is_file():
        raise HardeningError(f"Log path is not a file: {log_path}", EXIT_PRECONDITION_FAILED)

    fallback_epoch = log_path.stat().st_mtime
    events: list[Event] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            result = classify_line(line)
            if result is None:
                continue
            events.append(Event(timestamp=parse_timestamp(line, fallback_epoch), result=result, line=line))
    return sorted(events, key=lambda item: item.timestamp)


def detect_failures(events: list[Event], threshold: int, window_minutes: int, now: datetime | None = None) -> DetectionResult:
    current = now or now_utc()
    window_start = current - timedelta(minutes=window_minutes)
    window_events = [event for event in events if event.timestamp >= window_start]
    consecutive = 0
    tail: list[Event] = []
    for event in window_events:
        if event.result == "success":
            consecutive = 0
            tail = []
            continue
        consecutive += 1
        tail.append(event)
    triggered = consecutive >= threshold
    if triggered:
        reason = f"{consecutive} consecutive failures within {window_minutes} minutes"
    elif not window_events:
        reason = "no classified log events in window"
    else:
        reason = f"latest failure streak {consecutive} below threshold {threshold}"
    return DetectionResult(
        triggered=triggered,
        consecutive_failures=consecutive,
        window_minutes=window_minutes,
        threshold=threshold,
        events_in_window=tail if tail else window_events,
        reason=reason,
    )


def run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_allowed_repo(repo_path: Path) -> None:
    if not repo_path.exists():
        raise HardeningError(
            f"Repository path missing: {repo_path}. Provision it manually; cloning is intentionally unsupported.",
            EXIT_PRECONDITION_FAILED,
        )
    if not (repo_path / ".git").exists():
        raise HardeningError(f"Target path is not a git repo: {repo_path}", EXIT_PRECONDITION_FAILED)

    origin = run_git(repo_path, ["remote", "get-url", "origin"])
    if origin.returncode != 0:
        raise HardeningError(
            f"Unable to read origin remote: {(origin.stderr or origin.stdout).strip()}",
            EXIT_PRECONDITION_FAILED,
        )
    remote_url = origin.stdout.strip()
    host = parse_remote_host(remote_url)
    if host not in ALLOWLISTED_HOSTS:
        raise HardeningError(
            f"Origin host is not allowlisted: {host or 'unknown'} ({remote_url})",
            EXIT_PRECONDITION_FAILED,
        )


def parse_remote_host(remote_url: str) -> str | None:
    if "://" in remote_url:
        parsed = urlparse(remote_url)
        return parsed.hostname.lower() if parsed.hostname else None
    scp_match = re.match(r"^[^@]+@(?P<host>[^:]+):.+$", remote_url)
    if scp_match:
        return scp_match.group("host").lower()
    return None


def has_local_changes(repo_path: Path) -> bool:
    status = run_git(repo_path, ["status", "--porcelain"])
    if status.returncode != 0:
        raise HardeningError(
            f"Unable to inspect git status: {(status.stderr or status.stdout).strip()}",
            EXIT_PRECONDITION_FAILED,
        )
    return bool(status.stdout.strip())


def ensure_clean_or_allowed(repo_path: Path, allow_local_changes: bool) -> bool:
    local_changes = has_local_changes(repo_path)
    if local_changes and not allow_local_changes:
        raise HardeningError(
            "Local uncommitted changes detected; rerun with --force-allow-local-changes to permit backup + pull.",
            EXIT_PRECONDITION_FAILED,
        )
    return local_changes


def create_backup(repo_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{stamp}-anthropic-agent-skills.tar.gz"
    with tarfile.open(backup_path, "w:gz") as archive:
        archive.add(repo_path, arcname="anthropic-agent-skills")
    return backup_path


def current_branch(repo_path: Path) -> str:
    branch = run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch.returncode != 0:
        raise HardeningError(
            f"Unable to determine current branch: {(branch.stderr or branch.stdout).strip()}",
            EXIT_PRECONDITION_FAILED,
        )
    return branch.stdout.strip()


def ensure_upstream(repo_path: Path, branch: str) -> str:
    upstream = run_git(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream.returncode == 0:
        return upstream.stdout.strip()
    candidate = f"origin/{branch}"
    verify = run_git(repo_path, ["show-ref", "--verify", f"refs/remotes/{candidate}"])
    if verify.returncode != 0:
        raise HardeningError(
            f"No upstream configured and remote branch missing: {candidate}",
            EXIT_PRECONDITION_FAILED,
        )
    return candidate


def git_fetch(repo_path: Path) -> None:
    result = run_git(repo_path, ["fetch", "--prune", "origin"])
    if result.returncode != 0:
        raise HardeningError(f"git fetch failed: {(result.stderr or result.stdout).strip()}", EXIT_APPLIED_FAILED)


def rev_parse(repo_path: Path, ref: str) -> str:
    result = run_git(repo_path, ["rev-parse", ref])
    if result.returncode != 0:
        raise HardeningError(
            f"Unable to resolve git ref {ref}: {(result.stderr or result.stdout).strip()}",
            EXIT_PRECONDITION_FAILED,
        )
    return result.stdout.strip()


def merge_base(repo_path: Path, left: str, right: str) -> str:
    result = run_git(repo_path, ["merge-base", left, right])
    if result.returncode != 0:
        raise HardeningError(
            f"Unable to compute merge-base for {left} and {right}: {(result.stderr or result.stdout).strip()}",
            EXIT_APPLIED_FAILED,
        )
    return result.stdout.strip()


def fast_forward_only(repo_path: Path, upstream: str) -> str:
    head_sha = rev_parse(repo_path, "HEAD")
    upstream_sha = rev_parse(repo_path, upstream)
    if head_sha == upstream_sha:
        return "already-up-to-date"
    base_sha = merge_base(repo_path, "HEAD", upstream)
    if base_sha != head_sha:
        raise HardeningError(
            f"Fast-forward merge check failed for {upstream}: local branch has diverged.",
            EXIT_APPLIED_FAILED,
        )
    result = run_git(repo_path, ["merge", "--ff-only", upstream])
    if result.returncode != 0:
        raise HardeningError(
            f"Fast-forward merge failed for {upstream}: {(result.stderr or result.stdout).strip()}",
            EXIT_APPLIED_FAILED,
        )
    return "fast-forwarded"


def append_audit(audit_path: Path, task_id: str, action: str, result: str, details: dict[str, Any], apply_mode: bool) -> None:
    payload = {
        "timestamp_ms": int(time.time() * 1000),
        "task_id": task_id,
        "action": action,
        "result": result,
        "details": details,
    }
    if not apply_mode:
        return
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def preflight_repo_checks(args: argparse.Namespace) -> tuple[bool, bool]:
    repo_exists = args.repo_path.exists()
    if not repo_exists:
        if args.apply and args.force_allow_missing:
            raise HardeningError(
                f"Repository path missing and cloning is unsupported: {args.repo_path}",
                EXIT_PRECONDITION_FAILED,
            )
        raise HardeningError(
            f"Repository path missing: {args.repo_path}. Use --force-allow-missing only for operator-reviewed runs.",
            EXIT_PRECONDITION_FAILED,
        )
    ensure_allowed_repo(args.repo_path)
    local_changes = has_local_changes(args.repo_path)
    if local_changes and not args.force_allow_local_changes:
        raise HardeningError(
            "Local uncommitted changes detected; rerun with --force-allow-local-changes to permit backup + pull.",
            EXIT_PRECONDITION_FAILED,
        )
    return repo_exists, local_changes


def maybe_apply(detection: DetectionResult, args: argparse.Namespace) -> tuple[int, str]:
    repo_exists, local_changes = preflight_repo_checks(args)
    details = {
        "threshold": args.threshold,
        "window_minutes": args.window_minutes,
        "consecutive_failures": detection.consecutive_failures,
        "repo_path": str(args.repo_path),
        "log_path": str(args.log_path),
        "repo_exists": repo_exists,
        "local_changes": local_changes,
    }
    if not args.apply:
        append_audit(args.audit_path, args.task_id, "auto_pull", "ok", {**details, "mode": "dry-run"}, False)
        return EXIT_OK, "threshold exceeded; dry-run only"

    print(f"ALERT: {detection.reason}", file=sys.stderr)
    append_audit(args.audit_path, args.task_id, "alert", "ok", details, True)
    if local_changes:
        backup_path = create_backup(args.repo_path, args.backup_dir)
        details["backup_path"] = str(backup_path)
        append_audit(args.audit_path, args.task_id, "backup", "ok", details, True)

    git_fetch(args.repo_path)
    branch = current_branch(args.repo_path)
    upstream = ensure_upstream(args.repo_path, branch)
    merge_result = fast_forward_only(args.repo_path, upstream)
    details.update({"branch": branch, "upstream": upstream, "merge_result": merge_result})
    append_audit(args.audit_path, args.task_id, "auto_pull", "ok", details, True)
    return EXIT_APPLIED_SUCCESS, merge_result


def verify_mode(detection: DetectionResult, args: argparse.Namespace) -> int:
    if not detection.triggered:
        print(summary_line("ok", detection.reason, apply_mode=False, verify=True))
        return EXIT_OK
    preflight_repo_checks(args)
    print(summary_line("would-apply", detection.reason, apply_mode=False, verify=True))
    return EXIT_VERIFY_WOULD_APPLY


def summary_line(status: str, message: str, apply_mode: bool, verify: bool) -> str:
    mode = "verify" if verify else ("apply" if apply_mode else "dry-run")
    return f"summary mode={mode} status={status} message={message}"


def run_selftest() -> int:
    with tempfile.TemporaryDirectory(prefix="skills-pull-hardening-") as tmpdir:
        root = Path(tmpdir)
        logs = root / "logs"
        logs.mkdir()
        log_path = logs / "skills_pull.log"
        now = now_utc()
        lines = [
            f"{format_ts(now - timedelta(minutes=30))} exit=1 FAILED first\n",
            f"{format_ts(now - timedelta(minutes=20))} ERROR second\n",
            f"{format_ts(now - timedelta(minutes=10))} TRACEBACK third\n",
        ]
        log_path.write_text("".join(lines), encoding="utf-8")

        events = load_events(log_path)
        detection = detect_failures(events, threshold=3, window_minutes=60, now=now)
        if not detection.triggered or detection.consecutive_failures != 3:
            raise HardeningError("selftest failure detector check failed", EXIT_USAGE_ERROR)

        repo = root / "anthropic-agent-skills"
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "clone", str(remote), str(repo)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "selftest@example.com"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Self Test"], cwd=repo, check=True, capture_output=True, text=True)
        (repo / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "branch", "--set-upstream-to=origin/main", "main"], cwd=repo, check=True, capture_output=True, text=True)

        if parse_remote_host("git@github.com:anthropics/anthropic-agent-skills.git") != "github.com":
            raise HardeningError("selftest remote host parsing failed", EXIT_USAGE_ERROR)

    print(summary_line("ok", "selftest passed", apply_mode=False, verify=False))
    return EXIT_OK


def validate_args(args: argparse.Namespace) -> None:
    if args.threshold < 1:
        raise HardeningError("--threshold must be >= 1", EXIT_USAGE_ERROR)
    if args.window_minutes < 1:
        raise HardeningError("--window-minutes must be >= 1", EXIT_USAGE_ERROR)
    if args.verify and args.apply:
        raise HardeningError("--verify cannot be combined with --apply", EXIT_USAGE_ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        validate_args(args)
        if args.selftest:
            return run_selftest()

        events = load_events(args.log_path)
        detection = detect_failures(events, threshold=args.threshold, window_minutes=args.window_minutes)

        if args.verify:
            return verify_mode(detection, args)

        if not detection.triggered:
            print(summary_line("ok", detection.reason, apply_mode=args.apply, verify=False))
            return EXIT_OK

        code, message = maybe_apply(detection, args)
        status = "ok" if code in {EXIT_OK, EXIT_APPLIED_SUCCESS} else "err"
        print(summary_line(status, message, apply_mode=args.apply, verify=False))
        return code
    except HardeningError as exc:
        print(summary_line("err", str(exc), apply_mode=args.apply, verify=args.verify))
        if args.apply:
            append_audit(
                args.audit_path,
                args.task_id,
                "error",
                "err",
                {"message": str(exc), "repo_path": str(args.repo_path), "log_path": str(args.log_path)},
                True,
            )
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected fallback
        print(summary_line("err", f"unexpected error: {exc}", apply_mode=args.apply, verify=args.verify))
        if args.apply:
            append_audit(
                args.audit_path,
                args.task_id,
                "error",
                "err",
                {"message": f"unexpected error: {exc}", "repo_path": str(args.repo_path), "log_path": str(args.log_path)},
                True,
            )
        return EXIT_USAGE_ERROR


if __name__ == "__main__":
    sys.exit(main())
