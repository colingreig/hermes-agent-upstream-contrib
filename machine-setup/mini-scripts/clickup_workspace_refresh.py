#!/usr/bin/env python3
"""Refresh a cached ClickUp workspace topology map.

This script maintains a versioned local cache at
``~/.hermes/state/clickup-map.json`` plus human-readable markdown mirrors at
``~/.hermes/state/clickup-workspace-map.md`` and the stable canonical brain
note ``~/brain/clickup-workspace-map.md``.

The cache defaults to a 6 hour TTL. Importers can use ``ensure_workspace_map``
for on-demand reads with optional forced refreshes. The supported canonical
note APIs are ``write_workspace_map_note()`` and
``read_workspace_map_note()``; the latter is the filesystem-backed equivalent
of ``read_note("clickup-workspace-map")``. The CLI read equivalent is
``clickup_workspace_refresh.py --read-note``.
"""

from __future__ import annotations

import argparse
import contextvars
import datetime as dt
import email.utils
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

_SOURCE_PATH = Path(__file__).resolve()
if _SOURCE_PATH.parent.name == "mini-scripts" and _SOURCE_PATH.parent.parent.name == "machine-setup":
    REPO_ROOT = _SOURCE_PATH.parents[2]
else:
    # Deployed Mini path: ~/.hermes/scripts/clickup_workspace_refresh.py.
    REPO_ROOT = _SOURCE_PATH.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("HERMES_HOME", str(Path.home() / ".hermes"))

from hermes_constants import display_hermes_home, get_hermes_home  # noqa: E402

API = "https://api.clickup.com/api/v2"
DEFAULT_TEAM_ID = "9017245888"
SCHEMA_VERSION = 3
REFRESH_TTL_SECONDS = 6 * 60 * 60
TASK_LOOKBACK_DAYS = 7
TASK_PAGE_LIMIT = 10
THIS_SCRIPT_NAME = "clickup_workspace_refresh.py"
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_FALLBACK_BACKOFF_SECONDS = 15
FIELD_DISCOVERY_MIN_INTERVAL_SECONDS = 0.25

# 86e1vw79j: this note documents a specific historical incident (the
# 2026-07-09 out-of-band rewrite that upgraded the output schema and hand-ran
# the script once outside the cron dispatcher, causing the emitted cadence to
# drift from the registered cron). It is deliberately a literal, not derived
# — it is a permanent record, unlike the cadence text below which must stay
# live. Kept in the source (not just a ClickUp comment) so it regenerates
# into every future map instead of being lost on the next overwrite.
_ROOT_CAUSE_DATE = "2026-07-09"
ROOT_CAUSE_NOTE_2026_07_09 = (
    "2026-07-09: the script was upgraded to a new output schema and manually "
    "test-run once outside the cron dispatcher, which is why the map briefly "
    "diverged from the registered cron schedule. The cadence line above is "
    "now always derived from the live cron registration (see "
    "_derive_refresh_cadence), not hardcoded, so this can't recur."
)

STATE_DIR = get_hermes_home() / "state"
DEFAULT_JSON_PATH = STATE_DIR / "clickup-map.json"
DEFAULT_MARKDOWN_PATH = STATE_DIR / "clickup-workspace-map.md"
PRIOR_MARKDOWN_PATH = STATE_DIR / "clickup-workspace-map.prev.md"
DRIFT_LOG = STATE_DIR / "clickup_topology_drift.jsonl"

DEFAULT_BRAIN_NOTE_PATH = Path.home() / "brain" / "clickup-workspace-map.md"

_LIST_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_LIST_FIELDS_CACHE: dict[str, list[dict[str, Any]]] = {}
_LAST_FIELD_DISCOVERY_AT: float | None = None
_HELPER_MODULE: Any | None = None
_HELPER_MODULE_ATTEMPTED = False


@dataclass
class RefreshEvidence:
    """Secret-free evidence for one refresh or cache-reuse attempt."""

    started_at: str
    request_count: int = 0
    rate_limit_count: int = 0
    retry_delays_seconds: list[float] | None = None
    rate_limit_guidance: list[dict[str, str]] | None = None
    cache_present: bool = False
    cache_used: bool = False
    cache_retained: bool = False

    def __post_init__(self) -> None:
        if self.retry_delays_seconds is None:
            self.retry_delays_seconds = []
        if self.rate_limit_guidance is None:
            self.rate_limit_guidance = []


_ACTIVE_REFRESH_EVIDENCE: contextvars.ContextVar[RefreshEvidence | None] = contextvars.ContextVar(
    "clickup_workspace_refresh_evidence", default=None
)

_CLIENT_ALIAS_STOPWORDS = {
    "active",
    "ad",
    "ads",
    "aeo",
    "app",
    "brand",
    "content",
    "crm",
    "cro",
    "delivery",
    "dev",
    "development",
    "growth",
    "list",
    "lists",
    "marketing",
    "operations",
    "ppc",
    "reporting",
    "seo",
    "space",
    "spaces",
    "strategy",
    "task",
    "tasks",
    "web",
    "work",
}


@dataclass(frozen=True)
class ClickUpApiError(RuntimeError):
    status: int
    path: str
    message: str

    def __str__(self) -> str:
        return f"ClickUp API error {self.status} on {self.path}: {self.message}"


class WorkspaceMapNoteError(RuntimeError):
    """The canonical workspace-map brain note could not be read or written."""


class WorkspaceMapValidationError(RuntimeError):
    """A refresh response was incomplete and must not be published."""


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_ms() -> int:
    return int(_now_utc().timestamp() * 1000)


def _iso_utc(ts_ms: int | None = None) -> str:
    if ts_ms is None:
        return _now_utc().isoformat(timespec="seconds")
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).isoformat(
        timespec="seconds"
    )


def _sanitize_http_error(err: urllib.error.HTTPError) -> str:
    try:
        return err.read().decode("utf-8", "replace")[:300]
    except Exception:
        return ""


