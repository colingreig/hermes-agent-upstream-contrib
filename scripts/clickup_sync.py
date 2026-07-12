#!/usr/bin/env python3
# NOTE (2026-07-12, ClickUp 86e261t2b): this is a committed mirror of the live,
# untracked operational script at ~/.hermes/scripts/clickup_sync.py on the
# mini. That directory has no git history of its own (see
# ~/.hermes/scripts/verify-hermes-patches.sh's docstring for the *separate*
# prod-live-patches mechanism, which only covers files inside the
# ~/.hermes/hermes-agent checkout -- this file is not one of those). This
# mirror exists purely for auditability/history; it is NOT imported or
# executed from this repo and is NOT auto-synced back to the mini. If you
# change ~/.hermes/scripts/clickup_sync.py in prod, update this file too (or
# vice versa) and note the drift.
#
# Fix landed here: _curl() was building the ClickUp Authorization header from
# the LITERAL string "***" (a redaction/masking placeholder that leaked into
# the real request path) instead of calling the already-defined, unused
# _token() helper. Every request got a 401 "Oauth token not found" as a
# result. Root cause was a copy/paste of a log-safe masked value into the
# live code path, not a bad/expired/misrouted credential -- re-pasting or
# rotating the token would not have fixed it.
"""ClickUp task-index sync helpers.

This module maintains per-list JSON caches under ~/.hermes/state/clickup-tasks/
and exposes a local-first combined task index for the poll gate and review-SLA
sweeps.

Design goals:
- cheap local reads for hot paths
- delta sync when the cache is fresh-ish
- periodic full reconciliation to recover from missed deletions / list drift
- atomic writes for cache files
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, cast

TEAM_ID = os.environ.get("CLICKUP_TEAM_ID", "9017245888")
MAP_PATH = Path(os.path.expanduser("~/.hermes/state/clickup-map.json"))
CACHE_DIR = Path(os.path.expanduser("~/.hermes/state/clickup-tasks"))
STALE_AFTER_S = int(os.environ.get("CLICKUP_TASK_INDEX_STALE_AFTER_S", str(30 * 60)))
FULL_RECONCILE_AFTER_S = int(os.environ.get("CLICKUP_TASK_INDEX_FULL_RECONCILE_AFTER_S", str(24 * 60 * 60)))
SYNC_OVERLAP_MS = int(os.environ.get("CLICKUP_TASK_INDEX_SYNC_OVERLAP_MS", str(5 * 60 * 1000)))
API_BASE = "https://api.clickup.com/api/v2"
BLOCKLIST_PATH = Path(os.environ.get("IGNITE_BLOCKLIST_JSON", os.path.expanduser("~/.claude/skills/ignite-state/references/blocklist.json")))

_MAP_CACHE: Optional[Dict[str, Any]] = None
_BLOCKLIST_CACHE: Optional[Dict[str, Any]] = None


def _token() -> str:
    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token:
        print("ERROR: CLICKUP_API_TOKEN not set in env", file=sys.stderr)
        raise RuntimeError("CLICKUP_API_TOKEN missing")
    return token


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _curl(path: str, *, timeout: int = 45) -> Dict[str, Any]:
    url = f"{API_BASE}{path}"
    args = [
        "curl",
        "-sS",
        "-X",
        "GET",
        "-H",
        f"Authorization: {_token()}",
        "-H",
        "Content-Type: application/json",
        "-w",
        "\n__HTTP_STATUS__%{http_code}",
        url,
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timeout calling {path}") from exc
    except Exception as exc:
        raise RuntimeError(f"network failure calling {path}: {exc}") from exc

    text = result.stdout or ""
    if "__HTTP_STATUS__" in text:
        body_text, _, status_text = text.rpartition("__HTTP_STATUS__")
        status_text = status_text.strip().splitlines()[0]
    else:
        body_text, status_text = text, "0"
    try:
        status = int(status_text)
    except ValueError:
        status = 0
    if status in {401, 403}:
        raise PermissionError(f"ClickUp auth failed (HTTP {status}) on {path}: {body_text[:200]}")
    if status >= 500 or status == 0:
        raise RuntimeError(f"ClickUp server/network error (HTTP {status}) on {path}: {body_text[:200]}")
    try:
        return json.loads(body_text) if body_text.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"malformed JSON from {path}: {exc}; body[:200]={body_text[:200]!r}") from exc


def load_map() -> Dict[str, Any]:
    global _MAP_CACHE
    if _MAP_CACHE is not None:
        return _MAP_CACHE
    try:
        with MAP_PATH.open(encoding="utf-8") as fh:
            _MAP_CACHE = json.load(fh)
            return cast(Dict[str, Any], _MAP_CACHE)
    except Exception as exc:
        raise RuntimeError(f"could not read ClickUp map at {MAP_PATH}: {exc}") from exc


def load_blocklist() -> Dict[str, Any]:
    global _BLOCKLIST_CACHE
    if _BLOCKLIST_CACHE is not None:
        return _BLOCKLIST_CACHE
    default = {
        "clickup_project_blocklist": {"oeconnection": "OEC project board", "partstech": "PartsTech project board"},
        "publish_domain_blocklist": {"tofinoelopement": "Client site with human-gated publish policy"},
    }
    try:
        with BLOCKLIST_PATH.open(encoding="utf-8") as fh:
            loaded = json.load(fh) or {}
        if not isinstance(loaded, dict):
            loaded = {}
    except Exception:
        loaded = {}
    merged = {
        "clickup_project_blocklist": dict(loaded.get("clickup_project_blocklist") or default["clickup_project_blocklist"]),
        "publish_domain_blocklist": dict(loaded.get("publish_domain_blocklist") or default["publish_domain_blocklist"]),
    }
    _BLOCKLIST_CACHE = merged
    return merged


def _normalize_block_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _coerce_blocklist(values: Iterable[Any]) -> Set[str]:
    out: Set[str] = set()
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            out.add(text)
    return out


def clickup_project_blocklist() -> Set[str]:
    data = load_blocklist()
    return _coerce_blocklist(data.get("clickup_project_blocklist") or [])


def publish_domain_blocklist() -> Set[str]:
    data = load_blocklist()
    return _coerce_blocklist(data.get("publish_domain_blocklist") or [])


def _extract_hostname(candidate: str) -> str:
    candidate = (candidate or "").strip().lower()
    if not candidate:
        return ""
    if "://" in candidate:
        parsed = urllib.parse.urlparse(candidate)
        host = parsed.hostname or ""
    else:
        parsed = urllib.parse.urlparse("//" + candidate)
        host = parsed.hostname or candidate.split("/")[0]
    return host.strip(".")


def is_publish_domain_blocked(domain: str) -> Tuple[bool, Optional[str]]:
    """Return (True, reason) when a publish target matches the shared blocklist.

    The companion publish adapter can call this on a hostname or URL. We reduce
    the input to its hostname first so full URLs, bare hosts, and subdomain
    hosts all share the same default-allow matching behavior.
    """
    blocklist = publish_domain_blocklist()
    candidate = _extract_hostname(domain)
    if _matches_blocklisted_name(candidate, blocklist):
        return True, f"domain:{candidate!r}"
    return False, None


def _matches_blocklisted_name(candidate: str, blocklist: Set[str]) -> bool:
    candidate_raw = (candidate or "").strip().lower()
    candidate_norm = _normalize_block_token(candidate_raw)
    if not candidate_raw and not candidate_norm:
        return False
    for token in blocklist:
        token_raw = token
        token_norm = _normalize_block_token(token_raw)
        if not token_raw:
            continue
        if token_raw in candidate_raw:
            return True
        if token_norm and token_norm in candidate_norm:
            return True
    return False


def is_clickup_project_blocked(task: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Return (True, reason) when a task belongs to a blocked ClickUp project.

    The check is intentionally default-allow: only projects whose names match a
    configured blocklist token are excluded. Folder/list ids are not persisted in
    config; the live map supplies the names we compare against.
    """
    blocklist = clickup_project_blocklist()
    folder = task.get("folder") or {}
    if _matches_blocklisted_name(str(folder.get("name") or ""), blocklist):
        return True, f"folder-name:{folder.get('name')!r}"
    lst = task.get("list") or {}
    if _matches_blocklisted_name(str(lst.get("name") or ""), blocklist):
        return True, f"list-name:{lst.get('name')!r}"
    return False, None


