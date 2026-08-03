#!/usr/bin/env python3
"""Verify real outcomes for the rebuilt Mini cron and LaunchAgent fleet.

This monitor intentionally runs outside Hermes cron.  It validates the
source-controlled contract inventory against the live 15-job jobs.json, then
checks two independent signals for each enabled cron job:

* the scheduler's persisted status/cadence; and
* the newest saved output document (or a named semantic artifact).

LaunchAgents are checked for expected loaded/retired state plus an independent
endpoint, receipt, or fresh semantic log.  A launchd exit code alone is never
accepted as outcome proof.

Findings go directly through ``hermes send --to slack:hermes``.  Alert dedupe
is delivery-aware: a signature is persisted only after the send succeeds.
``--drill-all`` injects one synthetic finding per contract through the same
formatter, Slack sender, receipt, and dedupe path without mutating production
job or LaunchAgent state.

Every run is archived.  The point-in-time receipt is overwritten in place, so
it can only ever describe the newest probe -- an alarm storm that has since
churned is untriageable from it.  Each run therefore also appends one bounded
record to a rotated history ledger, and every alarm-relevant transition keeps a
full receipt snapshot, so a storm can be reconstructed after the fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from xml.parsers.expat import ExpatError


HOME = Path.home()
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONTRACTS = SCRIPT_DIR / "fleet_outcome_contracts.json"
DEFAULT_JOBS = HOME / ".hermes/cron/jobs.json"
DEFAULT_OUTPUT_ROOT = HOME / ".hermes/cron/output"
DEFAULT_LAUNCH_AGENTS = HOME / "Library/LaunchAgents"
DEFAULT_STATE = HOME / ".hermes/state/fleet-outcome-alert-state.json"
DEFAULT_RECEIPT = HOME / ".hermes/state/fleet-outcome-probe.json"
DEFAULT_DRILL_STATE = HOME / ".hermes/state/fleet-outcome-drill-alert-state.json"
DEFAULT_DRILL_RECEIPT = HOME / ".hermes/state/fleet-outcome-drill-receipt.json"
DEFAULT_RELEASE_RECEIPT = HOME / ".hermes/releases/.mini-release-last-receipt.json"
HERMES_BIN = HOME / ".local/bin/hermes"
SLACK_TARGET = "slack:hermes"
REALERT_SECONDS = 6 * 60 * 60
NEW_FINDING_CONFIRM_SECONDS = 4 * 60
RECOVERY_CONFIRM_SECONDS = 4 * 60
CUTOVER_GRACE_SECONDS = 10 * 60
# An incident that has stayed open this long with an unchanged finding set is
# far more often a contract that drifted stale than a live outage, so the alert
# says so instead of repeating an unactionable page.
STALE_INCIDENT_SECONDS = 24 * 60 * 60
HISTORY_MAX_BYTES = 4 * 1024 * 1024
HISTORY_SNAPSHOT_LIMIT = 240
HISTORY_DETAIL_CHARS = 300
NOTABLE_ALARM_ACTIONS = frozenset(
    {
        "sent",
        "recovery-sent",
        "delivery-failed",
        "recovery-delivery-failed",
        "unreported",
    }
)
MONITORED_LABEL_RE = re.compile(
    r"^(?:ai\.hermes\.|com\.hermes\.|com\.ignite\.|com\.colingreig\.(?:hermes|ignite|pull_anthropic))"
)


class ProbeError(RuntimeError):
    """The monitor itself could not evaluate its declared contracts."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"{path} is unreadable: {exc}") from exc


def _load_optional_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _history_paths(receipt_path: Path, history_dir: Path | None = None) -> tuple[Path, Path]:
    """Return the (ledger, snapshot directory) archive for a probe receipt."""
    root = (
        history_dir
        if history_dir is not None
        else receipt_path.parent / f"{receipt_path.stem}-history"
    )
    return root.parent / f"{root.name}.jsonl", root


DEFAULT_HISTORY_LEDGER, DEFAULT_HISTORY_DIR = _history_paths(DEFAULT_RECEIPT)


def _history_tail(path: Path, limit: int = 1) -> list[dict[str, Any]]:
    """Return up to ``limit`` newest archived records, oldest first.

    Only the tail of the ledger is read so a rotated multi-megabyte archive
    never becomes a reason to skip reading history during triage.
    """
    if limit <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, max(65536, limit * 8192))
            handle.seek(size - window)
            data = handle.read(window)
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if window < size and lines:
        # The window can start mid-record; that leading fragment is not JSON.
        lines = lines[1:]
    records: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records[-limit:]


def _append_history(path: Path, record: dict[str, Any]) -> None:
    """Append one bounded record, rotating a single previous generation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    try:
        current = path.stat().st_size
    except OSError:
        current = 0
    if current and current + len(line.encode("utf-8")) > HISTORY_MAX_BYTES:
        os.replace(path, path.with_name(f"{path.name}.1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, 0o600)


def _prune_history_snapshots(directory: Path, *, limit: int | None = None) -> None:
    limit = HISTORY_SNAPSHOT_LIMIT if limit is None else limit
    try:
        snapshots = sorted(path for path in directory.glob("*.json") if path.is_file())
    except OSError:
        return
    for stale in snapshots[: max(0, len(snapshots) - limit)]:
        try:
            stale.unlink()
        except OSError:
            continue


def _history_record(receipt: dict[str, Any]) -> dict[str, Any]:
    """Project a receipt onto the compact, storm-triageable ledger shape."""
    alarm = dict(receipt.get("alarm") or {})
    return {
        "checked_at": receipt.get("checked_at"),
        "mode": receipt.get("mode"),
        "status": receipt.get("status"),
        "finding_count": receipt.get("finding_count"),
        "alarm_action": alarm.get("action"),
        "alarm_reason": alarm.get("reason"),
        "alarm_error": alarm.get("error"),
        "incident_id": alarm.get("incident_id"),
        "incident_opened_at": alarm.get("incident_opened_at"),
        "signature": alarm.get("signature"),
        "suppressed_until": alarm.get("suppressed_until"),
        "findings": [
            {
                "surface": item.get("surface"),
                "id": item.get("id"),
                "code": item.get("code"),
                "detail": str(item.get("detail") or "")[:HISTORY_DETAIL_CHARS],
            }
            for item in list(receipt.get("findings") or [])
        ],
    }


def _is_history_transition(
    previous: dict[str, Any] | None, record: dict[str, Any]
) -> bool:
    """Report whether this run is worth keeping a full receipt snapshot for."""
    if previous is None:
        return True
    if str(record.get("alarm_action") or "") in NOTABLE_ALARM_ACTIONS:
        return True
    if previous.get("status") != record.get("status"):
        return True
    if previous.get("incident_id") != record.get("incident_id"):
        return True
    # Finding-set churn inside one incident is exactly what the overwritten
    # receipt used to erase, so it earns a snapshot even when it is deduped.
    return previous.get("signature") != record.get("signature")


def archive_probe_run(
    receipt: dict[str, Any],
    *,
    receipt_path: Path,
    history_dir: Path | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Archive one probe run so an alarm storm stays triageable afterwards."""
    ledger_path, snapshot_dir = _history_paths(receipt_path, history_dir)
    record = _history_record(receipt)
    previous = _history_tail(ledger_path, 1)
    transition = _is_history_transition(previous[-1] if previous else None, record)
    summary: dict[str, Any] = {
        "ledger_path": str(ledger_path),
        "snapshot_dir": str(snapshot_dir),
        "transition": transition,
        "snapshot": None,
    }
    if transition:
        action = str((receipt.get("alarm") or {}).get("action") or "unknown")
        slug = re.sub(r"[^a-z0-9]+", "-", action.lower()).strip("-") or "unknown"
        name = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{slug}.json"
        summary["snapshot"] = name
        snapshot_receipt = dict(receipt)
        snapshot_receipt["history"] = dict(summary)
        _atomic_json(snapshot_dir / name, snapshot_receipt)
        _prune_history_snapshots(snapshot_dir)
    record["snapshot"] = summary["snapshot"]
    _append_history(ledger_path, record)
    return summary