def _load_clickup_helper() -> Any | None:
    global _HELPER_MODULE, _HELPER_MODULE_ATTEMPTED
    if _HELPER_MODULE_ATTEMPTED:
        return _HELPER_MODULE
    _HELPER_MODULE_ATTEMPTED = True
    helper_path = get_hermes_home() / "scripts" / "clickup_triage_ops.py"
    if not helper_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_clickup_triage_ops_helper", helper_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _HELPER_MODULE = module
    return module


_CLICKUP_TOKEN_CACHE: str | None = None


def _resolve_clickup_token() -> str:
    """Resolve the ClickUp API token.

    Resolution order:
      1. ``os.environ["CLICKUP_API_TOKEN"]`` — the normal scheduler path.
         This keeps the common case fast and avoids an unnecessary 1Password
         lookup while the gateway environment is healthy.
      2. ``agent.lazy_secret_resolver.get("CLICKUP_API_TOKEN")`` — per-call
         restart-race fallback. Import/lookup failures are swallowed here
         exactly like that module's own fail-open contract.

    Never logs the resolved value. Result is cached in-process so repeated
    calls to ``_fallback_req`` don't re-resolve on every request.
    """
    global _CLICKUP_TOKEN_CACHE
    if _CLICKUP_TOKEN_CACHE:
        return _CLICKUP_TOKEN_CACHE

    token = (os.environ.get("CLICKUP_API_TOKEN") or "").strip()
    if token:
        _CLICKUP_TOKEN_CACHE = token
        return token

    value: str | None = None
    try:
        from agent import lazy_secret_resolver

        value = lazy_secret_resolver.get("CLICKUP_API_TOKEN")
    except Exception:
        value = None

    token = (value or "").strip()
    if token:
        _CLICKUP_TOKEN_CACHE = token
    return token


def _headers_to_dict(headers: Any) -> dict[str, str]:
    """Normalize HTTP headers while keeping helper-backed requests compatible."""
    if headers is None:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except Exception:
        return {}


def _fallback_req(
    method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any, dict[str, str]]:
    token = _resolve_clickup_token()
    if not token:
        print(
            "ERROR: CLICKUP_API_TOKEN not set in env and could not be resolved via 1Password",
            file=sys.stderr,
        )
        raise SystemExit(2)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw.strip() else {}), _headers_to_dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, None, _headers_to_dict(err.headers)
    except Exception as err:  # pragma: no cover - exercised as exit path
        print(f"request error: {err!r}", file=sys.stderr)
        return 0, None, {}


def _req(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    helper = _load_clickup_helper()
    if helper is not None and hasattr(helper, "_req"):
        return helper._req(method, path, body)
    return _fallback_req(method, path, body)


def _split_response(response: Any) -> tuple[int, Any, dict[str, str]]:
    """Accept legacy two-tuples from the deployed helper plus header-aware ones."""
    if not isinstance(response, tuple) or len(response) not in (2, 3):
        raise ClickUpApiError(status=0, path="(request)", message="invalid response tuple")
    status, data = response[:2]
    headers = response[2] if len(response) == 3 else {}
    return int(status or 0), data, _headers_to_dict(headers)


def _header_delay_seconds(headers: dict[str, str]) -> float | None:
    """Return ClickUp's Retry-After/reset guidance when it is usable."""
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=dt.timezone.utc)
                return max(0.0, retry_at.timestamp() - time.time())
            except (TypeError, ValueError, IndexError, OverflowError):
                pass

    for name in ("x-ratelimit-reset", "x-rate-limit-reset", "ratelimit-reset"):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            reset = float(raw)
        except ValueError:
            continue
        # ClickUp deployments use either an epoch reset timestamp or a
        # remaining-seconds value. Treat clearly future epoch values as dates.
        return max(0.0, reset - time.time()) if reset > time.time() else max(0.0, reset)
    return None


def _rate_limit_delay_seconds(headers: dict[str, str], attempt: int) -> float:
    suggested = _header_delay_seconds(headers)
    fallback = RATE_LIMIT_FALLBACK_BACKOFF_SECONDS * (2**attempt)
    delay = fallback if suggested is None else suggested
    # The attempt count is bounded, but server guidance is not truncated: a
    # 429 with Retry-After: 120 must never be retried at 60 seconds.
    return max(1.0, delay)


def _rate_limit_guidance(headers: dict[str, str]) -> dict[str, str]:
    """Keep only retry metadata; headers can otherwise carry sensitive data."""
    return {
        key: headers[key]
        for key in ("retry-after", "x-ratelimit-reset", "x-rate-limit-reset", "ratelimit-reset")
        if key in headers
    }


def _get(path: str) -> dict[str, Any]:
    for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
        status, data, headers = _split_response(_req("GET", path))
        evidence = _ACTIVE_REFRESH_EVIDENCE.get()
        if evidence is not None:
            evidence.request_count += 1
        if status == 429 and attempt < RATE_LIMIT_MAX_RETRIES:
            delay = _rate_limit_delay_seconds(headers, attempt)
            if evidence is not None:
                evidence.rate_limit_count += 1
                evidence.retry_delays_seconds.append(delay)
                evidence.rate_limit_guidance.append(_rate_limit_guidance(headers))
            print(
                f"WARNING: ClickUp 429 on GET {path}, retry "
                f"{attempt + 1}/{RATE_LIMIT_MAX_RETRIES} in {delay:g}s",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue
        break
    if status in (401, 403):
        print(f"ERROR: ClickUp auth failed (HTTP {status}) on GET {path}", file=sys.stderr)
        raise SystemExit(3)
    if not status or status >= 500:
        _log_failure(f"api_error status={status} endpoint=GET {path}")
        print(
            f"ERROR: ClickUp server/network error (HTTP {status}) on GET {path}",
            file=sys.stderr,
        )
        raise SystemExit(4)
    if status >= 400 or data is None:
        if status == 429:
            evidence = _ACTIVE_REFRESH_EVIDENCE.get()
            if evidence is not None:
                evidence.rate_limit_count += 1
                evidence.rate_limit_guidance.append(_rate_limit_guidance(headers))
            _log_failure(
                f"rate_limit_exhausted endpoint=GET {path} retries={RATE_LIMIT_MAX_RETRIES}"
            )
        raise ClickUpApiError(status=status, path=path, message="request failed")
    if not isinstance(data, dict):
        raise ClickUpApiError(status=status, path=path, message="non-object response")
    return data


def _log_failure(reason: str) -> None:
    try:
        DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _iso_utc(),
            "kind": "clickup_topology_refresh_failed",
            "severity": "escalate",
            "reason": reason,
        }
        with DRIFT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:  # pragma: no cover - best effort
        print(f"WARN: could not write drift log: {exc}", file=sys.stderr)