def status_type_for_task(task: Dict[str, Any]) -> str:
    status = task.get("status") or {}
    stype = (status.get("type") or "").strip().lower()
    if stype:
        return stype
    list_id = str(((task.get("list") or {}).get("id")) or "")
    name = (status.get("status") or "").strip().lower()
    if not list_id or not name:
        return ""
    for item in list_statuses(list_id):
        if (item.get("status") or "").strip().lower() == name:
            return (item.get("type") or "").strip().lower()
    return ""


def active_list_ids() -> List[str]:
    data = load_map()
    ids: List[str] = []
    for item in data.get("lists", []) or []:
        if item.get("archived"):
            continue
        lid = item.get("id")
        if lid:
            ids.append(str(lid))
    return ids


def list_meta(list_id: str) -> Dict[str, Any]:
    data = load_map()
    for item in data.get("lists", []) or []:
        if str(item.get("id")) == str(list_id):
            return item
    return {"id": list_id}


def list_statuses(list_id: str) -> List[Dict[str, Any]]:
    return list_meta(list_id).get("statuses") or []


def review_status_name_for_list(list_id: str) -> str:
    statuses = sorted(
        list_statuses(list_id),
        key=lambda s: int(s.get("orderindex") or 0),
    )
    if len(statuses) < 4:
        return ""
    non_closed = [s for s in statuses if (s.get("type") or "").strip().lower() != "closed"]
    if not non_closed:
        return ""
    candidate = non_closed[-1]
    if (candidate.get("type") or "").strip().lower() not in {"custom", "done"}:
        return ""
    return (candidate.get("status") or "").strip().lower()


