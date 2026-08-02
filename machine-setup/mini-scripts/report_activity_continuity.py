#!/usr/bin/env python3
"""Read-only Hermes Mac mini lifecycle continuity and parity adapter.

Consumes the durable report-activity journal, machine-readable Mini health
attestation, immutable fleet activation provenance, and a bounded authoritative
ClickUp sample. It never queries the cron execution database or mutates state.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import report_activity_journal as journal  # noqa: E402

SCHEMA = "hermes-mini-activity-continuity/v1"
HEALTH_SCHEMA = "hermes-mini-health-attestation/v1"
SCOPE = "Hermes Mac mini"
WINDOW_HOURS = 6
DEFAULT_SAMPLE_SIZE = 12
QUALIFYING_KINDS = frozenset(("claim", "review_handoff", "validator_complete"))
DEFAULT_STATE_DIR = Path(os.path.expanduser("~/.hermes/state/report-activity"))
DEFAULT_FLEET_RECEIPTS = Path(os.path.expanduser("~/.hermes/logs/fleet-config-installs"))
DEFAULT_HEALTH_ATTESTATION = Path(
    os.path.expanduser(
        "~/.hermes/runtime-current/machine-setup/mini-scripts/mini_health_attestation.py"
    )
)
DEFAULT_CLICKUP = journal.DEFAULT_CLICKUP
DEFAULT_JOBS_PATH = Path(os.path.expanduser("~/.hermes/cron/jobs.json"))
REQUIRED_WRITER_JOBS = (
    "clickup-executor",
    "hermes-pr-validate",
    "clickup-lifecycle",
)
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ContinuityError(RuntimeError):
    """An input cannot support a truthful continuity conclusion."""


def parse_timestamp(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        parsed = dt.datetime.fromtimestamp(numeric, tz=dt.timezone.utc)
    else:
        try:
            raw = str(value)
            if re.fullmatch(r"\d{8}T\d{6}Z", raw):
                parsed = dt.datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=dt.timezone.utc
                )
            else:
                parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ContinuityError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContinuityError(f"timestamp is not timezone-aware: {value!r}")
    return parsed.astimezone(dt.timezone.utc)


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def scheduled_slot(value: dt.datetime) -> dt.datetime:
    value = parse_timestamp(value)
    hour = value.hour - (value.hour % WINDOW_HOURS)
    return value.replace(hour=hour, minute=0, second=0, microsecond=0)


def require_scheduled_slot(value: Any) -> dt.datetime:
    if value in (None, ""):
        raise ContinuityError("nominal scheduled slot is required")
    try:
        slot = parse_timestamp(value)
    except ContinuityError as exc:
        raise ContinuityError(f"invalid nominal scheduled slot: {exc}") from exc
    if scheduled_slot(slot) != slot:
        raise ContinuityError(
            "nominal scheduled slot must be an exact six-hour UTC boundary"
        )
    return slot


def slot_windows(
    slot: dt.datetime,
) -> tuple[tuple[dt.datetime, dt.datetime], tuple[dt.datetime, dt.datetime]]:
    slot = scheduled_slot(slot)
    width = dt.timedelta(hours=WINDOW_HOURS)
    return (slot - (2 * width), slot - width), (slot - width, slot)


def _result(
    *,
    slot: dt.datetime | None,
    state: str,
    reasons: list[str],
    windows: dict[str, Any],
    parity: dict[str, Any],
    provenance: dict[str, Any] | None,
    health_attestation: dict[str, Any] | None = None,
    concern_id: str | None = None,
) -> dict[str, Any]:
    total = sum(
        int((windows.get(name) or {}).get("total") or 0)
        for name in ("previous", "current")
    )
    reason_text = "; ".join(reasons) if reasons else (
        f"{total} qualifying lifecycle events across two complete covered windows"
    )
    slot_id = iso(slot) if slot is not None else None
    return {
        "schema": SCHEMA,
        "scope": SCOPE,
        "state": state,
        "slot_id": slot_id,
        "concern_id": concern_id,
        "detail": (
            f"Activity continuity {state} for the Hermes Mac mini at slot "
            f"{slot_id or 'UNKNOWN'}: {reason_text}. Known residual: a process can crash "
            "after ClickUp succeeds but before the outbox append."
        ),
        "reasons": reasons,
        "windows": windows,
        "parity": parity,
        "provenance": provenance,
        "health_attestation": health_attestation,
        "limitations": [
            "Hermes Mac mini only; never fleet-wide",
            "post-ClickUp/pre-append process crash can omit an event",
        ],
    }


def verify_provenance(receipt: Any) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ContinuityError("fleet activation receipt is not an object")
    if receipt.get("result") != "success":
        raise ContinuityError("fleet activation receipt is not successful")
    timestamp = parse_timestamp(receipt.get("timestamp"))
    lease = receipt.get("production_write_lease") or {}
    commit = str(lease.get("commit_sha") or "")
    if not COMMIT_RE.fullmatch(commit):
        raise ContinuityError("fleet activation receipt lacks a full commit SHA")
    manifest_sha = str(receipt.get("manifest_sha256") or "")
    if not SHA256_RE.fullmatch(manifest_sha):
        raise ContinuityError("fleet activation receipt lacks a valid manifest hash")
    manifest_path = str(receipt.get("manifest_path") or "")
    if f"-{commit[:12]}" not in manifest_path:
        raise ContinuityError("fleet manifest path is not bound to the receipt commit")
    job_steps = [
        step for step in receipt.get("steps") or []
        if isinstance(step, dict) and step.get("step") == "jobs_json"
    ]
    if len(job_steps) != 1 or job_steps[0].get("status") != "installed":
        raise ContinuityError("fleet receipt does not prove jobs_json installation")
    return {
        "status": "OK",
        "coverage_started_at": iso(timestamp),
        "source_commit": commit,
        "manifest_sha256": manifest_sha,
        "receipt_timestamp": receipt.get("timestamp"),
    }


def load_latest_provenance(
    root: Path = DEFAULT_FLEET_RECEIPTS,
) -> dict[str, Any]:
    try:
        paths = sorted(root.glob("*/install-receipt.json"), reverse=True)
    except OSError as exc:
        raise ContinuityError(f"fleet receipt inventory unreadable: {exc}") from exc
    failures: list[str] = []
    for path in paths:
        try:
            verified = verify_provenance(
                json.loads(path.read_text(encoding="utf-8"))
            )
            verified["receipt_path"] = str(path)
            return verified
        except (OSError, json.JSONDecodeError, ContinuityError) as exc:
            failures.append(f"{path.parent.name}: {exc}")
    suffix = f" ({'; '.join(failures[:3])})" if failures else ""
    raise ContinuityError(
        f"no valid fleet activation provenance available{suffix}"
    )


def run_health_attestation(
    path: Path = DEFAULT_HEALTH_ATTESTATION,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    try:
        result = runner(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except Exception as exc:
        raise ContinuityError(
            f"Mini health attestation unavailable: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContinuityError(
            f"Mini health attestation returned malformed JSON: {exc}"
        ) from exc
    if result.returncode != 0:
        raise ContinuityError(
            f"Mini health attestation exited {result.returncode}"
        )
    if not isinstance(payload, dict) or payload.get("schema") != HEALTH_SCHEMA:
        raise ContinuityError("Mini health attestation schema mismatch")
    if payload.get("exit_code") != 0 or payload.get("healthy") is not True:
        raise ContinuityError("Mini health attestation is unhealthy or unknown")
    checks = {
        str(check.get("id")): check.get("state")
        for check in payload.get("checks") or []
        if isinstance(check, dict)
    }
    required = (
        "runtime.commit",
        "runtime.source-binding",
        "execution.inflight-classification",
    )
    for check_id in required:
        if checks.get(check_id) != "pass":
            raise ContinuityError(
                f"Mini health attestation lacks passing {check_id}"
            )
    return {
        "status": "OK",
        "schema": payload["schema"],
        "generated_at": payload.get("generated_at"),
        "summary": payload.get("summary"),
    }


def verify_writer_coverage(path: Path = DEFAULT_JOBS_PATH) -> dict[str, Any]:
    """Verify the live enabled lifecycle cron prompts still contain emitters."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityError(f"lifecycle writer inventory unreadable: {exc}") from exc
    jobs = payload.get("jobs") if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        raise ContinuityError("lifecycle writer inventory is not a job list")
    by_name = {
        str(job.get("name") or job.get("id") or ""): job
        for job in jobs
        if isinstance(job, dict)
    }
    verified: list[str] = []
    for name in REQUIRED_WRITER_JOBS:
        job = by_name.get(name)
        if not job or job.get("enabled") is False:
            raise ContinuityError(f"required lifecycle writer is missing or disabled: {name}")
        prompt = str(job.get("prompt") or job.get("message") or "")
        missing = [
            marker for marker in (
                "report_activity_journal.py",
                "confirm-transition",
                "86e2gnz71",
            )
            if marker not in prompt
        ]
        if missing:
            raise ContinuityError(
                f"enabled lifecycle writer {name} is uninstrumented: "
                + ",".join(missing)
            )
        verified.append(name)
    return {"status": "OK", "verified_jobs": verified, "path": str(path)}