def _log_drift(diff_lines: list[str]) -> None:
    try:
        DRIFT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _iso_utc(),
            "kind": "clickup_topology_drift",
            "severity": "info",
            "diff_summary": diff_lines[:50],
            "n_changes": len(diff_lines),
        }
        with DRIFT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        print(
            f"ALERT: clickup_topology_drift: {len(diff_lines)} change(s) logged to {DRIFT_LOG}",
            file=sys.stderr,
        )
    except Exception as exc:  # pragma: no cover - best effort
        print(f"WARN: could not write drift log: {exc}", file=sys.stderr)


def fetch_spaces(team_id: str) -> list[dict[str, Any]]:
    return _get(f"/team/{team_id}/space?archived=false").get("spaces", [])


def fetch_folders(space_id: str) -> list[dict[str, Any]]:
    return _get(f"/space/{space_id}/folder?archived=false").get("folders", [])


def fetch_space_lists(space_id: str) -> list[dict[str, Any]]:
    return _get(f"/space/{space_id}/list?archived=false").get("lists", [])


def fetch_folder_lists(folder_id: str) -> tuple[list[dict[str, Any]], bool]:
    return _get(f"/folder/{folder_id}/list?archived=false").get("lists", []), True


def fetch_list_detail(list_id: str) -> dict[str, Any]:
    if list_id not in _LIST_DETAIL_CACHE:
        _LIST_DETAIL_CACHE[list_id] = _get(f"/list/{list_id}")
    return _LIST_DETAIL_CACHE[list_id]


def fetch_list_fields(list_id: str) -> list[dict[str, Any]]:
    global _LAST_FIELD_DISCOVERY_AT
    if list_id not in _LIST_FIELDS_CACHE:
        if _LAST_FIELD_DISCOVERY_AT is not None:
            delay = FIELD_DISCOVERY_MIN_INTERVAL_SECONDS - (time.monotonic() - _LAST_FIELD_DISCOVERY_AT)
            if delay > 0:
                time.sleep(delay)
        _LIST_FIELDS_CACHE[list_id] = _get(f"/list/{list_id}/field").get("fields", [])
        _LAST_FIELD_DISCOVERY_AT = time.monotonic()
    return _LIST_FIELDS_CACHE[list_id]


def fetch_recent_task_tags(
    team_id: str, lookback_days: int = TASK_LOOKBACK_DAYS
) -> tuple[Counter, int, list[dict[str, Any]]]:
    """Return the recent tag sample plus observed per-list assignment patterns.

    The workspace endpoint already returns task assignees and list metadata, so
    capture the assignment view during the existing bounded task walk rather
    than spending another API budget on a second, duplicate scan.
    """
    counter: Counter = Counter()
    total_seen = 0
    assignment_counts: dict[str, dict[str, Any]] = {}
    cutoff_ms = int((_now_utc().timestamp() - lookback_days * 86400) * 1000)
    for page in range(TASK_PAGE_LIMIT):
        data = _get(
            f"/team/{team_id}/task?page={page}&subtasks=false&include_closed=false"
            f"&order_by=updated&reverse=true&date_updated_gt={cutoff_ms}"
        )
        tasks = data.get("tasks", [])
        if not tasks:
            break
        for task in tasks:
            total_seen += 1
            for tag in task.get("tags") or []:
                name = tag.get("name")
                if name:
                    counter[name] += 1
            list_data = task.get("list") or {}
            list_id = str(list_data.get("id") or "unknown")
            pattern = assignment_counts.setdefault(
                list_id,
                {
                    "list_id": list_id,
                    "list_name": list_data.get("name") or "(unknown list)",
                    "sampled_task_count": 0,
                    "assignees": Counter(),
                },
            )
            pattern["sampled_task_count"] += 1
            assignees = task.get("assignees") or []
            if assignees:
                for assignee in assignees:
                    name = assignee.get("username") or assignee.get("email") or assignee.get("id")
                    if name:
                        pattern["assignees"][str(name)] += 1
            else:
                pattern["assignees"]["(unassigned)"] += 1
        if data.get("last_page"):
            break
    patterns = [
        {
            "list_id": pattern["list_id"],
            "list_name": pattern["list_name"],
            "sampled_task_count": pattern["sampled_task_count"],
            "assignees": [
                {"name": name, "count": count}
                for name, count in sorted(
                    pattern["assignees"].items(), key=lambda item: (-item[1], item[0])
                )
            ],
        }
        for pattern in assignment_counts.values()
    ]
    return counter, total_seen, sorted(
        patterns, key=lambda item: (item["list_name"], item["list_id"])
    )


def _normalize_status(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": status.get("id"),
        "status": status.get("status"),
        "type": status.get("type"),
        "orderindex": status.get("orderindex"),
        "color": status.get("color"),
    }


