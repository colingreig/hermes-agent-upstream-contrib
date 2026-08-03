"""Read-only fleet incident evidence collector and operator commands."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Any
import urllib.error
import urllib.request

from cron.executor_admission import executor_drain_status
from cron.fleet_drain import begin_fleet_drain, cancel_fleet_drain, fleet_drain_status
from hermes_constants import get_hermes_home


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


FLEET_OUTCOME_HISTORY_LIMIT = 24


def _jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` newest records from a rotated JSONL ledger."""
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


def _fleet_outcome_evidence(home: Path) -> dict[str, Any]:
    """Join the latest fleet-outcome probe receipt with its run history.

    The probe receipt is overwritten in place every run, so the archived
    ledger and per-transition snapshots are what make a past alarm storm
    triageable.  Both are surfaced here so one command answers "what did the
    alarm plane actually see, and when".
    """
    state_dir = home / "state"
    receipt_path = state_dir / "fleet-outcome-probe.json"
    ledger_path = state_dir / "fleet-outcome-probe-history.jsonl"
    snapshot_dir = state_dir / "fleet-outcome-probe-history"
    receipt = _read_json(receipt_path)
    alarm = dict((receipt or {}).get("alarm") or {})
    history = _jsonl_tail(ledger_path, FLEET_OUTCOME_HISTORY_LIMIT)
    try:
        snapshots = sorted(
            path.name for path in snapshot_dir.glob("*.json") if path.is_file()
        )
    except OSError:
        snapshots = []
    alert_state = _read_json(state_dir / "fleet-outcome-alert-state.json") or {}
    return {
        "receipt_path": str(receipt_path),
        "history_path": str(ledger_path),
        "snapshot_dir": str(snapshot_dir),
        "history_available": bool(history),
        "latest": {
            "checked_at": (receipt or {}).get("checked_at"),
            "status": (receipt or {}).get("status"),
            "finding_count": (receipt or {}).get("finding_count"),
            "alarm_action": alarm.get("action"),
            "alarm_reason": alarm.get("reason"),
            "incident_id": alarm.get("incident_id"),
        }
        if receipt
        else None,
        "incident_open": bool(alert_state.get("active")),
        "incident_id": alert_state.get("incident_id"),
        "incident_opened_at": alert_state.get("incident_opened_at"),
        "alert_count": alert_state.get("alert_count"),
        "history": history,
        "recent_snapshots": snapshots[-FLEET_OUTCOME_HISTORY_LIMIT:],
    }


