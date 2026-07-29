#!/usr/bin/env python3
"""Independent, read-only delivery-chain watcher for the MacBook.

The watcher consumes JSON through read-only file, HTTPS GET, or ``ssh cat``
collectors, correlates every task through ``task_delivery/v1``, evaluates
ownership and delivery SLAs, and persists durable local evidence.  It never
claims work, changes task state, restarts a service, merges a PR, releases a
lease, or repairs observed state.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from task_delivery import CorrelationError, correlate
from delivery_watch_safety import redact_sensitive


SCHEMA = "hermes_delivery_watch/v1"
SNAPSHOT_SCHEMA = "hermes_delivery_snapshot/v1"
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_STATE_DIR = Path(
    os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
) / "state" / "task-delivery-watch"
DEFAULT_CONFIG = Path(
    os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
) / "config.delivery-watch.json"
HERMES_BIN = Path(os.path.expanduser("~/.local/bin/hermes"))
ALERT_THRESHOLDS = {
    "eligible_unowned": timedelta(minutes=40),
    "claim_to_pr": timedelta(minutes=90),
    "pr_to_ci": timedelta(minutes=45),
    "ci_to_review": timedelta(minutes=20),
    "review_to_validator": timedelta(minutes=90),
}


class WatchError(RuntimeError):
    """A watcher configuration, collection, or persistence error."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_mappings(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextlib.contextmanager
def _state_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".watch.lock"
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_json(path: Path, default: object) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchError(f"cannot read JSON {path}: {exc}") from exc