def _summarize_type_config(type_config: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if "default" in type_config:
        summary["default"] = type_config.get("default")
    if "placeholder" in type_config:
        summary["placeholder"] = type_config.get("placeholder")
    options = type_config.get("options")
    if isinstance(options, list):
        summary["options"] = [
            {
                "id": option.get("id"),
                "name": option.get("name"),
                "color": option.get("color"),
                "orderindex": option.get("orderindex"),
            }
            for option in options
            if isinstance(option, dict)
        ]
    return summary


def _normalize_custom_field(field: dict[str, Any]) -> dict[str, Any]:
    type_config = field.get("type_config") or {}
    return {
        "id": field.get("id"),
        "name": field.get("name"),
        "type": field.get("type"),
        "required": bool(field.get("required", False)),
        "hide_from_guests": bool(field.get("hide_from_guests", False)),
        "type_config": type_config,
        "metadata": _summarize_type_config(type_config),
    }


def _slugify_alias(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def _alias_candidates(name: str) -> set[str]:
    lowered = name.lower()
    candidates = {lowered}
    for separator in ("|", "/", "-", ":", "("):
        if separator in lowered:
            head = lowered.split(separator, 1)[0].strip()
            if head:
                candidates.add(head)
    cleaned = re.sub(
        r"\b(ppc|seo|aeo|content|cro|web|reporting|strategy|marketing|development|dev|active tasks?)\b",
        " ",
        lowered,
    )
    cleaned = re.sub(r"[^a-z0-9.]+", " ", cleaned)
    tokens = [t for t in cleaned.split() if t not in _CLIENT_ALIAS_STOPWORDS and len(t) > 2]
    if tokens:
        candidates.add(" ".join(tokens))
    return {_slugify_alias(candidate) for candidate in candidates if _slugify_alias(candidate)}


def _cron_expr_to_hours(expr: str) -> float | None:
    """Best-effort conversion of a simple 5-field cron expr to a period in hours.

    Only handles the shapes this refresh cron realistically uses (fixed
    minute + ``*/N`` hour, or ``*/N`` minute with ``*`` hour). Anything else
    returns ``None`` — callers fall back to showing the raw expr instead of
    guessing.
    """
    parts = expr.split()
    if len(parts) != 5:
        return None
    minute, hour = parts[0], parts[1]
    hour_step = re.fullmatch(r"\*/(\d+)", hour)
    if hour_step and minute.isdigit():
        return float(hour_step.group(1))
    minute_step = re.fullmatch(r"\*/(\d+)", minute)
    if hour == "*" and minute_step:
        return round(int(minute_step.group(1)) / 60, 4)
    return None


def _derive_refresh_cadence(script_name: str = THIS_SCRIPT_NAME) -> dict[str, Any]:
    """Read the ACTUAL registered cron schedule for this script's own job.

    86e1vw79j (4 prior FAIL cycles): the cadence text/hours were a hardcoded
    literal that drifted stale the moment the registered cron changed. This
    always re-derives from the live job registration instead, so the map can
    never claim a cadence the cron isn't actually running.
    """
    try:
        from cron.jobs import load_jobs  # local import: optional dependency

        for job in load_jobs():
            if job.get("script") != script_name:
                continue
            expr = str((job.get("schedule") or {}).get("expr") or "").strip()
            if not expr:
                continue
            return {
                "hours": _cron_expr_to_hours(expr),
                "cron_expr": expr,
                "source": "cron_registration",
            }
    except Exception:
        pass
    # Cron subsystem unreadable or job not found (e.g. renamed) — report the
    # script's own TTL as a best-effort fallback rather than asserting a
    # cadence we couldn't actually verify.
    return {
        "hours": REFRESH_TTL_SECONDS / 3600,
        "cron_expr": None,
        "source": "ttl_fallback",
    }


def build_clients_aliases(
    spaces: list[dict[str, Any]],
    folders: list[dict[str, Any]],
    lists: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    alias_map: dict[str, list[dict[str, Any]]] = {}
    for kind, rows in (("space", spaces), ("folder", folders), ("list", lists)):
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            for alias in _alias_candidates(name):
                refs = alias_map.setdefault(alias, [])
                ref = {"kind": kind, "id": row.get("id"), "name": name}
                if ref not in refs:
                    refs.append(ref)
    return {key: sorted(value, key=lambda item: (item["kind"], str(item["name"]))) for key, value in sorted(alias_map.items())}


def build_workspace_map(team_id: str) -> dict[str, Any]:
    global _LAST_FIELD_DISCOVERY_AT
    _LIST_DETAIL_CACHE.clear()
    _LIST_FIELDS_CACHE.clear()
    _LAST_FIELD_DISCOVERY_AT = None

    spaces_raw = sorted(fetch_spaces(team_id), key=lambda item: (item.get("name", ""), str(item.get("id", ""))))
    task_sample = fetch_recent_task_tags(team_id)
    recent_tags, recent_task_count = task_sample[:2]
    assignment_patterns = task_sample[2] if len(task_sample) > 2 else []

    spaces: list[dict[str, Any]] = []
    folders: list[dict[str, Any]] = []
    lists: list[dict[str, Any]] = []
    seen_list_ids: set[str] = set()

    for space in spaces_raw:
        space_id = str(space.get("id"))
        folder_rows = sorted(fetch_folders(space_id), key=lambda item: (item.get("name", ""), str(item.get("id", ""))))
        space_lists_raw = sorted(fetch_space_lists(space_id), key=lambda item: (item.get("name", ""), str(item.get("id", ""))))
        spaces.append(
            {
                "id": space_id,
                "name": space.get("name"),
                "archived": bool(space.get("archived", False)),
                "private": bool(space.get("private", False)),
                "folder_ids": [str(folder.get("id")) for folder in folder_rows],
                "folderless_list_ids": [str(item.get("id")) for item in space_lists_raw],
            }
        )

        for folder in folder_rows:
            folder_id = str(folder.get("id"))
            folder_lists_raw, fetch_ok = fetch_folder_lists(folder_id)
            folder_lists_raw = sorted(folder_lists_raw, key=lambda item: (item.get("name", ""), str(item.get("id", ""))))
            folder_list_refs: list[dict[str, Any]] = []
            for list_stub in folder_lists_raw:
                list_id = str(list_stub.get("id"))
                folder_list_refs.append({
                    "id": list_id,
                    "name": list_stub.get("name"),
                    "archived": bool(list_stub.get("archived", False)),
                })
                if list_id in seen_list_ids:
                    continue
                seen_list_ids.add(list_id)
                detail = fetch_list_detail(list_id)
                lists.append(
                    {
                        "id": list_id,
                        "name": detail.get("name") or list_stub.get("name"),
                        "space_id": space_id,
                        "space_name": space.get("name"),
                        "folder_id": folder_id,
                        "folder_name": folder.get("name"),
                        "archived": bool(detail.get("archived", list_stub.get("archived", False))),
                        "permission_level": detail.get("permission_level"),
                        "statuses": [
                            _normalize_status(status)
                            for status in sorted(detail.get("statuses") or [], key=lambda item: str(item.get("orderindex", "")))
                        ],
                        "custom_fields": [
                            _normalize_custom_field(field)
                            for field in sorted(fetch_list_fields(list_id), key=lambda item: (item.get("name", ""), str(item.get("id", ""))))
                        ],
                    }
                )

            folders.append(
                {
                    "id": folder_id,
                    "name": folder.get("name"),
                    "space_id": space_id,
                    "space_name": space.get("name"),
                    "archived": bool(folder.get("archived", False)),
                    "hidden": bool(folder.get("hidden", False)),
                    "fetch_ok": fetch_ok,
                    "lists": folder_list_refs,
                }
            )

        for list_stub in space_lists_raw:
            list_id = str(list_stub.get("id"))
            if list_id in seen_list_ids:
                continue
            seen_list_ids.add(list_id)
            detail = fetch_list_detail(list_id)
            lists.append(
                {
                    "id": list_id,
                    "name": detail.get("name") or list_stub.get("name"),
                    "space_id": space_id,
                    "space_name": space.get("name"),
                    "folder_id": None,
                    "folder_name": None,
                    "archived": bool(detail.get("archived", list_stub.get("archived", False))),
                    "permission_level": detail.get("permission_level"),
                    "statuses": [
                        _normalize_status(status)
                        for status in sorted(detail.get("statuses") or [], key=lambda item: str(item.get("orderindex", "")))
                    ],
                    "custom_fields": [
                        _normalize_custom_field(field)
                        for field in sorted(fetch_list_fields(list_id), key=lambda item: (item.get("name", ""), str(item.get("id", ""))))
                    ],
                }
            )

    lists.sort(key=lambda item: (item.get("space_name") or "", item.get("folder_name") or "", item.get("name") or "", str(item.get("id") or "")))
    folders.sort(key=lambda item: (item.get("space_name") or "", item.get("name") or "", str(item.get("id") or "")))

    generated_at = _now_ms()
    cadence = _derive_refresh_cadence()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generated_at_iso": _iso_utc(generated_at),
        "team_id": team_id,
        "refresh_cadence_hours": cadence["hours"],
        "refresh_cadence_cron_expr": cadence["cron_expr"],
        "refresh_cadence_source": cadence["source"],
        "root_cause_note_2026_07_09": ROOT_CAUSE_NOTE_2026_07_09,
        "spaces": spaces,
        "folders": folders,
        "lists": lists,
        "task_tags": {
            "sampled_task_count": recent_task_count,
            "lookback_days": TASK_LOOKBACK_DAYS,
            "tags": [
                {"name": name, "count": count}
                for name, count in sorted(recent_tags.items(), key=lambda item: (-item[1], item[0]))
            ],
        },
        "assignment_patterns": assignment_patterns,
        "write_boundaries": [
            {
                "list_id": list_row["id"],
                "list_name": list_row["name"],
                "permission_level": list_row.get("permission_level") or "unknown",
                "evidence": "GET /list/{id} permission_level (read-only snapshot)".format(
                    id=list_row["id"]
                ),
            }
            for list_row in lists
        ],
        "clients_aliases": build_clients_aliases(spaces, folders, lists),
    }


def _format_cadence_text(workspace_map: dict[str, Any]) -> str:
    """Render the derived cadence (see ``_derive_refresh_cadence``) as prose."""
    hours = workspace_map.get("refresh_cadence_hours")
    expr = workspace_map.get("refresh_cadence_cron_expr")
    source = workspace_map.get("refresh_cadence_source")
    if hours is None:
        text = "unknown (cron expr not recognized)" if expr else "unknown (cron registration unavailable)"
    elif hours < 1:
        text = f"every {round(hours * 60)} minutes"
    elif float(hours).is_integer():
        n = int(hours)
        text = f"every {n} hour{'s' if n != 1 else ''}"
    else:
        text = f"every {hours} hours"
    if expr:
        text += f" (`{expr}`)"
    if source == "ttl_fallback":
        text += " — fallback default, live cron registration could not be read"
    return text


def render_markdown_mirror(workspace_map: dict[str, Any]) -> str:
    out: list[str] = []
    generated_iso = workspace_map.get("generated_at_iso") or _iso_utc(workspace_map.get("generated_at"))
    out.append(f"# ClickUp workspace map (auto-generated {generated_iso})")
    out.append("")
    out.append("## Header")
    out.append("")
    out.append(f"**Team ID:** `{workspace_map['team_id']}`  ")
    out.append(f"**Schema version:** `{workspace_map['schema_version']}`  ")
    out.append(f"**Refresh cadence:** {_format_cadence_text(workspace_map)}  ")
    out.append(f"**JSON cache:** `{display_hermes_home()}/state/clickup-map.json`")
    out.append("")
    out.append(f"**Root cause note ({_ROOT_CAUSE_DATE}):** {workspace_map.get('root_cause_note_2026_07_09', ROOT_CAUSE_NOTE_2026_07_09)}")
    out.append("")
    aliases = workspace_map.get("clients_aliases") or {}
    if aliases:
        out.append(
            "**Known client aliases:** "
            + ", ".join(sorted(aliases))
            + ""
        )
        out.append("")

    out.append("## Spaces")
    out.append("")
    out.append("| id | name | archived | private | folder_ids | folderless_list_ids |")
    out.append("|----|------|----------|---------|------------|----------------------|")
    for space in workspace_map.get("spaces", []):
        out.append(
            "| {id} | {name} | {archived} | {private} | {folder_ids} | {list_ids} |".format(
                id=space.get("id"),
                name=space.get("name"),
                archived=space.get("archived"),
                private=space.get("private"),
                folder_ids=", ".join(space.get("folder_ids") or []),
                list_ids=", ".join(space.get("folderless_list_ids") or []),
            )
        )
    out.append("")

    out.append("## Folders")
    out.append("")
    out.append("| id | name | space | archived | hidden | fetch_ok | lists |")
    out.append("|----|------|-------|----------|--------|----------|-------|")
    for folder in workspace_map.get("folders", []):
        out.append(
            "| {id} | {name} | {space} | {archived} | {hidden} | {fetch_ok} | {lists} |".format(
                id=folder.get("id"),
                name=folder.get("name"),
                space=folder.get("space_name"),
                archived=folder.get("archived"),
                hidden=folder.get("hidden"),
                fetch_ok=folder.get("fetch_ok"),
                lists=", ".join(list_row.get("name") or "" for list_row in folder.get("lists", [])),
            )
        )
    out.append("")

    out.append("## Lists")
    out.append("")
    for list_row in workspace_map.get("lists", []):
        out.append(f"### {list_row.get('name')} (`{list_row.get('id')}`)")
        out.append("")
        out.append(f"- Space: {list_row.get('space_name')} (`{list_row.get('space_id')}`)")
        out.append(
            f"- Folder: {list_row.get('folder_name') or '(space-level)'} "
            f"(`{list_row.get('folder_id') or '-'}`)"
        )
        out.append(f"- Archived: {list_row.get('archived')}")
        out.append("")

    out.append("## Statuses per list")
    out.append("")
    for list_row in workspace_map.get("lists", []):
        out.append(f"### {list_row.get('name')} (`{list_row.get('id')}`)")
        out.append("")
        out.append("| status | type | orderindex | color |")
        out.append("|--------|------|------------|-------|")
        for status in list_row.get("statuses", []):
            out.append(
                f"| {status.get('status')} | {status.get('type')} | {status.get('orderindex')} | {status.get('color')} |"
            )
        if not list_row.get("statuses"):
            out.append("| _(none returned)_ |  |  |  |")
        out.append("")

    out.append("## Custom fields per list")
    out.append("")
    for list_row in workspace_map.get("lists", []):
        out.append(f"### {list_row.get('name')} (`{list_row.get('id')}`)")
        out.append("")
        out.append("| name | type | required | metadata |")
        out.append("|------|------|----------|----------|")
        for field in list_row.get("custom_fields", []):
            out.append(
                f"| {field.get('name')} | {field.get('type')} | {field.get('required')} | "
                f"{json.dumps(field.get('metadata') or {}, sort_keys=True)} |"
            )
        if not list_row.get("custom_fields"):
            out.append("| _(none)_ |  |  |  |")
        out.append("")

    tags = workspace_map.get("task_tags") or {}
    out.append("## Tag taxonomy")
    out.append("")
    out.append(
        f"_Sampled {tags.get('sampled_task_count', 0)} task(s) updated in the last {tags.get('lookback_days', TASK_LOOKBACK_DAYS)} days._"
    )
    out.append("")
    if tags.get("tags"):
        out.append("| tag | count |")
        out.append("|-----|-------|")
        for tag in tags.get("tags", []):
            out.append(f"| {tag.get('name')} | {tag.get('count')} |")
    else:
        out.append("_(no tags observed in the sample window)_")
    out.append("")

    out.append("## Assignment patterns")
    out.append("")
    out.append("_Observed in the same recent task sample as the tag taxonomy; this is descriptive, not an assignment policy._")
    out.append("")
    out.append("| list | sampled tasks | observed assignees |")
    out.append("|------|---------------|--------------------|")
    for pattern in workspace_map.get("assignment_patterns") or []:
        rendered_assignees = ", ".join(
            f"{assignee.get('name')} ({assignee.get('count')})"
            for assignee in pattern.get("assignees") or []
        ) or "(none)"
        out.append(
            f"| {pattern.get('list_name')} (`{pattern.get('list_id')}`) | "
            f"{pattern.get('sampled_task_count')} | {rendered_assignees} |"
        )
    if not workspace_map.get("assignment_patterns"):
        out.append("| _(no recent tasks sampled)_ | 0 |  |")
    out.append("")

    out.append("## Last-known write boundaries")
    out.append("")
    out.append("_Read-only observation from the ClickUp list metadata; this refresh does not attempt writes._")
    out.append("")
    out.append("| list | permission level | evidence |")
    out.append("|------|------------------|----------|")
    for boundary in workspace_map.get("write_boundaries") or []:
        out.append(
            f"| {boundary.get('list_name')} (`{boundary.get('list_id')}`) | "
            f"{boundary.get('permission_level')} | {boundary.get('evidence')} |"
        )
    if not workspace_map.get("write_boundaries"):
        out.append("| _(no lists returned)_ | unknown |  |")
    out.append("")

    out.append("## Refresh notes")
    out.append("")
    out.append(f"- Generated at: `{generated_iso}`")
    out.append(f"- Cron source: `{workspace_map.get('refresh_cadence_source', 'unknown')}`")
    out.append(f"- Drift log: `{display_hermes_home()}/state/clickup_topology_drift.jsonl`")
    out.append("- This job is read-only against ClickUp; it only updates the local cache and this canonical note.")
    out.append("")
    return "\n".join(out)


def _normalize_markdown_for_drift(body: str) -> str:
    lines_out: list[str] = []
    for line in body.splitlines():
        if line.startswith("# ClickUp workspace map (auto-generated"):
            continue
        line = re.sub(
            r"^_Sampled \d+ task\(s\) updated in the last \d+ days\._$",
            "_Sampled N task(s) updated in the last D days._",
            line,
        )
        lines_out.append(line)
    return "\n".join(lines_out)


def detect_markdown_drift(prior_body: str, new_body: str) -> list[str]:
    prior_normalized = _normalize_markdown_for_drift(prior_body)
    new_normalized = _normalize_markdown_for_drift(new_body)
    if prior_normalized == new_normalized:
        return []
    return list(
        unified_diff(
            prior_normalized.splitlines(),
            new_normalized.splitlines(),
            fromfile="prior",
            tofile="current",
            lineterm="",
        )
    )


def read_cached_workspace_map(path: Path = DEFAULT_JSON_PATH) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def cache_is_fresh(
    workspace_map: dict[str, Any] | None,
    *,
    now_ms: int | None = None,
    max_age_seconds: int = REFRESH_TTL_SECONDS,
) -> bool:
    if not workspace_map:
        return False
    if workspace_map.get("schema_version") != SCHEMA_VERSION:
        return False
    generated_at = workspace_map.get("generated_at")
    if not isinstance(generated_at, int):
        return False
    if now_ms is None:
        now_ms = _now_ms()
    return now_ms - generated_at <= max_age_seconds * 1000


def _atomic_write_text(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        fd, raw_temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        temp_path = Path(raw_temp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _write_json(path: Path, workspace_map: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(workspace_map, indent=2, sort_keys=True) + "\n")


def _write_markdown(path: Path, body: str) -> None:
    _atomic_write_text(path, body)


def _receipt_path_for(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.receipt.json")


def _workspace_map_bytes(workspace_map: dict[str, Any]) -> bytes:
    return (json.dumps(workspace_map, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _validate_workspace_map(workspace_map: dict[str, Any], team_id: str) -> None:
    """Refuse incomplete API output before replacing the last known-good map."""
    # The topology itself is the publish boundary. Supplemental observations
    # were added after the original schema and remain backward-compatible.
    required_lists = ("spaces", "folders", "lists")
    if not isinstance(workspace_map, dict):
        raise WorkspaceMapValidationError("workspace map is not an object")
    if workspace_map.get("schema_version") != SCHEMA_VERSION:
        raise WorkspaceMapValidationError("workspace map has an unexpected schema version")
    if workspace_map.get("team_id") != team_id:
        raise WorkspaceMapValidationError("workspace map team does not match refresh request")
    if not isinstance(workspace_map.get("generated_at"), int):
        raise WorkspaceMapValidationError("workspace map is missing generated_at")
    for key in required_lists:
        if not isinstance(workspace_map.get(key), list):
            raise WorkspaceMapValidationError(f"workspace map is missing complete {key}")
    if not isinstance(workspace_map.get("task_tags"), dict):
        raise WorkspaceMapValidationError("workspace map is missing task tag evidence")
    if not isinstance(workspace_map.get("clients_aliases"), dict):
        raise WorkspaceMapValidationError("workspace map is missing client aliases")


def _evidence_fields(evidence: RefreshEvidence) -> dict[str, Any]:
    return {
        "request_count": evidence.request_count,
        "cache_present": evidence.cache_present,
        "cache_used": evidence.cache_used,
        "cache_retained": evidence.cache_retained,
        "rate_limit": {
            "count": evidence.rate_limit_count,
            "guidance": evidence.rate_limit_guidance,
            "retry_delays_seconds": evidence.retry_delays_seconds,
        },
    }


def _refresh_receipt(
    workspace_map: dict[str, Any] | None,
    evidence: RefreshEvidence,
    *,
    outcome: str,
    error: str | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "attempted_at": evidence.started_at,
        "written_at": _iso_utc(),
        **_evidence_fields(evidence),
    }
    if workspace_map is not None:
        receipt.update(
            {
                "team_id": workspace_map.get("team_id"),
                "generated_at": workspace_map.get("generated_at"),
                "map_sha256": hashlib.sha256(_workspace_map_bytes(workspace_map)).hexdigest(),
            }
        )
    if error is not None:
        receipt["error"] = error
    return receipt


def _write_refresh_receipt(
    path: Path,
    workspace_map: dict[str, Any] | None,
    evidence: RefreshEvidence,
    *,
    outcome: str,
    error: str | None = None,
) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            _refresh_receipt(workspace_map, evidence, outcome=outcome, error=error),
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _receipt_matches_workspace_map(receipt_path: Path, workspace_map: dict[str, Any]) -> bool:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected_sha = hashlib.sha256(_workspace_map_bytes(workspace_map)).hexdigest()
    return receipt.get("outcome") == "success" and all(
        receipt.get(key) == expected
        for key, expected in (
            ("schema_version", SCHEMA_VERSION),
            ("team_id", workspace_map.get("team_id")),
            ("generated_at", workspace_map.get("generated_at")),
            ("map_sha256", expected_sha),
        )
    )


class _WorkspaceMapLock:
    def __init__(self, output_path: Path) -> None:
        self.output_path = output_path
        self.handle: Any | None = None
        self.receipt_path: Path | None = None
        self.evidence: RefreshEvidence | None = None
        self.cached: dict[str, Any] | None = None

    def record_failure_context(
        self,
        receipt_path: Path,
        evidence: RefreshEvidence,
        cached: dict[str, Any] | None,
    ) -> None:
        self.receipt_path = receipt_path
        self.evidence = evidence
        self.cached = cached

    def __enter__(self) -> None:
        """Serialize cache reads, refreshes, receipts, and map publication."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.output_path.with_name(f".{self.output_path.name}.lock")
        self.handle = lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        assert self.handle is not None
        try:
            if _exc_type is not None and self.receipt_path is not None and self.evidence is not None:
                self.evidence.cache_retained = self.cached is not None
                try:
                    _write_refresh_receipt(
                        self.receipt_path,
                        self.cached,
                        self.evidence,
                        outcome="failed",
                        error=f"{_exc_type.__name__}: {_exc}",
                    )
                except Exception as receipt_exc:
                    _log_failure(f"refresh_failure_receipt_write_failed error={receipt_exc!r}")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
        return False


def _workspace_map_lock(output_path: Path) -> _WorkspaceMapLock:
    """Serialize cache reads, refreshes, receipts, and map publication."""
    return _WorkspaceMapLock(output_path)


def write_workspace_map_note(
    body: str,
    path: Path = DEFAULT_BRAIN_NOTE_PATH,
) -> None:
    """Atomically overwrite the one canonical brain note.

    This intentionally uses the filesystem rather than the optional
    ``basic-memory`` CLI: the refresh job's read/write contract must work on
    hosts where that binary is not installed. Failures are terminal because a
    requested brain write is part of a successful refresh, not best effort.
    """
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(raw_temp_path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(temp_path, path)
        temp_path = None
    except OSError as exc:
        raise WorkspaceMapNoteError(
            f"could not write canonical workspace-map note at {path}: {exc}"
        ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def read_workspace_map_note(
    path: Path = DEFAULT_BRAIN_NOTE_PATH,
) -> str:
    """Read the canonical note (equivalent to ``read_note`` for this map)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceMapNoteError(
            f"could not read canonical workspace-map note at {path}: {exc}"
        ) from exc


def ensure_workspace_map(
    *,
    team_id: str = DEFAULT_TEAM_ID,
    force: bool = False,
    max_age_seconds: int = REFRESH_TTL_SECONDS,
    output_path: Path = DEFAULT_JSON_PATH,
    markdown_path: Path = DEFAULT_MARKDOWN_PATH,
    receipt_path: Path | None = None,
    write_brain_note: bool = True,
    brain_note_path: Path = DEFAULT_BRAIN_NOTE_PATH,
) -> dict[str, Any]:
    if receipt_path is None:
        receipt_path = _receipt_path_for(output_path)
    evidence = RefreshEvidence(started_at=_iso_utc())
    evidence_token = _ACTIVE_REFRESH_EVIDENCE.set(evidence)
    try:
        # A flock covers both artifact replacement and the decision to reuse a
        # cache, so concurrent cron/manual invocations cannot publish a map
        # and receipt from different attempts.
        lock = _workspace_map_lock(output_path)
        with lock:
            cached = read_cached_workspace_map(output_path)
            lock.record_failure_context(receipt_path, evidence, cached)
            evidence.cache_present = cached is not None
            cache_receipt_valid = not receipt_path.exists() or (
                cached is not None and _receipt_matches_workspace_map(receipt_path, cached)
            )
            if receipt_path.exists() and not cache_receipt_valid:
                _log_failure(f"cache_receipt_mismatch path={output_path}")
            if not force and cache_receipt_valid and cache_is_fresh(cached, max_age_seconds=max_age_seconds):
                evidence.cache_used = True
                if write_brain_note:
                    write_workspace_map_note(
                        render_markdown_mirror(cached),
                        brain_note_path,
                    )
                _write_refresh_receipt(receipt_path, cached, evidence, outcome="success")
                return cached

            workspace_map = build_workspace_map(team_id)
            _validate_workspace_map(workspace_map, team_id)
            markdown = render_markdown_mirror(workspace_map)
            prior_markdown = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else None
            if prior_markdown is not None:
                _write_markdown(markdown_path.with_name(PRIOR_MARKDOWN_PATH.name), prior_markdown)

            # These are all prepared from a fully validated in-memory map.
            # Publish receipt then map under the same lock; a process death in
            # between creates a mismatched receipt that the next reader marks
            # red rather than calling the retained map fresh.
            _write_refresh_receipt(receipt_path, workspace_map, evidence, outcome="success")
            _write_json(output_path, workspace_map)
            _write_markdown(markdown_path, markdown)
            if prior_markdown is not None:
                diff_lines = detect_markdown_drift(prior_markdown, markdown)
                if diff_lines:
                    _log_drift(diff_lines)
            if write_brain_note:
                write_workspace_map_note(markdown, brain_note_path)
            return workspace_map
    except Exception as exc:
        _log_failure(f"refresh_failed error={type(exc).__name__}: {exc}")
        raise
    finally:
        _ACTIVE_REFRESH_EVIDENCE.reset(evidence_token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-id", default=os.environ.get("CLICKUP_TEAM_ID", DEFAULT_TEAM_ID))
    parser.add_argument("--output", default=str(DEFAULT_JSON_PATH), help="JSON cache path")
    parser.add_argument("--markdown-output", default=str(DEFAULT_MARKDOWN_PATH), help="Markdown mirror path")
    parser.add_argument(
        "--brain-output",
        default=str(DEFAULT_BRAIN_NOTE_PATH),
        help="Canonical brain note path",
    )
    parser.add_argument("--max-age-seconds", type=int, default=REFRESH_TTL_SECONDS, help="Cache TTL before refresh")
    parser.add_argument("--force", action="store_true", help="Bypass the 6h cache TTL and refresh now")
    parser.add_argument("--local-only", action="store_true", help="Skip the markdown brain-note mirror write")
    parser.add_argument(
        "--read-note",
        action="store_true",
        help='Read the canonical note (equivalent to read_note("clickup-workspace-map")) and exit',
    )
    parser.add_argument("--print-json", action="store_true", help="Print the resulting workspace map JSON to stdout")
    args = parser.parse_args()

    try:
        if args.read_note:
            sys.stdout.write(read_workspace_map_note(Path(args.brain_output)))
            return 0

        workspace_map = ensure_workspace_map(
            team_id=args.team_id,
            force=args.force,
            max_age_seconds=args.max_age_seconds,
            output_path=Path(args.output),
            markdown_path=Path(args.markdown_output),
            write_brain_note=not args.local_only,
            brain_note_path=Path(args.brain_output),
        )
    except (WorkspaceMapNoteError, WorkspaceMapValidationError, ClickUpApiError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.print_json:
        print(json.dumps(workspace_map, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
