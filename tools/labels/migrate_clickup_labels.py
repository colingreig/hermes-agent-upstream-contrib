#!/usr/bin/env python3
"""Migrate ClickUp tags to the canonical label registry.

Reads tools/labels/labels.json (canonical registry) and
tools/labels/clickup-tag-map.json (566 live-tag -> disposition mappings) and
either audits live ClickUp tag state against them, or applies the mapping.

Modes (mutually exclusive): --audit, --dry-run (default), --apply.
See tools/labels/README.md for the documented CLI contract.

Python 3 stdlib only. No third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LABELS_PATH = os.path.join(SCRIPT_DIR, "labels.json")
MAP_PATH = os.path.join(SCRIPT_DIR, "clickup-tag-map.json")
RECEIPTS_DIR = os.path.join(SCRIPT_DIR, "receipts")

API_BASE = "https://api.clickup.com/api/v2"

ACTIONABLE_DISPOSITIONS = ("rename", "merge", "explode", "delete")
NOOP_DISPOSITIONS = ("keep_unmapped", "needs_decision")
ALL_DISPOSITIONS = ACTIONABLE_DISPOSITIONS + NOOP_DISPOSITIONS


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


class FatalError(Exception):
    """Raised to abort the run with a clean, non-traceback message."""


# --------------------------------------------------------------------------
# Rate limiter
# --------------------------------------------------------------------------

class RateLimiter:
    """Simple token-bucket style limiter: sleeps to keep req/min under cap."""

    def __init__(self, per_minute: int = 90):
        self.min_interval = 60.0 / max(1, per_minute)
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


# --------------------------------------------------------------------------
# ClickUp HTTP client
# --------------------------------------------------------------------------

class ClickUpClient:
    def __init__(self, token: str, rate_limiter: RateLimiter, verbose: bool = False,
                 dry_run: bool = True, call_log: list = None, errors: list = None):
        self.token = token
        self.rate_limiter = rate_limiter
        self.verbose = verbose
        self.dry_run = dry_run
        self.call_log = call_log if call_log is not None else []
        self.errors = errors if errors is not None else []
        self.api_calls_made = 0

    def _build_url(self, path: str, params=None) -> str:
        url = f"{API_BASE}{path}"
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{qs}"
        return url

    def request(self, method: str, path: str, params=None, body=None,
                write: bool = False, max_5xx_attempts: int = 5, tolerate_404: bool = False):
        """Perform a ClickUp API request. Reads always execute (even in
        dry-run); writes are logged-and-skipped when self.dry_run is True
        unless the caller explicitly wants a real call (write=True is just
        a marker for logging/receipt purposes, gating happens in callers).
        tolerate_404=True treats a 404 as an already-satisfied idempotent
        no-op (e.g. removing a tag that's already gone) instead of fatal."""
        url = self._build_url(path, params)
        data = None
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        attempt = 0
        while True:
            attempt += 1
            self.rate_limiter.wait()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status = resp.getcode()
                    raw = resp.read()
                    self.api_calls_made += 1
                    parsed = json.loads(raw) if raw else None
                    if self.verbose:
                        eprint(f"[api] {method} {path} -> {status}")
                    self.call_log.append({
                        "method": method, "path": path, "params": params,
                        "body": body, "status": status,
                    })
                    return status, parsed
            except urllib.error.HTTPError as e:
                self.api_calls_made += 1
                raw = e.read()
                try:
                    parsed_err = json.loads(raw) if raw else None
                except Exception:
                    parsed_err = {"raw": raw.decode("utf-8", "replace")}
                status = e.code
                if self.verbose:
                    eprint(f"[api] {method} {path} -> {status} {parsed_err}")

                if status == 404 and tolerate_404:
                    self.call_log.append({
                        "method": method, "path": path, "params": params,
                        "body": body, "status": status, "note": "tolerated (already absent)",
                    })
                    return status, None

                if status == 401 or status == 403:
                    msg = f"Auth failure ({status}) on {method} {path}: {parsed_err}"
                    self.errors.append(msg)
                    raise FatalError(msg)

                if status == 429:
                    retry_after = e.headers.get("Retry-After")
                    try:
                        sleep_s = float(retry_after) if retry_after else 5.0
                    except ValueError:
                        sleep_s = 5.0
                    if self.verbose:
                        eprint(f"[api] 429 rate limited, sleeping {sleep_s}s")
                    if attempt > max_5xx_attempts:
                        msg = f"Repeated 429 on {method} {path}, giving up after {attempt} attempts"
                        self.errors.append(msg)
                        raise FatalError(msg)
                    time.sleep(sleep_s)
                    continue

                if 500 <= status < 600:
                    if attempt >= max_5xx_attempts:
                        msg = f"{status} on {method} {path} after {attempt} attempts: {parsed_err}"
                        self.errors.append(msg)
                        raise FatalError(msg)
                    backoff = min(2 ** attempt, 30)
                    if self.verbose:
                        eprint(f"[api] {status} server error, backoff {backoff}s (attempt {attempt})")
                    time.sleep(backoff)
                    continue

                # Any other 4xx: not retryable.
                msg = f"{status} on {method} {path}: {parsed_err}"
                self.errors.append(msg)
                self.call_log.append({
                    "method": method, "path": path, "params": params,
                    "body": body, "status": status, "error": parsed_err,
                })
                raise FatalError(msg)
            except urllib.error.URLError as e:
                if attempt >= max_5xx_attempts:
                    msg = f"Network error on {method} {path} after {attempt} attempts: {e}"
                    self.errors.append(msg)
                    raise FatalError(msg)
                backoff = min(2 ** attempt, 30)
                time.sleep(backoff)
                continue

    # -- read helpers -----------------------------------------------------

    def get_team_id(self) -> str:
        override = os.environ.get("CLICKUP_TEAM_ID")
        if override:
            return override
        status, parsed = self.request("GET", "/team")
        teams = (parsed or {}).get("teams", [])
        if not teams:
            raise FatalError("GET /team returned no workspaces; cannot resolve team_id for task search")
        return teams[0]["id"]

    def get_space_tags(self, space_id: str):
        status, parsed = self.request("GET", f"/space/{space_id}/tag")
        return (parsed or {}).get("tags", [])

    def get_tasks_for_tag(self, team_id: str, space_id: str, tag_name: str):
        """Page through all tasks (any status, incl. subtasks) carrying tag_name
        in the given space."""
        tasks = []
        page = 0
        while True:
            params = [
                ("tags[]", tag_name),
                ("space_ids[]", space_id),
                ("include_closed", "true"),
                ("subtasks", "true"),
                ("page", str(page)),
            ]
            status, parsed = self.request("GET", f"/team/{team_id}/task", params=params)
            batch = (parsed or {}).get("tasks", [])
            tasks.extend(batch)
            last_page = (parsed or {}).get("last_page", None)
            if last_page is True or len(batch) < 100:
                break
            page += 1
        return tasks

    # -- write helpers (gated by caller's dry_run check) -------------------

    def rename_tag(self, space_id: str, old_name: str, new_name: str, tag_bg: str, tag_fg: str = "#ffffff"):
        body = {"tag": {"name": new_name, "tag_bg": tag_bg, "tag_fg": tag_fg}}
        return self.request("PUT", f"/space/{space_id}/tag/{urllib.parse.quote(old_name)}", body=body)

    def create_space_tag(self, space_id: str, name: str, tag_bg: str, tag_fg: str = "#ffffff"):
        body = {"tag": {"name": name, "tag_bg": tag_bg, "tag_fg": tag_fg}}
        return self.request("POST", f"/space/{space_id}/tag", body=body)

    def delete_space_tag(self, space_id: str, name: str):
        return self.request("DELETE", f"/space/{space_id}/tag/{urllib.parse.quote(name)}", tolerate_404=True)

    def add_task_tag(self, task_id: str, tag_name: str):
        return self.request("POST", f"/task/{task_id}/tag/{urllib.parse.quote(tag_name)}")

    def remove_task_tag(self, task_id: str, tag_name: str):
        return self.request("DELETE", f"/task/{task_id}/tag/{urllib.parse.quote(tag_name)}", tolerate_404=True)


# --------------------------------------------------------------------------
# Registry helpers
# --------------------------------------------------------------------------

def load_registry():
    with open(LABELS_PATH, "r") as f:
        labels = json.load(f)
    with open(MAP_PATH, "r") as f:
        tag_map = json.load(f)
    return labels, tag_map


def canonical_tag_set(labels: dict) -> set:
    out = set()
    for ns, nsdef in labels["namespaces"].items():
        for val in nsdef["values"].keys():
            out.add(f"{ns}:{val}")
    return out


def canonical_tag_color(labels: dict, canonical_tag: str) -> str:
    ns, _, val = canonical_tag.partition(":")
    nsdef = labels["namespaces"].get(ns, {})
    valdef = nsdef.get("values", {}).get(val, {})
    return valdef.get("color") or nsdef.get("color") or "#607d8b"


def forbidden_patterns(labels: dict):
    return [re.compile(p) for p in labels["rules"].get("forbidden_patterns", [])]


def build_map_index(tag_map: dict):
    """tag name -> mapping entry (global; entry carries which spaces it applies to)."""
    return {m["tag"]: m for m in tag_map["mappings"]}


# --------------------------------------------------------------------------
# Audit mode
# --------------------------------------------------------------------------

def run_audit(client: ClickUpClient, labels: dict, tag_map: dict, space_ids, verbose: bool) -> int:
    canonical = canonical_tag_set(labels)
    fpats = forbidden_patterns(labels)
    by_tag = build_map_index(tag_map)

    already_canonical = {}       # space_id -> [tag,...]
    mapping_pending = {}         # space_id -> [(tag, disposition, to), ...]
    new_sprawl = {}              # space_id -> [tag,...]
    forbidden_hits = {}          # space_id -> [tag,...]
    noop_quarantined = {}        # space_id -> [(tag, disposition), ...] (keep_unmapped/needs_decision)
    stale_map_rows = []          # [(tag, space_id, disposition), ...] map says it's in this space but it isn't live

    live_tags_by_space = {}

    for space_id in space_ids:
        live = client.get_space_tags(space_id)
        live_names = sorted({t["name"] for t in live})
        live_tags_by_space[space_id] = set(live_names)

        already_canonical[space_id] = []
        mapping_pending[space_id] = []
        new_sprawl[space_id] = []
        forbidden_hits[space_id] = []
        noop_quarantined[space_id] = []

        for name in live_names:
            if name in canonical:
                already_canonical[space_id].append(name)

            entry = by_tag.get(name)
            if entry and space_id in entry.get("spaces", []):
                disp = entry["disposition"]
                if disp in ACTIONABLE_DISPOSITIONS:
                    mapping_pending[space_id].append((name, disp, entry.get("to", [])))
                elif disp in NOOP_DISPOSITIONS:
                    noop_quarantined[space_id].append((name, disp))
            elif name not in canonical:
                new_sprawl[space_id].append(name)

            for pat in fpats:
                if pat.search(name):
                    forbidden_hits[space_id].append(name)
                    break

    # Stale map rows: map entry claims this space, but tag doesn't exist live there.
    for m in tag_map["mappings"]:
        for space_id in m.get("spaces", []):
            if space_id not in space_ids:
                continue
            if m["tag"] not in live_tags_by_space.get(space_id, set()):
                stale_map_rows.append((m["tag"], space_id, m["disposition"]))

    # ---- report -----------------------------------------------------
    print("=" * 72)
    print("CLICKUP LABEL AUDIT")
    print("=" * 72)

    total_canonical = sum(len(v) for v in already_canonical.values())
    total_pending = sum(len(v) for v in mapping_pending.values())
    total_sprawl = sum(len(v) for v in new_sprawl.values())
    total_forbidden = sum(len(v) for v in forbidden_hits.values())
    total_stale = len(stale_map_rows)
    total_noop = sum(len(v) for v in noop_quarantined.values())

    print(f"\n(a) Already canonical: {total_canonical}")
    for sid, names in already_canonical.items():
        if names:
            print(f"    space {sid}: {len(names)}")
            if verbose:
                for n in names:
                    print(f"      - {n}")

    print(f"\n(b) Mapping pending (actionable, not yet applied): {total_pending}")
    for sid, rows in mapping_pending.items():
        if rows:
            print(f"    space {sid}: {len(rows)}")
            if verbose:
                for name, disp, to in rows:
                    print(f"      - {name} -> {disp} -> {to}")

    print(f"\n(c) NEW SPRAWL (live tag in neither registry nor map): {total_sprawl}  <-- headline number")
    for sid, names in new_sprawl.items():
        if names:
            print(f"    space {sid}: {len(names)}")
            for n in names:
                print(f"      - {n}")

    print(f"\n(d) Stale map rows (map says live, but tag no longer exists): {total_stale}")
    for tag, sid, disp in stale_map_rows:
        print(f"    - {tag} (space {sid}, disposition={disp})")

    print(f"\n(e) Forbidden-pattern matches: {total_forbidden}")
    for sid, names in forbidden_hits.items():
        if names:
            print(f"    space {sid}: {len(names)}")
            for n in names:
                print(f"      - {n}")

    print(f"\n(supplementary) keep_unmapped / needs_decision quarantined, no action: {total_noop}")
    for sid, rows in noop_quarantined.items():
        if rows:
            print(f"    space {sid}: {len(rows)}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"spaces audited:       {len(space_ids)}")
    print(f"already canonical:    {total_canonical}")
    print(f"mapping pending:      {total_pending}")
    print(f"new sprawl:           {total_sprawl}")
    print(f"stale map rows:       {total_stale}")
    print(f"forbidden matches:    {total_forbidden}")
    print(f"quarantined (no-op):  {total_noop}")
    print(f"api calls made:       {client.api_calls_made}")
    print("=" * 72)

    return 1 if (total_sprawl > 0 or total_forbidden > 0) else 0


# --------------------------------------------------------------------------
# Plan building (shared by dry-run and apply)
# --------------------------------------------------------------------------

class PlanStep:
    def __init__(self, kind, space_id, tag=None, detail=None):
        self.kind = kind            # e.g. "rename", "merge_degraded", "create_tag", "add_task_tag", ...
        self.space_id = space_id
        self.tag = tag
        self.detail = detail or {}

    def describe(self) -> str:
        d = self.detail
        if self.kind == "rename":
            return f"[{self.space_id}] RENAME tag '{self.tag}' -> '{d['new']}'"
        if self.kind == "rename_degraded_to_merge":
            return f"[{self.space_id}] RENAME '{self.tag}' collides with existing '{d['new']}' -> degraded to MERGE"
        if self.kind == "create_canonical_tag":
            return f"[{self.space_id}] CREATE canonical tag '{self.tag}' (if missing)"
        if self.kind == "merge_task":
            return (f"[{self.space_id}] task {d['task_id']}: add {d['to']}, "
                    f"remove '{self.tag}'")
        if self.kind == "delete_space_tag":
            return f"[{self.space_id}] DELETE space tag '{self.tag}' ({d.get('task_count', 0)} tasks carried it)"
        return f"[{self.space_id}] {self.kind} '{self.tag}' {d}"


def build_plan(client: ClickUpClient, labels: dict, tag_map: dict, space_ids,
                only_dispositions, only_tags, limit, verbose):
    """Compute the full ordered plan of API calls, without executing writes
    (reads to check current live state ARE executed, since the plan must
    reflect real state to be idempotent/accurate)."""
    by_tag = build_map_index(tag_map)
    fpats = forbidden_patterns(labels)

    entries = []
    if only_tags:
        for t in only_tags:
            if t not in by_tag:
                raise FatalError(f"--tag '{t}' is not present in {os.path.basename(MAP_PATH)}; refusing to silently skip")
            entries.append(by_tag[t])
    else:
        for m in tag_map["mappings"]:
            if only_dispositions and m["disposition"] not in only_dispositions:
                continue
            entries.append(m)

    steps = []
    counts = {
        "rename": 0, "rename_degraded_to_merge": 0, "merge": 0, "explode": 0,
        "delete": 0, "keep_unmapped": 0, "needs_decision": 0,
        "tasks_touched": set(), "canonical_tags_created": set(),
    }

    tasks_touched_limit_hit = [False]

    def under_limit():
        if limit is None:
            return True
        return len(counts["tasks_touched"]) < limit

    live_tags_cache = {}

    def live_tags_for(space_id):
        if space_id not in live_tags_cache:
            live_tags_cache[space_id] = {t["name"] for t in client.get_space_tags(space_id)}
        return live_tags_cache[space_id]

    for entry in entries:
        tag = entry["tag"]
        disposition = entry["disposition"]
        to = entry.get("to", [])
        applicable_spaces = [s for s in entry.get("spaces", []) if s in space_ids]
        if not applicable_spaces:
            continue

        if disposition == "keep_unmapped":
            counts["keep_unmapped"] += 1
            continue
        if disposition == "needs_decision":
            counts["needs_decision"] += 1
            continue

        for space_id in applicable_spaces:
            live = live_tags_for(space_id)

            if disposition == "rename":
                new_name = to[0]
                if tag not in live:
                    continue  # already renamed / doesn't exist here: idempotent no-op
                if new_name in live:
                    # Collision: degrade to merge semantics for this (tag, space).
                    steps.append(PlanStep("rename_degraded_to_merge", space_id, tag, {"new": new_name}))
                    counts["rename_degraded_to_merge"] += 1
                    _plan_merge_like(client, labels, space_id, tag, [new_name], steps, counts,
                                      under_limit, live_tags_for)
                else:
                    steps.append(PlanStep("rename", space_id, tag, {"new": new_name}))
                    counts["rename"] += 1

            elif disposition == "merge":
                if tag not in live:
                    continue
                counts["merge"] += 1
                _plan_merge_like(client, labels, space_id, tag, to, steps, counts,
                                  under_limit, live_tags_for)

            elif disposition == "explode":
                if tag not in live:
                    continue
                counts["explode"] += 1
                _plan_merge_like(client, labels, space_id, tag, to, steps, counts,
                                  under_limit, live_tags_for)

            elif disposition == "delete":
                if tag not in live:
                    continue
                if not under_limit():
                    continue
                tasks = client.get_tasks_for_tag(client_team_id_cache["id"], space_id, tag)
                task_ids = [t["id"] for t in tasks]
                # A space-level tag delete is atomic (one call removes it from
                # every carrier task); it cannot be partially applied like the
                # per-task merge/explode loop. So if it would carry
                # tasks_touched past --limit, defer the whole tag to a future
                # run instead of silently blowing through the cap.
                if limit is not None and len(counts["tasks_touched"]) + len(task_ids) > limit:
                    continue
                for tid in task_ids:
                    counts["tasks_touched"].add(tid)
                # A single space-level tag delete removes it from every task
                # that carries it; no per-task calls needed. Affected ids are
                # recorded on the step so the receipt stays reversible.
                steps.append(PlanStep("delete_space_tag", space_id, tag,
                                       {"task_count": len(task_ids), "task_ids": task_ids}))
                counts["delete"] += 1

    return steps, counts


# team_id resolution is needed inside build_plan for merge/explode/delete task
# lookups; resolved once by the caller and stashed here to avoid a parameter
# threading exercise across every recursive helper.
client_team_id_cache = {"id": None}


def _plan_merge_like(client: ClickUpClient, labels: dict, space_id, old_tag, to_tags,
                      steps, counts, under_limit, live_tags_for):
    """Shared merge/explode/degraded-rename planning: ensure canonical target
    tags exist, then per-task add-then-remove, then delete the old space tag
    once no tasks carry it."""
    live = live_tags_for(space_id)
    for target in to_tags:
        if target not in live:
            steps.append(PlanStep("create_canonical_tag", space_id, target,
                                   {"color": canonical_tag_color(labels, target)}))
            counts["canonical_tags_created"].add((space_id, target))
            live.add(target)  # plan assumes creation succeeds for downstream idempotency checks

    if not under_limit():
        return

    tasks = client.get_tasks_for_tag(client_team_id_cache["id"], space_id, old_tag)
    for t in tasks:
        if not under_limit():
            break
        task_id = t["id"]
        existing_tag_names = {tg["name"] for tg in t.get("tags", [])}
        counts["tasks_touched"].add(task_id)
        for target in to_tags:
            if target not in existing_tag_names:
                steps.append(PlanStep("merge_task_add", space_id, old_tag, {"task_id": task_id, "to": target}))
        steps.append(PlanStep("merge_task_remove", space_id, old_tag, {"task_id": task_id}))

    steps.append(PlanStep("delete_old_tag_if_empty", space_id, old_tag, {"candidate_task_count": len(tasks)}))


# --------------------------------------------------------------------------
# Plan execution
# --------------------------------------------------------------------------

def execute_plan(client: ClickUpClient, labels: dict, steps, receipt, apply: bool):
    """Walk the plan in order and issue writes. In dry-run, no writes are
    issued (execute_plan is simply not called with apply=True); this function
    is only ever invoked with apply=True from the caller, dry-run prints the
    plan directly instead."""
    for step in steps:
        try:
            if step.kind == "rename":
                # Re-check live state right before writing: another process
                # (or an earlier step in this same run) may have already
                # renamed/removed the tag, or the target may now exist.
                live = {t["name"] for t in client.get_space_tags(step.space_id)}
                if step.tag not in live:
                    receipt["notes"].append(
                        f"space {step.space_id}: rename skipped, '{step.tag}' no longer live (already applied)")
                    continue
                if step.detail["new"] in live:
                    receipt["notes"].append(
                        f"space {step.space_id}: rename skipped, target '{step.detail['new']}' now exists "
                        f"(collision appeared since plan); leaving '{step.tag}' for a future merge pass")
                    continue
                status, _ = client.rename_tag(
                    step.space_id, step.tag, step.detail["new"],
                    canonical_tag_color(labels, step.detail["new"]))
                receipt["writes"].append({"op": "rename", "space": step.space_id,
                                            "from": step.tag, "to": step.detail["new"], "status": status})

            elif step.kind == "create_canonical_tag":
                live = {t["name"] for t in client.get_space_tags(step.space_id)}
                if step.tag in live:
                    continue  # already created (this run or a previous one) - idempotent skip
                status, _ = client.create_space_tag(step.space_id, step.tag, step.detail["color"])
                receipt["writes"].append({"op": "create_canonical_tag", "space": step.space_id,
                                            "tag": step.tag, "status": status})

            elif step.kind == "merge_task_add":
                status, _ = client.add_task_tag(step.detail["task_id"], step.detail["to"])
                receipt["writes"].append({"op": "add_task_tag", "space": step.space_id,
                                            "task_id": step.detail["task_id"], "old": step.tag,
                                            "new": step.detail["to"], "status": status})
                receipt["tasks_touched"].append({"task_id": step.detail["task_id"], "old": step.tag,
                                                   "new": step.detail["to"], "action": "add"})

            elif step.kind == "merge_task_remove":
                status, _ = client.remove_task_tag(step.detail["task_id"], step.tag)
                receipt["writes"].append({"op": "remove_task_tag", "space": step.space_id,
                                            "task_id": step.detail["task_id"], "tag": step.tag, "status": status})
                receipt["tasks_touched"].append({"task_id": step.detail["task_id"], "old": step.tag,
                                                   "new": None, "action": "remove"})

            elif step.kind == "delete_old_tag_if_empty":
                remaining = client.get_tasks_for_tag(client_team_id_cache["id"], step.space_id, step.tag)
                if remaining:
                    receipt["errors"].append(
                        f"space {step.space_id}: tag '{step.tag}' still carried by "
                        f"{len(remaining)} task(s) after merge/explode pass; leaving space tag in place")
                    continue
                live = {t["name"] for t in client.get_space_tags(step.space_id)}
                if step.tag not in live:
                    continue
                status, _ = client.delete_space_tag(step.space_id, step.tag)
                receipt["writes"].append({"op": "delete_space_tag", "space": step.space_id,
                                            "tag": step.tag, "status": status})

            elif step.kind == "delete_space_tag":
                live = {t["name"] for t in client.get_space_tags(step.space_id)}
                if step.tag not in live:
                    continue
                status, _ = client.delete_space_tag(step.space_id, step.tag)
                receipt["writes"].append({"op": "delete_space_tag", "space": step.space_id,
                                            "tag": step.tag, "status": status,
                                            "affected_task_ids": step.detail.get("task_ids", [])})

            elif step.kind == "rename_degraded_to_merge":
                receipt["notes"].append(
                    f"space {step.space_id}: rename '{step.tag}' -> '{step.detail['new']}' "
                    f"collided with an existing tag; degraded to merge semantics")

        except FatalError:
            raise


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Migrate ClickUp tags to the canonical label registry.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="Read-only audit report.")
    mode.add_argument("--dry-run", action="store_true", help="Print the plan, write nothing (default).")
    mode.add_argument("--apply", action="store_true", help="Execute the migration.")

    p.add_argument("--space", action="append", default=[], help="Space id to scope to (repeatable).")
    p.add_argument("--only", action="append", default=[], choices=list(ALL_DISPOSITIONS),
                   help="Only process this disposition (repeatable).")
    p.add_argument("--tag", action="append", default=[], help="Only process this exact tag (repeatable).")
    p.add_argument("--limit", type=int, default=None, help="Cap number of tasks touched.")
    p.add_argument("--receipt", default=None, help="Receipt output path.")
    p.add_argument("--yes", action="store_true", help="Skip interactive confirmation on --apply.")
    p.add_argument("--verbose", action="store_true", help="Verbose logging.")
    p.add_argument("--rate-limit", type=int, default=90, help="Max requests/min (default 90).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        eprint("ERROR: CLICKUP_API_TOKEN is not set in the environment. Cannot authenticate to ClickUp.")
        return 2

    try:
        labels, tag_map = load_registry()
    except Exception as e:
        eprint(f"ERROR: failed to load registry files: {e}")
        return 2

    labels_sha = sha256_of_file(LABELS_PATH)
    map_sha = sha256_of_file(MAP_PATH)

    all_space_ids = list(tag_map["spaces"].keys())
    space_ids = args.space if args.space else all_space_ids
    unknown = [s for s in space_ids if s not in all_space_ids]
    if unknown:
        eprint(f"ERROR: unknown --space id(s) not present in clickup-tag-map.json: {unknown}")
        return 2

    mode = "audit" if args.audit else ("apply" if args.apply else "dry_run")

    rate_limiter = RateLimiter(per_minute=args.rate_limit)
    call_log = []
    errors = []
    client = ClickUpClient(token, rate_limiter, verbose=args.verbose,
                            dry_run=(mode != "apply"), call_log=call_log, errors=errors)

    receipt = {
        "mode": mode,
        "utc_timestamp": datetime.now(timezone.utc).isoformat(),
        "labels_json_sha256": labels_sha,
        "clickup_tag_map_json_sha256": map_sha,
        "dry_run": mode != "apply",
        "space_ids": space_ids,
        "only_dispositions": args.only,
        "only_tags": args.tag,
        "limit": args.limit,
        "counts": {},
        "writes": [],
        "tasks_touched": [],
        "notes": [],
        "errors": errors,
        "api_calls_made": 0,
    }

    receipt_path = args.receipt or os.path.join(
        RECEIPTS_DIR, f"{utc_now_iso()}.json")

    def flush_receipt():
        receipt["api_calls_made"] = client.api_calls_made
        receipt["errors"] = errors
        os.makedirs(os.path.dirname(os.path.abspath(receipt_path)), exist_ok=True)
        with open(receipt_path, "w") as f:
            json.dump(receipt, f, indent=2, default=str)
        eprint(f"\nReceipt written: {receipt_path}")

    try:
        if mode == "audit":
            exit_code = run_audit(client, labels, tag_map, space_ids, args.verbose)
            receipt["counts"] = {"audit_run": True}
            flush_receipt()
            return exit_code

        # dry_run or apply: need team_id for task-search endpoints used by
        # merge/explode/delete planning.
        client_team_id_cache["id"] = client.get_team_id()

        steps, counts = build_plan(
            client, labels, tag_map, space_ids,
            only_dispositions=args.only or None,
            only_tags=args.tag or None,
            limit=args.limit,
            verbose=args.verbose,
        )

        serializable_counts = {
            k: (len(v) if isinstance(v, set) else v) for k, v in counts.items()
        }
        receipt["counts"] = serializable_counts

        print("=" * 72)
        print(f"PLAN ({'APPLY' if mode == 'apply' else 'DRY RUN'})")
        print("=" * 72)
        for step in steps:
            print(step.describe())
        print("-" * 72)
        print(f"rename:                    {counts['rename']}")
        print(f"rename (degraded->merge):  {counts['rename_degraded_to_merge']}")
        print(f"merge:                     {counts['merge']}")
        print(f"explode:                   {counts['explode']}")
        print(f"delete:                    {counts['delete']}")
        print(f"keep_unmapped (no-op):     {counts['keep_unmapped']}")
        print(f"needs_decision (no-op):    {counts['needs_decision']}")
        print(f"tasks touched:             {len(counts['tasks_touched'])}")
        print(f"canonical tags to create:  {len(counts['canonical_tags_created'])}")
        print(f"total planned steps:       {len(steps)}")
        print("=" * 72)

        if mode == "dry_run":
            print("\nDRY RUN: zero writes issued.")
            flush_receipt()
            return 0

        # mode == apply
        destructive_count = counts["delete"] + counts["merge"] + counts["explode"] + counts["rename_degraded_to_merge"]
        if not args.yes:
            print(f"\nAbout to apply: {counts['rename']} rename(s), {counts['merge']} merge(s), "
                  f"{counts['explode']} explode(s), {counts['delete']} delete(s), "
                  f"touching {len(counts['tasks_touched'])} task(s).")
            confirm = input("Type 'apply' to proceed: ").strip()
            if confirm != "apply":
                print("Aborted: confirmation not given.")
                receipt["notes"].append("apply aborted at confirmation prompt")
                flush_receipt()
                return 1

        execute_plan(client, labels, steps, receipt, apply=True)

        print("\n" + "=" * 72)
        print("APPLY SUMMARY")
        print("=" * 72)
        print(f"tags renamed:      {counts['rename']}")
        print(f"tags merged:       {counts['merge']}")
        print(f"tags exploded:     {counts['explode']}")
        print(f"tags deleted:      {counts['delete']}")
        print(f"tags untouched:    {counts['keep_unmapped'] + counts['needs_decision']}")
        print(f"tasks touched:     {len(counts['tasks_touched'])}")
        print(f"api calls made:    {client.api_calls_made}")
        print(f"errors:            {len(errors)}")
        print("=" * 72)

        flush_receipt()
        return 0 if not errors else 1

    except FatalError as e:
        eprint(f"\nFATAL: {e}")
        flush_receipt()
        return 1
    except KeyboardInterrupt:
        eprint("\nInterrupted.")
        flush_receipt()
        return 130


if __name__ == "__main__":
    sys.exit(main())