def _expand_path(value: str, *, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _age_seconds(path: Path, now: datetime) -> float:
    return max(0.0, now.timestamp() - path.stat().st_mtime)


def _finding(surface: str, identifier: str, code: str, detail: str) -> dict[str, str]:
    return {
        "surface": surface,
        "id": identifier,
        "code": code,
        "detail": detail,
    }


def _patterns_match(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns)


_CANONICAL_FAILED_CRON_HEADER_RE = re.compile(
    r"^#\s+Cron Job:\s+.+\s+\(FAILED\)\s*$",
    re.MULTILINE,
)


def _canonical_failed_cron_evidence(text: str) -> str | None:
    """Return scheduler-trusted failed-run evidence (header + ## Error).

    Matches the artifact shape emitted by ``cron.scheduler.run_job`` on
    exceptions.  Prompt text is intentionally excluded so response-only
    contracts can classify genuine runtime failures without scanning skills.
    """
    header = _CANONICAL_FAILED_CRON_HEADER_RE.search(text)
    if header is None:
        return None
    error_marker = "## Error"
    if error_marker not in text:
        return None
    header_line = text[header.start() : header.end()]
    error_section = text.rsplit(error_marker, 1)[1]
    return f"{header_line}\n\n{error_marker}{error_section}"


def _check_text_evidence(
    *,
    surface: str,
    identifier: str,
    path: Path,
    outcome: dict[str, Any],
    now: datetime,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if not path.is_file():
        return [_finding(surface, identifier, "evidence_missing", f"missing {path}")]

    max_age = int(outcome["max_age_seconds"])
    age = _age_seconds(path, now)
    if age > max_age:
        findings.append(
            _finding(
                surface,
                identifier,
                "evidence_stale",
                f"{path} age {int(age)}s exceeds {max_age}s",
            )
        )
        return findings

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [_finding(surface, identifier, "evidence_unreadable", f"{path}: {exc}")]
    if outcome.get("kind") == "text_artifact":
        tail_lines = int(outcome.get("tail_lines", 200))
        text = "\n".join(text.splitlines()[-tail_lines:])
        run_start_pattern = outcome.get("run_start_pattern")
        run_end_pattern = outcome.get("run_end_pattern")
        latest_record_pattern = outcome.get("latest_record_pattern")
        if run_end_pattern:
            ends = list(
                re.finditer(str(run_end_pattern), text, re.IGNORECASE | re.MULTILINE)
            )
            if not ends:
                return [
                    _finding(
                        surface,
                        identifier,
                        "run_boundary_missing",
                        f"{path} has no declared run-end marker",
                    )
                ]
            start = ends[-2].end() if len(ends) > 1 else 0
            trailing = text[ends[-1].end() :]
            text = text[start:]
            if trailing.strip():
                findings.append(
                    _finding(
                        surface,
                        identifier,
                        "run_incomplete",
                        f"{path} has evidence after its latest completed-run marker",
                    )
                )
        elif run_start_pattern:
            starts = list(
                re.finditer(str(run_start_pattern), text, re.IGNORECASE | re.MULTILINE)
            )
            if not starts:
                return [
                    _finding(
                        surface,
                        identifier,
                        "run_boundary_missing",
                        f"{path} has no declared run-start marker",
                    )
                ]
            text = text[starts[-1].start() :]
        elif latest_record_pattern:
            records = [
                line
                for line in text.splitlines()
                if re.search(
                    str(latest_record_pattern),
                    line,
                    re.IGNORECASE | re.MULTILINE,
                )
            ]
            if not records:
                return [
                    _finding(
                        surface,
                        identifier,
                        "record_boundary_missing",
                        f"{path} has no declared outcome record",
                    )
                ]
            text = records[-1]
    if outcome.get("response_only"):
        marker = "## Response"
        failed_evidence = _canonical_failed_cron_evidence(text)
        if failed_evidence is not None:
            text = failed_evidence
        elif marker not in text:
            return [
                _finding(
                    surface,
                    identifier,
                    "response_section_missing",
                    f"{path} has no {marker} section",
                )
            ]
        else:
            text = text.rsplit(marker, 1)[1]

    forbidden = list(outcome.get("failure_patterns") or [])
    if forbidden and _patterns_match(text, forbidden):
        findings.append(
            _finding(surface, identifier, "failure_marker", f"{path} contains a failure marker")
        )
    required = list(outcome.get("success_patterns") or [])
    if required and not _patterns_match(text, required):
        findings.append(
            _finding(
                surface,
                identifier,
                "success_marker_missing",
                f"{path} has none of the declared semantic success markers",
            )
        )
    for pattern in list(outcome.get("required_patterns") or []):
        if not re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            findings.append(
                _finding(
                    surface,
                    identifier,
                    "required_marker_missing",
                    f"{path} lacks required semantic marker {pattern!r}",
                )
            )
    return findings


def _latest_output(output_root: Path, job_id: str) -> Path | None:
    job_root = output_root / job_id
    try:
        candidates = [path for path in job_root.glob("*.md") if path.is_file()]
    except OSError:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _check_artifact(
    *,
    surface: str,
    identifier: str,
    outcome: dict[str, Any],
    home: Path,
    now: datetime,
) -> list[dict[str, str]]:
    path = _expand_path(str(outcome["path"]), home=home)
    findings = _check_text_evidence(
        surface=surface,
        identifier=identifier,
        path=path,
        outcome=outcome,
        now=now,
    )
    if findings or outcome.get("kind") != "json_artifact":
        return findings
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [_finding(surface, identifier, "artifact_invalid_json", f"{path}: {exc}")]
    if not isinstance(payload, dict):
        return [_finding(surface, identifier, "artifact_wrong_shape", f"{path} is not an object")]
    for key, expected in (outcome.get("required_values") or {}).items():
        if payload.get(key) != expected:
            findings.append(
                _finding(
                    surface,
                    identifier,
                    "artifact_value_mismatch",
                    f"{path} key {key!r} is {payload.get(key)!r}, expected {expected!r}",
                )
            )
    linked = outcome.get("linked_artifact")
    if linked:
        linked_path = _expand_path(str(linked["path"]), home=home)
        hash_key = str(linked.get("sha256_key") or "sha256")
        try:
            observed_hash = hashlib.sha256(linked_path.read_bytes()).hexdigest()
        except OSError as exc:
            findings.append(
                _finding(
                    surface,
                    identifier,
                    "linked_artifact_missing",
                    f"{linked_path}: {exc}",
                )
            )
        else:
            if payload.get(hash_key) != observed_hash:
                findings.append(
                    _finding(
                        surface,
                        identifier,
                        "linked_artifact_hash_mismatch",
                        f"{path} {hash_key!r} does not match {linked_path}",
                    )
                )
    timestamp_key = outcome.get("timestamp_key")
    if timestamp_key:
        observed = _parse_time(payload.get(timestamp_key))
        if observed is None:
            findings.append(
                _finding(
                    surface,
                    identifier,
                    "artifact_timestamp_invalid",
                    f"{path} key {timestamp_key!r} is absent or invalid",
                )
            )
        else:
            age = (now - observed).total_seconds()
            max_age = int(outcome["max_age_seconds"])
            if age < -60:
                findings.append(
                    _finding(
                        surface,
                        identifier,
                        "artifact_timestamp_future",
                        f"{path} timestamp is {int(-age)}s in the future",
                    )
                )
            elif age > max_age:
                findings.append(
                    _finding(
                        surface,
                        identifier,
                        "artifact_timestamp_stale",
                        f"{path} timestamp age {int(age)}s exceeds {max_age}s",
                    )
                )
    return findings


def _check_cron_contracts(
    contracts: list[dict[str, Any]],
    *,
    jobs_path: Path,
    output_root: Path,
    home: Path,
    now: datetime,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    payload = _load_json(jobs_path)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ProbeError(f"{jobs_path} does not contain a jobs array")

    by_id = {
        str(job.get("id")): job
        for job in jobs
        if isinstance(job, dict) and str(job.get("id") or "").strip()
    }
    by_contract = {str(contract["id"]): contract for contract in contracts}
    if len(by_contract) != len(contracts):
        raise ProbeError("duplicate cron contract id")

    findings: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    for job_id, job in by_id.items():
        if job.get("enabled") and job_id not in by_contract:
            findings.append(
                _finding("cron", job_id, "uncovered_enabled_job", str(job.get("name") or job_id))
            )

    for contract in contracts:
        job_id = str(contract["id"])
        job = by_id.get(job_id)
        if job is None:
            findings.append(_finding("cron", job_id, "declared_job_missing", contract["name"]))
            continue
        if str(job.get("name") or "") != str(contract["name"]):
            findings.append(
                _finding(
                    "cron",
                    job_id,
                    "name_drift",
                    f"live={job.get('name')!r} contract={contract['name']!r}",
                )
            )

        expected_enabled = bool(contract["enabled"])
        live_enabled = bool(job.get("enabled"))
        if live_enabled != expected_enabled:
            findings.append(
                _finding(
                    "cron",
                    job_id,
                    "enabled_state_drift",
                    f"live enabled={live_enabled}, expected {expected_enabled}",
                )
            )
            continue
        if not expected_enabled:
            evidence.append({"surface": "cron", "id": job_id, "status": "disabled-as-declared"})
            continue

        status = str(job.get("last_status") or "")
        if status != "ok":
            findings.append(
                _finding("cron", job_id, "scheduler_not_ok", f"last_status={status or 'unset'}")
            )
        last_run = _parse_time(job.get("last_run_at"))
        max_age = int(contract["max_age_seconds"])
        if last_run is None:
            findings.append(_finding("cron", job_id, "last_run_missing", "last_run_at is invalid"))
        else:
            age = (now - last_run).total_seconds()
            if age < -60:
                findings.append(
                    _finding(
                        "cron",
                        job_id,
                        "last_run_future",
                        f"last run is {int(-age)}s in the future",
                    )
                )
            elif age > max_age:
                findings.append(
                    _finding(
                        "cron",
                        job_id,
                        "last_run_stale",
                        f"last run age {int(age)}s exceeds {max_age}s",
                    )
                )

        outcome = dict(contract["outcome"])
        kind = outcome.get("kind")
        if kind == "cron_output":
            latest = _latest_output(output_root, job_id)
            if latest is None:
                findings.append(
                    _finding("cron", job_id, "output_missing", f"no output under {output_root / job_id}")
                )
            else:
                findings.extend(
                    _check_text_evidence(
                        surface="cron",
                        identifier=job_id,
                        path=latest,
                        outcome=outcome,
                        now=now,
                    )
                )
                evidence.append(
                    {"surface": "cron", "id": job_id, "status": "checked", "evidence": str(latest)}
                )
        elif kind in {"text_artifact", "json_artifact"}:
            findings.extend(
                _check_artifact(
                    surface="cron",
                    identifier=job_id,
                    outcome=outcome,
                    home=home,
                    now=now,
                )
            )
            evidence.append(
                {
                    "surface": "cron",
                    "id": job_id,
                    "status": "checked",
                    "evidence": str(_expand_path(str(outcome["path"]), home=home)),
                }
            )
        else:
            raise ProbeError(f"cron {job_id} has unsupported outcome kind {kind!r}")
    return findings, evidence


def _launchd_domains() -> tuple[str, str]:
    uid = os.getuid()
    return f"gui/{uid}", f"user/{uid}"


def _launchctl_print_target(domain: str, label: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _default_launchctl(domain: str, label: str) -> subprocess.CompletedProcess[str]:
    return _launchctl_print_target(domain, label)


def _launch_agent_registration(
    label: str,
    *,
    launchctl: Callable[[str, str], subprocess.CompletedProcess[str]],
) -> tuple[str | None, subprocess.CompletedProcess[str] | None, subprocess.CompletedProcess[str] | None]:
    gui_domain, user_domain = _launchd_domains()
    gui_result = launchctl(gui_domain, label)
    user_result = launchctl(user_domain, label)
    gui_loaded = gui_result.returncode == 0
    user_loaded = user_result.returncode == 0
    if gui_loaded and user_loaded:
        return None, gui_result, user_result
    if gui_loaded:
        return gui_domain, gui_result, user_result
    if user_loaded:
        return user_domain, gui_result, user_result
    return None, gui_result, user_result


def _plist_labels(path: Path) -> tuple[set[str], list[dict[str, str]]]:
    """Return monitored labels plus non-fatal warnings for unreadable plists.

    A malformed or unreadable plist (e.g. a third-party LaunchAgent with
    invalid XML) must never abort the probe -- it is skipped and recorded
    as a distinguishable, non-fatal warning so it neither trips a false
    contract failure nor silently vanishes from the receipt.
    """
    labels: set[str] = set()
    warnings: list[dict[str, str]] = []
    if not path.is_dir():
        return labels, warnings
    for plist_path in path.glob("*.plist"):
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, ValueError, ExpatError) as exc:
            warnings.append(
                _finding(
                    "launchd",
                    plist_path.name,
                    "plist_unreadable",
                    f"skipped unreadable plist {plist_path}: {exc}",
                )
            )
            continue
        label = str(payload.get("Label") or "")
        if label and MONITORED_LABEL_RE.search(label):
            labels.add(label)
    return labels, warnings


def _labels_from_launchctl_domain(text: str) -> set[str]:
    marker = "\n\tservices = {\n"
    if marker not in text:
        raise ProbeError("launchctl domain output has no services inventory")
    services = text.split(marker, 1)[1].split("\n\t}\n", 1)[0]
    candidates = {
        match.group(1)
        for line in services.splitlines()
        if (
            match := re.search(
                r"\s((?:ai\.hermes|com\.hermes|com\.ignite|"
                r"com\.colingreig\.(?:hermes|ignite|pull_anthropic))"
                r"[A-Za-z0-9_.-]*)\s*$",
                line,
            )
        )
    }
    return {label for label in candidates if MONITORED_LABEL_RE.search(label)}


def _launch_domain_inventory(domain: str) -> set[str]:
    try:
        result = subprocess.run(
            ["launchctl", "print", domain],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"could not inventory loaded LaunchAgents in {domain}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "launchctl print failed").strip()
        raise ProbeError(
            f"could not inventory loaded LaunchAgents in {domain}: {detail[-500:]}"
        )
    return _labels_from_launchctl_domain(result.stdout)


def _launchctl_inventory() -> set[str]:
    labels: set[str] = set()
    errors: list[str] = []
    for domain in _launchd_domains():
        try:
            labels |= _launch_domain_inventory(domain)
        except ProbeError as exc:
            errors.append(str(exc))
    if not labels and errors:
        raise ProbeError(errors[-1])
    return labels


def _check_endpoint(
    *,
    surface: str,
    identifier: str,
    outcome: dict[str, Any],
) -> list[dict[str, str]]:
    kind = outcome["kind"]
    timeout = float(outcome.get("timeout_seconds", 5))
    if kind == "tcp":
        try:
            with socket.create_connection(
                (str(outcome["host"]), int(outcome["port"])),
                timeout=timeout,
            ):
                return []
        except OSError as exc:
            return [_finding(surface, identifier, "endpoint_failed", str(exc))]
    if kind == "http":
        try:
            with urllib.request.urlopen(str(outcome["url"]), timeout=timeout) as response:
                body = response.read(262144).decode("utf-8", errors="replace")
                if response.status not in set(outcome.get("statuses") or [200]):
                    return [
                        _finding(
                            surface,
                            identifier,
                            "http_status",
                            f"{outcome['url']} returned {response.status}",
                        )
                    ]
                required_values = dict(outcome.get("required_values") or {})
                if required_values:
                    try:
                        payload = json.loads(body)
                    except json.JSONDecodeError as exc:
                        return [
                            _finding(
                                surface,
                                identifier,
                                "http_invalid_json",
                                f"{outcome['url']} returned malformed JSON: {exc}",
                            )
                        ]
                    if not isinstance(payload, dict):
                        return [
                            _finding(
                                surface,
                                identifier,
                                "http_wrong_shape",
                                f"{outcome['url']} did not return a JSON object",
                            )
                        ]
                    for key, expected in required_values.items():
                        if payload.get(key) != expected:
                            return [
                                _finding(
                                    surface,
                                    identifier,
                                    "http_value_mismatch",
                                    f"{outcome['url']} key {key!r} is "
                                    f"{payload.get(key)!r}, expected {expected!r}",
                                )
                            ]
                patterns = list(outcome.get("success_patterns") or [])
                if patterns and not _patterns_match(body, patterns):
                    return [
                        _finding(
                            surface,
                            identifier,
                            "http_semantic_failure",
                            f"{outcome['url']} lacked its success marker",
                        )
                    ]
                for pattern in list(outcome.get("required_patterns") or []):
                    if not re.search(pattern, body, re.IGNORECASE | re.MULTILINE):
                        return [
                            _finding(
                                surface,
                                identifier,
                                "http_required_marker_missing",
                                f"{outcome['url']} lacked marker {pattern!r}",
                            )
                        ]
                return []
        except (OSError, urllib.error.URLError) as exc:
            return [_finding(surface, identifier, "endpoint_failed", str(exc))]
    raise ProbeError(f"unsupported endpoint kind {kind!r}")


def _check_launch_contracts(
    contracts: list[dict[str, Any]],
    *,
    launch_agents_dir: Path,
    home: Path,
    now: datetime,
    launchctl: Callable[[str, str], subprocess.CompletedProcess[str]],
    loaded_inventory: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    by_label = {str(contract["label"]): contract for contract in contracts}
    if len(by_label) != len(contracts):
        raise ProbeError("duplicate LaunchAgent contract label")

    findings: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    discovered_labels, plist_warnings = _plist_labels(launch_agents_dir)
    evidence.extend(plist_warnings)
    discovered = discovered_labels | loaded_inventory
    for label in sorted(discovered - set(by_label)):
        findings.append(_finding("launchd", label, "uncovered_plist", str(launch_agents_dir)))

    for contract in contracts:
        label = str(contract["label"])
        expected = str(contract["expected"])
        loaded_domain, gui_result, user_result = _launch_agent_registration(
            label,
            launchctl=launchctl,
        )
        loaded = loaded_domain is not None
        duplicate = (
            gui_result is not None
            and user_result is not None
            and gui_result.returncode == 0
            and user_result.returncode == 0
        )
        plist_path = launch_agents_dir / f"{label}.plist"

        if duplicate:
            findings.append(
                _finding(
                    "launchd",
                    label,
                    "duplicate_domain_registration",
                    f"{label} is loaded in both {_launchd_domains()[0]} and {_launchd_domains()[1]}",
                )
            )
            continue

        if expected == "retired":
            if loaded:
                findings.append(
                    _finding("launchd", label, "retired_agent_loaded", "launchctl print succeeded")
                )
            if plist_path.exists():
                findings.append(
                    _finding("launchd", label, "retired_plist_present", str(plist_path))
                )
            if not loaded and not plist_path.exists():
                evidence.append({"surface": "launchd", "id": label, "status": "retired-as-declared"})
            continue
        if expected != "loaded":
            raise ProbeError(f"LaunchAgent {label} has unsupported expected state {expected!r}")
        if not loaded:
            detail_parts = []
            for domain, result in zip(_launchd_domains(), (gui_result, user_result)):
                if result is None:
                    continue
                detail = (result.stderr or result.stdout or "launchctl print failed").strip()
                if detail:
                    detail_parts.append(f"{domain}: {detail[-150:]}")
            findings.append(
                _finding(
                    "launchd",
                    label,
                    "agent_not_loaded",
                    " | ".join(detail_parts)[-300:] or "launchctl print failed in both domains",
                )
            )
            continue
        if not plist_path.is_file():
            findings.append(
                _finding("launchd", label, "active_plist_missing", str(plist_path))
            )

        outcome = dict(contract["outcome"])
        kind = outcome.get("kind")
        if kind == "self":
            pass
        elif kind in {"tcp", "http"}:
            findings.extend(
                _check_endpoint(surface="launchd", identifier=label, outcome=outcome)
            )
        elif kind in {"text_artifact", "json_artifact"}:
            findings.extend(
                _check_artifact(
                    surface="launchd",
                    identifier=label,
                    outcome=outcome,
                    home=home,
                    now=now,
                )
            )
        else:
            raise ProbeError(f"LaunchAgent {label} has unsupported outcome kind {kind!r}")
        evidence.append(
            {
                "surface": "launchd",
                "id": label,
                "status": "checked",
                "outcome_kind": kind,
                "domain": loaded_domain,
            }
        )
    return findings, evidence


def _check_operational_contracts(
    contracts: list[dict[str, Any]], *, home: Path, now: datetime
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Check contention/recovery/storage/drift signals without taking locks."""
    findings: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    for contract in contracts:
        identifier = str(contract.get("id") or "unknown")
        kind = contract.get("kind")
        path = _expand_path(str(contract.get("path") or ""), home=home)
        if kind in {"sqlite_health", "admission_health"}:
            if not path.is_file():
                findings.append(_finding("operational", identifier, "database_missing", f"missing {path}"))
                continue
            try:
                size = path.stat().st_size
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05)
                conn.row_factory = sqlite3.Row
                try:
                    check = conn.execute("PRAGMA quick_check").fetchone()
                    if check is None or check[0] != "ok":
                        findings.append(_finding("operational", identifier, "database_quick_check_failed", str(check[0] if check else "no result")))
                    if kind == "admission_health":
                        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        active_lease_queries = []
                        if "admission_leases" in tables:
                            active_lease_queries.append(
                                "SELECT ledger_execution_id,heartbeat_at,expires_at FROM admission_leases WHERE state='active'"
                            )
                        if "executor_lease" in tables:
                            active_lease_queries.append(
                                "SELECT ledger_execution_id,heartbeat_at,expires_at FROM executor_lease WHERE state='active'"
                            )
                        for query in active_lease_queries:
                            for row in conn.execute(query):
                                expires = _parse_time(row["expires_at"])
                                if expires is None or expires < now:
                                    findings.append(_finding("operational", identifier, "stale_heartbeat", f"expired admission {row['ledger_execution_id']}"))
                        recovery_times = []
                        if "admission_recovery_receipts" in tables:
                            recent = conn.execute("SELECT recovered_at FROM admission_recovery_receipts ORDER BY recovered_at DESC LIMIT 1").fetchone()
                            if recent:
                                recovery_times.append(recent[0])
                        if "recovery_receipts" in tables:
                            recent = conn.execute("SELECT recovered_at FROM recovery_receipts ORDER BY recovered_at DESC LIMIT 1").fetchone()
                            if recent:
                                recovery_times.append(recent[0])
                        recent_recoveries = [
                            (parsed, value)
                            for value in recovery_times
                            if (parsed := _parse_time(value)) is not None
                        ]
                        if recent_recoveries:
                            recovered, value = max(recent_recoveries, key=lambda item: item[0])
                            if (now - recovered).total_seconds() < int(contract.get("recovery_alarm_seconds", 3600)):
                                findings.append(_finding("operational", identifier, "owner_dead_recovery", f"owner recovery at {value}"))
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                message = str(exc).lower()
                code = "database_busy" if "locked" in message or "busy" in message else "database_unreadable"
                findings.append(_finding("operational", identifier, code, f"{path}: {exc}"))
                continue
            max_size = int(contract.get("max_size_bytes", 0))
            if max_size and size > max_size:
                findings.append(_finding("operational", identifier, "database_size", f"{path} is {size} bytes (limit {max_size})"))
            evidence.append({"surface": "operational", "id": identifier, "path": str(path), "size_bytes": size})
        elif kind == "repeated_preflight_failures":
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            pattern = str(contract.get("pattern") or "preflight.*(?:fail|error)")
            count = len(re.findall(pattern, text[-int(contract.get("tail_bytes", 262144)):], re.IGNORECASE))
            if count >= int(contract.get("threshold", 3)):
                findings.append(_finding("operational", identifier, "repeated_preflight_failures", f"{count} recent matching records in {path}"))
            evidence.append({"surface": "operational", "id": identifier, "matches": count})
        elif kind == "governed_manifest":
            manifest = _load_optional_json(path)
            for item in manifest.get("files", []):
                source = path.parent / str(item.get("source") or "")
                try:
                    actual = hashlib.sha256(source.read_bytes()).hexdigest()
                except OSError as exc:
                    findings.append(_finding("operational", identifier, "governed_path_unreadable", f"{source}: {exc}"))
                    continue
                if actual != item.get("sha256"):
                    findings.append(_finding("operational", identifier, "governed_path_drift", f"hash drift: {source}"))
            evidence.append({"surface": "operational", "id": identifier, "path": str(path)})
        else:
            raise ProbeError(f"operational check {identifier} has unsupported kind {kind!r}")
    return findings, evidence


def evaluate(
    contracts: dict[str, Any],
    *,
    jobs_path: Path,
    output_root: Path,
    launch_agents_dir: Path,
    home: Path,
    now: datetime,
    launchctl: Callable[[str, str], subprocess.CompletedProcess[str]] = _default_launchctl,
    launch_inventory: Callable[[], set[str]] = _launchctl_inventory,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if contracts.get("schema_version") != 1:
        raise ProbeError("unsupported or missing contract schema_version")
    cron_contracts = contracts.get("cron_jobs")
    launch_contracts = contracts.get("launch_agents")
    operational_contracts = contracts.get("operational_checks", [])
    if not isinstance(cron_contracts, list) or not isinstance(launch_contracts, list):
        raise ProbeError("contracts require cron_jobs and launch_agents arrays")
    if not isinstance(operational_contracts, list):
        raise ProbeError("operational_checks must be an array")

    cron_findings, cron_evidence = _check_cron_contracts(
        cron_contracts,
        jobs_path=jobs_path,
        output_root=output_root,
        home=home,
        now=now,
    )
    launch_findings, launch_evidence = _check_launch_contracts(
        launch_contracts,
        launch_agents_dir=launch_agents_dir,
        home=home,
        now=now,
        launchctl=launchctl,
        loaded_inventory=launch_inventory(),
    )
    operational_findings, operational_evidence = _check_operational_contracts(
        operational_contracts, home=home, now=now
    )
    return (
        cron_findings + launch_findings + operational_findings,
        cron_evidence + launch_evidence + operational_evidence,
    )


def _signature(findings: list[dict[str, str]]) -> str:
    keys = sorted(f"{item['surface']}:{item['id']}:{item['code']}" for item in findings)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _finding_identities(findings: list[dict[str, str]]) -> list[str]:
    """Return stable contract identities, deliberately excluding finding codes.

    A contract can move through stale, missing-marker, and failure-marker states
    during one outage.  Those are detail changes inside the same incident, not
    three separate reasons to page Slack.
    """

    return sorted({f"{item['surface']}:{item['id']}" for item in findings})


def _new_incident_id(now_ts: float, identities: list[str]) -> str:
    seed = f"{now_ts:.6f}\n" + "\n".join(identities)
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _release_cut_observed_at(path: Path = DEFAULT_RELEASE_RECEIPT) -> datetime | None:
    """Return the boundary only for a receipt that changed the live runtime."""

    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("schema_version") != 2:
            return None
        if receipt.get("event") not in {"advanced", "cut", "rollback"}:
            return None
        target = receipt.get("to_commit")
        runtime_target = receipt.get("runtime_target")
        if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{40,64}", target):
            return None
        if not isinstance(runtime_target, str) or not runtime_target.startswith("/"):
            return None
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except (OSError, json.JSONDecodeError):
        return None


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


def _triage_hint() -> str:
    return (
        f"Triage: {DEFAULT_RECEIPT} (latest run) · "
        f"{DEFAULT_HISTORY_LEDGER} (run-by-run timeline) · "
        f"{DEFAULT_HISTORY_DIR}/ (full receipts per transition) · "
        "`hermes fleet incident-report --json`."
    )


def _alert_message(
    findings: list[dict[str, str]],
    *,
    drill: bool,
    context: dict[str, Any] | None = None,
) -> str:
    context = dict(context or {})
    prefix = "SYNTHETIC DRILL — " if drill else ""
    lines = [
        f"🚨 {prefix}Hermes fleet outcome coverage alarm",
        f"{len(findings)} contract failure(s); every listed job lacks current outcome proof.",
    ]
    incident_id = str(context.get("incident_id") or "")
    if incident_id:
        opened_at = context.get("incident_opened_at")
        alert_count = int(context.get("alert_count") or 0)
        parts = [f"Incident {incident_id[:12]}"]
        if isinstance(opened_at, (int, float)) and opened_at:
            opened = datetime.fromtimestamp(float(opened_at), tz=timezone.utc)
            age = float(context.get("now_ts") or opened_at) - float(opened_at)
            parts.append(f"opened {opened.isoformat()} ({_format_duration(age)} ago)")
        if alert_count:
            parts.append(f"alert #{alert_count}")
        lines.append(" · ".join(parts))
    reason = str(context.get("reason") or "")
    if reason:
        lines.append(f"Why this alert fired now: {reason}")
    for item in findings[:40]:
        lines.append(f"• {item['surface']} {item['id']} [{item['code']}]: {item['detail']}")
    if len(findings) > 40:
        lines.append(f"• … {len(findings) - 40} additional finding(s) in the probe receipt")
    if context.get("stale_incident"):
        lines.append(
            "⚠️ This incident has stayed open with an unchanged finding set for "
            f"{_format_duration(float(context.get('incident_age') or 0))}. "
            "Confirm the contract still matches reality (contract drift) before "
            "treating it as a live outage."
        )
    lines.append(_triage_hint())
    return "\n".join(lines)


def _recovery_message(
    incident_id: str,
    *,
    context: dict[str, Any] | None = None,
) -> str:
    context = dict(context or {})
    lines = [
        "✅ Hermes fleet outcome coverage recovered",
        "All declared cron and LaunchAgent outcome contracts are currently satisfied.",
        f"Incident: {incident_id[:12]}",
    ]
    detail: list[str] = []
    duration = context.get("incident_age")
    if isinstance(duration, (int, float)) and duration:
        detail.append(f"open {_format_duration(float(duration))}")
    alert_count = int(context.get("alert_count") or 0)
    if alert_count:
        detail.append(f"{alert_count} alert(s) delivered")
    if detail:
        lines.append(" · ".join(detail))
    lines.append(f"Timeline: {DEFAULT_HISTORY_LEDGER}")
    return "\n".join(lines)


def _send_slack(message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(HERMES_BIN), "send", "--to", SLACK_TARGET, message],
        capture_output=True,
        text=True,
        timeout=30,
    )


def route_alarm(
    findings: list[dict[str, str]],
    *,
    state_path: Path,
    now: datetime,
    drill: bool,
    real_alert: bool,
    sender: Callable[[str], subprocess.CompletedProcess[str]] = _send_slack,
    emit_dry_run: bool = True,
    cutover_at: datetime | None = None,
) -> dict[str, Any]:
    state = _load_optional_json(state_path)
    now_ts = now.timestamp()
    cutover_ts = cutover_at.timestamp() if cutover_at is not None else 0.0
    in_cutover_grace = bool(
        cutover_at is not None
        and -60 <= now_ts - cutover_ts < CUTOVER_GRACE_SECONDS
    )
    if findings:
        signature = _signature(findings)
        alert_findings = findings
        identities = _finding_identities(findings)
        identity_set = set(identities)
        last_signature = str(state.get("delivered_signature") or "")
        last_alert_at = float(state.get("last_alert_at") or 0)
        if state.get("active"):
            # Schema migration: an incident opened by the old exact-signature
            # router owns the identities visible on the first upgraded probe.
            delivered = set(state.get("delivered_finding_identities") or identities)
            incident_id = str(
                state.get("incident_id")
                or last_signature
                or _new_incident_id(last_alert_at or now_ts, sorted(delivered))
            )
            incident_opened_at = float(
                state.get("incident_opened_at") or last_alert_at or now_ts
            )
            incident_age = now_ts - incident_opened_at
            alert_count = int(state.get("alert_count") or 1)
            pending = dict(state.get("pending_new_findings") or {})
            new_identities = identity_set - delivered
            pending = {
                key: float(pending.get(key) or now_ts)
                for key in sorted(new_identities)
            }
            state.update(
                {
                    "incident_id": incident_id,
                    "incident_opened_at": incident_opened_at,
                    "delivered_finding_identities": sorted(delivered),
                    "pending_new_findings": pending,
                }
            )
            state.pop("clean_since", None)
            persistent_new = {
                key for key, first_seen in pending.items()
                if now_ts - first_seen >= NEW_FINDING_CONFIRM_SECONDS
            }
            if new_identities and (in_cutover_grace or not persistent_new):
                if real_alert:
                    _atomic_json(state_path, state)
                confirm_at = min(
                    first_seen + NEW_FINDING_CONFIRM_SECONDS
                    for first_seen in pending.values()
                )
                if in_cutover_grace:
                    return {
                        "action": "cutover-suppressed",
                        "reason": (
                            f"{len(new_identities)} new contract(s) appeared inside the "
                            f"{CUTOVER_GRACE_SECONDS // 60}m release-cut grace window; "
                            "held until the window expires."
                        ),
                        "signature": signature,
                        "incident_id": incident_id,
                        "incident_opened_at": incident_opened_at,
                        "suppressed_until": cutover_ts + CUTOVER_GRACE_SECONDS,
                        "pending_identities": sorted(new_identities),
                    }
                return {
                    "action": "new-finding-pending",
                    "reason": (
                        f"{len(new_identities)} new contract(s) joined incident "
                        f"{incident_id[:12]} but have not yet persisted for "
                        f"{NEW_FINDING_CONFIRM_SECONDS // 60}m."
                    ),
                    "signature": signature,
                    "incident_id": incident_id,
                    "incident_opened_at": incident_opened_at,
                    "pending_until": confirm_at,
                    "pending_identities": sorted(new_identities),
                }
            if not persistent_new and now_ts - last_alert_at < REALERT_SECONDS:
                if real_alert and state != _load_optional_json(state_path):
                    _atomic_json(state_path, state)
                return {
                    "action": "deduped",
                    "reason": (
                        f"Same incident {incident_id[:12]} (open "
                        f"{_format_duration(incident_age)}); no new contract and the "
                        f"{REALERT_SECONDS // 3600}h re-alert interval has not elapsed."
                    ),
                    "signature": signature,
                    "incident_id": incident_id,
                    "incident_opened_at": incident_opened_at,
                    "next_alert_at": last_alert_at + REALERT_SECONDS,
                }
            if persistent_new:
                alert_identities = delivered | persistent_new
                alert_findings = [
                    item for item in findings
                    if f"{item['surface']}:{item['id']}" in alert_identities
                ]
                alert_reason = (
                    f"{len(persistent_new)} newly persistent contract(s) joined open "
                    f"incident {incident_id[:12]}: "
                    + ", ".join(sorted(persistent_new)[:5])
                )
            else:
                alert_reason = (
                    f"{REALERT_SECONDS // 3600}h re-alert: incident "
                    f"{incident_id[:12]} has been unresolved for "
                    f"{_format_duration(incident_age)}."
                )
            alert_count += 1
        else:
            incident_id = str(
                state.get("pending_incident_id")
                or _new_incident_id(now_ts, identities)
            )
            incident_opened_at = float(
                state.get("pending_incident_since") or now_ts
            )
            incident_age = now_ts - incident_opened_at
            alert_count = 1
            pending = dict(state.get("pending_new_findings") or {})
            pending = {
                key: float(pending.get(key) or now_ts)
                for key in identities
            }
            alert_reason = (
                f"New incident {incident_id[:12]} opened on "
                f"{len(identities)} contract(s): " + ", ".join(identities[:5])
            )
            if in_cutover_grace:
                state.update(
                    {
                        "pending_incident_id": incident_id,
                        "pending_incident_since": incident_opened_at,
                        "pending_finding_identities": identities,
                        "pending_new_findings": pending,
                        "pending_from_cutover": True,
                    }
                )
                if real_alert:
                    _atomic_json(state_path, state)
                return {
                    "action": "cutover-suppressed",
                    "reason": (
                        f"{len(identities)} contract(s) failed inside the "
                        f"{CUTOVER_GRACE_SECONDS // 60}m release-cut grace window; "
                        "held until the window expires."
                    ),
                    "signature": signature,
                    "incident_id": incident_id,
                    "incident_opened_at": incident_opened_at,
                    "suppressed_until": cutover_ts + CUTOVER_GRACE_SECONDS,
                    "pending_identities": identities,
                }
            if state.get("pending_from_cutover"):
                persistent_new = {
                    key for key, first_seen in pending.items()
                    if now_ts - first_seen >= NEW_FINDING_CONFIRM_SECONDS
                }
                state["pending_new_findings"] = pending
                if not persistent_new:
                    if real_alert:
                        _atomic_json(state_path, state)
                    return {
                        "action": "new-finding-pending",
                        "reason": (
                            "Release-cut grace window expired; the surviving "
                            f"{len(identities)} contract(s) still need "
                            f"{NEW_FINDING_CONFIRM_SECONDS // 60}m of persistence "
                            "before paging."
                        ),
                        "signature": signature,
                        "incident_id": incident_id,
                        "incident_opened_at": incident_opened_at,
                        "pending_until": min(
                            first_seen + NEW_FINDING_CONFIRM_SECONDS
                            for first_seen in pending.values()
                        ),
                        "pending_identities": identities,
                    }
                alert_findings = [
                    item for item in findings
                    if f"{item['surface']}:{item['id']}" in persistent_new
                ]
                alert_reason = (
                    f"{len(persistent_new)} contract(s) outlasted the release-cut "
                    f"grace window and opened incident {incident_id[:12]}: "
                    + ", ".join(sorted(persistent_new)[:5])
                )
            state.update(
                {
                    "pending_incident_id": incident_id,
                    "pending_incident_since": incident_opened_at,
                    "pending_finding_identities": identities,
                }
            )
            if real_alert:
                _atomic_json(state_path, state)
        delivery_signature = _signature(alert_findings)
        stale_incident = incident_age >= STALE_INCIDENT_SECONDS
        context = {
            "incident_id": incident_id,
            "incident_opened_at": incident_opened_at,
            "incident_age": incident_age,
            "alert_count": alert_count,
            "reason": alert_reason,
            "stale_incident": stale_incident,
            "now_ts": now_ts,
        }
        message = _alert_message(alert_findings, drill=drill, context=context)
        if not real_alert:
            if emit_dry_run:
                print(message)
            return {
                "action": "dry-run",
                "reason": alert_reason,
                "signature": delivery_signature,
                "incident_id": incident_id,
                "incident_opened_at": incident_opened_at,
            }
        try:
            result = sender(message)
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "action": "delivery-failed",
                "reason": f"Slack delivery raised while sending: {alert_reason}",
                "signature": delivery_signature,
                "incident_id": incident_id,
                "incident_opened_at": incident_opened_at,
                "error": str(exc)[-500:],
            }
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown send failure").strip()
            return {
                "action": "delivery-failed",
                "reason": f"Slack rejected the send: {alert_reason}",
                "signature": delivery_signature,
                "incident_id": incident_id,
                "incident_opened_at": incident_opened_at,
                "error": detail[-500:],
            }
        state.update(
            {
                "delivered_signature": delivery_signature,
                "last_alert_at": now_ts,
                "active": True,
                "incident_id": incident_id,
                "incident_opened_at": incident_opened_at,
                "alert_count": alert_count,
                "delivered_finding_identities": sorted(
                    set(state.get("delivered_finding_identities") or [])
                    | set(_finding_identities(alert_findings))
                ),
            }
        )
        state.pop("pending_incident_id", None)
        state.pop("pending_incident_since", None)
        state.pop("pending_finding_identities", None)
        state.pop("pending_from_cutover", None)
        remaining_pending = {
            key: value
            for key, value in dict(state.get("pending_new_findings") or {}).items()
            if key not in set(_finding_identities(alert_findings))
        }
        if remaining_pending:
            state["pending_new_findings"] = remaining_pending
        else:
            state.pop("pending_new_findings", None)
        state.pop("clean_since", None)
        _atomic_json(state_path, state)
        return {
            "action": "sent",
            "reason": alert_reason,
            "signature": delivery_signature,
            "incident_id": incident_id,
            "incident_opened_at": incident_opened_at,
            "alert_count": alert_count,
            "stale_incident": stale_incident,
        }

    previous_signature = str(state.get("delivered_signature") or "")
    if not state.get("active") or not previous_signature:
        if state.get("pending_incident_id"):
            state.pop("pending_incident_id", None)
            state.pop("pending_incident_since", None)
            state.pop("pending_finding_identities", None)
            state.pop("pending_new_findings", None)
            state.pop("pending_from_cutover", None)
            if real_alert:
                _atomic_json(state_path, state)
        return {
            "action": "clean",
            "reason": "Every declared contract satisfied; no incident is open.",
        }
    incident_id = str(state.get("incident_id") or previous_signature)
    incident_opened_at = float(
        state.get("incident_opened_at") or state.get("last_alert_at") or now_ts
    )
    incident_age = now_ts - incident_opened_at
    alert_count = int(state.get("alert_count") or 1)
    recovery_context = {
        "incident_id": incident_id,
        "incident_opened_at": incident_opened_at,
        "incident_age": incident_age,
        "alert_count": alert_count,
    }
    clean_since = float(state.get("clean_since") or now_ts)
    state["clean_since"] = clean_since
    if in_cutover_grace or now_ts - clean_since < RECOVERY_CONFIRM_SECONDS:
        if real_alert:
            _atomic_json(state_path, state)
        if in_cutover_grace:
            return {
                "action": "recovery-suppressed",
                "reason": (
                    f"Incident {incident_id[:12]} looks clean inside the "
                    f"{CUTOVER_GRACE_SECONDS // 60}m release-cut grace window; the "
                    "recovery is held until the window expires."
                ),
                "signature": previous_signature,
                "incident_id": incident_id,
                "incident_opened_at": incident_opened_at,
                "suppressed_until": cutover_ts + CUTOVER_GRACE_SECONDS,
            }
        return {
            "action": "recovery-pending",
            "reason": (
                f"Incident {incident_id[:12]} has been clean for "
                f"{_format_duration(now_ts - clean_since)}; recovery is sent after "
                f"{RECOVERY_CONFIRM_SECONDS // 60}m of confirmed clean runs."
            ),
            "signature": previous_signature,
            "incident_id": incident_id,
            "incident_opened_at": incident_opened_at,
            "pending_until": clean_since + RECOVERY_CONFIRM_SECONDS,
        }
    recovery_reason = (
        f"Incident {incident_id[:12]} stayed clean for "
        f"{RECOVERY_CONFIRM_SECONDS // 60}m after {_format_duration(incident_age)} open."
    )
    message = _recovery_message(incident_id, context=recovery_context)
    if not real_alert:
        if emit_dry_run:
            print(message)
        return {
            "action": "recovery-dry-run",
            "reason": recovery_reason,
            "signature": previous_signature,
            "incident_id": incident_id,
        }
    try:
        result = sender(message)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "action": "recovery-delivery-failed",
            "reason": f"Slack delivery raised while sending recovery: {recovery_reason}",
            "signature": previous_signature,
            "incident_id": incident_id,
            "error": str(exc)[-500:],
        }
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown send failure").strip()
        return {
            "action": "recovery-delivery-failed",
            "reason": f"Slack rejected the recovery send: {recovery_reason}",
            "signature": previous_signature,
            "incident_id": incident_id,
            "error": detail[-500:],
        }
    state.update(
        {
            "active": False,
            "last_recovery_at": now_ts,
            "recovered_signature": previous_signature,
            "recovered_incident_id": incident_id,
            "recovered_incident_opened_at": incident_opened_at,
            "recovered_incident_alert_count": alert_count,
        }
    )
    state.pop("clean_since", None)
    state.pop("pending_new_findings", None)
    state.pop("incident_opened_at", None)
    state.pop("alert_count", None)
    _atomic_json(state_path, state)
    return {
        "action": "recovery-sent",
        "reason": recovery_reason,
        "signature": previous_signature,
        "incident_id": incident_id,
        "incident_opened_at": incident_opened_at,
        "alert_count": alert_count,
    }


def _inject_contract_failures(
    contracts: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Exercise one real failure predicate for every declared contract."""
    observed_at = now or _now()
    injected: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="fleet-outcome-drill-") as temporary:
        root = Path(temporary)
        output_root = root / "output"
        launch_agents_dir = root / "LaunchAgents"
        launch_agents_dir.mkdir()

        for contract in contracts["cron_jobs"]:
            job_id = str(contract["id"])
            declared_enabled = bool(contract["enabled"])
            # A disabled/retired cron contract has no artifact or staleness
            # predicate to trip -- the checker records "disabled-as-declared"
            # and returns immediately.  Simulate the job coming back live
            # instead, mirroring how retired LaunchAgents are drilled by
            # faking them loaded, so the real enabled_state_drift predicate
            # is exercised rather than the contract being skipped.
            live_enabled = True if not declared_enabled else declared_enabled
            jobs_path = root / f"jobs-{job_id}.json"
            _atomic_json(
                jobs_path,
                {
                    "jobs": [
                        {
                            "id": job_id,
                            "name": contract["name"],
                            "enabled": live_enabled,
                            "last_status": "ok",
                            "last_run_at": observed_at.isoformat(),
                        }
                    ]
                },
            )
            findings, _ = _check_cron_contracts(
                [contract],
                jobs_path=jobs_path,
                output_root=output_root,
                home=root,
                now=observed_at,
            )
            matching = [item for item in findings if item["id"] == job_id]
            if not matching:
                raise ProbeError(f"drill could not trip cron contract {job_id}")
            selected = dict(matching[0])
            selected["detail"] = f"SYNTHETIC DRILL: {selected['detail']}"
            injected.append(selected)

        for contract in contracts["launch_agents"]:
            label = str(contract["label"])
            should_look_loaded = contract["expected"] == "retired"

            def fake_launchctl(_domain: str, _label: str, loaded: bool = should_look_loaded):
                return subprocess.CompletedProcess(
                    ["launchctl"],
                    0 if loaded else 113,
                    "synthetic loaded service" if loaded else "",
                    "" if loaded else "synthetic missing service",
                )

            findings, _ = _check_launch_contracts(
                [contract],
                launch_agents_dir=launch_agents_dir,
                home=root,
                now=observed_at,
                launchctl=fake_launchctl,
                loaded_inventory={label} if should_look_loaded else set(),
            )
            matching = [item for item in findings if item["id"] == label]
            if not matching:
                raise ProbeError(f"drill could not trip LaunchAgent contract {label}")
            selected = dict(matching[0])
            selected["detail"] = f"SYNTHETIC DRILL: {selected['detail']}"
            injected.append(selected)
    return injected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contracts", type=Path, default=DEFAULT_CONTRACTS)
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--launch-agents-dir", type=Path, default=DEFAULT_LAUNCH_AGENTS)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument(
        "--history-dir",
        type=Path,
        help="archive root; defaults to '<receipt stem>-history' beside the receipt",
    )
    parser.add_argument("--drill-all", action="store_true")
    parser.add_argument(
        "--real-alert",
        action="store_true",
        help="send Slack; production runs imply this, drills require it explicitly",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        help="diagnostic production evaluation only; print instead of sending Slack",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = _now()
    contracts = _load_json(args.contracts)
    if not isinstance(contracts, dict):
        raise ProbeError(f"{args.contracts} is not an object")

    if args.drill_all:
        findings = _inject_contract_failures(contracts, now=now)
        evidence: list[dict[str, Any]] = []
        real_alert = args.real_alert and not args.no_alert
        state_path = DEFAULT_DRILL_STATE if args.state == DEFAULT_STATE else args.state
        receipt_path = DEFAULT_DRILL_RECEIPT if args.receipt == DEFAULT_RECEIPT else args.receipt
    else:
        findings, evidence = evaluate(
            contracts,
            jobs_path=args.jobs,
            output_root=args.output_root,
            launch_agents_dir=args.launch_agents_dir,
            home=HOME,
            now=now,
        )
        real_alert = not args.no_alert
        state_path = args.state
        receipt_path = args.receipt

    alarm: dict[str, Any] = {"action": "unreported"}
    try:
        alarm = route_alarm(
            findings,
            state_path=state_path,
            now=now,
            drill=args.drill_all,
            real_alert=real_alert,
            emit_dry_run=not args.json,
            cutover_at=None if args.drill_all else _release_cut_observed_at(),
        )
    finally:
        # The receipt must land even if alarm delivery raised unexpectedly
        # (e.g. a hung `hermes send` subprocess) — a stale receipt with no
        # alert is worse than an alert with a partial/failed-delivery receipt.
        receipt = {
            "schema_version": 1,
            "checked_at": now.isoformat(),
            "mode": "drill" if args.drill_all else "production",
            "status": "alert" if findings else "clean",
            "state_path": str(state_path),
            "receipt_path": str(receipt_path),
            "contract_count": len(contracts["cron_jobs"]) + len(contracts["launch_agents"]),
            "finding_count": len(findings),
            "findings": findings,
            "evidence": evidence,
            "alarm": alarm,
        }
        # The receipt above is overwritten every run, so archive this run before
        # publishing it.  Archiving is best-effort: a failed write records its
        # reason in the receipt rather than losing the probe result entirely.
        try:
            receipt["history"] = archive_probe_run(
                receipt,
                receipt_path=receipt_path,
                history_dir=args.history_dir,
                now=now,
            )
        except OSError as exc:
            receipt["history"] = {
                "error": f"{type(exc).__name__}: {exc}"[-300:],
                "transition": None,
                "snapshot": None,
            }
        _atomic_json(receipt_path, receipt)
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    elif alarm.get("action") in {"delivery-failed", "recovery-delivery-failed"}:
        print(f"[fleet-outcome-probe] Slack {alarm['action']}: {alarm.get('error')}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProbeError as exc:
        print(f"[fleet-outcome-probe] monitor error: {exc}", file=sys.stderr)
        raise SystemExit(2)
