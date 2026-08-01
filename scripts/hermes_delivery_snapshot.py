#!/usr/bin/env python3
"""Build a normalized, read-only Hermes delivery evidence snapshot.

The producer is deliberately separate from the watcher.  It reads ClickUp via
the existing read-only CLI, GitHub via ``gh``, and the Hermes Mac mini via
bounded SSH reads.  It never starts an agent or mutates any observed system.
Only the requested local output file is atomically replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from delivery_watch_safety import redact_sensitive


SCHEMA = "hermes_delivery_snapshot/v1"
DEFAULT_LIST_ID = "901714465284"  # AI Dev Assistant
DEFAULT_HERMES_HOME = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
DEFAULT_CONFIG = DEFAULT_HERMES_HOME / "config.delivery-watch.json"
DEFAULT_OUTPUT = DEFAULT_HERMES_HOME / "state" / "delivery-input" / "macbook.json"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
SHA_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
PR_URL_RE = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([0-9]+)"
)
REPO_FIELD_RE = re.compile(
    r"(?im)^\s*(?:target\s+repo(?:sitory)?|repository)\s*:\s*"
    r"(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
)
HANDOFF_RE = re.compile(r"ignite-\s*HANDOFF:\s*v1", re.IGNORECASE)
VALIDATOR_PASS_RE = re.compile(r"ignite-validate:\s*PASS\b", re.IGNORECASE)
SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9_-]+$")

MINI_JSON_PATHS = {
    "jobs": "/Users/colingreig/.hermes/cron/jobs.json",
    "review_gate": "/Users/colingreig/.hermes/scripts/.review_gate_state.json",
    "release": "/Users/colingreig/.hermes/releases/.mini-release-last-receipt.json",
    "deployment": "/Users/colingreig/.hermes/scripts/.pr_pipeline_deployment.json",
    "ci_health": "/Users/colingreig/.hermes/scripts/.ci_health_state.json",
}
MINI_LEDGER_DB = "/Users/colingreig/.hermes/cron/executions.db"
MINI_ADMISSION_DB = "/Users/colingreig/.hermes/state/executor-admission.db"
MINI_LIFECYCLE_LOG = "/Users/colingreig/.hermes/state/ci-health/lifecycle.jsonl"


class SnapshotError(RuntimeError):
    """A read-only evidence source could not be queried safely."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_mappings(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _redact_error(value: str) -> str:
    return redact_sensitive(value, limit=300)


def _decode_json(raw: bytes, source: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"{source} did not return valid JSON: {exc}") from exc


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_config(path: Path) -> dict[str, Any]:
    try:
        raw = path.expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(f"cannot read snapshot config {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotError("snapshot config must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise SnapshotError("snapshot config must be an object")
    config = parsed.get("delivery_snapshot", {})
    if not isinstance(config, dict):
        raise SnapshotError("delivery_snapshot config must be an object")
    list_id = str(config.get("clickup_list_id", DEFAULT_LIST_ID))
    host = str(config.get("mini_host", "mini"))
    if not list_id.isdigit():
        raise SnapshotError("delivery_snapshot.clickup_list_id must be numeric")
    if SAFE_HOST_RE.fullmatch(host) is None:
        raise SnapshotError("delivery_snapshot.mini_host must be one safe host token")
    governing_ci = config.get(
        "governing_ci",
        [{"path": ".github/workflows/ci.yml", "events": ["pull_request", "push"]}],
    )
    if not isinstance(governing_ci, list) or not governing_ci or len(governing_ci) > 10:
        raise SnapshotError("delivery_snapshot.governing_ci must be a non-empty bounded list")
    normalized_ci: list[dict[str, Any]] = []
    for item in governing_ci:
        if not isinstance(item, dict):
            raise SnapshotError("each governing_ci entry must be an object")
        path_value = item.get("path")
        events = item.get("events")
        workflow_id = item.get("workflow_id")
        if (
            not isinstance(path_value, str)
            or re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path_value)
            is None
        ):
            raise SnapshotError("governing_ci.path must be a canonical workflow path")
        if (
            not isinstance(events, list)
            or not events
            or any(event not in {"pull_request", "push"} for event in events)
        ):
            raise SnapshotError("governing_ci.events must contain only pull_request/push")
        if workflow_id not in (None, "") and not str(workflow_id).isdigit():
            raise SnapshotError("governing_ci.workflow_id must be numeric")
        normalized_ci.append(
            {
                "path": path_value,
                "events": list(dict.fromkeys(events)),
                **(
                    {"workflow_id": str(workflow_id)}
                    if workflow_id not in (None, "")
                    else {}
                ),
            }
        )
    return {
        "clickup_list_id": list_id,
        "mini_host": host,
        "lookback_hours": max(24, min(168, int(config.get("lookback_hours", 72)))),
        "max_tasks": max(1, min(25, int(config.get("max_tasks", 25)))),
        "poll_timeout_seconds": max(
            30, min(240, int(config.get("poll_timeout_seconds", 240)))
        ),
        "governing_ci": normalized_ci,
    }


