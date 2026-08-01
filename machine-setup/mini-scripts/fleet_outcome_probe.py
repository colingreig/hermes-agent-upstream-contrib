#!/usr/bin/env python3
"""Verify real outcomes for the rebuilt Mini cron and LaunchAgent fleet.

This monitor intentionally runs outside Hermes cron.  It validates the
source-controlled contract inventory against the live 16-job jobs.json, then
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
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


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
HERMES_BIN = HOME / ".local/bin/hermes"
SLACK_TARGET = "slack:hermes"
REALERT_SECONDS = 6 * 60 * 60
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


def _expand_path(value: str, *, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _age_seconds(path: Path, now: datetime) -> float:
    return max(0.0, now.timestamp() - path.stat().st_mtime)


def _finding(surface: str, identifier: str, code: str, detail: str, *, name: str | None = None) -> dict[str, str]:
    out = {
        "surface": surface,
        "id": identifier,
        "code": code,
        "detail": detail,
    }
    if name:
        out["name"] = name
    return out


_CODE_PRIORITY = {
    "failure_marker": 10,
    "run_incomplete": 20,
    "success_marker_missing": 30,
    "required_marker_missing": 35,
    "scheduler_not_ok": 40,
    "last_run_stale": 45,
    "evidence_stale": 45,
    "output_missing": 50,
    "evidence_missing": 50,
    "uncovered_enabled_job": 60,
    "declared_job_missing": 65,
    "uncovered_plist": 70,
    "agent_not_loaded": 75,
}


_CODE_HUMAN = {
    "failure_marker": "is failing",
    "success_marker_missing": "didn't record a clean success",
    "required_marker_missing": "is missing a required success marker",
    "run_incomplete": "didn't finish cleanly",
    "run_boundary_missing": "has no completed-run marker",
    "uncovered_enabled_job": "is enabled with no outcome contract",
    "declared_job_missing": "is in contracts but missing from the live scheduler",
    "uncovered_plist": "LaunchAgent is present but not contracted",
    "agent_not_loaded": "LaunchAgent isn't loaded",
    "active_plist_missing": "LaunchAgent plist is missing",
    "retired_agent_loaded": "retired LaunchAgent is still loaded",
    "retired_plist_present": "retired LaunchAgent plist is still installed",
    "scheduler_not_ok": "last scheduler status wasn't ok",
    "last_run_missing": "has no valid last-run timestamp",
    "last_run_stale": "hasn't run within its allowed age",
    "last_run_future": "has a last-run timestamp in the future",
    "output_missing": "has no cron output to prove the run",
    "evidence_missing": "evidence file is missing",
    "evidence_stale": "evidence is stale",
    "evidence_unreadable": "evidence couldn't be read",
    "name_drift": "live name doesn't match the contract",
    "enabled_state_drift": "enabled state doesn't match the contract",
    "endpoint_failed": "health endpoint failed",
    "http_status": "health endpoint returned a bad status",
    "http_semantic_failure": "health endpoint reported failure",
}


def _short_launchd_name(label: str) -> str:
    prefix = "com.colingreig.hermes."
    if label.startswith(prefix):
        return label[len(prefix) :]
    return label


def _display_name(item: dict[str, str]) -> str:
    if item.get("name"):
        return item["name"]
    if item["code"] in {"uncovered_enabled_job", "declared_job_missing"}:
        return item.get("detail") or item["id"]
    if item["surface"] == "launchd":
        return _short_launchd_name(item["id"])
    return item["id"]


def _human_issue(item: dict[str, str]) -> str:
    return _CODE_HUMAN.get(item["code"], item["code"].replace("_", " "))


def _attach_display_names(
    findings: list[dict[str, str]],
    contracts: dict[str, Any],
) -> list[dict[str, str]]:
    cron_names = {
        str(contract["id"]): str(contract.get("name") or contract["id"])
        for contract in (contracts.get("cron_jobs") or [])
        if isinstance(contract, dict) and contract.get("id")
    }
    for item in findings:
        if item.get("name"):
            continue
        if item["surface"] == "cron":
            if item["code"] in {"uncovered_enabled_job", "declared_job_missing"}:
                item["name"] = item.get("detail") or item["id"]
            else:
                item["name"] = cron_names.get(item["id"], item["id"])
        else:
            item["name"] = _short_launchd_name(item["id"])
    return findings


def _group_findings(findings: list[dict[str, str]]) -> list[tuple[str, list[dict[str, str]]]]:
    """Group findings by job, ordered by worst issue then name."""
    buckets: dict[tuple[str, str], list[dict[str, str]]] = {}
    for item in findings:
        key = (item["surface"], item["id"])
        buckets.setdefault(key, []).append(item)

    def sort_key(pair: tuple[tuple[str, str], list[dict[str, str]]]):
        items = pair[1]
        worst = min(_CODE_PRIORITY.get(i["code"], 200) for i in items)
        return (worst, _display_name(items[0]).lower())

    grouped: list[tuple[str, list[dict[str, str]]]] = []
    for _, items in sorted(buckets.items(), key=sort_key):
        items_sorted = sorted(items, key=lambda i: _CODE_PRIORITY.get(i["code"], 200))
        grouped.append((_display_name(items_sorted[0]), items_sorted))
    return grouped


def _primary_issue(items: list[dict[str, str]]) -> str:
    return _human_issue(items[0])


def _summary_sentence(grouped: list[tuple[str, list[dict[str, str]]]]) -> str:
    n = len(grouped)
    if n == 0:
        return "No action needed — all declared fleet outcomes look healthy."
    if n == 1:
        name, items = grouped[0]
        return f"Action needed: {name} {_primary_issue(items)}."
    names = [name for name, _ in grouped[:3]]
    if n > 3:
        names.append(f"+{n - 3} more")
    return f"Action needed: {n} jobs aren't proving healthy — {', '.join(names)}."


def _next_step_for(grouped: list[tuple[str, list[dict[str, str]]]]) -> str:
    codes = {item["code"] for _, items in grouped for item in items}

    def names_with(*wanted: str) -> str:
        matched = [
            name
            for name, items in grouped
            if any(item["code"] in wanted for item in items)
        ]
        if not matched:
            return "the failed jobs"
        if len(matched) == 1:
            return matched[0]
        if len(matched) == 2:
            return f"{matched[0]} and {matched[1]}"
        return f"{matched[0]}, {matched[1]}, and {len(matched) - 2} more"

    if codes & {"failure_marker", "success_marker_missing", "run_incomplete", "required_marker_missing"}:
        return (
            f"Open the latest logs for {names_with('failure_marker', 'success_marker_missing', 'run_incomplete', 'required_marker_missing')}, "
            "fix the failure, and confirm the next run records success."
        )
    if codes & {"agent_not_loaded", "active_plist_missing", "retired_agent_loaded", "retired_plist_present"}:
        return (
            f"Fix LaunchAgent state for {names_with('agent_not_loaded', 'active_plist_missing', 'retired_agent_loaded', 'retired_plist_present')} "
            "(`launchctl print` / plist install)."
        )
    if "uncovered_enabled_job" in codes:
        return (
            f"Add an outcome contract for {names_with('uncovered_enabled_job')}, "
            "or disable the job if it shouldn't run."
        )
    if "declared_job_missing" in codes:
        return (
            f"Restore {names_with('declared_job_missing')} in the live scheduler, "
            "or remove it from fleet_outcome_contracts.json."
        )
    names = [name for name, _ in grouped[:2]]
    name_bit = ", ".join(names) if names else "the failed jobs"
    return f"Inspect {name_bit} in ~/.hermes/state/fleet-outcome-probe.json and repair the failed outcome."


def _alert_fact_lines(grouped: list[tuple[str, list[dict[str, str]]]]) -> list[str]:
    """One bullet per job — collapse multiple codes into one readable line."""
    facts: list[str] = []
    for name, items in grouped[:12]:
        issues = []
        seen = set()
        for item in items:
            phrase = _human_issue(item)
            if phrase not in seen:
                seen.add(phrase)
                issues.append(phrase)
        # Prefer the primary issue; mention a second only if distinct and useful
        if len(issues) == 1:
            facts.append(f"{name}: {issues[0]}")
        else:
            facts.append(f"{name}: {issues[0]} (also {'; '.join(issues[1:3])})")
    if len(grouped) > 12:
        facts.append(f"+{len(grouped) - 12} more jobs in the probe receipt")
    return facts


def _alert_message(findings: list[dict[str, str]], *, drill: bool) -> str:
    grouped = _group_findings(findings)
    prefix = "SYNTHETIC DRILL — " if drill else ""
    headline = f"{prefix}{_summary_sentence(grouped)}"
    lines = [f"🚨 {headline}"]
    for fact in _alert_fact_lines(grouped):
        lines.append(f"• {fact}")
    lines.append(f"Next: {_next_step_for(grouped)}")
    return "\n".join(lines)


def _recovery_message(previous_signature: str) -> str:
    return (
        "✅ No action needed — fleet outcome coverage recovered.\n"
        "All declared cron and LaunchAgent contracts are proving healthy again.\n"
        f"Cleared alert {previous_signature[:12]}"
    )


def _patterns_match(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns)


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
        if marker not in text:
            return [
                _finding(
                    surface,
                    identifier,
                    "response_section_missing",
                    f"{path} has no {marker} section",
                )
            ]
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


def _launchctl_print(label: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _plist_labels(path: Path) -> set[str]:
    labels: set[str] = set()
    if not path.is_dir():
        return labels
    for plist_path in path.glob("*.plist"):
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            continue
        label = str(payload.get("Label") or "")
        if label and MONITORED_LABEL_RE.search(label):
            labels.add(label)
    return labels


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


def _launchctl_inventory() -> set[str]:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"could not inventory loaded LaunchAgents: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "launchctl print failed").strip()
        raise ProbeError(f"could not inventory loaded LaunchAgents: {detail[-500:]}")
    return _labels_from_launchctl_domain(result.stdout)


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
    launchctl: Callable[[str], subprocess.CompletedProcess[str]],
    loaded_inventory: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    by_label = {str(contract["label"]): contract for contract in contracts}
    if len(by_label) != len(contracts):
        raise ProbeError("duplicate LaunchAgent contract label")

    findings: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    discovered = _plist_labels(launch_agents_dir) | loaded_inventory
    for label in sorted(discovered - set(by_label)):
        findings.append(_finding("launchd", label, "uncovered_plist", str(launch_agents_dir)))

    for contract in contracts:
        label = str(contract["label"])
        expected = str(contract["expected"])
        result = launchctl(label)
        loaded = result.returncode == 0
        plist_path = launch_agents_dir / f"{label}.plist"

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
            findings.append(
                _finding(
                    "launchd",
                    label,
                    "agent_not_loaded",
                    (result.stderr or result.stdout or "launchctl print failed").strip()[-300:],
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
            }
        )
    return findings, evidence


def evaluate(
    contracts: dict[str, Any],
    *,
    jobs_path: Path,
    output_root: Path,
    launch_agents_dir: Path,
    home: Path,
    now: datetime,
    launchctl: Callable[[str], subprocess.CompletedProcess[str]] = _launchctl_print,
    launch_inventory: Callable[[], set[str]] = _launchctl_inventory,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    if contracts.get("schema_version") != 1:
        raise ProbeError("unsupported or missing contract schema_version")
    cron_contracts = contracts.get("cron_jobs")
    launch_contracts = contracts.get("launch_agents")
    if not isinstance(cron_contracts, list) or not isinstance(launch_contracts, list):
        raise ProbeError("contracts require cron_jobs and launch_agents arrays")

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
    findings = _attach_display_names(cron_findings + launch_findings, contracts)
    return findings, cron_evidence + launch_evidence


def _signature(findings: list[dict[str, str]]) -> str:
    keys = sorted(f"{item['surface']}:{item['id']}:{item['code']}" for item in findings)
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


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
) -> dict[str, Any]:
    state = _load_optional_json(state_path)
    now_ts = now.timestamp()
    if findings:
        signature = _signature(findings)
        last_signature = str(state.get("delivered_signature") or "")
        last_alert_at = float(state.get("last_alert_at") or 0)
        if (
            state.get("active")
            and signature == last_signature
            and now_ts - last_alert_at < REALERT_SECONDS
        ):
            return {"action": "deduped", "signature": signature}
        message = _alert_message(findings, drill=drill)
        if not real_alert:
            if emit_dry_run:
                print(message)
            return {"action": "dry-run", "signature": signature}
        result = sender(message)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown send failure").strip()
            return {"action": "delivery-failed", "signature": signature, "error": detail[-500:]}
        state.update(
            {
                "delivered_signature": signature,
                "last_alert_at": now_ts,
                "active": True,
            }
        )
        _atomic_json(state_path, state)
        return {"action": "sent", "signature": signature}

    previous_signature = str(state.get("delivered_signature") or "")
    if not state.get("active") or not previous_signature:
        return {"action": "clean"}
    message = _recovery_message(previous_signature)
    if not real_alert:
        if emit_dry_run:
            print(message)
        return {"action": "recovery-dry-run", "signature": previous_signature}
    result = sender(message)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown send failure").strip()
        return {
            "action": "recovery-delivery-failed",
            "signature": previous_signature,
            "error": detail[-500:],
        }
    state.update(
        {
            "active": False,
            "last_recovery_at": now_ts,
            "recovered_signature": previous_signature,
        }
    )
    _atomic_json(state_path, state)
    return {"action": "recovery-sent", "signature": previous_signature}


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
            live_enabled = True if contract["enabled"] else True
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
            selected["name"] = str(contract["name"])
            injected.append(selected)

        for contract in contracts["launch_agents"]:
            label = str(contract["label"])
            should_look_loaded = contract["expected"] == "retired"

            def fake_launchctl(_label: str, loaded: bool = should_look_loaded):
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
            selected["name"] = _short_launchd_name(label)
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

    alarm = route_alarm(
        findings,
        state_path=state_path,
        now=now,
        drill=args.drill_all,
        real_alert=real_alert,
        emit_dry_run=not args.json,
    )
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
