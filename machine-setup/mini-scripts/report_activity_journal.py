#!/usr/bin/env python3
"""Durable Mini-local shadow outbox for report lifecycle activity.

This journal is deliberately independent of the cron execution ledger.  It is an
append-only, UTC-day-partitioned record of lifecycle facts that have already
happened: a durable executor claim, a verified ClickUp review handoff, or a
verified validator completion.  Nothing in this module is a continuity consumer;
the successor task 86e2gnz71 owns those consumers and signals.

The module is stdlib-only and safe to import from the small Mini scripts.  Writer
failures never roll back the lifecycle action, but they are returned as UNKNOWN
and recorded in a durable health-issue stream whenever the state directory is
still writable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
RETENTION_DAYS = 21
VALID_KINDS = frozenset({"claim", "review_handoff", "validator_complete"})
DEFAULT_STATE_DIR = Path(os.path.expanduser("~/.hermes/state/report-activity"))
DEFAULT_CLICKUP = Path(
    os.environ.get(
        "IGNITE_SKILLS_ROOT", os.path.expanduser("~/dev/ignite-skills-live/skills")
    )
) / "clickup" / "clickup.mjs"

# Declarative coverage contract for every enabled Mini lifecycle writer.  The
# health check treats an enabled row without a non-empty emitter declaration as
# UNKNOWN.  Agent-owned paths invoke ``confirm-transition`` only after their
# guarded ClickUp write; the two Python call sites invoke the module directly.
PRODUCER_INVENTORY: tuple[dict[str, Any], ...] = (
    {
        "id": "queue-poller-claim",
        "enabled": True,
        "kind": "claim",
        "emitter": "claim_store.acquire after file fsync",
    },
    {
        "id": "merged-before-claim-reconciliation",
        "enabled": True,
        "kind": "review_handoff",
        "emitter": "clickup-executor prompt confirm-transition after verified review",
    },
    {
        "id": "ordinary-publish-closeout",
        "enabled": True,
        "kind": "review_handoff",
        "emitter": "clickup-executor prompt confirm-transition after verified review",
    },
    {
        "id": "db-publish-closeout",
        "enabled": True,
        "kind": "review_handoff",
        "emitter": "closeout_actor DB backstop and clickup-executor prompt",
    },
    {
        "id": "closeout-actor",
        "enabled": True,
        "kind": "review_handoff",
        "emitter": "closeout_actor._do_flip after ClickUp read-after-write",
    },
    {
        "id": "clickup-lifecycle-reconciliation",
        "enabled": True,
        "kind": "review_handoff",
        "emitter": "clickup-lifecycle prompt confirm-transition after verified review",
    },
    {
        "id": "validator",
        "enabled": True,
        "kind": "validator_complete",
        "emitter": "hermes-pr-validate prompt confirm-transition after verified complete",
    },
)


class JournalError(RuntimeError):
    """A journal write, read, validation, or confirmation failed."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: dt.datetime | None) -> dt.datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        raise JournalError("timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _state_dir(state_dir: str | os.PathLike[str] | None) -> Path:
    return Path(state_dir).expanduser() if state_dir is not None else DEFAULT_STATE_DIR