def _sqlite_evidence(path: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {"path": str(path), "size_bytes": None, "quick_check": None, "error": None}
    try:
        evidence["size_bytes"] = path.stat().st_size
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            evidence["quick_check"] = row[0] if row else "no result"
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        evidence["error"] = str(exc)
    return evidence


def _production_write_evidence(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    result = _sqlite_evidence(path)
    result.update({"active_leases": [], "recovery_receipts": [], "fence_loss_receipts": []})
    if result["error"]:
        return result
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "active_leases" in tables:
                result["active_leases"] = [dict(row) for row in conn.execute("SELECT * FROM active_leases ORDER BY acquired_at")]
            if "recovery_receipts" in tables:
                result["recovery_receipts"] = [dict(row) for row in conn.execute("SELECT * FROM recovery_receipts ORDER BY recovered_at DESC LIMIT 20")]
            if "fence_loss_receipts" in tables:
                result["fence_loss_receipts"] = [dict(row) for row in conn.execute("SELECT * FROM fence_loss_receipts ORDER BY observed_at DESC LIMIT 20")]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def _execution_evidence(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    result = _sqlite_evidence(path)
    result.update({"active": [], "recent_terminal": []})
    if result["error"]:
        return result
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.05)
        conn.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "executions" in tables:
                result["active"] = [dict(row) for row in conn.execute(
                    "SELECT * FROM executions WHERE status IN ('claimed','running') ORDER BY claimed_at"
                )]
                result["recent_terminal"] = [dict(row) for row in conn.execute(
                    "SELECT * FROM executions WHERE status NOT IN ('claimed','running') "
                    "ORDER BY COALESCE(terminal_at,finished_at,claimed_at) DESC LIMIT 20"
                )]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def _gateway_evidence() -> dict[str, Any]:
    result = {"url": "http://127.0.0.1:8642/health", "reachable": False, "status": None, "error": None}
    try:
        with urllib.request.urlopen(result["url"], timeout=0.5) as response:
            result["status"] = response.status
            result["body"] = response.read(4096).decode("utf-8", errors="replace")
            result["reachable"] = response.status == 200
    except (OSError, urllib.error.URLError) as exc:
        result["error"] = str(exc)
    return result


def _process_evidence() -> list[dict[str, Any]]:
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            check=True, capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in completed.stdout.splitlines():
        if "hermes" not in line.lower():
            continue
        fields = line.strip().split(None, 3)
        if len(fields) == 4:
            rows.append({"pid": int(fields[0]), "ppid": int(fields[1]), "elapsed": fields[2], "command": fields[3]})
    return rows


def _governed_hash_findings(root: Path) -> list[dict[str, str]]:
    manifest_path = root / "machine-setup/mini-scripts/fleet_outcome_manifest.json"
    manifest = _read_json(manifest_path)
    if not manifest:
        return [{"code": "governed_manifest_unreadable", "path": str(manifest_path)}]
    base = manifest_path.parent
    findings = []
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not isinstance(item.get("source"), str):
            continue
        source = base / item["source"]
        try:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            findings.append({"code": "governed_path_unreadable", "path": str(source), "detail": str(exc)})
            continue
        if digest != item.get("sha256"):
            findings.append({"code": "governed_path_drift", "path": str(source), "actual_sha256": digest})
    return findings


def collect_incident_report(*, home: Path | None = None, root: Path | None = None) -> dict[str, Any]:
    """Collect one non-mutating evidence bundle for the active profile."""
    home = (home or get_hermes_home()).resolve()
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    runtime = home / "runtime-current"
    db_candidates = sorted({
        *home.glob("*.db"), *home.glob("*.sqlite3"),
        *(home / "state").glob("*.db"), *(home / "state").glob("*.sqlite3"),
        *(home / "cron").glob("*.db"),
    })
    databases = [_sqlite_evidence(path) for path in db_candidates if path.is_file()]
    governed_findings = _governed_hash_findings(root)
    database_findings = []
    for item in databases:
        if item["error"]:
            code = "database_busy" if "locked" in item["error"].lower() else "database_unreadable"
            database_findings.append({"code": code, "path": item["path"], "detail": item["error"]})
        elif item["quick_check"] != "ok":
            database_findings.append({"code": "database_quick_check_failed", "path": item["path"], "detail": item["quick_check"]})
        if (item["size_bytes"] or 0) > 1024 * 1024 * 1024:
            database_findings.append({"code": "database_large", "path": item["path"], "size_bytes": item["size_bytes"]})
    admission = executor_drain_status(
        database_path=home / "state/executor-admission.db"
    )
    admission_findings = []
    if admission.get("error"):
        admission_findings.append(
            {"code": "admission_database_unreadable", "detail": admission["error"]}
        )
    for lease in admission.get("generic_leases", []):
        if lease.get("expired"):
            admission_findings.append({"code": "stale_admission_heartbeat", "ledger_execution_id": lease.get("ledger_execution_id")})
    receipts = admission.get("generic_recovery_receipts", [])
    if receipts:
        admission_findings.append({"code": "owner_dead_recovery", "count": len(receipts), "latest": receipts[0].get("recovered_at")})
    release_receipt = _read_json(home / "releases/.mini-release-last-receipt.json")
    production_write_db = home / "state/production-write-lease.db"
    execution_ledger = _execution_evidence(home / "cron/executions.db")
    production_write = _production_write_evidence(production_write_db)
    report = {
        "schema_version": 1,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "profile_home": str(home),
        "read_only": True,
        "deployment": {
            "receipt": release_receipt,
            "deployed_sha": release_receipt.get("to_commit") if release_receipt else None,
            "certified_sha": (
                release_receipt.get("certified_source_commit")
                if release_receipt else None
            ),
            "runtime_current": str(runtime.resolve(strict=False)) if runtime.is_symlink() else None,
            "runtime_pointer_exists": runtime.exists() or runtime.is_symlink(),
        },
        "worktrees": [str(path) for path in sorted((Path.home() / "dev").glob("*/.claude/worktrees/*")) if path.is_dir()],
        "processes": _process_evidence(),
        "gateway": _gateway_evidence(),
        "drain": fleet_drain_status(
            marker_path=home / "state/fleet-drain.json"
        ),
        "execution_ledger": execution_ledger,
        "execution_admission": admission,
        "resource_admission": admission.get("generic_leases", []),
        "production_write_leases": production_write,
        "databases": databases,
        "fleet_outcome": _fleet_outcome_evidence(home),
        "findings": governed_findings + database_findings + admission_findings,
    }
    report["safe_to_cutover"] = bool(
        report["drain"].get("active")
        and admission.get("safe_to_cutover")
        and not (execution_ledger and execution_ledger.get("active"))
        and not (production_write and production_write.get("active_leases"))
        and not any(item["code"] in {"database_busy", "database_unreadable", "database_quick_check_failed", "governed_path_drift"} for item in report["findings"])
    )
    return report


def _print_human(report: dict[str, Any]) -> None:
    print(f"Fleet incident report ({report['collected_at']})")
    print(f"Profile: {report['profile_home']}")
    print(f"Drain active: {report['drain']['active']}")
    print(f"Safe to cut over: {report['safe_to_cutover']}")
    print(f"Gateway reachable: {report['gateway']['reachable']}")
    print(f"Active admission leases: {len(report['resource_admission'])}")
    outcome = report.get("fleet_outcome") or {}
    latest = outcome.get("latest") or {}
    print(
        "Fleet outcome probe: "
        f"{latest.get('status') or 'no receipt'} "
        f"({latest.get('finding_count') if latest else '?'} finding(s)) "
        f"alarm={latest.get('alarm_action') or 'unknown'}"
    )
    if latest.get("alarm_reason"):
        print(f"  Reason: {latest['alarm_reason']}")
    print(
        f"  Archived runs available: {len(outcome.get('history') or [])} "
        f"({outcome.get('history_path')})"
    )
    print(f"Findings: {len(report['findings'])}")
    for finding in report["findings"]:
        print(f"- {finding['code']}: {finding.get('path') or finding.get('ledger_execution_id') or finding.get('detail', '')}")


def fleet_command(args: argparse.Namespace) -> int:
    if args.fleet_action == "incident-report":
        report = collect_incident_report()
        print(json.dumps(report, indent=2, sort_keys=True) if args.json else "", end="" if args.json else "")
        if not args.json:
            _print_human(report)
        elif report:
            print()
        return 0
    if args.drain_action == "begin":
        result = begin_fleet_drain(reason=args.reason, actor="hermes-cli")
    elif args.drain_action == "cancel":
        result = cancel_fleet_drain()
    else:
        result = fleet_drain_status()
        result["executor_admission"] = executor_drain_status()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes fleet incident and drain operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser(subparsers)
    args = parser.parse_args(["fleet", *(argv or [])])
    return fleet_command(args)


def build_parser(subparsers):
    parser = subparsers.add_parser("fleet", help="Fleet drain and incident evidence")
    actions = parser.add_subparsers(dest="fleet_action", required=True)
    incident = actions.add_parser("incident-report", help="Collect a read-only incident report")
    incident.add_argument("--json", action="store_true")
    drain = actions.add_parser("drain", help="Manage the active profile's fleet drain")
    drain_actions = drain.add_subparsers(dest="drain_action", required=True)
    begin = drain_actions.add_parser("begin", help="Begin a reversible drain")
    begin.add_argument("--reason", default="operator requested")
    drain_actions.add_parser("cancel", help="Cancel the active drain")
    drain_actions.add_parser("status", help="Show drain and admission status")
    parser.set_defaults(func=fleet_command)
    return parser
