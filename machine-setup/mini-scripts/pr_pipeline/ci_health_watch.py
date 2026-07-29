#!/usr/bin/env python3
"""Hermes CI health watcher and managed hermes-ci lifecycle wrapper.

The normal poll remains cheap and zero-LLM: inspect the hermes-ci VM, expected
GitHub runners, and the latest default-branch CI conclusions, then alert through
``hermes send`` only on durable state transitions.  The VM lifecycle and
scheduled-run recovery policy are driven by ``ci_health_topology.json`` so the
Mini deployment reconciler can prove the canonical assets are present.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


ALLOWED = Path(os.path.expanduser("~/.hermes/allowed-repos.txt"))
PRODUCTION_STATE_PATH = Path(os.path.expanduser("~/.hermes/scripts/.ci_health_state.json"))
STATE_PATH = PRODUCTION_STATE_PATH
EVIDENCE_LOG_PATH = Path(os.path.expanduser("~/.hermes/state/ci-health/lifecycle.jsonl"))
INTENT_LOG_PATH = Path(os.path.expanduser("~/.hermes/state/ci-health/managed-lifecycle.jsonl"))
QUARANTINE_DIR = Path(os.path.expanduser("~/.hermes/state/ci-health/quarantine"))
TOPOLOGY_PATH = Path(__file__).resolve().with_name("ci_health_topology.json")
HERMES_BIN = Path(os.path.expanduser("~/.local/bin/hermes"))
SLACK_TARGET = "slack:hermes"
MAIN_BRANCHES = {"main", "master"}
RUNS_PER_REPO = 40
GH_TIMEOUT = 45
POLL_DEBOUNCE_COUNT = 2
GATING_EVENTS = {"push", "merge_group", "pull_request"}
EXPECTED_RUNNERS = {
    ("colingreig/elevatoruptime.com", "hermes-elevatoruptime.com"),
    ("colingreig/jdmbuysell-v4", "hermes-jdmbuysell-v4"),
    ("colingreig/thermal", "hermes-thermal"),
    ("colingreig/topdynamicspartners", "hermes-topdynamicspartners"),
}
EXPECTED_RECOVERY_IDENTITIES = {
    ("colingreig/jdmbuysell-v4", "Dead-image monitor", "schedule"),
    ("colingreig/thermal", "E2E Functional", "schedule"),
}


class MonitorError(RuntimeError):
    """Fail-closed monitor configuration or recovery error."""


@dataclass(frozen=True)
class RunnerStatus:
    repo: str
    name: str
    status: str
    busy: bool | None = None


@dataclass(frozen=True)
class VmStatus:
    available: bool
    boot_id: str | None
    uptime_seconds: int | None
    runner_status: str
    orbstack_evidence: dict[str, Any]
    resource_drift: tuple[str, ...] = ()


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


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
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextlib.contextmanager
def _state_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot read persisted recovery state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MonitorError(f"persisted recovery state {path} must be a JSON object")
    return data


def _state_has_synthetic_fixtures(data: dict[str, Any]) -> bool:
    recovery = data.get("recovery") if isinstance(data.get("recovery"), dict) else {}
    run_111 = recovery.get("111")

    def is_boot_a_marker(value: object) -> bool:
        if value == "boot-a":
            return True
        if not isinstance(value, str):
            return False
        transition = value.split(":", 1)[0]
        if "->" not in transition:
            return False
        prior, current = transition.split("->", 1)
        return prior == "boot-a" or current == "boot-a"

    def contains_boot_a(value: object) -> bool:
        if is_boot_a_marker(value):
            return True
        if isinstance(value, dict):
            return any(
                is_boot_a_marker(key)
                or contains_boot_a(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_boot_a(item) for item in value)
        return False

    return contains_boot_a(data) and (
        isinstance(run_111, dict) and run_111.get("original_run_id") == 111
    )


def _quarantine_synthetic_state(path: Path) -> Path | None:
    if path != PRODUCTION_STATE_PATH:
        return None
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not _state_has_synthetic_fixtures(data):
        return None
    quarantine_dir = QUARANTINE_DIR if path == PRODUCTION_STATE_PATH else path.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(quarantine_dir, 0o700)
    stamp = _now().strftime("%Y%m%dT%H%M%S%fZ")
    backup = quarantine_dir / f"{path.name}.{stamp}.synthetic-fixture.json"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    path.unlink()
    _append_jsonl(
        EVIDENCE_LOG_PATH,
        {
            "timestamp": _now_iso(),
            "event": "ci-health-state-quarantined",
            "reason": "synthetic-boot-a-and-run-111-fixtures",
            "backup": str(backup),
        },
    )
    return backup


def _save_state(obj: dict[str, Any], path: Path = STATE_PATH) -> None:
    _atomic_json(path, obj)


def _load_topology(path: Path = TOPOLOGY_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot read topology {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise MonitorError("topology must be a schema_version=1 object")
    vm = data.get("vm")
    runners = data.get("expected_runners")
    recovery = data.get("recovery_allowlist")
    if not isinstance(vm, dict) or not isinstance(vm.get("name"), str) or not vm["name"]:
        raise MonitorError("topology.vm.name is required")
    if not isinstance(runners, list) or not runners:
        raise MonitorError("topology.expected_runners must be a non-empty list")
    for runner in runners:
        if not isinstance(runner, dict) or not isinstance(runner.get("repo"), str) or not isinstance(runner.get("name"), str):
            raise MonitorError("each expected runner must declare repo and name")
        listener = runner.get("listener_command")
        if not isinstance(listener, list) or not listener or not all(isinstance(part, str) and part for part in listener):
            raise MonitorError("each expected runner must declare an exact listener_command string array")
    actual_runner_set = {(runner["repo"], runner["name"]) for runner in runners}
    if len(actual_runner_set) != len(runners):
        raise MonitorError("topology.expected_runners must not contain duplicate repo/name pairs")
    if actual_runner_set != EXPECTED_RUNNERS:
        raise MonitorError("topology.expected_runners must declare the four governed Hermes runners exactly")
    resources = data.get("desired_resources")
    if not isinstance(resources, dict) or resources != {
        "cpu_limit": 4,
        "memory_limit_mib": 6144,
        "concurrency_slots": 1,
        "semaphore_timeout_seconds": 1800,
        "diagnostic_retention_days": 7,
        "diagnostic_max_jobs_per_repo": 20,
    }:
        raise MonitorError("topology.desired_resources must declare the governed Hermes CI limits exactly")
    if not isinstance(recovery, list):
        raise MonitorError("topology.recovery_allowlist must be a list")
    identities: set[tuple[object, object, object]] = set()
    for item in recovery:
        if not isinstance(item, dict):
            raise MonitorError("recovery allowlist entries must be objects")
        identity = (item.get("repo"), item.get("workflow"), item.get("event"))
        identities.add(identity)
        if identity not in EXPECTED_RECOVERY_IDENTITIES:
            raise MonitorError("recovery allowlist contains an unknown workflow identity")
        if item.get("event") != "schedule" or item.get("max_reruns") != 1:
            raise MonitorError("recovery allowlist entries must be scheduled and capped at one rerun")
        if item.get("repo") == "colingreig/thermal":
            if item.get("job") != "e2e functional suite (advisory)" or item.get("expected_runner") != "hermes-thermal":
                raise MonitorError("Thermal recovery must declare the exact E2E job and runner")
            if item.get("mode") != "job-interruption":
                raise MonitorError("Thermal recovery must use job-interruption classification")
            if item.get("max_age_seconds") != 86400:
                raise MonitorError("Thermal recovery must use the governed 24-hour freshness bound")
        elif "max_age_seconds" in item or "mode" in item:
            raise MonitorError("legacy JDM recovery must retain managed-restart semantics")
    if len(recovery) != 2 or identities != EXPECTED_RECOVERY_IDENTITIES:
        raise MonitorError("recovery allowlist must declare the two governed scheduled recoveries exactly")
    interval = data.get("no_agent_interval_seconds")
    if not isinstance(interval, int) or interval <= 0 or interval > 300:
        raise MonitorError("no_agent_interval_seconds must be 300 seconds or faster")
    return data


def _repos() -> list[str]:
    try:
        return [ln.strip() for ln in ALLOWED.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    except Exception as exc:
        print(f"[ci-health] cannot read {ALLOWED}: {exc!r}", file=sys.stderr)
        return []


def _run(cmd: list[str], *, runner: CommandRunner = subprocess.run, timeout: int = GH_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return runner(cmd, capture_output=True, text=True, timeout=timeout)


def _gh_runs(repo: str, runner: CommandRunner = subprocess.run) -> list[dict[str, Any]] | None:
    try:
        result = _run(
            [
                "gh", "run", "list", "--repo", repo, "-L", str(RUNS_PER_REPO),
                "--json", "attempt,databaseId,workflowName,conclusion,status,createdAt,updatedAt,event,headBranch,headSha,url",
            ],
            runner=runner,
        )
        if result.returncode != 0:
            print(f"[ci-health] {repo}: gh rc={result.returncode} {result.stderr.strip()[:120]}", file=sys.stderr)
            return None
        data = json.loads(result.stdout or "[]")
        return data if isinstance(data, list) else None
    except Exception as exc:
        print(f"[ci-health] {repo}: {exc!r}", file=sys.stderr)
        return None


def _red_workflows(repo: str, gating_only: bool = False) -> dict[str, dict[str, Any]] | None:
    all_runs = _gh_runs(repo)
    if all_runs is None:
        return None
    runs = [
        item for item in all_runs
        if item.get("headBranch") in MAIN_BRANCHES
        and item.get("status") == "completed"
        and (not gating_only or item.get("event") in GATING_EVENTS)
    ]
    by_wf: dict[str, list[dict[str, Any]]] = {}
    for item in runs:
        by_wf.setdefault(item.get("workflowName") or "?", []).append(item)
    red: dict[str, dict[str, Any]] = {}
    for workflow, items in by_wf.items():
        if not items or items[0].get("conclusion") != "failure":
            continue
        streak = 0
        since = str(items[0].get("createdAt", ""))[:10]
        for item in items:
            if item.get("conclusion") == "failure":
                streak += 1
                since = str(item.get("createdAt", ""))[:10]
            else:
                break
        red[workflow] = {"since": since, "count": streak, "url": items[0].get("url", "")}
    return red


def _send_slack(msg: str, *, runner: CommandRunner = subprocess.run) -> bool:
    if os.environ.get("DRY_RUN"):
        print(f"[ci-health] DRY_RUN slack:\n{msg}")
        return True
    try:
        result = runner([str(HERMES_BIN), "send", "--to", SLACK_TARGET, msg], capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception as exc:
        print(f"[ci-health] slack send failed: {exc!r}", file=sys.stderr)
        return False


def _runner_statuses(topology: dict[str, Any], runner: CommandRunner = subprocess.run) -> dict[str, RunnerStatus] | None:
    statuses: dict[str, RunnerStatus] = {}
    for expected in topology["expected_runners"]:
        repo = expected["repo"]
        name = expected["name"]
        try:
            result = _run(["gh", "api", f"repos/{repo}/actions/runners", "--paginate"], runner=runner)
            if result.returncode != 0:
                print(f"[ci-health] {repo}: runners rc={result.returncode} {result.stderr.strip()[:120]}", file=sys.stderr)
                return None
            payload = json.loads(result.stdout or "{}")
            runners = payload.get("runners") if isinstance(payload, dict) else None
            if not isinstance(runners, list):
                return None
        except Exception as exc:
            print(f"[ci-health] {repo}: runners {exc!r}", file=sys.stderr)
            return None
        matches = [item for item in runners if isinstance(item, dict) and item.get("name") == name]
        if len(matches) > 1:
            print(f"[ci-health] {repo}: runner {name} is ambiguous", file=sys.stderr)
            return None
        match = matches[0] if matches else None
        status = str(match.get("status")) if match else "missing"
        statuses[f"{repo}::{name}"] = RunnerStatus(repo=repo, name=name, status=status, busy=match.get("busy") if match else None)
    return statuses


def _local_runner_statuses(topology: dict[str, Any], runner: CommandRunner = subprocess.run) -> dict[str, RunnerStatus] | None:
    vm_name = topology["vm"]["name"]
    orb = topology["vm"]["status_command"][0]
    result = _run([orb, "exec", "-m", vm_name, "ps", "-eo", "args="], runner=runner, timeout=20)
    if result.returncode != 0:
        print(f"[ci-health] local runner fallback ps rc={result.returncode} {result.stderr.strip()[:120]}", file=sys.stderr)
        return None
    process_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    statuses: dict[str, RunnerStatus] = {}
    for expected in topology["expected_runners"]:
        repo = expected["repo"]
        name = expected["name"]
        listener = expected.get("listener_command")
        if not isinstance(listener, list) or not all(isinstance(part, str) and part for part in listener):
            return None
        exact = " ".join(listener)
        matches = [line for line in process_lines if line == exact]
        if len(matches) > 1:
            print(f"[ci-health] local runner fallback {repo}: listener {name} is ambiguous", file=sys.stderr)
            return None
        statuses[f"{repo}::{name}"] = RunnerStatus(repo=repo, name=name, status="online" if matches else "missing", busy=None)
    return statuses


def _runner_statuses_with_fallback(topology: dict[str, Any], runner: CommandRunner = subprocess.run) -> dict[str, RunnerStatus] | None:
    statuses = _runner_statuses(topology, runner=runner)
    if statuses is not None:
        return statuses
    return _local_runner_statuses(topology, runner=runner)


def _probe_vm(topology: dict[str, Any], runner: CommandRunner = subprocess.run) -> VmStatus:
    vm = topology["vm"]
    name = vm["name"]
    evidence: dict[str, Any] = {"vm": name}
    boot_cmd = vm.get("boot_id_command")
    uptime_cmd = vm.get("uptime_command")
    list_cmd = vm.get("status_command")
    if not all(isinstance(cmd, list) and all(isinstance(part, str) for part in cmd) for cmd in (boot_cmd, uptime_cmd, list_cmd)):
        raise MonitorError("vm commands must be string arrays")
    status = _run(list_cmd, runner=runner, timeout=20)
    evidence["status_rc"] = status.returncode
    evidence["status_stdout"] = status.stdout.strip()[:500]
    evidence["status_stderr"] = status.stderr.strip()[:500]
    if status.returncode != 0:
        evidence["availability_reason"] = "vm-status-command-failed"
        raise MonitorError(f"vm status command failed: {evidence}")
    try:
        payload = json.loads(status.stdout or "null")
    except (TypeError, json.JSONDecodeError) as exc:
        evidence["availability_reason"] = f"vm-status-malformed:{exc}"
        raise MonitorError(f"vm status output is malformed: {evidence}") from exc
    candidates = payload.get("vms") if isinstance(payload, dict) else payload
    matches = [
        item for item in candidates
        if isinstance(item, dict) and item.get("name") == name
    ] if isinstance(candidates, list) else []
    evidence["status_matches"] = len(matches)
    if len(matches) != 1:
        evidence["availability_reason"] = "vm-missing-or-ambiguous"
        raise MonitorError(f"vm status did not identify exactly one managed VM: {evidence}")
    vm_state = matches[0].get("state") or matches[0].get("status")
    evidence["vm_state"] = vm_state
    if vm_state == "stopped":
        evidence["availability_reason"] = "vm-not-running"
        return VmStatus(available=False, boot_id=None, uptime_seconds=None, runner_status="unknown", orbstack_evidence=evidence)
    if vm_state != "running":
        evidence["availability_reason"] = "vm-state-unknown"
        raise MonitorError(f"vm status reported unknown state: {evidence}")

    boot = _run(boot_cmd, runner=runner, timeout=20)
    evidence["boot_rc"] = boot.returncode
    evidence["boot_stderr"] = boot.stderr.strip()[:500]
    if boot.returncode != 0 or not boot.stdout.strip():
        evidence["availability_reason"] = "vm-boot-id-probe-failed"
        raise MonitorError(f"vm boot-id probe failed: {evidence}")
    boot_id = boot.stdout.strip().splitlines()[0]
    uptime = _run(uptime_cmd, runner=runner, timeout=20)
    evidence["uptime_rc"] = uptime.returncode
    evidence["uptime_stderr"] = uptime.stderr.strip()[:500]
    try:
        uptime_seconds = int(float(uptime.stdout.strip())) if uptime.returncode == 0 else None
    except ValueError:
        uptime_seconds = None
    desired = topology["desired_resources"]
    config = matches[0].get("config") if isinstance(matches[0].get("config"), dict) else {}
    drift: list[str] = []
    if config.get("cpu_limit") != desired["cpu_limit"]:
        drift.append(f"cpu:{config.get('cpu_limit')}!={desired['cpu_limit']}")
    if config.get("memory_limit_mib") != desired["memory_limit_mib"]:
        drift.append(f"memory_mib:{config.get('memory_limit_mib')}!={desired['memory_limit_mib']}")
    runner_config_cmd = vm.get("runner_config_command")
    if not isinstance(runner_config_cmd, list) or not all(isinstance(part, str) and part for part in runner_config_cmd):
        raise MonitorError("vm.runner_config_command must be an exact string array")
    runner_config_result = _run(runner_config_cmd, runner=runner, timeout=20)
    if runner_config_result.returncode != 0:
        drift.append("runner-config:unreadable")
    else:
        try:
            runner_config = json.loads(runner_config_result.stdout)
        except (TypeError, json.JSONDecodeError):
            runner_config = None
        expected_runner_config = {
            "schema_version": 1,
            "concurrency_slots": desired["concurrency_slots"],
            "semaphore_timeout_seconds": desired["semaphore_timeout_seconds"],
            "diagnostic_retention_days": desired["diagnostic_retention_days"],
            "diagnostic_max_jobs_per_repo": desired["diagnostic_max_jobs_per_repo"],
        }
        if runner_config != expected_runner_config:
            drift.append("runner-config:drift")
    evidence["resource_drift"] = drift
    return VmStatus(
        available=True,
        boot_id=boot_id,
        uptime_seconds=uptime_seconds,
        runner_status="unknown",
        orbstack_evidence=evidence,
        resource_drift=tuple(drift),
    )


def _latest_managed_intent(state: dict[str, Any]) -> dict[str, Any] | None:
    intent = state.get("last_managed_intent")
    if not isinstance(intent, dict):
        return None
    if intent.get("consumed_at"):
        return None
    created = _parse_time(intent.get("timestamp"))
    if created is None or _now() - created > timedelta(minutes=30):
        return None
    return intent


def _transition_matches_intent(action: object, prior_boot: object, current_boot: object, prior_available: object, current_available: object) -> bool:
    if action == "restart":
        return bool(prior_boot) and bool(current_boot) and prior_boot != current_boot
    if action == "stop":
        return prior_available is True and current_available is False
    if action == "start":
        return prior_available is False and current_available is True
    return False


def _consume_managed_intent(
    state: dict[str, Any],
    *,
    prior_boot: object,
    current_boot: object,
    prior_available: object,
    current_available: object,
) -> dict[str, Any] | None:
    intent = _latest_managed_intent(state)
    if intent is None:
        return None
    if not _transition_matches_intent(intent.get("action"), prior_boot, current_boot, prior_available, current_available):
        return None
    consumed = dict(intent)
    consumed["consumed_at"] = _now_iso()
    state["last_managed_intent"] = consumed
    state.setdefault("managed_intents", []).append(consumed)
    return intent


def _record_transition(previous: dict[str, Any], vm: VmStatus, state: dict[str, Any], runner_status: str) -> dict[str, Any] | None:
    old = previous.get("vm") if isinstance(previous.get("vm"), dict) else {}
    prior_boot = old.get("boot_id")
    prior_available = old.get("available")
    if prior_boot == vm.boot_id and prior_available == vm.available:
        return None
    intent = _consume_managed_intent(
        state,
        prior_boot=prior_boot,
        current_boot=vm.boot_id,
        prior_available=prior_available,
        current_available=vm.available,
    )
    outage = state.get("vm_outage") if isinstance(state.get("vm_outage"), dict) else None
    observed_at = _now()
    timestamp = observed_at.isoformat()
    interruption_started_at = None
    interruption_ended_at = None
    if prior_available is True and vm.available is False:
        state["vm_outage"] = {"started_at": timestamp, "prior_boot_id": prior_boot}
    elif prior_available is False and vm.available is True and vm.boot_id and outage is not None:
        interruption_started_at = outage.get("started_at") if isinstance(outage.get("started_at"), str) else None
        interruption_ended_at = timestamp if interruption_started_at is not None else None
        state.pop("vm_outage", None)
    elif vm.available is True:
        state.pop("vm_outage", None)

    boot_changed = bool(prior_boot and vm.boot_id and prior_boot != vm.boot_id)
    managed_restart = intent is not None and intent.get("action") == "restart" and boot_changed
    if managed_restart:
        managed_started = _parse_time(intent.get("timestamp"))
        if managed_started is not None and managed_started <= observed_at:
            interruption_started_at = managed_started.isoformat()
            interruption_ended_at = timestamp
    recovery_eligible = interruption_started_at is not None and interruption_ended_at is not None
    record = {
        "timestamp": timestamp,
        "event": "hermes-ci-lifecycle-transition",
        "prior_boot_id": prior_boot,
        "current_boot_id": vm.boot_id,
        "prior_available": prior_available,
        "current_available": vm.available,
        "host_uptime_seconds": vm.uptime_seconds,
        "runner_status": runner_status,
        "orbstack_evidence": vm.orbstack_evidence,
        "initiator": "unknown",
        "reason": "unknown",
        "recovery_eligible": recovery_eligible,
    }
    if interruption_started_at is not None and interruption_ended_at is not None:
        record["interruption_started_at"] = interruption_started_at
        record["interruption_ended_at"] = interruption_ended_at
    if intent is not None:
        record["initiator"] = intent.get("actor") or "unknown"
        record["managed_action"] = intent.get("action")
        record["reason"] = intent.get("reason") or "unknown"
        record["managed_intent_timestamp"] = intent.get("timestamp")
    if managed_restart:
        record["restart_notification_eligible"] = True
        record["recovery_notification_eligible"] = vm.available is True
    _append_jsonl(EVIDENCE_LOG_PATH, record)
    return record


def _update_runner_alerts(
    state: dict[str, Any],
    statuses: dict[str, RunnerStatus] | None,
    *,
    send: Callable[[str], bool],
) -> tuple[list[str], list[str]]:
    runner_state = state.setdefault("runner_alerts", {})
    if statuses is None:
        return [], []
    newly_offline: list[str] = []
    recovered: list[str] = []
    for key, status in statuses.items():
        entry = runner_state.setdefault(key, {"offline_count": 0, "alerted": False})
        offline = status.status != "online"
        if offline:
            entry["offline_count"] = int(entry.get("offline_count") or 0) + 1
            if entry["offline_count"] >= POLL_DEBOUNCE_COUNT and not entry.get("alerted"):
                msg = f"🔴 *Hermes CI runner offline*\n`{status.repo}` expected runner `{status.name}` is `{status.status}` for {entry['offline_count']} consecutive polls."
                if send(msg):
                    entry["alerted"] = True
                    entry["alerted_at"] = _now_iso()
                    newly_offline.append(key)
        else:
            if entry.get("alerted"):
                msg = f"🟢 *Hermes CI runner recovered*\n`{status.repo}` runner `{status.name}` is online again."
                if send(msg):
                    recovered.append(key)
            entry.clear()
            entry.update({"offline_count": 0, "alerted": False, "recovered_at": _now_iso()})
    return newly_offline, recovered


def _update_resource_alert(
    state: dict[str, Any],
    drift: tuple[str, ...],
    *,
    send: Callable[[str], bool],
) -> bool:
    current = list(drift)
    previous = state.get("resource_drift") if isinstance(state.get("resource_drift"), list) else []
    alerted = False
    if current and current != previous:
        alerted = send(
            "🔴 *Hermes CI desired-state drift*\n"
            + ", ".join(f"`{item}`" for item in current)
        )
    elif not current and previous:
        alerted = send("🟢 *Hermes CI desired-state recovered*")
    state["resource_drift"] = current
    return alerted


def _vm_alert_key(record: dict[str, Any]) -> str:
    return f"{record.get('prior_boot_id')}->{record.get('current_boot_id')}:{record.get('prior_available')}->{record.get('current_available')}"


def _maybe_alert_vm_transition(state: dict[str, Any], record: dict[str, Any] | None, *, send: Callable[[str], bool]) -> bool:
    if record is None:
        return False
    if record.get("prior_boot_id") is None and record.get("prior_available") is None:
        return False
    key = _vm_alert_key(record)
    vm_alerts = state.setdefault("vm_alerts", {})
    initiator = record.get("initiator") or "unknown"
    reason = record.get("reason") or "unknown"
    alerted = False
    sent = vm_alerts.setdefault("sent", {})

    def send_once(kind: str, headline: str) -> None:
        nonlocal alerted
        sent_key = f"{key}:{kind}"
        if sent.get(sent_key):
            return
        msg = (
            f"{headline}\n"
            f"boot `{record.get('prior_boot_id')}` -> `{record.get('current_boot_id')}`, "
            f"available `{record.get('prior_available')}` -> `{record.get('current_available')}`; "
            f"initiator=`{initiator}`, reason=`{reason}`."
        )
        if send(msg):
            sent[sent_key] = _now_iso()
            vm_alerts["last_alert_key"] = key
            vm_alerts["last_alerted_at"] = sent[sent_key]
            alerted = True

    if record.get("restart_notification_eligible") is True:
        send_once("restart", "🔴 *hermes-ci VM restart/outage observed*")
        if record.get("recovery_notification_eligible") is True:
            send_once("recovery", "🟢 *hermes-ci VM recovered*")
    else:
        headline = "🟢 *hermes-ci VM recovered*" if record.get("current_available") is True and record.get("prior_available") is False else "🔴 *hermes-ci VM lifecycle transition observed*"
        send_once("transition", headline)
    return alerted


def _run_overlaps_restart(run: dict[str, Any], transition: dict[str, Any]) -> bool:
    started = _parse_time(run.get("createdAt"))
    ended = _parse_time(run.get("updatedAt")) or _parse_time(run.get("createdAt"))
    interruption_started = _parse_time(transition.get("interruption_started_at"))
    interruption_ended = _parse_time(transition.get("interruption_ended_at"))
    if started is None or ended is None:
        return False
    if interruption_started is None or interruption_ended is None:
        return False
    return started <= interruption_ended and ended >= interruption_started


def _recovery_allowlist_match(topology: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    for item in topology.get("recovery_allowlist", []):
        if (
            run.get("repo") == item.get("repo")
            and run.get("workflowName") == item.get("workflow")
            and run.get("event") == item.get("event")
            and item.get("max_reruns") == 1
        ):
            return item
    return None


def _allowlisted_recovery(topology: dict[str, Any], run: dict[str, Any]) -> bool:
    return _recovery_allowlist_match(topology, run) is not None


def _allowlisted_rerun_count(recovery_state: dict[str, Any], *, repo: object, workflow: object) -> int:
    count = 0
    for entry in recovery_state.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("repo") == repo and entry.get("workflow") == workflow and entry.get("attempt"):
            count += 1
    return count


def _expected_runner_for_repo(topology: dict[str, Any], repo: str) -> dict[str, Any] | None:
    matches = [item for item in topology["expected_runners"] if item.get("repo") == repo]
    return matches[0] if len(matches) == 1 else None


def _runner_recovered_for_run(topology: dict[str, Any], statuses: dict[str, RunnerStatus] | None, run: dict[str, Any]) -> bool:
    if statuses is None:
        return False
    expected = _expected_runner_for_repo(topology, str(run.get("repo") or ""))
    if expected is None:
        return False
    status = statuses.get(f"{expected['repo']}::{expected['name']}")
    return status is not None and status.status == "online"


def _runner_online(
    topology: dict[str, Any],
    statuses: dict[str, RunnerStatus] | None,
    *,
    repo: str,
    runner_name: str,
) -> bool:
    if statuses is None:
        return False
    expected = _expected_runner_for_repo(topology, repo)
    if expected is None or expected.get("name") != runner_name:
        return False
    status = statuses.get(f"{repo}::{runner_name}")
    return status is not None and status.status == "online"


def _gh_jobs(repo: str, run_id: int, *, runner: CommandRunner) -> list[dict[str, Any]] | None:
    try:
        result = _run(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest"],
            runner=runner,
        )
        if result.returncode != 0:
            print(f"[ci-health] {repo}: jobs for {run_id} rc={result.returncode} {result.stderr.strip()[:120]}", file=sys.stderr)
            return None
        payload = json.loads(result.stdout or "{}")
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return jobs if isinstance(jobs, list) else None
    except Exception as exc:
        print(f"[ci-health] {repo}: jobs for {run_id}: {exc!r}", file=sys.stderr)
        return None


def _gh_default_branch_sha(repo: str, *, runner: CommandRunner) -> str | None:
    result = _run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"], runner=runner)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch:
        return None
    result = _run(["gh", "api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"], runner=runner)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha if len(sha) == 40 and all(char in "0123456789abcdef" for char in sha) else None


def _gh_run(repo: str, run_id: int, *, runner: CommandRunner) -> dict[str, Any] | None:
    result = _run(["gh", "api", f"repos/{repo}/actions/runs/{run_id}"], runner=runner)
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _classify_job_interruption(
    jobs: list[dict[str, Any]],
    policy: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    matches = [job for job in jobs if isinstance(job, dict) and job.get("name") == policy.get("job")]
    if len(matches) != 1:
        return None, "job-missing-or-ambiguous"
    job = matches[0]
    if job.get("status") != "completed" or job.get("conclusion") != "failure":
        return None, "job-not-completed-failure"
    if not isinstance(job.get("id"), int):
        return None, "job-id-missing"
    if job.get("runner_name") != policy.get("expected_runner"):
        return None, "unexpected-runner"
    steps = job.get("steps")
    if not isinstance(steps, list) or not steps:
        return None, "steps-missing"
    nonterminal: list[dict[str, Any]] = []
    for step in steps:
        if not isinstance(step, dict):
            return None, "step-not-object"
        status = step.get("status")
        conclusion = step.get("conclusion")
        if status == "completed":
            if conclusion not in {"success", "skipped"}:
                return None, "completed-step-not-success-or-skipped"
            continue
        if status in {"in_progress", "pending"} and conclusion is None:
            nonterminal.append(step)
            continue
        return None, "inconsistent-step-state"
    if not nonterminal:
        return None, "no-truly-unfinished-step"
    return job, "runner-interruption-nonterminal-steps"


def _recovery_candidates(topology: dict[str, Any], runner: CommandRunner) -> list[dict[str, Any]] | None:
    policies = topology.get("recovery_allowlist", [])
    repos = sorted({item["repo"] for item in policies})
    runs_by_repo: dict[str, list[dict[str, Any]]] = {}
    for repo in repos:
        runs = _gh_runs(repo, runner=runner)
        if runs is None:
            return None
        runs_by_repo[repo] = runs
    candidates: list[dict[str, Any]] = []
    for policy in policies:
        qualifying: list[dict[str, Any]] = []
        for raw_run in runs_by_repo[policy["repo"]]:
            run = dict(raw_run)
            if (
                run.get("workflowName") != policy.get("workflow")
                or run.get("event") != policy.get("event")
                or run.get("status") != "completed"
            ):
                continue
            run = dict(run)
            run["repo"] = policy["repo"]
            if policy.get("mode") == "job-interruption" or run.get("conclusion") in {"failure", "cancelled", "timed_out"}:
                qualifying.append(run)
        if qualifying and policy.get("mode") == "job-interruption":
            newest = max(
                qualifying,
                key=lambda item: _parse_time(item.get("createdAt")) or datetime.min.replace(tzinfo=timezone.utc),
            )
            candidates.append(newest)
        elif qualifying:
            candidates.extend(qualifying)
    return candidates


def _run_is_current_and_fresh(
    run: dict[str, Any],
    policy: dict[str, Any],
    *,
    runner: CommandRunner,
) -> tuple[bool, str | None]:
    created = _parse_time(run.get("createdAt"))
    if created is None:
        return False, None
    age = (_now() - created).total_seconds()
    if age < -300 or age > int(policy["max_age_seconds"]):
        return False, None
    current_sha = _gh_default_branch_sha(str(run.get("repo")), runner=runner)
    if current_sha is None or run.get("headSha") != current_sha:
        return False, current_sha
    return True, current_sha


def _reconcile_recovery_results(
    topology: dict[str, Any],
    state: dict[str, Any],
    *,
    runner: CommandRunner,
) -> int:
    observed = 0
    recovery_state = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    for entry in recovery_state.values():
        if (
            not isinstance(entry, dict)
            or entry.get("result") != "dispatched"
            or entry.get("rerun_conclusion") is not None
        ):
            continue
        repo = entry.get("repo")
        run_id = entry.get("original_run_id")
        if not isinstance(repo, str) or not isinstance(run_id, int):
            continue
        policy = next(
            (
                item
                for item in topology["recovery_allowlist"]
                if item.get("repo") == repo and item.get("workflow") == entry.get("workflow")
            ),
            None,
        )
        if policy is None:
            continue
        if policy.get("mode") == "job-interruption":
            jobs = _gh_jobs(repo, run_id, runner=runner)
            if jobs is None:
                continue
            matches = [
                job
                for job in jobs
                if isinstance(job, dict)
                and job.get("name") == policy.get("job")
                and job.get("id") != entry.get("job_id")
            ]
            if len(matches) != 1 or matches[0].get("status") != "completed":
                continue
            conclusion = matches[0].get("conclusion")
            if not isinstance(conclusion, str) or not conclusion:
                continue
            entry["rerun_job_id"] = matches[0].get("id")
        else:
            run = _gh_run(repo, run_id, runner=runner)
            if run is None or run.get("status") != "completed":
                continue
            if int(run.get("run_attempt") or 0) <= int(entry.get("original_run_attempt") or 1):
                continue
            conclusion = run.get("conclusion")
            if not isinstance(conclusion, str) or not conclusion:
                continue
        entry["rerun_conclusion"] = conclusion
        entry["observed_at"] = _now_iso()
        observed += 1
    return observed


def _maybe_recover(
    topology: dict[str, Any],
    state: dict[str, Any],
    statuses: dict[str, RunnerStatus] | None,
    latest_transition: dict[str, Any] | None,
    *,
    runner: CommandRunner,
    send: Callable[[str], bool],
    state_path: Path,
) -> list[int]:
    candidates = _recovery_candidates(topology, runner)
    if candidates is None:
        return []
    recovered: list[int] = []
    recovery_state = state.setdefault("recovery", {})
    for run in candidates:
        run_id = run.get("databaseId")
        key = str(run_id or "")
        if not key or key in recovery_state:
            continue
        allowlist = _recovery_allowlist_match(topology, run)
        if allowlist is None:
            send(f"⚠️ *CI recovery refused*\n`{run.get('repo')}` / {run.get('workflowName')} run {run_id} is not allowlisted for automatic rerun.")
            recovery_state[key] = {"refused_at": _now_iso(), "reason": "not-allowlisted", "original_run_id": run_id}
            continue
        if allowlist.get("mode") == "job-interruption":
            current_and_fresh, default_sha = _run_is_current_and_fresh(run, allowlist, runner=runner)
            if not current_and_fresh:
                continue
            jobs = _gh_jobs(str(run.get("repo")), int(run_id), runner=runner)
            if jobs is None:
                continue
            job, classification = _classify_job_interruption(jobs, allowlist)
            if job is None:
                continue
            expected_runner = str(allowlist["expected_runner"])
            if not _runner_online(
                topology,
                statuses,
                repo=str(run.get("repo")),
                runner_name=expected_runner,
            ):
                continue
            dispatch_sha = _gh_default_branch_sha(str(run.get("repo")), runner=runner)
            if dispatch_sha is None or run.get("headSha") != dispatch_sha:
                continue
            recovery_state[key] = {
                "original_run_id": run_id,
                "job_id": job.get("id"),
                "repo": run.get("repo"),
                "workflow": run.get("workflowName"),
                "runner": expected_runner,
                "classification": classification,
                "attempt": 1,
                "original_run_attempt": int(run.get("attempt") or 1),
                "job_name": allowlist.get("job"),
                "head_sha": run.get("headSha"),
                "default_branch_sha": default_sha,
                "classified_at": _now_iso(),
                "dispatch_default_branch_sha": dispatch_sha,
                "dispatch_verified_at": _now_iso(),
                "persisted_before_dispatch_at": _now_iso(),
                "result": "dispatch-pending",
            }
            _save_state(state, state_path)
            command = [
                "gh",
                "run",
                "rerun",
                str(run_id),
                "--job",
                str(job.get("id")),
                "--repo",
                str(run.get("repo")),
            ]
            result = _run(command, runner=runner, timeout=GH_TIMEOUT)
            recovery_state[key]["dispatch_rc"] = result.returncode
            recovery_state[key]["dispatched_at"] = _now_iso() if result.returncode == 0 else None
            recovery_state[key]["result"] = "dispatched" if result.returncode == 0 else "dispatch-failed"
            recovery_state[key]["result_at"] = _now_iso()
            if result.returncode == 0:
                recovered.append(int(run_id))
                send(
                    "🟡 *CI job rerun dispatched*\n"
                    f"`{run.get('repo')}` / {run.get('workflowName')} job {job.get('id')} "
                    f"was classified `{classification}` on `{expected_runner}`."
                )
            break
        if latest_transition is None or not _run_overlaps_restart(run, latest_transition):
            continue
        if latest_transition.get("recovery_eligible") is not True:
            continue
        if not _runner_recovered_for_run(topology, statuses, run):
            continue
        if _allowlisted_rerun_count(recovery_state, repo=run.get("repo"), workflow=run.get("workflowName")) >= int(allowlist.get("max_reruns") or 0):
            continue
        recovery_state[key] = {
            "original_run_id": run_id,
            "repo": run.get("repo"),
            "workflow": run.get("workflowName"),
            "runner": _expected_runner_for_repo(topology, str(run.get("repo")))["name"],
            "attempt": 1,
            "original_run_attempt": int(run.get("attempt") or 1),
            "classification": "managed-vm-restart-overlap",
            "classified_at": _now_iso(),
            "dispatch_verified_at": _now_iso(),
            "persisted_before_dispatch_at": _now_iso(),
            "correlated_transition_at": latest_transition.get("timestamp"),
            "result": "dispatch-pending",
        }
        _save_state(state, state_path)
        result = _run(["gh", "run", "rerun", str(run_id), "--repo", str(run.get("repo"))], runner=runner, timeout=GH_TIMEOUT)
        recovery_state[key]["dispatch_rc"] = result.returncode
        recovery_state[key]["dispatched_at"] = _now_iso() if result.returncode == 0 else None
        recovery_state[key]["result"] = "dispatched" if result.returncode == 0 else "dispatch-failed"
        recovery_state[key]["result_at"] = _now_iso()
        if result.returncode == 0:
            recovered.append(int(run_id))
            send(f"🟡 *CI run rerun dispatched*\n`{run.get('repo')}` / {run.get('workflowName')} original run {run_id} overlapped hermes-ci restart and runner recovered.")
        break
    return recovered


def _current_red(repos: Iterable[str]) -> dict[str, dict[str, Any]] | None:
    current: dict[str, dict[str, Any]] = {}
    for repo in repos:
        red = _red_workflows(repo)
        if red is None:
            return None
        for workflow, info in red.items():
            current[f"{repo}::{workflow}"] = info
    return current


def _ci_red_alerts(state: dict[str, Any], current: dict[str, dict[str, Any]], *, send: Callable[[str], bool]) -> tuple[int, int]:
    prev = state.get("red", {}) if isinstance(state.get("red"), dict) else {}
    newly_red = [key for key in current if key not in prev]
    recovered = [key for key in prev if key not in current]
    lines: list[str] = []
    if newly_red:
        lines.append(f"🔴 *CI went red on `main`* ({len(newly_red)}):")
        for key in sorted(newly_red):
            repo, workflow = key.split("::", 1)
            item = current[key]
            lines.append(f"  • `{repo}` / {workflow} — failing since {item['since']} ({item['count']} run(s)) {item['url']}")
    if recovered:
        lines.append("🟢 *CI recovered* ({0}): ".format(len(recovered)) + ", ".join(f"`{k.split('::',1)[0]}` / {k.split('::',1)[1]}" for k in sorted(recovered)))
    if lines:
        total = len(current)
        lines.append(f"_(total still red: {total} across {len({k.split('::',1)[0] for k in current})} repo(s) — `ci_health_watch.py`)_")
        send("\n".join(lines))
    state["red"] = current
    return len(newly_red), len(recovered)


def poll_once(*, runner: CommandRunner = subprocess.run, topology_path: Path = TOPOLOGY_PATH, state_path: Path = STATE_PATH) -> dict[str, Any]:
    topology = _load_topology(topology_path)
    with _state_lock(state_path):
        _quarantine_synthetic_state(state_path)
        state = _load_state(state_path)
        previous = dict(state)
        send = lambda msg: _send_slack(msg, runner=runner)

        vm = _probe_vm(topology, runner=runner)
        statuses = _runner_statuses_with_fallback(topology, runner=runner)
        runner_summary = "unknown" if statuses is None else ",".join(f"{key}={value.status}" for key, value in sorted(statuses.items()))
        transition = _record_transition(previous, vm, state, runner_summary)
        vm_alerted = _maybe_alert_vm_transition(state, transition, send=send)
        resource_alerted = _update_resource_alert(state, vm.resource_drift, send=send)
        runner_alerted, runner_recovered = _update_runner_alerts(state, statuses, send=send)
        recovery_results_observed = _reconcile_recovery_results(topology, state, runner=runner)
        saved_transition = state.get("last_transition") if isinstance(state.get("last_transition"), dict) else None
        correlation_transition = transition or saved_transition
        rerun_ids = _maybe_recover(
            topology,
            state,
            statuses,
            correlation_transition,
            runner=runner,
            send=send,
            state_path=state_path,
        )

        repos = _repos()
        current = _current_red(repos) if repos else {}
        if current is None:
            newly_red = ci_recovered = 0
        else:
            newly_red, ci_recovered = _ci_red_alerts(state, current, send=send)
        state["generated_at"] = _now_iso()
        state["vm"] = {"available": vm.available, "boot_id": vm.boot_id, "uptime_seconds": vm.uptime_seconds}
        if transition is not None:
            state["last_transition"] = transition
        _save_state(state, state_path)
        return {
            "checked": len(repos),
            "red": len(current) if current is not None else None,
            "newly_red": newly_red,
            "recovered": ci_recovered,
            "vm_alerted": vm_alerted,
            "resource_alerted": resource_alerted,
            "resource_drift": list(vm.resource_drift),
            "runner_alerted": len(runner_alerted),
            "runner_recovered": len(runner_recovered),
            "rerun_ids": rerun_ids,
            "recovery_results_observed": recovery_results_observed,
        }


def record_managed_lifecycle(
    action: str,
    actor: str,
    reason: str,
    *,
    runner: CommandRunner = subprocess.run,
    topology_path: Path = TOPOLOGY_PATH,
    state_path: Path = STATE_PATH,
) -> int:
    topology = _load_topology(topology_path)
    if action not in {"start", "stop", "restart"}:
        raise MonitorError("action must be start, stop, or restart")
    if not actor.strip() or not reason.strip():
        raise MonitorError("actor and reason are required")
    commands = topology["vm"].get("managed_commands")
    if not isinstance(commands, dict) or action not in commands:
        raise MonitorError(f"no managed command for action {action}")
    command = commands[action]
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise MonitorError("managed command must be a string array")
    record = {"timestamp": _now_iso(), "actor": actor, "action": action, "reason": reason, "vm": topology["vm"]["name"]}
    with _state_lock(state_path):
        _append_jsonl(INTENT_LOG_PATH, record)
        state = _load_state(state_path)
        state["last_managed_intent"] = record
        _save_state(state, state_path)
    result = runner(command, capture_output=True, text=True, timeout=120)
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    lifecycle = sub.add_parser("lifecycle", help="record and execute a managed hermes-ci lifecycle action")
    lifecycle.add_argument("action", choices=("start", "stop", "restart"))
    lifecycle.add_argument("--actor", required=True)
    lifecycle.add_argument("--reason", required=True)
    lifecycle.add_argument("--topology", type=Path, default=TOPOLOGY_PATH)
    poll = sub.add_parser("poll", help="run one monitor poll")
    poll.add_argument("--topology", type=Path, default=TOPOLOGY_PATH)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lifecycle":
            return record_managed_lifecycle(args.action, args.actor, args.reason, topology_path=args.topology)
        report = poll_once(topology_path=getattr(args, "topology", TOPOLOGY_PATH))
        print(json.dumps(report), file=sys.stderr)
        return 0
    except MonitorError as exc:
        print(f"[ci-health] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ci-health] unexpected: {exc!r}", file=sys.stderr)
        print(json.dumps({"error": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