def _load_config(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WatchError(f"cannot read watcher config {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WatchError("watcher config must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise WatchError("watcher config must be an object")
    config = parsed.get("delivery_watch", parsed)
    if not isinstance(config, dict):
        raise WatchError("delivery_watch config must be an object")
    collectors = config.get("collectors")
    if not isinstance(collectors, list) or not collectors:
        raise WatchError("delivery_watch.collectors must be a non-empty list")
    return config


def _decode_payload(raw: bytes, source_name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WatchError(f"{source_name} did not return a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise WatchError(f"{source_name} must return a JSON object")
    return value


def _validate_snapshot_payload(
    payload: dict[str, Any],
    source_name: str,
    *,
    now: datetime,
    max_age_seconds: int,
) -> dict[str, Any]:
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise WatchError(
            f"{source_name} schema must be exactly {SNAPSHOT_SCHEMA}"
        )
    generated_at = _parse_time(payload.get("generated_at"))
    if generated_at is None:
        raise WatchError(f"{source_name} generated_at is missing or invalid")
    age = (now - generated_at).total_seconds()
    if age < -60 or age > max_age_seconds:
        raise WatchError(
            f"{source_name} generated_at is stale or future-dated: age_seconds={round(age, 3)}"
        )
    return payload


def _collect_file(collector: dict[str, Any]) -> dict[str, Any]:
    path = Path(os.path.expanduser(str(collector.get("path", ""))))
    if not path.is_absolute():
        raise WatchError("file collector path must be absolute")
    try:
        return _decode_payload(path.read_bytes(), f"file:{path}")
    except OSError as exc:
        raise WatchError(redact_sensitive(f"cannot read collector file {path}: {exc}")) from exc


def _collect_https(collector: dict[str, Any]) -> dict[str, Any]:
    url = collector.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise WatchError("http-json collector requires an https:// URL")
    headers = {"Accept": "application/json"}
    for header, env_name in _mapping(collector.get("header_env")).items():
        value = os.environ.get(str(env_name))
        if not value:
            raise WatchError(f"missing environment value for HTTP header {header}")
        headers[str(header)] = value
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=int(collector.get("timeout_seconds", 30))) as response:
            return _decode_payload(response.read(), url)
    except (OSError, urllib.error.URLError) as exc:
        raise WatchError(redact_sensitive(f"HTTPS GET failed for {url}: {exc}")) from exc


def _collect_ssh_cat(collector: dict[str, Any]) -> dict[str, Any]:
    host = collector.get("host")
    path = collector.get("path")
    if not isinstance(host, str) or re.fullmatch(r"[A-Za-z0-9_.-]+", host) is None:
        raise WatchError("ssh-json collector host must be one token")
    if not isinstance(path, str) or re.fullmatch(r"/[A-Za-z0-9_./-]+", path) is None:
        raise WatchError("ssh-json collector path must be an absolute shell-safe path")
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(collector.get('timeout_seconds', 20))}",
        host,
        "cat",
        "--",
        path,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=int(collector.get("timeout_seconds", 20)) + 5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WatchError(f"ssh-json collection failed: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise WatchError(
            redact_sensitive(
                f"ssh-json collection returned {result.returncode}: {stderr}", limit=300
            )
        )
    return _decode_payload(result.stdout, f"ssh:{host}:{path}")


def _merge_payload(target: dict[str, Any], payload: dict[str, Any]) -> None:
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        target.setdefault("tasks", []).extend(item for item in tasks if isinstance(item, dict))
    watch = payload.get("watch")
    if isinstance(watch, dict):
        merged_watch = target.setdefault("watch", {})
        for key, value in watch.items():
            if isinstance(value, list):
                merged_watch.setdefault(key, []).extend(value)
            else:
                merged_watch[key] = value
    collection = payload.get("collection")
    if isinstance(collection, dict):
        merged_collection = target.setdefault("collection", {})
        for name, value in collection.items():
            source_name = str(name)
            state = _mapping(value)
            if state.get("status") != "OK":
                merged_collection[source_name] = {
                    "status": "UNKNOWN",
                    **(
                        {"error": redact_sensitive(state.get("error"))}
                        if state.get("error")
                        else {}
                    ),
                }
            elif _mapping(merged_collection.get(source_name)).get("status") != "UNKNOWN":
                merged_collection[source_name] = {"status": "OK"}


def collect_snapshot(
    config: dict[str, Any], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Collect a normalized snapshot using read-only transports only."""
    observed = now or _now()
    merged: dict[str, Any] = {
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": _iso(observed),
        "tasks": [],
        "watch": {},
        "collection": {},
    }
    for index, raw_collector in enumerate(config["collectors"]):
        collector = _mapping(raw_collector)
        name = str(collector.get("name") or f"collector-{index}")
        kind = collector.get("kind")
        try:
            if kind == "file":
                payload = _collect_file(collector)
            elif kind == "http-json":
                payload = _collect_https(collector)
            elif kind == "ssh-json":
                payload = _collect_ssh_cat(collector)
            else:
                raise WatchError(f"unsupported read-only collector kind {kind!r}")
            payload = _validate_snapshot_payload(
                payload,
                name,
                now=observed,
                max_age_seconds=max(
                    60, min(3600, int(collector.get("max_age_seconds", 600)))
                ),
            )
            _merge_payload(merged, payload)
            merged["collection"][name] = {"status": "OK"}
        except WatchError as exc:
            merged["collection"][name] = {
                "status": "UNKNOWN",
                "error": redact_sensitive(exc),
            }
    return merged


def _alert(
    alerts: list[dict[str, Any]],
    kind: str,
    identity: str,
    message: str,
    *,
    severity: str = "error",
) -> None:
    signature = hashlib.sha256(f"{kind}\0{identity}".encode()).hexdigest()[:24]
    alerts.append(
        {
            "signature": signature,
            "kind": kind,
            "identity": identity,
            "severity": severity,
            "message": message,
        }
    )


def _older_than(value: object, now: datetime, threshold: timedelta) -> bool:
    parsed = _parse_time(value)
    return parsed is not None and now - parsed > threshold


def evaluate_alerts(
    snapshot: dict[str, Any],
    correlations: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Evaluate ownership, lifecycle, promotion, receipt, and delivery SLAs."""
    alerts: list[dict[str, Any]] = []
    for name, state in _mapping(snapshot.get("collection")).items():
        if _mapping(state).get("status") != "OK":
            _alert(alerts, "source_unknown", str(name), f"Evidence source {name} is UNKNOWN")
    for result in correlations:
        task_id = str(_mapping(result.get("task")).get("id"))
        if result.get("delivery_status") == "UNKNOWN":
            _alert(alerts, "delivery_unknown", task_id, f"Task {task_id} delivery evidence is UNKNOWN")

    watch = _mapping(snapshot.get("watch"))
    owners = _list_of_mappings(watch.get("owners"))
    ownership_intervals: list[
        tuple[datetime, datetime, str, dict[str, Any]]
    ] = []
    active_owners: list[dict[str, Any]] = []
    for owner in owners:
        identity = f"{owner.get('task_id')}:{owner.get('run_id')}"
        started = _parse_time(
            owner.get("execution_started_at") or owner.get("claimed_at")
        )
        finished = _parse_time(owner.get("execution_finished_at"))
        if finished is None:
            active_owners.append(owner)
        if started is not None:
            ownership_intervals.append(
                (started, finished or now, identity, owner)
            )
        if not owner.get("fencing_token"):
            _alert(alerts, "unfenced_owner", identity, f"Owner {identity} has no fencing token")
        heartbeat = _parse_time(owner.get("heartbeat_at"))
        expires = _parse_time(owner.get("lease_expires_at"))
        if heartbeat is None or expires is None or heartbeat > expires or now > expires:
            _alert(alerts, "lease_heartbeat_missing", identity, f"Owner {identity} has no current lease heartbeat")
        budget = owner.get("budget_seconds")
        if started and isinstance(budget, (int, float)) and not owner.get("execution_finished_at"):
            if now - started > timedelta(seconds=float(budget)):
                _alert(alerts, "execution_over_budget", identity, f"Execution {identity} exceeded its budget")
        if _older_than(owner.get("claimed_at"), now, ALERT_THRESHOLDS["claim_to_pr"]) and not owner.get("pr_opened_at"):
            _alert(alerts, "claim_to_pr_sla", identity, f"Execution {identity} has no PR after 90 minutes")
        if _older_than(owner.get("pr_opened_at"), now, ALERT_THRESHOLDS["pr_to_ci"]) and not owner.get("ci_terminal_at"):
            _alert(alerts, "pr_to_ci_sla", identity, f"Execution {identity} has no terminal exact-head CI after 45 minutes")
        if _older_than(owner.get("ci_terminal_at"), now, ALERT_THRESHOLDS["ci_to_review"]) and not owner.get("in_review_at"):
            _alert(alerts, "ci_to_review_sla", identity, f"Execution {identity} was not handed to review within 20 minutes")
        if _older_than(owner.get("in_review_at"), now, ALERT_THRESHOLDS["review_to_validator"]) and not owner.get("validator_started_at"):
            _alert(alerts, "review_to_validator_sla", identity, f"Execution {identity} has no validator after 90 minutes")
    overlap_pairs: set[tuple[str, str]] = set()
    for index, (left_start, left_end, left_identity, left_owner) in enumerate(
        ownership_intervals
    ):
        for right_start, right_end, right_identity, right_owner in ownership_intervals[
            index + 1 :
        ]:
            left_key = (
                str(left_owner.get("run_id")),
                str(left_owner.get("fencing_token")),
            )
            right_key = (
                str(right_owner.get("run_id")),
                str(right_owner.get("fencing_token")),
            )
            if left_key == right_key:
                continue
            if max(left_start, right_start) < min(left_end, right_end):
                overlap_pairs.add(tuple(sorted((left_identity, right_identity))))
    if len(active_owners) > 1 and not overlap_pairs:
        # Missing start times must not make multiple current owners invisible.
        active_identities = sorted(
            f"{owner.get('task_id')}:{owner.get('run_id')}"
            for owner in active_owners
        )
        overlap_pairs.add((active_identities[0], active_identities[-1]))
    for left_identity, right_identity in sorted(overlap_pairs):
        identity = f"{left_identity}|{right_identity}"
        _alert(
            alerts,
            "duplicate_ownership",
            identity,
            "Executor ownership intervals overlap globally: "
            f"{left_identity} and {right_identity}",
        )

    for queued in _list_of_mappings(watch.get("queue")):
        task_id = str(queued.get("task_id"))
        if not queued.get("owner_run_id") and _older_than(
            queued.get("eligible_at"), now, ALERT_THRESHOLDS["eligible_unowned"]
        ):
            _alert(alerts, "eligible_unowned_sla", task_id, f"Eligible task {task_id} is unowned after 40 minutes")

    review_gate = _mapping(watch.get("review_gate"))
    if review_gate.get("status") not in (None, "clean"):
        _alert(alerts, "review_gate_failure", "review-gate", "The review gate is not clean")
    for event in _list_of_mappings(watch.get("lifecycle_events")):
        if event.get("valid") is not True or event.get("false_alert") is True:
            identity = str(event.get("id") or event.get("timestamp") or "unknown")
            _alert(alerts, "invalid_lifecycle_event", identity, f"Lifecycle event {identity} is invalid")
    for promotion in _list_of_mappings(watch.get("promotions")):
        identity = str(promotion.get("id") or promotion.get("prod_sha") or "unknown")
        if promotion.get("certified") is not True:
            _alert(alerts, "uncertified_promotion", identity, f"Promotion {identity} lacks certification")
        receipt_id = promotion.get("receipt_id")
        receipt_sha = promotion.get("receipt_sha")
        prod_sha = promotion.get("prod_sha")
        if not receipt_id or not receipt_sha or not prod_sha or str(receipt_sha) != str(prod_sha):
            _alert(alerts, "promotion_receipt_mismatch", identity, f"Promotion {identity} lacks an exact-SHA receipt")
    return sorted(alerts, key=lambda item: item["signature"])


def _incident_transition(
    state_dir: Path,
    alerts: list[dict[str, Any]],
    *,
    observed_at: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checkpoint_path = state_dir / "checkpoint.json"
    old = _load_json(checkpoint_path, {})
    old_open = _mapping(_mapping(old).get("open_incidents"))
    current = {alert["signature"]: alert for alert in alerts}
    events: list[dict[str, Any]] = []
    open_incidents: dict[str, dict[str, Any]] = {}
    for signature, alert in current.items():
        prior = _mapping(old_open.get(signature))
        incident = {
            **alert,
            "opened_at": prior.get("opened_at", observed_at),
            "last_seen_at": observed_at,
            "observations": int(prior.get("observations", 0)) + 1,
        }
        open_incidents[signature] = incident
        if not prior:
            events.append({"event": "opened", "timestamp": observed_at, **incident})
    for signature, prior in old_open.items():
        if signature not in current:
            events.append({"event": "closed", "timestamp": observed_at, **_mapping(prior)})
    checkpoint = {
        "schema": SCHEMA,
        "last_run_at": observed_at,
        "open_incidents": open_incidents,
    }
    return checkpoint, events


def _send_slack(config: dict[str, Any], transition: dict[str, Any]) -> bool:
    target = config.get("slack_target")
    if not isinstance(target, str) or not target:
        return False
    event = transition["event"].upper()
    message = f"[delivery-watch] {event} {transition['kind']}: {transition['message']}"
    try:
        result = subprocess.run(
            [str(HERMES_BIN), "send", "--to", target, message],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _ping_deadman(config: dict[str, Any]) -> bool:
    url = config.get("deadman_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=15):
            return True
    except (OSError, urllib.error.URLError):
        return False


def run_once(
    config: dict[str, Any],
    state_dir: Path,
    *,
    snapshot: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
    alert: bool = True,
    ping_deadman: bool = True,
) -> dict[str, Any]:
    observed = now or _now()
    observed_at = _iso(observed)
    if snapshot is None:
        evidence = collect_snapshot(config, now=observed)
    else:
        source_name = "direct-snapshot"
        try:
            validated = _validate_snapshot_payload(
                snapshot,
                source_name,
                now=observed,
                max_age_seconds=max(
                    60,
                    min(3600, int(config.get("snapshot_max_age_seconds", 600))),
                ),
            )
            collection = dict(_mapping(validated.get("collection")))
            collection.setdefault(source_name, {"status": "OK"})
            evidence = {
                **validated,
                "collection": collection,
            }
        except (WatchError, TypeError, ValueError) as exc:
            evidence = {
                "tasks": [],
                "watch": {},
                "collection": {
                    source_name: {
                        "status": "UNKNOWN",
                        "error": redact_sensitive(exc),
                    }
                },
            }
    global_unknown = [
        name
        for name, state in _mapping(evidence.get("collection")).items()
        if _mapping(state).get("status") != "OK"
    ]
    correlations: list[dict[str, Any]] = []
    for task_snapshot in _list_of_mappings(evidence.get("tasks")):
        normalized = dict(task_snapshot)
        sources = _mapping(normalized.get("sources"))
        for name, state in _mapping(evidence.get("collection")).items():
            # A collector-level failure is authoritative for this poll.  A
            # task-local value may be a stale OK from an earlier snapshot and
            # must never mask an UNKNOWN live source.
            if _mapping(state).get("status") != "OK" or name not in sources:
                sources[name] = state
        normalized["sources"] = sources
        try:
            correlations.append(correlate(normalized, generated_at=observed_at))
        except CorrelationError as exc:
            task_id = str(_mapping(normalized.get("task")).get("id") or "unknown")
            correlations.append(
                {
                    "schema": "task_delivery/v1",
                    "generated_at": observed_at,
                    "task": {"id": task_id, "lane": _mapping(normalized.get("task")).get("lane")},
                    "delivery_status": "UNKNOWN",
                    "unknown_sources": ["correlator_input"],
                    "missing_evidence": [],
                    "identity_mismatches": [],
                    "error": redact_sensitive(exc),
                }
            )
    alerts = evaluate_alerts(evidence, correlations, now=observed)
    with _state_lock(state_dir):
        checkpoint, transitions = _incident_transition(state_dir, alerts, observed_at=observed_at)
        event = {
            "schema": SCHEMA,
            "timestamp": observed_at,
            "collection_unknown": global_unknown,
            "correlations": correlations,
            "alerts": alerts,
            "transition_count": len(transitions),
            "review_gate": _mapping(evidence.get("watch")).get("review_gate"),
        }
        _append_jsonl(state_dir / "events.jsonl", event)
        for transition in transitions:
            _append_jsonl(state_dir / "incidents.jsonl", transition)
        _atomic_json(state_dir / "checkpoint.json", checkpoint)
        heartbeat = {
            "schema": SCHEMA,
            "timestamp": observed_at,
            "status": "UNKNOWN" if global_unknown else ("ALERT" if alerts else "OK"),
            "open_incidents": len(alerts),
            "task_count": len(correlations),
        }
        _atomic_json(state_dir / "heartbeat.json", heartbeat)
    sent = 0
    if alert:
        sent = sum(1 for transition in transitions if _send_slack(config, transition))
    deadman_pinged = _ping_deadman(config) if ping_deadman else False
    return {
        "schema": SCHEMA,
        "timestamp": observed_at,
        "status": heartbeat["status"],
        "task_count": len(correlations),
        "open_incidents": len(alerts),
        "transitions": len(transitions),
        "slack_notifications": sent,
        "deadman_pinged": deadman_pinged,
        "correlations": correlations,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise WatchError(f"cannot read evidence log {path}: {exc}") from exc
    return records


def final_evidence(
    state_dir: Path, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Evaluate the 24-72 hour acceptance window from durable watcher evidence."""
    current = now or _now()
    events = _read_jsonl(state_dir / "events.jsonl")
    checkpoint = _mapping(_load_json(state_dir / "checkpoint.json", {}))
    timestamps = [_parse_time(event.get("timestamp")) for event in events]
    observed_times = sorted(value for value in timestamps if value is not None)
    start = min(observed_times) if observed_times else None
    duration = current - start if start else timedelta()
    cadence_ok = bool(observed_times) and current - observed_times[-1] <= timedelta(minutes=10)
    if cadence_ok:
        cadence_ok = all(
            later - earlier <= timedelta(minutes=10)
            for earlier, later in zip(observed_times, observed_times[1:])
        )
    window_events = sorted([
        event
        for event in events
        if (_parse_time(event.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
        >= current - timedelta(hours=72)
    ], key=lambda event: _parse_time(event.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc))
    delivered: dict[str, dict[str, Any]] = {}
    all_alert_kinds: set[str] = set()
    violation_in_final_12h = False
    review_clean_max = 0
    review_clean_current = 0
    for event in window_events:
        event_time = _parse_time(event.get("timestamp"))
        event_alerts = _list_of_mappings(event.get("alerts"))
        for alert_item in event_alerts:
            all_alert_kinds.add(str(alert_item.get("kind")))
        gate = _mapping(event.get("review_gate"))
        if gate.get("status") == "clean":
            review_clean_current += 1
            review_clean_max = max(review_clean_max, review_clean_current)
        else:
            review_clean_current = 0
        if event_time and event_time >= current - timedelta(hours=12):
            if (
                event_alerts
                or event.get("collection_unknown")
                or event.get("status") == "UNKNOWN"
            ):
                violation_in_final_12h = True
        for result in _list_of_mappings(event.get("correlations")):
            task_id = str(_mapping(result.get("task")).get("id"))
            if result.get("delivery_status") == "DELIVERED":
                delivered[task_id] = result
            elif event_time and event_time >= current - timedelta(hours=12):
                if result.get("delivery_status") == "UNKNOWN":
                    violation_in_final_12h = True

    chains = list(delivered.values())
    run_ids = [str(_mapping(chain.get("executor")).get("run_id")) for chain in chains]
    fencing_tokens = [str(_mapping(chain.get("executor")).get("fencing_token")) for chain in chains]
    pr_sets = [tuple(chain.get("pr_head_sha_set") or [chain.get("delivery_head_sha")]) for chain in chains]
    ci_sets = [
        tuple(
            (str(run.get("run_id")), str(run.get("head_sha")))
            for run in _list_of_mappings(_mapping(chain.get("ci")).get("runs"))
        )
        for chain in chains
    ]
    distinct_chains = (
        len(run_ids) == len(set(run_ids))
        and len(fencing_tokens) == len(set(fencing_tokens))
        and len(pr_sets) == len(set(pr_sets))
        and len(ci_sets) == len(set(ci_sets))
    )
    forbidden = {
        "duplicate_ownership",
        "invalid_lifecycle_event",
        "uncertified_promotion",
        "promotion_receipt_mismatch",
    }
    open_incidents = _mapping(checkpoint.get("open_incidents"))
    checks = {
        "window_at_least_24h": duration >= timedelta(hours=24),
        "window_at_most_72h": duration <= timedelta(hours=72),
        "five_minute_watcher_cadence": cadence_ok,
        "three_distinct_deliveries": len(chains) >= 3 and distinct_chains,
        "no_ownership_lifecycle_or_promotion_violation": not bool(forbidden & all_alert_kinds),
        "three_consecutive_clean_review_runs": review_clean_max >= 3,
        "final_12h_no_unknown_or_open_alert": (
            not violation_in_final_12h and not open_incidents
        ),
    }
    result = {
        "schema": "task_delivery_acceptance/v1",
        "generated_at": _iso(current),
        "observation_started_at": _iso(start) if start else None,
        "observation_hours": round(duration.total_seconds() / 3600, 3),
        "delivered_task_ids": sorted(delivered),
        "checks": checks,
        "accepted": all(checks.values()),
    }
    _atomic_json(state_dir / "final-evidence.json", result)
    return result


def status(state_dir: Path) -> dict[str, Any]:
    return {
        "heartbeat": _load_json(state_dir / "heartbeat.json", {}),
        "checkpoint": _load_json(state_dir / "checkpoint.json", {}),
        "final_evidence": _load_json(state_dir / "final-evidence.json", {}),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="collect and evaluate one read-only poll")
    mode.add_argument("--status", action="store_true", help="print persisted watcher status without collecting")
    mode.add_argument("--final-evidence", action="store_true", help="evaluate and persist 24-72h acceptance evidence")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--snapshot", type=Path, help="offline normalized snapshot; bypass collectors")
    parser.add_argument("--no-alert", action="store_true", help="do not send Slack transition notifications")
    parser.add_argument("--no-deadman", action="store_true", help="do not ping the off-box dead-man URL")
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.status:
            result = status(args.state_dir)
        elif args.final_evidence:
            result = final_evidence(args.state_dir)
        else:
            config = _load_config(args.config)
            snapshot = _load_json(args.snapshot, {}) if args.snapshot else None
            result = run_once(
                config,
                args.state_dir,
                snapshot=snapshot,
                alert=not args.no_alert,
                ping_deadman=not args.no_deadman,
            )
    except WatchError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "UNKNOWN", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.once and result.get("status") == "UNKNOWN":
        return 2
    if args.final_evidence and not result.get("accepted"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