class LiveBackend:
    """Bounded adapters for the three live read-only authorities."""

    def __init__(self, *, mini_host: str, deadline: Optional[float] = None):
        self.mini_host = mini_host
        self.deadline = deadline
        self.node = shutil.which("node")
        self.gh = shutil.which("gh")
        self.ssh = shutil.which("ssh")
        candidates = (
            DEFAULT_HERMES_HOME / "scripts" / "clickup" / "clickup.mjs",
            Path.home() / ".codex" / "skills" / "clickup" / "clickup.mjs",
        )
        self.clickup_cli = next((path for path in candidates if path.is_file()), None)

    @staticmethod
    def _run(
        argv: list[str],
        *,
        timeout: int,
        source: str,
        input_bytes: Optional[bytes] = None,
        env: Optional[dict[str, str]] = None,
        deadline: Optional[float] = None,
    ) -> bytes:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotError(f"{source} skipped: whole-poll deadline exhausted")
            timeout = max(1, min(timeout, int(remaining)))
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL if input_bytes is None else None,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SnapshotError(f"{source} failed: {exc}") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise SnapshotError(
                f"{source} returned {result.returncode}: {_redact_error(stderr)}"
            )
        return result.stdout

    def clickup_list(self, list_id: str) -> list[dict[str, Any]]:
        if self.node is None or self.clickup_cli is None:
            raise SnapshotError("existing ClickUp CLI or node is unavailable")
        raw = self._run(
            [
                self.node,
                str(self.clickup_cli),
                "list",
                list_id,
                "--include-closed",
                "--fresh",
                "--json",
            ],
            timeout=45,
            source="clickup-list",
            env={**os.environ, "CLICKUP_NO_CACHE": "1"},
            deadline=self.deadline,
        )
        return _list_of_mappings(_decode_json(raw, "clickup-list"))

    def clickup_comments(self, task_id: str) -> list[dict[str, Any]]:
        if SAFE_TASK_RE.fullmatch(task_id) is None:
            raise SnapshotError(f"unsafe ClickUp task id {task_id!r}")
        if self.node is None or self.clickup_cli is None:
            raise SnapshotError("existing ClickUp CLI or node is unavailable")
        raw = self._run(
            [self.node, str(self.clickup_cli), "comments", task_id, "--json"],
            timeout=30,
            source=f"clickup-comments:{task_id}",
            env={**os.environ, "CLICKUP_NO_CACHE": "1"},
            deadline=self.deadline,
        )
        return _list_of_mappings(_decode_json(raw, f"clickup-comments:{task_id}"))

    def github_pr(self, repository: str, number: int) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
            raise SnapshotError(f"unsafe GitHub repository {repository!r}")
        if self.gh is None:
            raise SnapshotError("gh CLI is unavailable")
        raw = self._run(
            [
                self.gh,
                "pr",
                "view",
                str(number),
                "--repo",
                repository,
                "--json",
                "number,headRefOid,url,state,mergedAt,statusCheckRollup",
            ],
            timeout=30,
            source=f"github-pr:{repository}#{number}",
            deadline=self.deadline,
        )
        value = _decode_json(raw, f"github-pr:{repository}#{number}")
        if not isinstance(value, dict):
            raise SnapshotError(f"github-pr:{repository}#{number} was not an object")
        return value

    def github_run(self, repository: str, run_id: str) -> dict[str, Any]:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None:
            raise SnapshotError(f"unsafe GitHub repository {repository!r}")
        if not str(run_id).isdigit():
            raise SnapshotError(f"unsafe GitHub run id {run_id!r}")
        if self.gh is None:
            raise SnapshotError("gh CLI is unavailable")
        raw = self._run(
            [
                self.gh,
                "api",
                f"repos/{repository}/actions/runs/{run_id}",
            ],
            timeout=30,
            source=f"github-run:{repository}#{run_id}",
            deadline=self.deadline,
        )
        value = _decode_json(raw, f"github-run:{repository}#{run_id}")
        if not isinstance(value, dict):
            raise SnapshotError(f"github-run:{repository}#{run_id} was not an object")
        return value

    def _ssh(
        self,
        remote_argv: list[str],
        *,
        timeout: int,
        source: str,
        input_bytes: Optional[bytes] = None,
    ) -> bytes:
        if self.ssh is None:
            raise SnapshotError("ssh is unavailable")
        return self._run(
            [
                self.ssh,
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={min(timeout, 20)}",
                self.mini_host,
                *remote_argv,
            ],
            timeout=timeout + 5,
            source=source,
            input_bytes=input_bytes,
            deadline=self.deadline,
        )

    def mini_json(self, name: str) -> dict[str, Any]:
        path = MINI_JSON_PATHS[name]
        raw = self._ssh(["cat", "--", path], timeout=20, source=f"mini:{name}")
        value = _decode_json(raw, f"mini:{name}")
        if not isinstance(value, dict):
            raise SnapshotError(f"mini:{name} was not an object")
        if name == "release":
            value = dict(value)
            value["_content_sha256"] = hashlib.sha256(raw).hexdigest()
        return value

    def mini_lifecycle(self) -> list[dict[str, Any]]:
        raw = self._ssh(
            ["/usr/bin/tail", "-n", "200", MINI_LIFECYCLE_LOG],
            timeout=20,
            source="mini:lifecycle",
        )
        records: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", errors="strict").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
        return records

    def mini_processes(self) -> list[dict[str, Any]]:
        raw = self._ssh(
            ["/bin/ps", "-axo", "pid=,lstart=,command="],
            timeout=20,
            source="mini:processes",
        )
        processes: list[dict[str, Any]] = []
        pattern = re.compile(
            r"^\s*(\d+)\s+(.{24})\s+(.+)$"
        )
        for line in raw.decode("utf-8", errors="replace").splitlines():
            match = pattern.match(line)
            if match:
                processes.append(
                    {
                        "pid": int(match.group(1)),
                        "started_at": match.group(2).strip(),
                        "command": match.group(3),
                    }
                )
        return processes

    def mini_runtime(self) -> dict[str, Any]:
        link = "/Users/colingreig/.hermes/runtime-current"
        target = self._ssh(
            ["/usr/bin/readlink", link], timeout=15, source="mini:runtime-target"
        ).decode("utf-8", errors="strict").strip()
        head = self._ssh(
            ["/usr/bin/git", "-C", link, "rev-parse", "HEAD^{commit}"],
            timeout=20,
            source="mini:runtime-head",
        ).decode("utf-8", errors="strict").strip()
        if not target.startswith("/Users/colingreig/.hermes/releases/"):
            raise SnapshotError("Mini runtime target is outside the governed releases directory")
        if SHA_RE.fullmatch(head) is None:
            raise SnapshotError("Mini runtime head is not a full SHA")
        return {"target": target, "head_sha": head}

    def mini_claims(self) -> list[dict[str, Any]]:
        raw = self._ssh(
            [
                "/usr/bin/find",
                "/Users/colingreig/.hermes/state/claims",
                "-maxdepth",
                "1",
                "-type",
                "f",
                "-name",
                r"\*.claim",
                "-print",
            ],
            timeout=20,
            source="mini:claims-index",
        )
        claims: list[dict[str, Any]] = []
        paths = raw.decode("utf-8", errors="strict").splitlines()[:50]
        for raw_path in paths:
            if re.fullmatch(
                r"/Users/colingreig/\.hermes/state/claims/[A-Za-z0-9_-]+\.claim",
                raw_path,
            ) is None:
                raise SnapshotError("Mini claim index returned an unsafe path")
            payload = self._ssh(
                ["cat", "--", raw_path], timeout=10, source="mini:claim"
            )
            value = _decode_json(payload, "mini:claim")
            if isinstance(value, dict):
                claims.append({"_path": raw_path, **value})
        return claims

    def mini_sqlite(self, name: str) -> list[dict[str, Any]]:
        if name == "admission":
            db = MINI_ADMISSION_DB
            query = (
                "SELECT task_id,job_id,owner_run_id,fencing_token,acquired_at,"
                "heartbeat_at,expires_at,ledger_execution_id,state,terminal_status,"
                "finalized_at FROM executor_lease WHERE state='active' LIMIT 1"
            )
        elif name == "admission_history":
            db = MINI_ADMISSION_DB
            query = (
                "SELECT task_id,job_id,owner_run_id,fencing_token,acquired_at,"
                "heartbeat_at,expires_at,ledger_execution_id,state,terminal_status,"
                "finalized_at,released_at,recovered_at,recovery_receipt_id,revision "
                "FROM executor_lease_history "
                "WHERE julianday(acquired_at) >= julianday('now','-72 hours') "
                "ORDER BY acquired_at DESC LIMIT 100"
            )
        elif name == "ledger":
            db = MINI_LEDGER_DB
            query = (
                "SELECT id,job_id,source,process_id,pid,owner_token,status,claimed_at,"
                "started_at,heartbeat_at,lease_expires_at,finished_at,terminal_at,"
                "terminal_reason FROM executions ORDER BY claimed_at DESC LIMIT 250"
            )
        else:
            raise SnapshotError(f"unsupported Mini SQLite source {name}")
        # OpenSSH does not preserve remote argv quoting. Feed the fixed SELECT
        # over stdin so the remote shell sees no SQL text and sqlite remains in
        # explicit read-only mode.
        raw = self._ssh(
            ["/usr/bin/sqlite3", "-readonly", "-json", db],
            timeout=25,
            source=f"mini:{name}",
            input_bytes=(query + ";\n").encode("utf-8"),
        )
        return _list_of_mappings(_decode_json(raw or b"[]", f"mini:{name}"))