def _event_id(identity: dict[str, Any]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "ra1-" + hashlib.sha256(encoded).hexdigest()


def build_event(
    *,
    kind: str,
    task_id: str,
    source: str,
    run_id: str | None = None,
    execution_id: str | None = None,
    clickup_updated_at: str | None = None,
    clickup_transition_at: str | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Build a validated event with a stable identity independent of append time."""
    if kind not in VALID_KINDS:
        raise JournalError(f"unsupported kind: {kind!r}")
    task_id = str(task_id or "").strip()
    source = str(source or "").strip()
    if not task_id or not source:
        raise JournalError("task_id and source are required")
    if kind != "claim" and not (clickup_transition_at or clickup_updated_at):
        raise JournalError(f"{kind} requires a confirmed ClickUp timestamp")

    timestamp = _as_utc(now)
    identity = {
        "v": SCHEMA_VERSION,
        "utc_day": timestamp.date().isoformat(),
        "kind": kind,
        "task_id": task_id,
        "source": source,
        "run_id": run_id or None,
        "execution_id": execution_id or None,
        "clickup_updated_at": clickup_updated_at or None,
        "clickup_transition_at": clickup_transition_at or None,
    }
    event: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "event_id": _event_id(identity),
        "ts": _iso(timestamp),
        "kind": kind,
        "task_id": task_id,
        "source": source,
    }
    for key in ("run_id", "execution_id", "clickup_updated_at", "clickup_transition_at"):
        if identity[key] is not None:
            event[key] = identity[key]
    return event


def _validate_event(event: Any) -> str | None:
    if not isinstance(event, dict):
        return "record is not an object"
    required = ("v", "event_id", "ts", "kind", "task_id", "source")
    missing = [key for key in required if not event.get(key)]
    if missing:
        return "missing fields: " + ",".join(missing)
    if event.get("v") != SCHEMA_VERSION:
        return f"unsupported v={event.get('v')!r}"
    if event.get("kind") not in VALID_KINDS:
        return f"unsupported kind={event.get('kind')!r}"
    if event.get("kind") != "claim" and not (
        event.get("clickup_transition_at") or event.get("clickup_updated_at")
    ):
        return "transition event lacks confirmed ClickUp timestamp"
    try:
        parsed = dt.datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return "ts is not timezone-aware"
        if parsed.utcoffset() != dt.timedelta(0):
            return "ts is not UTC"
    except (TypeError, ValueError):
        return "invalid ts"
    identity = {
        "v": event["v"],
        "utc_day": parsed.date().isoformat(),
        "kind": event["kind"],
        "task_id": event["task_id"],
        "source": event["source"],
        "run_id": event.get("run_id") or None,
        "execution_id": event.get("execution_id") or None,
        "clickup_updated_at": event.get("clickup_updated_at") or None,
        "clickup_transition_at": event.get("clickup_transition_at") or None,
    }
    if event["event_id"] != _event_id(identity):
        return "event_id does not match stable event identity"
    return None


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_line(path: Path, line: bytes, lock_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        existed = path.exists()
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise JournalError(f"short append: {written}/{len(line)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            _fsync_dir(path.parent)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _prune_locked(root: Path, today: dt.date) -> list[str]:
    cutoff = today - dt.timedelta(days=RETENTION_DAYS - 1)
    removed: list[str] = []
    for path in root.glob("????-??-??.jsonl"):
        try:
            file_day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if file_day < cutoff:
            path.unlink()
            removed.append(path.name)
    if removed:
        _fsync_dir(root)
    return removed


def append_event(
    event: dict[str, Any],
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Append once per UTC day under a process-safe flock and fsync the bytes."""
    invalid = _validate_event(event)
    if invalid:
        raise JournalError(invalid)
    root = _state_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
    day = timestamp.astimezone(dt.timezone.utc).date().isoformat()
    path = root / f"{day}.jsonl"
    lock_path = root / ".journal.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        seen: set[str] = set()
        if path.exists():
            with open(path, "rb") as fh:
                for raw in fh:
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise JournalError(f"cannot dedupe against corrupt {path.name}: {exc}") from exc
                    if isinstance(record, dict) and record.get("event_id"):
                        seen.add(str(record["event_id"]))
        if event["event_id"] in seen:
            return {"status": "ok", "appended": False, "deduped": True, "event": event}

        line = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        existed = path.exists()
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise JournalError(f"short append: {written}/{len(line)} bytes")
            os.fsync(fd)
        finally:
            os.close(fd)
        if not existed:
            _fsync_dir(root)
        removed = _prune_locked(root, dt.date.fromisoformat(day))
        return {
            "status": "ok",
            "appended": True,
            "deduped": False,
            "event": event,
            "pruned": removed,
        }
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def mark_degraded(
    reason: str,
    *,
    source: str,
    state_dir: str | os.PathLike[str] | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Best-effort durable health marker.  Returns False if even this cannot write."""
    root = _state_dir(state_dir)
    issue = {
        "v": SCHEMA_VERSION,
        "ts": _iso(_as_utc(now)),
        "source": str(source),
        "reason": str(reason)[:1000],
    }
    try:
        _append_line(
            root / "health-issues.jsonl",
            (json.dumps(issue, sort_keys=True, separators=(",", ":")) + "\n").encode(),
            root / ".health.lock",
        )
        return True
    except Exception as exc:
        print(f"report activity health UNKNOWN: {reason}; marker failed: {exc}", file=sys.stderr)
        return False


def safe_emit(
    *,
    state_dir: str | os.PathLike[str] | None = None,
    **event_kwargs: Any,
) -> dict[str, Any]:
    """Lifecycle-safe emission: return UNKNOWN and mark health instead of raising."""
    try:
        return append_event(build_event(**event_kwargs), state_dir=state_dir)
    except Exception as exc:
        source = str(event_kwargs.get("source") or "unknown")
        mark_degraded(f"journal append failed: {exc}", source=source, state_dir=state_dir)
        return {"status": "UNKNOWN", "appended": False, "error": str(exc)}


def _inventory_reasons(inventory: Any) -> list[str]:
    if not isinstance(inventory, (list, tuple)):
        return ["producer inventory is not a list"]
    reasons: list[str] = []
    seen: set[str] = set()
    for index, producer in enumerate(inventory):
        if not isinstance(producer, dict):
            reasons.append(f"producer[{index}] is not an object")
            continue
        producer_id = str(producer.get("id") or "").strip()
        if not producer_id:
            reasons.append(f"producer[{index}] lacks id")
        elif producer_id in seen:
            reasons.append(f"duplicate producer id: {producer_id}")
        seen.add(producer_id)
        if producer.get("enabled") is True and not str(producer.get("emitter") or "").strip():
            reasons.append(f"enabled producer lacks emitter declaration: {producer_id or index}")
        if producer.get("enabled") is True and producer.get("kind") not in VALID_KINDS:
            reasons.append(f"enabled producer has invalid kind: {producer_id or index}")
    return reasons


def health(
    *,
    state_dir: str | os.PathLike[str] | None = None,
    inventory: Any = PRODUCER_INVENTORY,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return OK or UNKNOWN; corruption/read/coverage/writer failures degrade visibly."""
    root = _state_dir(state_dir)
    reasons = _inventory_reasons(inventory)
    files = 0
    events = 0
    if root.exists():
        try:
            journal_files = sorted(root.glob("????-??-??.jsonl"))
        except OSError as exc:
            journal_files = []
            reasons.append(f"journal directory read failed: {exc}")
        for path in journal_files:
            files += 1
            try:
                data = path.read_bytes()
            except OSError as exc:
                reasons.append(f"{path.name}: read failed: {exc}")
                continue
            if data and not data.endswith(b"\n"):
                reasons.append(f"{path.name}: incomplete trailing line")
            for line_number, raw in enumerate(data.splitlines(), start=1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    reasons.append(f"{path.name}:{line_number}: corrupt JSON: {exc}")
                    continue
                invalid = _validate_event(record)
                if invalid:
                    reasons.append(f"{path.name}:{line_number}: {invalid}")
                else:
                    events += 1
        issue_path = root / "health-issues.jsonl"
        if issue_path.exists():
            try:
                cutoff = _as_utc(now) - dt.timedelta(days=RETENTION_DAYS)
                for line_number, raw in enumerate(issue_path.read_bytes().splitlines(), start=1):
                    if not raw.strip():
                        continue
                    try:
                        issue = json.loads(raw)
                        issue_ts = dt.datetime.fromisoformat(str(issue["ts"]).replace("Z", "+00:00"))
                        if issue_ts >= cutoff:
                            reasons.append(
                                f"writer degraded ({issue.get('source', 'unknown')}): {issue.get('reason', 'unknown')}"
                            )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                        reasons.append(f"health-issues.jsonl:{line_number}: corrupt record: {exc}")
            except OSError as exc:
                reasons.append(f"health-issues.jsonl: read failed: {exc}")
    return {
        "status": "UNKNOWN" if reasons else "OK",
        "degraded": bool(reasons),
        "reasons": reasons,
        "files": files,
        "events": events,
        "retention_days": RETENTION_DAYS,
        "continuity_consumers_enabled": True,
        "continuity_successor_task": "86e2gnz71",
    }


def read_events(
    start: dt.datetime,
    end: dt.datetime,
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Read validated unique events in the half-open UTC range ``[start, end)``.

    This is the journal's deliberately small public consumer API. It never
    mutates retention or health state. Any unreadable, partial, corrupt, or
    invalid record makes the result UNKNOWN; exact duplicate event IDs are
    deduplicated and reported so consumers cannot inflate activity counts.
    """
    start_utc = _as_utc(start)
    end_utc = _as_utc(end)
    if end_utc <= start_utc:
        raise JournalError("event range end must be after start")

    root = _state_dir(state_dir)
    reasons: list[str] = []
    events: list[dict[str, Any]] = []
    duplicate_event_ids: list[str] = []
    seen: set[str] = set()
    day = start_utc.date()
    final_day = (end_utc - dt.timedelta(microseconds=1)).date()
    while day <= final_day:
        path = root / f"{day.isoformat()}.jsonl"
        day += dt.timedelta(days=1)
        if not path.exists():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            reasons.append(f"{path.name}: read failed: {exc}")
            continue
        if data and not data.endswith(b"\n"):
            reasons.append(f"{path.name}: incomplete trailing line")
        for line_number, raw in enumerate(data.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                reasons.append(f"{path.name}:{line_number}: corrupt JSON: {exc}")
                continue
            invalid = _validate_event(event)
            if invalid:
                reasons.append(f"{path.name}:{line_number}: {invalid}")
                continue
            event_id = str(event["event_id"])
            if event_id in seen:
                duplicate_event_ids.append(event_id)
                continue
            seen.add(event_id)
            timestamp = dt.datetime.fromisoformat(str(event["ts"]).replace("Z", "+00:00"))
            if start_utc <= timestamp < end_utc:
                events.append(event)

    events.sort(key=lambda event: (str(event["ts"]), str(event["event_id"])))
    return {
        "status": "UNKNOWN" if reasons else "OK",
        "reasons": reasons,
        "events": events,
        "duplicates": len(duplicate_event_ids),
        "duplicate_event_ids": sorted(set(duplicate_event_ids)),
        "start": _iso(start_utc),
        "end": _iso(end_utc),
    }


def _extract_task(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
        return payload["task"]
    if isinstance(payload, dict):
        return payload
    raise JournalError("ClickUp task response is not an object")


def _status_name(task: dict[str, Any]) -> str:
    status = task.get("status")
    if isinstance(status, dict):
        status = status.get("status")
    return str(status or "").strip().casefold()


def _default_fetch_task(task_id: str, clickup_path: Path = DEFAULT_CLICKUP) -> dict[str, Any]:
    result = subprocess.run(
        ["node", str(clickup_path), "task", task_id, "--json"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise JournalError(f"ClickUp confirmation read rejected rc={result.returncode}: {(result.stderr or '')[:300]}")
    try:
        return _extract_task(json.loads(result.stdout))
    except json.JSONDecodeError as exc:
        raise JournalError(f"ClickUp confirmation returned invalid JSON: {exc}") from exc


def confirm_transition(
    *,
    kind: str,
    task_id: str,
    source: str,
    expected_status: str,
    run_id: str | None = None,
    execution_id: str | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    fetch_task: Callable[[str], dict[str, Any]] = _default_fetch_task,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Read ClickUp after a successful writer call, verify status, then emit once."""
    if kind not in {"review_handoff", "validator_complete"}:
        raise JournalError("confirmed transition kind must be review_handoff or validator_complete")
    try:
        task = _extract_task(fetch_task(task_id))
        observed = _status_name(task)
        expected = expected_status.strip().casefold()
        if observed != expected:
            raise JournalError(f"ClickUp confirmation mismatch: expected {expected!r}, observed {observed!r}")
        confirmed_ts = task.get("date_updated") or task.get("updated_at")
        if not confirmed_ts:
            raise JournalError("ClickUp confirmation lacks date_updated/updated_at")
        result = safe_emit(
            kind=kind,
            task_id=task_id,
            source=source,
            run_id=run_id,
            execution_id=execution_id,
            clickup_updated_at=str(confirmed_ts),
            state_dir=state_dir,
            now=now,
        )
        result["confirmed"] = True
        result["observed_status"] = observed
        return result
    except Exception as exc:
        mark_degraded(f"ClickUp transition confirmation failed: {exc}", source=source, state_dir=state_dir)
        return {
            "status": "UNKNOWN",
            "confirmed": False,
            "appended": False,
            "error": str(exc),
        }


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    emit = sub.add_parser("emit")
    emit.add_argument("--kind", choices=sorted(VALID_KINDS), required=True)
    emit.add_argument("--task-id", required=True)
    emit.add_argument("--source", required=True)
    emit.add_argument("--run-id")
    emit.add_argument("--execution-id")
    emit.add_argument("--clickup-updated-at")
    emit.add_argument("--clickup-transition-at")

    confirm = sub.add_parser("confirm-transition")
    confirm.add_argument("--kind", choices=("review_handoff", "validator_complete"), required=True)
    confirm.add_argument("--task-id", required=True)
    confirm.add_argument("--source", required=True)
    confirm.add_argument("--expected-status", required=True)
    confirm.add_argument("--run-id")
    confirm.add_argument("--execution-id")
    confirm.add_argument("--clickup-path", type=Path, default=DEFAULT_CLICKUP)

    sub.add_parser("health")
    args = parser.parse_args(argv)
    if args.command == "health":
        result = health(state_dir=args.state_dir)
    elif args.command == "emit":
        result = safe_emit(
            kind=args.kind,
            task_id=args.task_id,
            source=args.source,
            run_id=args.run_id,
            execution_id=args.execution_id,
            clickup_updated_at=args.clickup_updated_at,
            clickup_transition_at=args.clickup_transition_at,
            state_dir=args.state_dir,
        )
    else:
        result = confirm_transition(
            kind=args.kind,
            task_id=args.task_id,
            source=args.source,
            expected_status=args.expected_status,
            run_id=args.run_id,
            execution_id=args.execution_id,
            state_dir=args.state_dir,
            fetch_task=lambda task_id: _default_fetch_task(task_id, args.clickup_path),
        )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "OK" or result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