def cache_path(list_id: str) -> Path:
    return CACHE_DIR / f"{list_id}.json"


def load_cache(list_id: str) -> Optional[Dict[str, Any]]:
    path = cache_path(list_id)
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _page_tasks(endpoint: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page = 0
    while True:
        data = _curl(endpoint.format(page=page))
        tasks = data.get("tasks") or []
        if tasks:
            out.extend(tasks)
        if data.get("last_page", True) or not tasks:
            break
        page += 1
        if page > 200:
            break
    return out


def _full_sync_list(list_id: str) -> List[Dict[str, Any]]:
    endpoint = f"/list/{list_id}/task?page={{page}}&subtasks=true&include_closed=true"
    return _page_tasks(endpoint)


def _delta_sync_list(list_id: str, since_ms: int) -> List[Dict[str, Any]]:
    query = urllib.parse.urlencode({
        "page": 0,
        "subtasks": "true",
        "include_closed": "true",
        "date_updated_gt": str(max(0, since_ms)),
    })
    endpoint = f"/list/{list_id}/task?{query.replace('page=0', 'page={{page}}', 1)}"
    try:
        return _page_tasks(endpoint)
    except (RuntimeError, PermissionError):
        # If the delta filter is not accepted for some list variant, fall back
        # to a full reconciliation for that list.
        return _full_sync_list(list_id)


def sync_list_cache(list_id: str, *, force: bool = False) -> Dict[str, Any]:
    now = int(time.time() * 1000)
    cached = load_cache(list_id)
    last_synced_ms = int(cached.get("last_synced_ms", 0)) if cached else 0
    last_full_sync_ms = int(cached.get("last_full_sync_ms", 0)) if cached else 0
    age_s = (now - last_synced_ms) / 1000 if last_synced_ms else float("inf")
    full_age_s = (now - last_full_sync_ms) / 1000 if last_full_sync_ms else float("inf")

    if not force and cached and age_s < STALE_AFTER_S:
        return cached

    if not cached or full_age_s >= FULL_RECONCILE_AFTER_S:
        mode = "full"
        tasks = _full_sync_list(list_id)
    else:
        mode = "delta"
        tasks = _delta_sync_list(list_id, max(0, last_synced_ms - SYNC_OVERLAP_MS))

    tasks_by_id: Dict[str, Dict[str, Any]] = {}
    if cached and isinstance(cached.get("tasks_by_id"), dict) and mode == "delta":
        tasks_by_id.update(cached.get("tasks_by_id") or {})
    for task in tasks:
        tid = str(task.get("id") or "")
        if tid:
            tasks_by_id[tid] = task

    # Full reconciles represent the current list state; delta syncs only patch
    # what changed since the last sync. We keep the live task index only.
    if mode == "full":
        tasks_by_id = {str(task.get("id")): task for task in tasks if task.get("id")}

    meta = list_meta(list_id)
    record = {
        "schema_version": 1,
        "team_id": TEAM_ID,
        "list_id": str(list_id),
        "list_name": meta.get("name"),
        "space_id": meta.get("space_id"),
        "space_name": meta.get("space_name"),
        "last_synced_ms": now,
        "last_full_sync_ms": now if mode == "full" else last_full_sync_ms,
        "sync_mode": mode,
        "tasks": list(tasks_by_id.values()),
        "tasks_by_id": tasks_by_id,
    }
    _atomic_write_json(cache_path(list_id), record)
    return record


def load_team_task_index(*, force: bool = False) -> Dict[str, Any]:
    """Return a combined local-first task index across all active lists."""
    lists = active_list_ids()
    combined_tasks: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []
    list_records: List[Dict[str, Any]] = []
    for list_id in lists:
        try:
            record = sync_list_cache(list_id, force=force)
            list_records.append(record)
            for task in record.get("tasks") or []:
                tid = str(task.get("id") or "")
                if tid:
                    combined_tasks[tid] = task
        except Exception as exc:
            errors.append({"list_id": str(list_id), "error": str(exc)})
            cached = load_cache(list_id)
            if cached:
                list_records.append(cached)
                for task in cached.get("tasks") or []:
                    tid = str(task.get("id") or "")
                    if tid and tid not in combined_tasks:
                        combined_tasks[tid] = task
    return {
        "team_id": TEAM_ID,
        "lists": list_records,
        "tasks": list(combined_tasks.values()),
        "errors": errors,
    }