class FixtureBackend:
    """Offline adapter with the same return contract as :class:`LiveBackend`."""

    def __init__(self, path: Path):
        value = _decode_json(path.read_bytes(), f"fixture:{path}")
        if not isinstance(value, dict):
            raise SnapshotError("fixture must be an object")
        self.value = value

    def clickup_list(self, _list_id: str) -> list[dict[str, Any]]:
        return _list_of_mappings(self.value.get("clickup_tasks"))

    def clickup_comments(self, task_id: str) -> list[dict[str, Any]]:
        return _list_of_mappings(_mapping(self.value.get("clickup_comments")).get(task_id))

    def github_pr(self, repository: str, number: int) -> dict[str, Any]:
        value = _mapping(self.value.get("github_prs")).get(f"{repository}#{number}")
        if not isinstance(value, dict):
            raise SnapshotError(f"fixture GitHub PR missing: {repository}#{number}")
        return value

    def github_run(self, repository: str, run_id: str) -> dict[str, Any]:
        value = _mapping(self.value.get("github_runs")).get(f"{repository}#{run_id}")
        if not isinstance(value, dict):
            raise SnapshotError(f"fixture GitHub run missing: {repository}#{run_id}")
        return value

    def mini_json(self, name: str) -> dict[str, Any]:
        value = _mapping(self.value.get("mini_json")).get(name)
        if not isinstance(value, dict):
            raise SnapshotError(f"fixture Mini JSON missing: {name}")
        return value

    def mini_lifecycle(self) -> list[dict[str, Any]]:
        return _list_of_mappings(self.value.get("mini_lifecycle"))

    def mini_processes(self) -> list[dict[str, Any]]:
        return _list_of_mappings(self.value.get("mini_processes"))

    def mini_runtime(self) -> dict[str, Any]:
        value = self.value.get("mini_runtime")
        if not isinstance(value, dict):
            raise SnapshotError("fixture Mini runtime missing")
        return value

    def mini_claims(self) -> list[dict[str, Any]]:
        return _list_of_mappings(self.value.get("mini_claims"))

    def mini_sqlite(self, name: str) -> list[dict[str, Any]]:
        value = _mapping(self.value.get("mini_sqlite")).get(name)
        if not isinstance(value, list):
            raise SnapshotError(f"fixture Mini SQLite source missing: {name}")
        return _list_of_mappings(value)