def _extract_task(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
        payload = payload["task"]
    if not isinstance(payload, dict):
        raise ContinuityError("ClickUp task response is not an object")
    return payload


def fetch_clickup_task(
    task_id: str,
    *,
    clickup_path: Path = DEFAULT_CLICKUP,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    result = runner(
        ["node", str(clickup_path), "task", task_id, "--json"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise ContinuityError(
            f"ClickUp task read rejected for {task_id}: rc={result.returncode}"
        )
    try:
        return _extract_task(json.loads(result.stdout))
    except json.JSONDecodeError as exc:
        raise ContinuityError(
            f"ClickUp task read returned malformed JSON for {task_id}"
        ) from exc


def _status(task: dict[str, Any]) -> str:
    value = task.get("status")
    if isinstance(value, dict):
        value = value.get("status")
    return str(value or "").strip().casefold()


def _authoritative_transition_timestamp(
    kind: str, task: dict[str, Any]
) -> dt.datetime:
    if kind == "validator_complete":
        value = task.get("date_closed")
        field = "date_closed"
    elif kind == "review_handoff":
        value = task.get("date_updated") or task.get("updated_at")
        field = "date_updated/updated_at"
    else:
        raise ContinuityError(f"{kind} has no ClickUp transition timestamp contract")
    if value in (None, ""):
        raise ContinuityError(
            f"authoritative ClickUp task lacks {field} transition evidence"
        )
    return parse_timestamp(value)


def _event_matches_task(
    event: dict[str, Any], task: dict[str, Any]
) -> tuple[bool, str]:
    status = _status(task)
    allowed = {
        "claim": {
            "in progress", "in review", "ready for review", "complete", "closed"
        },
        "review_handoff": {
            "in review", "ready for review", "complete", "closed"
        },
        "validator_complete": {"complete", "closed"},
    }[str(event["kind"])]
    if status not in allowed:
        return False, f"{event['kind']} disagrees with status {status!r}"
    if str(task.get("id") or "") != str(event.get("task_id") or ""):
        return False, "authoritative task id mismatch"
    if event["kind"] == "claim":
        return True, "matched"
    transition_at = event.get("clickup_transition_at")
    if not transition_at:
        return False, f"{event['kind']} lacks exact transition timestamp identity"
    event_at = parse_timestamp(transition_at)
    authoritative_at = _authoritative_transition_timestamp(
        str(event["kind"]), task
    )
    if authoritative_at != event_at:
        return False, (
            f"authoritative {event['kind']} timestamp {iso(authoritative_at)} "
            f"does not equal outbox transition {iso(event_at)}"
        )
    return True, "matched"


def reconcile_clickup(
    events: list[dict[str, Any]],
    *,
    fetch_task: Callable[[str], dict[str, Any]],
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict[str, Any]:
    if sample_size < 1 or sample_size > 100:
        raise ContinuityError(
            "ClickUp sample size must be between 1 and 100"
        )
    unique = {str(event["event_id"]): event for event in events}
    ordered = sorted(
        unique.values(),
        key=lambda event: hashlib.sha256(
            str(event["event_id"]).encode()
        ).hexdigest(),
    )
    sample = ordered[:sample_size]
    rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    for event in sample:
        try:
            task = _extract_task(fetch_task(str(event["task_id"])))
            matched, detail = _event_matches_task(event, task)
        except Exception as exc:
            matched = False
            detail = f"{type(exc).__name__}: {exc}"
        rows.append({
            "event_id": event["event_id"],
            "task_id": event["task_id"],
            "kind": event["kind"],
            "matched": matched,
            "detail": detail,
        })
        if not matched:
            reasons.append(f"{event['event_id']}: {detail}")
    return {
        "status": "OK" if not reasons else "UNKNOWN",
        "population": len(ordered),
        "sample_limit": sample_size,
        "sampled": len(sample),
        "matched": sum(bool(row["matched"]) for row in rows),
        "rows": rows,
        "reasons": reasons,
    }


def _counts(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind = {kind: 0 for kind in sorted(QUALIFYING_KINDS)}
    for event in events:
        by_kind[str(event["kind"])] += 1
    return {"total": len(events), "by_kind": by_kind}


def evaluate_continuity(
    *,
    now: dt.datetime,
    nominal_scheduled_slot: Any = None,
    state_dir: Path = DEFAULT_STATE_DIR,
    strict_validator_completed: int,
    report_window_min: int = 360,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    inventory: Any = journal.PRODUCER_INVENTORY,
    provenance: dict[str, Any] | None = None,
    health_attestation: dict[str, Any] | None = None,
    writer_coverage: dict[str, Any] | None = None,
    fetch_task: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = parse_timestamp(now)
    try:
        slot = require_scheduled_slot(nominal_scheduled_slot)
    except ContinuityError as exc:
        return _result(
            slot=None,
            state="UNKNOWN",
            reasons=[str(exc)],
            windows={},
            parity={"status": "UNKNOWN", "reasons": [str(exc)]},
            provenance=provenance,
            health_attestation=health_attestation,
        )
    previous, current = slot_windows(slot)
    reasons: list[str] = []

    journal_health = journal.health(
        state_dir=state_dir, inventory=inventory, now=now
    )
    if (
        journal_health.get("status") != "OK"
        or journal_health.get("degraded")
    ):
        reasons.extend(
            str(reason) for reason in journal_health.get("reasons") or []
        )
        if not reasons:
            reasons.append("journal health is UNKNOWN")

    combined = journal.read_events(
        previous[0], current[1], state_dir=state_dir
    )
    if combined["status"] != "OK":
        reasons.extend(
            str(reason) for reason in combined.get("reasons") or []
        )
    events = list(combined.get("events") or [])
    previous_events = [
        event for event in events
        if previous[0] <= parse_timestamp(event["ts"]) < previous[1]
    ]
    current_events = [
        event for event in events
        if current[0] <= parse_timestamp(event["ts"]) < current[1]
    ]
    windows = {
        "previous": {
            "start": iso(previous[0]),
            "end": iso(previous[1]),
            **_counts(previous_events),
        },
        "current": {
            "start": iso(current[0]),
            "end": iso(current[1]),
            **_counts(current_events),
        },
        "duplicates_deduped": int(combined.get("duplicates") or 0),
    }

    verified_provenance = provenance
    if verified_provenance is None:
        try:
            verified_provenance = load_latest_provenance()
        except ContinuityError as exc:
            reasons.append(str(exc))
    elif verified_provenance.get("status") != "OK":
        reasons.append(
            str(verified_provenance.get("reason") or "provenance UNKNOWN")
        )

    verified_health = health_attestation
    if verified_health is None:
        try:
            verified_health = run_health_attestation()
        except ContinuityError as exc:
            reasons.append(str(exc))
    elif (
        verified_health.get("status") != "OK"
        or verified_health.get("schema") != HEALTH_SCHEMA
    ):
        reasons.append(
            str(
                verified_health.get("reason")
                or "Mini health attestation is missing, unhealthy, or unbound"
            )
        )

    verified_writers = writer_coverage
    if verified_writers is None:
        try:
            verified_writers = verify_writer_coverage()
        except ContinuityError as exc:
            reasons.append(str(exc))
    elif verified_writers.get("status") != "OK":
        reasons.append(
            str(
                verified_writers.get("reason")
                or "enabled lifecycle writer coverage is UNKNOWN"
            )
        )

    fetch_task = fetch_task or (
        lambda task_id: fetch_clickup_task(task_id)
    )
    try:
        parity = reconcile_clickup(
            events, fetch_task=fetch_task, sample_size=sample_size
        )
    except ContinuityError as exc:
        parity = {"status": "UNKNOWN", "reasons": [str(exc)]}
    if parity.get("status") != "OK":
        reasons.extend(
            str(reason)
            for reason in parity.get("reasons") or ["ClickUp parity UNKNOWN"]
        )

    report_start = now - dt.timedelta(minutes=report_window_min)
    report_events = journal.read_events(
        report_start, now, state_dir=state_dir
    )
    if report_events.get("status") != "OK":
        reasons.extend(
            str(reason) for reason in report_events.get("reasons") or []
        )
    outbox_completed = sum(
        event.get("kind") == "validator_complete"
        for event in report_events.get("events") or []
    )
    completion_agrees = (
        outbox_completed == int(strict_validator_completed)
    )
    parity["strict_completion"] = {
        "authoritative_validator_completed": int(
            strict_validator_completed
        ),
        "outbox_validator_complete": outbox_completed,
        "agrees": completion_agrees,
        "metric_source": (
            "strict PASS + terminal status + in-window date_closed"
        ),
        "outbox_role": "parity evidence only",
    }
    if not completion_agrees:
        reasons.append(
            "strict validator-completion disagreement: "
            f"authoritative={strict_validator_completed}, "
            f"outbox={outbox_completed}"
        )
        parity["status"] = "UNKNOWN"

    if reasons:
        return _result(
            slot=slot,
            state="UNKNOWN",
            reasons=sorted(set(reasons)),
            windows=windows,
            parity=parity,
            provenance=verified_provenance,
            health_attestation=verified_health,
        )

    coverage_start = parse_timestamp(
        verified_provenance["coverage_started_at"]
    )
    if coverage_start > previous[0]:
        return _result(
            slot=slot,
            state="PROVISIONAL",
            reasons=[
                f"coverage starts at {iso(coverage_start)} after required "
                f"window start {iso(previous[0])}"
            ],
            windows=windows,
            parity=parity,
            provenance=verified_provenance,
            health_attestation=verified_health,
        )

    total = len(previous_events) + len(current_events)
    state = "ACTIVE" if total else "INACTIVE"
    concern_id = None
    if state == "INACTIVE":
        concern_id = (
            "hermes-mini-activity-continuity:"
            + hashlib.sha256(iso(slot).encode()).hexdigest()[:24]
        )
    return _result(
        slot=slot,
        state=state,
        reasons=[],
        windows=windows,
        parity=parity,
        provenance=verified_provenance,
        health_attestation=verified_health,
        concern_id=concern_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument(
        "--state-dir", type=Path, default=DEFAULT_STATE_DIR
    )
    evaluate.add_argument(
        "--now", help="UTC/offset timestamp; defaults to current UTC"
    )
    evaluate.add_argument(
        "--scheduled-slot",
        help="Nominal six-hour UTC slot; late/manual reruns reuse the original value",
    )
    evaluate.add_argument(
        "--strict-validator-completed", type=int, required=True
    )
    evaluate.add_argument("--report-window-min", type=int, default=360)
    evaluate.add_argument(
        "--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE
    )
    evaluate.add_argument(
        "--fleet-receipts", type=Path, default=DEFAULT_FLEET_RECEIPTS
    )
    evaluate.add_argument(
        "--health-attestation",
        type=Path,
        default=DEFAULT_HEALTH_ATTESTATION,
    )
    evaluate.add_argument(
        "--clickup-path", type=Path, default=DEFAULT_CLICKUP
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = (
        parse_timestamp(args.now)
        if args.now
        else dt.datetime.now(dt.timezone.utc)
    )
    try:
        provenance = load_latest_provenance(args.fleet_receipts)
    except ContinuityError as exc:
        provenance = {"status": "UNKNOWN", "reason": str(exc)}
    try:
        health_attestation = run_health_attestation(
            args.health_attestation
        )
    except ContinuityError as exc:
        health_attestation = {
            "status": "UNKNOWN",
            "schema": HEALTH_SCHEMA,
            "reason": str(exc),
        }
    result = evaluate_continuity(
        now=now,
        nominal_scheduled_slot=args.scheduled_slot,
        state_dir=args.state_dir,
        strict_validator_completed=args.strict_validator_completed,
        report_window_min=args.report_window_min,
        sample_size=args.sample_size,
        provenance=provenance,
        health_attestation=health_attestation,
        fetch_task=lambda task_id: fetch_clickup_task(
            task_id, clickup_path=args.clickup_path
        ),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