def _status_name(task: dict[str, Any]) -> str:
    value = task.get("status")
    return str(_mapping(value).get("status") or value or "").strip().lower()


def _timestamp_ms(value: object) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _candidate_tasks(
    tasks: list[dict[str, Any]], *, now: datetime, lookback_hours: int, limit: int
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(hours=lookback_hours)
    selected: list[dict[str, Any]] = []
    for task in tasks:
        status = _status_name(task)
        updated = _timestamp_ms(task.get("date_updated"))
        if status in {"to do", "in progress", "in review"} or "review" in status:
            selected.append(task)
        elif status in {"complete", "closed"} and updated is not None and updated >= cutoff:
            selected.append(task)
    return sorted(
        selected,
        key=lambda item: (
            _status_name(item) not in {"complete", "closed"},
            int(str(item.get("date_updated") or "0")),
        ),
        reverse=True,
    )[:limit]


def _comment_text(comment: dict[str, Any]) -> str:
    return str(comment.get("comment_text") or comment.get("text_content") or "")


def _combined_text(task: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    pieces = [
        str(task.get("name") or ""),
        str(task.get("text_content") or task.get("description") or ""),
        *(_comment_text(comment) for comment in comments),
    ]
    return "\n".join(piece for piece in pieces if piece)


def _sha_near_marker(text: str, marker: re.Pattern[str]) -> Optional[str]:
    matches = list(marker.finditer(text))
    if not matches:
        return None
    tail = text[matches[-1].start() :]
    sha = SHA_RE.search(tail)
    return sha.group(0).lower() if sha else None


def _field(text: str, names: Iterable[str]) -> Optional[str]:
    joined = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"(?im)^\s*(?:{joined})\s*[:=]\s*[`\"]?([A-Za-z0-9_.:/-]+)",
        text,
    )
    return match.group(1).strip("`\"") if match else None


def _authorized_run(
    run: dict[str, Any], governing_ci: list[dict[str, Any]]
) -> tuple[str, str, str]:
    path = str(run.get("path") or "")
    event = str(run.get("event") or "")
    workflow_id = str(run.get("workflow_id") or run.get("workflowDatabaseId") or "")
    head_sha = str(run.get("head_sha") or run.get("headSha") or "").lower()
    if not path or not event or not workflow_id or SHA_RE.fullmatch(head_sha) is None:
        raise SnapshotError("workflow path/id/event or full head SHA is missing")
    authority = next(
        (
            item
            for item in governing_ci
            if item.get("path") == path
            and event in item.get("events", [])
            and (
                item.get("workflow_id") in (None, "")
                or str(item.get("workflow_id")) == workflow_id
            )
        ),
        None,
    )
    if authority is None:
        raise SnapshotError(
            f"workflow {path}@{workflow_id}:{event} is not configured governing CI"
        )
    return f"{path}@{workflow_id}:{event}", head_sha, str(
        run.get("id") or run.get("databaseId") or ""
    )


def _github_identity(
    task: dict[str, Any],
    comments: list[dict[str, Any]],
    backend: Union[LiveBackend, FixtureBackend],
    collection: dict[str, Any],
    governing_ci: list[dict[str, Any]],
) -> tuple[Optional[str], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    text = _combined_text(task, comments)
    memberships: list[tuple[str, int]] = []
    for owner, repo, number in PR_URL_RE.findall(text):
        value = (f"{owner}/{repo}", int(number))
        if value not in memberships:
            memberships.append(value)
    repository_match = REPO_FIELD_RE.search(text)
    repository = repository_match.group(1) if repository_match else None
    if memberships and repository is None:
        repository = memberships[0][0]
    if len({item[0] for item in memberships}) > 1:
        collection[f"github:{task.get('id')}"] = {
            "status": "UNKNOWN",
            "error": "task references pull requests from multiple repositories",
        }
        return repository, [], [], []

    pull_requests: list[dict[str, Any]] = []
    ci_runs: list[dict[str, Any]] = []
    workflows: list[str] = []
    for stack_index, (repo, number) in enumerate(memberships):
        source = f"github:{repo}#{number}"
        try:
            evidence = backend.github_pr(repo, number)
            head_sha = str(evidence.get("headRefOid") or "").lower()
            if SHA_RE.fullmatch(head_sha) is None:
                raise SnapshotError("PR headRefOid is not a full SHA")
            pull_requests.append(
                {
                    "repository": repo,
                    "number": number,
                    "head_sha": head_sha,
                    "stack_index": stack_index,
                }
            )
            run_ids: list[str] = []
            for check in _list_of_mappings(evidence.get("statusCheckRollup")):
                details = str(check.get("detailsUrl") or check.get("targetUrl") or "")
                run_match = re.search(r"/actions/runs/([0-9]+)", details)
                if run_match is not None and run_match.group(1) not in run_ids:
                    run_ids.append(run_match.group(1))
            for run_id in run_ids:
                run_source = f"github-run:{repo}#{run_id}"
                try:
                    run = backend.github_run(repo, run_id)
                    workflow, run_head, observed_run_id = _authorized_run(
                        run, governing_ci
                    )
                    ci_runs.append(
                        {
                            "repository": repo,
                            "workflow": workflow,
                            "run_id": observed_run_id or run_id,
                            "head_sha": run_head,
                            "status": str(run.get("status") or "").lower(),
                            "conclusion": str(run.get("conclusion") or "").lower(),
                        }
                    )
                    if workflow not in workflows:
                        workflows.append(workflow)
                    collection[run_source] = {"status": "OK"}
                except SnapshotError as exc:
                    collection[run_source] = {
                        "status": "UNKNOWN",
                        "error": _redact_error(str(exc)),
                    }
            collection[source] = {"status": "OK"}
        except SnapshotError as exc:
            collection[source] = {"status": "UNKNOWN", "error": _redact_error(str(exc))}
    return repository, pull_requests, ci_runs, workflows


def _handoff_and_validator(
    comments: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    handoff: dict[str, Any] = {}
    validator: dict[str, Any] = {}
    for comment in comments:
        text = _comment_text(comment)
        comment_id = str(comment.get("id") or comment.get("date") or "")
        if HANDOFF_RE.search(text):
            handoff = {
                "id": f"clickup-comment:{comment_id}",
                "head_sha": _sha_near_marker(text, HANDOFF_RE),
            }
        if VALIDATOR_PASS_RE.search(text):
            validator = {
                "identity": f"clickup-comment:{comment_id}",
                "verdict": "PASS",
                "head_sha": _sha_near_marker(text, VALIDATOR_PASS_RE),
            }
    return handoff, validator


def _mini_sources(
    backend: Union[LiveBackend, FixtureBackend], collection: dict[str, Any]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in MINI_JSON_PATHS:
        source = f"mini:{name}"
        try:
            result[name] = backend.mini_json(name)
            collection[source] = {"status": "OK"}
        except (SnapshotError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            collection[source] = {"status": "UNKNOWN", "error": _redact_error(str(exc))}
    try:
        result["lifecycle"] = backend.mini_lifecycle()
        collection["mini:lifecycle"] = {"status": "OK"}
    except (SnapshotError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        collection["mini:lifecycle"] = {
            "status": "UNKNOWN",
            "error": _redact_error(str(exc)),
        }
    for name in ("admission", "admission_history", "ledger"):
        try:
            result[name] = backend.mini_sqlite(name)
            collection[f"mini:{name}"] = {"status": "OK"}
        except SnapshotError as exc:
            collection[f"mini:{name}"] = {
                "status": "UNKNOWN",
                "error": _redact_error(str(exc)),
            }
    for name, method in (
        ("claims", backend.mini_claims),
        ("processes", backend.mini_processes),
        ("runtime", backend.mini_runtime),
    ):
        try:
            result[name] = method()
            collection[f"mini:{name}"] = {"status": "OK"}
        except SnapshotError as exc:
            collection[f"mini:{name}"] = {
                "status": "UNKNOWN",
                "error": _redact_error(str(exc)),
            }
    return result


def _review_gate(mini: dict[str, Any]) -> dict[str, Any]:
    jobs = _list_of_mappings(_mapping(mini.get("jobs")).get("jobs"))
    review_job_ids = {
        str(job.get("id"))
        for job in jobs
        if "review" in str(job.get("name") or "").lower()
        and ("gate" in str(job.get("name") or "").lower() or "poll" in str(job.get("name") or "").lower())
    }
    ledger = _list_of_mappings(mini.get("ledger"))
    runs = [row for row in ledger if str(row.get("job_id")) in review_job_ids]
    runs.sort(key=lambda row: str(row.get("claimed_at") or ""), reverse=True)
    if not review_job_ids or not runs:
        return {"status": "UNKNOWN", "consecutive_clean_runs": 0}
    clean = 0
    for row in runs:
        if row.get("status") == "completed":
            clean += 1
        else:
            break
    return {
        "status": "clean" if runs[0].get("status") == "completed" else "failed",
        "consecutive_clean_runs": clean,
        "last_run_id": runs[0].get("id"),
        "last_run_at": runs[0].get("terminal_at") or runs[0].get("finished_at"),
    }


def _lifecycle_events(
    mini: dict[str, Any], *, since: datetime
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in _list_of_mappings(mini.get("lifecycle")):
        timestamp = record.get("timestamp")
        try:
            observed_at = datetime.fromisoformat(
                str(timestamp).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError:
            observed_at = None
        if observed_at is not None and observed_at < since:
            continue
        classification = record.get("classification")
        prior = record.get("prior_boot_id")
        current = record.get("current_boot_id")
        valid = False
        if classification == "transition":
            valid = bool(
                isinstance(prior, str)
                and isinstance(current, str)
                and UUID_RE.fullmatch(prior)
                and UUID_RE.fullmatch(current)
                and prior != current
            )
        elif classification == "baseline-reset":
            valid = bool(
                record.get("prior_boot_valid") is False
                and isinstance(current, str)
                and UUID_RE.fullmatch(current)
                and record.get("managed_intent_active") is not True
                and record.get("outage_active") is not True
            )
        elif classification == "unknown":
            valid = False
        events.append(
            {
                "id": record.get("fingerprint")
                or record.get("id")
                or f"{record.get('timestamp')}:{prior}->{current}",
                "timestamp": timestamp,
                "classification": classification,
                "valid": valid,
                "false_alert": record.get("false_alert") is True,
            }
        )
    return events


def _promotion(mini: dict[str, Any]) -> list[dict[str, Any]]:
    release = _mapping(mini.get("release"))
    prod_sha = release.get("to_commit")
    certified_sha = release.get("certified_source_commit")
    authority_receipt = release.get("promotion_authority_receipt_id")
    if not prod_sha:
        return []
    return [
        {
            "id": authority_receipt or release.get("_content_sha256"),
            "prod_sha": prod_sha,
            "certified": bool(authority_receipt and certified_sha == prod_sha),
            "receipt_id": authority_receipt,
            "receipt_sha": certified_sha,
        }
    ]


def _owners(mini: dict[str, Any]) -> list[dict[str, Any]]:
    admission = _list_of_mappings(mini.get("admission"))
    ledger_by_id = {
        str(row.get("id")): row for row in _list_of_mappings(mini.get("ledger"))
    }
    owners: list[dict[str, Any]] = []
    represented_executions: set[str] = set()
    represented_pids: set[int] = set()
    for lease in admission:
        ledger = _mapping(ledger_by_id.get(str(lease.get("ledger_execution_id"))))
        represented_executions.add(str(lease.get("ledger_execution_id")))
        if isinstance(ledger.get("pid"), int):
            represented_pids.add(ledger["pid"])
        owners.append(
            {
                "task_id": lease.get("task_id"),
                "job_id": lease.get("job_id"),
                "run_id": lease.get("owner_run_id"),
                "fencing_token": lease.get("fencing_token"),
                "claimed_at": lease.get("acquired_at"),
                "heartbeat_at": lease.get("heartbeat_at"),
                "lease_expires_at": lease.get("expires_at"),
                "execution_started_at": ledger.get("started_at") or lease.get("acquired_at"),
                "execution_finished_at": ledger.get("finished_at") or lease.get("finalized_at"),
            }
        )
    for ledger in _list_of_mappings(mini.get("ledger")):
        if (
            ledger.get("status") not in {"claimed", "running"}
            or str(ledger.get("id")) in represented_executions
        ):
            continue
        pid = ledger.get("pid")
        if isinstance(pid, int):
            represented_pids.add(pid)
        owners.append(
            {
                "task_id": f"cron:{ledger.get('job_id')}",
                "job_id": ledger.get("job_id"),
                "run_id": ledger.get("id"),
                "fencing_token": ledger.get("owner_token"),
                "claimed_at": ledger.get("claimed_at"),
                "heartbeat_at": ledger.get("heartbeat_at"),
                "lease_expires_at": ledger.get("lease_expires_at"),
                "execution_started_at": ledger.get("started_at") or ledger.get("claimed_at"),
                "execution_finished_at": ledger.get("finished_at"),
                "pid": pid,
                "owner_kind": "cron-ledger",
            }
        )
    process_by_pid = {
        row.get("pid"): row for row in _list_of_mappings(mini.get("processes"))
    }
    for claim in _list_of_mappings(mini.get("claims")):
        pid = claim.get("pid") or _mapping(claim.get("owner")).get("pid")
        if not isinstance(pid, int) or pid not in process_by_pid or pid in represented_pids:
            continue
        represented_pids.add(pid)
        owners.append(
            {
                "task_id": claim.get("task_id")
                or Path(str(claim.get("_path") or "unknown")).stem,
                "run_id": claim.get("run_id") or f"legacy-pid:{pid}",
                "fencing_token": claim.get("fencing_token"),
                "claimed_at": claim.get("claimed_at"),
                "heartbeat_at": claim.get("heartbeat_at"),
                "lease_expires_at": claim.get("expires_at"),
                "execution_started_at": claim.get("started_at") or claim.get("claimed_at"),
                "execution_finished_at": claim.get("finished_at"),
                "pid": pid,
                "owner_kind": "legacy-claim",
            }
        )
    executor_process = re.compile(
        r"(?i)(ignite-execute|clickup-queue-poller|opencode_exec|executor[_-]admission)"
    )
    for process in _list_of_mappings(mini.get("processes")):
        pid = process.get("pid")
        if (
            not isinstance(pid, int)
            or pid in represented_pids
            or executor_process.search(str(process.get("command") or "")) is None
        ):
            continue
        owners.append(
            {
                "task_id": f"detached-process:{pid}",
                "run_id": f"pid:{pid}",
                "fencing_token": None,
                "claimed_at": process.get("started_at"),
                "heartbeat_at": None,
                "lease_expires_at": None,
                "execution_started_at": process.get("started_at"),
                "execution_finished_at": None,
                "pid": pid,
                "owner_kind": "detached-process",
            }
        )
    return owners


def _task_lane(text: str) -> str:
    if re.search(r"(?i)\b(?:lane\s*:\s*mini|mini release|mini runtime|deploy(?:ment)? target\s*:\s*mini)\b", text):
        return "mini"
    return "repo-only"


def _normalize_task(
    raw: dict[str, Any],
    comments: list[dict[str, Any]],
    backend: Union[LiveBackend, FixtureBackend],
    mini: dict[str, Any],
    collection: dict[str, Any],
    governing_ci: list[dict[str, Any]],
) -> dict[str, Any]:
    task_id = str(raw.get("id") or "")
    text = _combined_text(raw, comments)
    repository, prs, ci_runs, workflows = _github_identity(
        raw, comments, backend, collection, governing_ci
    )
    handoff, validator = _handoff_and_validator(comments)
    lane = _task_lane(text)
    admission_rows = [
        row
        for row in (
            _list_of_mappings(mini.get("admission"))
            + _list_of_mappings(mini.get("admission_history"))
        )
        if str(row.get("task_id")) == task_id
    ]
    admission_rows.sort(
        key=lambda row: str(row.get("acquired_at") or ""), reverse=True
    )
    admission = admission_rows[0] if admission_rows else {}
    ledger = next(
        (
            row
            for row in _list_of_mappings(mini.get("ledger"))
            if str(row.get("id")) == str(admission.get("ledger_execution_id"))
        ),
        {},
    )
    admission_state = str(admission.get("state") or "")
    ledger_status = str(ledger.get("status") or "")
    terminal_status = str(admission.get("terminal_status") or "")
    lifecycle_join_ok = (
        admission_state == "active" and ledger_status in {"claimed", "running"}
    ) or (
        admission_state in {"finalized", "released", "recovered"}
        and terminal_status in {"completed", "failed", "interrupted"}
        and ledger_status == terminal_status
    )
    ledger_join_ok = bool(
        admission
        and ledger
        and str(ledger.get("id")) == str(admission.get("ledger_execution_id"))
        and str(ledger.get("job_id")) == str(admission.get("job_id"))
        and ledger.get("owner_token")
        and lifecycle_join_ok
    )
    join_source = f"mini:ledger-join:{task_id}"
    collection[join_source] = (
        {"status": "OK"}
        if ledger_join_ok
        else {
            "status": "UNKNOWN",
            "error": "no exact admission-to-ledger execution/job/fence/status match",
        }
    )
    executor = {
        "job_id": admission.get("job_id"),
        "run_id": admission.get("owner_run_id"),
        "fencing_token": admission.get("fencing_token"),
    }
    ledger_evidence = (
        {
            "execution_id": ledger.get("id"),
            "job_id": ledger.get("job_id"),
            "run_id": admission.get("owner_run_id"),
            "fencing_token": admission.get("fencing_token"),
            "owner_token": ledger.get("owner_token"),
            "status": ledger.get("status"),
        }
        if ledger_join_ok
        else {}
    )
    delivery_head = prs[-1]["head_sha"] if prs else _field(text, ("delivery_head_sha", "head_sha"))
    allow_no_pr = bool(re.search(r"(?im)^\s*allow_no_pr\s*:\s*true\s*$", text))
    no_pr_authority = {
        "authority": _field(text, ("no_pr_authority",)),
        "receipt_id": _field(text, ("no_pr_receipt_id",)),
        "head_sha": _field(text, ("no_pr_head_sha",)),
    }
    if allow_no_pr and not prs:
        no_pr_run = _field(text, ("ci_run_id",))
        if repository and no_pr_run:
            source = f"github-run:{repository}#{no_pr_run}"
            try:
                run = backend.github_run(repository, no_pr_run)
                workflow, head_sha, observed_run_id = _authorized_run(
                    run, governing_ci
                )
                workflows.append(workflow)
                ci_runs.append(
                    {
                        "repository": repository,
                        "workflow": workflow,
                        "run_id": observed_run_id or no_pr_run,
                        "head_sha": head_sha,
                        "status": str(run.get("status") or "").lower(),
                        "conclusion": str(run.get("conclusion") or "").lower(),
                    }
                )
                collection[source] = {"status": "OK"}
            except SnapshotError as exc:
                collection[source] = {
                    "status": "UNKNOWN",
                    "error": _redact_error(str(exc)),
                }
        if not delivery_head:
            delivery_head = no_pr_authority.get("head_sha")
    task_sources = {
        "clickup": collection.get("clickup", {"status": "UNKNOWN"}),
        f"clickup-comments:{task_id}": collection.get(
            f"clickup-comments:{task_id}", {"status": "UNKNOWN"}
        ),
    }
    task_sources[join_source] = collection[join_source]
    for key, status in collection.items():
        if (key.startswith("github:") or key.startswith("github-run:")) and (
            f"github:{task_id}" == key
            or (repository is not None and key.startswith(f"github:{repository}#"))
            or (repository is not None and key.startswith(f"github-run:{repository}#"))
            or any(key == f"github:{pr['repository']}#{pr['number']}" for pr in prs)
        ):
            task_sources[key] = status
    if lane == "mini":
        for key, status in collection.items():
            if key.startswith("mini:"):
                task_sources[key] = status

    normalized: dict[str, Any] = {
        "task": {"id": task_id, "lane": lane},
        "sources": task_sources,
        "executor": executor,
        "ledger": ledger_evidence,
        "repository": repository,
        "governing_workflows": workflows,
        "pull_requests": prs,
        "stacked": len(prs) > 1,
        "ci": {"runs": ci_runs},
        "handoff": handoff,
        "validator": validator,
        "delivery_head_sha": delivery_head,
        "allow_no_pr": allow_no_pr,
        "no_pr_authority": no_pr_authority,
        "observed_task_status": _status_name(raw),
        "observed_task_updated_at": raw.get("date_updated"),
    }
    if lane == "mini":
        release = _mapping(mini.get("release"))
        deployment = _mapping(mini.get("deployment"))
        runtime = _mapping(mini.get("runtime"))
        receipt_head = release.get("to_commit")
        content_receipt = release.get("_content_sha256")
        promotion_receipt = release.get("promotion_authority_receipt_id")
        runtime_target = runtime.get("target")
        exact = bool(
            delivery_head
            and release.get("schema_version") == 2
            and receipt_head == delivery_head
            and release.get("certified_source_commit") == delivery_head
            and deployment.get("source_commit") == delivery_head
            and runtime.get("head_sha") == delivery_head
            and release.get("runtime_target") == runtime_target
            and isinstance(content_receipt, str)
            and re.fullmatch(r"[0-9a-f]{64}", content_receipt)
            and isinstance(promotion_receipt, str)
            and re.fullmatch(r"[0-9a-f]{64}", promotion_receipt)
        )
        delivery_source = f"mini:delivery-join:{task_id}"
        collection[delivery_source] = (
            {"status": "OK"}
            if exact
            else {
                "status": "UNKNOWN",
                "error": "runtime/deployment/certification/promotion/release receipt identity mismatch",
            }
        )
        normalized["sources"][delivery_source] = collection[delivery_source]
        normalized["deployment"] = {
            "target": runtime_target if exact else None,
            "head_sha": receipt_head if exact else None,
        }
        normalized["release"] = {
            "authority": "mini-release-cut" if exact else None,
            "receipt_id": content_receipt if exact else None,
            "head_sha": receipt_head if exact else None,
            "promotion_authority_receipt_id": promotion_receipt if exact else None,
        }
    # Retain read-only timestamps for SLA evaluation without treating them as
    # delivery proof.
    normalized["_watch"] = {
        "claimed_at": admission.get("acquired_at") or ledger.get("claimed_at"),
        "heartbeat_at": admission.get("heartbeat_at") or ledger.get("heartbeat_at"),
        "lease_expires_at": admission.get("expires_at") or ledger.get("lease_expires_at"),
    }
    return normalized


def build_snapshot(
    backend: Union[LiveBackend, FixtureBackend],
    config: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    observed = now or _now()
    poll_deadline = time.monotonic() + config.get("poll_timeout_seconds", 240)
    if isinstance(backend, LiveBackend):
        backend.deadline = poll_deadline
    collection: dict[str, Any] = {}
    try:
        raw_tasks = backend.clickup_list(config["clickup_list_id"])
        collection["clickup"] = {"status": "OK"}
    except SnapshotError as exc:
        raw_tasks = []
        collection["clickup"] = {"status": "UNKNOWN", "error": _redact_error(str(exc))}
    candidates = _candidate_tasks(
        raw_tasks,
        now=observed,
        lookback_hours=config["lookback_hours"],
        limit=config["max_tasks"],
    )
    comments_by_task: dict[str, list[dict[str, Any]]] = {}

    def collect_comments(task: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        task_id = str(task.get("id") or "")
        try:
            return task_id, backend.clickup_comments(task_id), {"status": "OK"}
        except SnapshotError as exc:
            return task_id, [], {
                "status": "UNKNOWN",
                "error": _redact_error(str(exc)),
            }

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as pool:
        for task_id, comments, state in pool.map(collect_comments, candidates):
            comments_by_task[task_id] = comments
            collection[f"clickup-comments:{task_id}"] = state

    mini = _mini_sources(backend, collection)

    def normalize(task: dict[str, Any]) -> dict[str, Any]:
        return _normalize_task(
            task,
            comments_by_task.get(str(task.get("id") or ""), []),
            backend,
            mini,
            collection,
            config.get(
                "governing_ci",
                [{"path": ".github/workflows/ci.yml", "events": ["pull_request", "push"]}],
            ),
        )

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(candidates)))) as pool:
        normalized = list(pool.map(normalize, candidates))
    if time.monotonic() >= poll_deadline:
        collection["poll-deadline"] = {
            "status": "UNKNOWN",
            "error": "whole-poll deadline exhausted",
        }
    owners = _owners(mini)
    owner_by_task = {
        str(owner.get("task_id")): owner.get("run_id") for owner in owners
    }
    queue = [
        {
            "task_id": str(task.get("id")),
            "eligible_at": _iso(_timestamp_ms(task.get("date_updated")) or observed),
            "owner_run_id": owner_by_task.get(str(task.get("id"))),
        }
        for task in candidates
        if _status_name(task) == "to do"
    ]
    return {
        "schema": SCHEMA,
        "generated_at": _iso(observed),
        "collection": collection,
        "tasks": normalized,
        "watch": {
            "owners": owners,
            "queue": queue,
            "review_gate": _review_gate(mini),
            "lifecycle_events": _lifecycle_events(
                mini, since=observed - timedelta(hours=config["lookback_hours"])
            ),
            "promotions": _promotion(mini),
        },
    }


def _run_watcher(config_path: Path, output: Path) -> int:
    import hermes_delivery_watch as watcher

    config = watcher._load_config(config_path)
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    result = watcher.run_once(config, watcher.DEFAULT_STATE_DIR, snapshot=snapshot)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "UNKNOWN" else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fixture", type=Path, help="offline source fixture; no live commands")
    parser.add_argument(
        "--observed-at",
        help="fixed ISO-8601 observation time for deterministic fixture runs",
    )
    parser.add_argument(
        "--run-watcher",
        action="store_true",
        help="evaluate the just-written snapshot with the installed watcher",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        config = _load_config(args.config)
        observed_at: Optional[datetime] = None
        if args.observed_at:
            if not args.fixture:
                raise SnapshotError("--observed-at is only allowed with --fixture")
            try:
                observed_at = datetime.fromisoformat(args.observed_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SnapshotError("--observed-at must be a valid ISO-8601 timestamp") from exc
            if observed_at.tzinfo is None:
                raise SnapshotError("--observed-at must include a timezone offset")
        backend: Union[LiveBackend, FixtureBackend]
        backend = FixtureBackend(args.fixture) if args.fixture else LiveBackend(
            mini_host=config["mini_host"]
        )
        snapshot = build_snapshot(backend, config, now=observed_at)
        _atomic_json(args.output, snapshot)
        if args.run_watcher:
            return _run_watcher(args.config, args.output)
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "UNKNOWN"
                    if any(
                        _mapping(value).get("status") != "OK"
                        for value in _mapping(snapshot.get("collection")).values()
                    )
                    else "OK",
                    "generated_at": snapshot["generated_at"],
                    "output": str(args.output.expanduser().resolve()),
                    "task_count": len(snapshot["tasks"]),
                    "unknown_sources": sorted(
                        name
                        for name, value in snapshot["collection"].items()
                        if _mapping(value).get("status") != "OK"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, SnapshotError, ValueError) as exc:
        print(
            json.dumps(
                {"schema": SCHEMA, "status": "UNKNOWN", "error": _redact_error(str(exc))},
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
