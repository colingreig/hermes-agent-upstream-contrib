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
import copy
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


ALLOWED = Path(os.path.expanduser("~/.hermes/allowed-repos.txt"))
STATE_PATH = Path(os.path.expanduser("~/.hermes/scripts/.ci_health_state.json"))
EVIDENCE_LOG_PATH = Path(os.path.expanduser("~/.hermes/state/ci-health/lifecycle.jsonl"))
INTENT_LOG_PATH = Path(os.path.expanduser("~/.hermes/state/ci-health/managed-lifecycle.jsonl"))
TOPOLOGY_PATH = Path(__file__).resolve().with_name("ci_health_topology.json")
HERMES_BIN = Path(os.path.expanduser("~/.local/bin/hermes"))
SLACK_TARGET = "slack:hermes"
MAIN_BRANCHES = {"main", "master"}
RUNS_PER_REPO = 40
GH_TIMEOUT = 45
POLL_DEBOUNCE_COUNT = 2
GATING_EVENTS = {"push", "merge_group", "pull_request"}
STATE_SCHEMA_VERSION = 2
LIFECYCLE_SCHEMA_VERSION = 1


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
        return {"schema_version": STATE_SCHEMA_VERSION}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"cannot read persisted recovery state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MonitorError(f"persisted recovery state {path} must be a JSON object")
    schema_version = data.get("schema_version")
    if schema_version is None:
        # Schema-less v1 state is migrated in memory and only committed after a
        # complete, trustworthy poll. Existing alert/recovery fields survive.
        data["schema_version"] = STATE_SCHEMA_VERSION
    elif (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != STATE_SCHEMA_VERSION
    ):
        raise MonitorError(
            f"persisted recovery state {path} has unsupported schema_version={schema_version!r}"
        )
    vm = data.get("vm")
    if vm is not None and not isinstance(vm, dict):
        raise MonitorError(f"persisted recovery state {path}.vm must be an object")
    if isinstance(vm, dict):
        if "available" in vm and not isinstance(vm.get("available"), bool):
            raise MonitorError(f"persisted recovery state {path}.vm.available must be boolean")
        if "boot_id" in vm and vm.get("boot_id") is not None and not isinstance(vm.get("boot_id"), str):
            raise MonitorError(f"persisted recovery state {path}.vm.boot_id must be a string or null")
    lifecycle = data.get("lifecycle")
    if lifecycle is not None:
        if not isinstance(lifecycle, dict):
            raise MonitorError(f"persisted recovery state {path}.lifecycle must be an object")
        lifecycle_schema = lifecycle.get("schema_version")
        if (
            not isinstance(lifecycle_schema, int)
            or isinstance(lifecycle_schema, bool)
            or lifecycle_schema != LIFECYCLE_SCHEMA_VERSION
        ):
            raise MonitorError(
                f"persisted recovery state {path}.lifecycle has unsupported schema_version"
            )
        if lifecycle.get("classification") not in {
            "baseline",
            "baseline-reset",
            "stable",
            "transition",
            "unavailable",
        }:
            raise MonitorError(
                f"persisted recovery state {path}.lifecycle classification is invalid"
            )
        if _parse_time(lifecycle.get("observed_at")) is None:
            raise MonitorError(
                f"persisted recovery state {path}.lifecycle observed_at is invalid"
            )
        fingerprint = lifecycle.get("probe_fingerprint")
        digest = (
            fingerprint.removeprefix("sha256:")
            if isinstance(fingerprint, str) and fingerprint.startswith("sha256:")
            else ""
        )
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise MonitorError(
                f"persisted recovery state {path}.lifecycle probe_fingerprint is invalid"
            )
        lifecycle_boot = lifecycle.get("canonical_boot_uuid")
        if lifecycle_boot is not None and _canonical_boot_uuid(lifecycle_boot) != lifecycle_boot:
            raise MonitorError(
                f"persisted recovery state {path}.lifecycle canonical_boot_uuid is invalid"
            )
        stored_boot = vm.get("boot_id") if isinstance(vm, dict) else None
        canonical_stored_boot = _canonical_boot_uuid(stored_boot)
        if (
            lifecycle_boot is not None
            and canonical_stored_boot is not None
            and lifecycle_boot != canonical_stored_boot
        ):
            raise MonitorError(
                f"persisted recovery state {path} lifecycle and vm boot IDs disagree"
            )
    return data


def _save_state(obj: dict[str, Any], path: Path = STATE_PATH) -> None:
    obj["schema_version"] = STATE_SCHEMA_VERSION
    _atomic_json(path, obj)


def _canonical_boot_uuid(value: object) -> str | None:
    """Return the canonical UUID form of a kernel boot ID, else ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = uuid.UUID(candidate)
    except (ValueError, AttributeError):
        return None
    canonical = str(parsed)
    # UUID() accepts compact/braced/URN inputs. The kernel contract is the
    # canonical 36-character hyphenated form; rejecting aliases prevents a
    # formatting change from masquerading as a lifecycle transition.
    return canonical if candidate.lower() == canonical else None


def _probe_fingerprint(
    topology: dict[str, Any],
    vm: VmStatus,
    canonical_boot_uuid: str | None,
) -> str:
    payload = {
        "topology_vm": topology["vm"]["name"],
        "status_command": topology["vm"].get("status_command"),
        "boot_id_command": topology["vm"].get("boot_id_command"),
        "status_matches": vm.orbstack_evidence.get("status_matches"),
        "vm_state": vm.orbstack_evidence.get("vm_state"),
        "available": vm.available,
        "canonical_boot_uuid": canonical_boot_uuid,
        "uptime_seconds": vm.uptime_seconds,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _lifecycle_observation(
    topology: dict[str, Any],
    vm: VmStatus,
    *,
    canonical_boot_uuid: str | None,
    observed_at: str,
    classification: str,
) -> dict[str, Any]:
    return {
        "schema_version": LIFECYCLE_SCHEMA_VERSION,
        "probe_fingerprint": _probe_fingerprint(topology, vm, canonical_boot_uuid),
        "canonical_boot_uuid": canonical_boot_uuid,
        "observed_at": observed_at,
        "classification": classification,
    }


def _active_baseline_reset_blockers(state: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if isinstance(state.get("vm_outage"), dict):
        blockers.append("outage-active")
    if _latest_managed_intent(state) is not None:
        blockers.append("managed-intent-active")
    return blockers


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
    expected_runner_set = {
        ("colingreig/jdmbuysell-v4", "hermes-jdmbuysell-v4"),
        ("colingreig/topdynamicspartners", "hermes-topdynamicspartners"),
        ("colingreig/elevatoruptime.com", "hermes-elevatoruptime.com"),
    }
    actual_runner_set = {(runner["repo"], runner["name"]) for runner in runners}
    if actual_runner_set != expected_runner_set:
        raise MonitorError("topology.expected_runners must declare the three audited Hermes runners exactly")
    if not isinstance(recovery, list):
        raise MonitorError("topology.recovery_allowlist must be a list")
    for item in recovery:
        if not isinstance(item, dict):
            raise MonitorError("recovery allowlist entries must be objects")
        if item.get("repo") != "colingreig/jdmbuysell-v4" or item.get("workflow") != "Dead-image monitor":
            raise MonitorError("recovery allowlist is fail-closed to jdmbuysell-v4 / Dead-image monitor")
        if item.get("event") != "schedule" or item.get("max_reruns") != 1:
            raise MonitorError("recovery allowlist entries must be scheduled and capped at one rerun")
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
                "--json", "databaseId,workflowName,conclusion,status,createdAt,updatedAt,event,headBranch,url",
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
    result = _run(["orb", "exec", "-m", vm_name, "ps", "-eo", "args="], runner=runner, timeout=20)
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
    return VmStatus(available=True, boot_id=boot_id, uptime_seconds=uptime_seconds, runner_status="unknown", orbstack_evidence=evidence)


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


def _record_transition(
    previous: dict[str, Any],
    vm: VmStatus,
    state: dict[str, Any],
    runner_status: str,
    observation: dict[str, Any],
) -> dict[str, Any] | None:
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
        "observed_at": observation["observed_at"],
        "event": "hermes-ci-lifecycle-transition",
        "classification": observation["classification"],
        "probe_fingerprint": observation["probe_fingerprint"],
        "canonical_boot_uuid": observation["canonical_boot_uuid"],
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


def _record_baseline_reset(
    previous: dict[str, Any],
    vm: VmStatus,
    state: dict[str, Any],
    runner_status: str,
    observation: dict[str, Any],
) -> dict[str, Any]:
    old_vm = previous.get("vm") if isinstance(previous.get("vm"), dict) else {}
    record = {
        "timestamp": observation["observed_at"],
        "observed_at": observation["observed_at"],
        "event": "hermes-ci-lifecycle-baseline-reset",
        "classification": "baseline-reset",
        "probe_fingerprint": observation["probe_fingerprint"],
        "canonical_boot_uuid": observation["canonical_boot_uuid"],
        "prior_boot_id": old_vm.get("boot_id"),
        "current_boot_id": observation["canonical_boot_uuid"],
        "prior_available": old_vm.get("available"),
        "current_available": vm.available,
        "host_uptime_seconds": vm.uptime_seconds,
        "runner_status": runner_status,
        "orbstack_evidence": vm.orbstack_evidence,
        "initiator": "unknown",
        "reason": "invalid-persisted-boot-id",
        "recovery_eligible": False,
    }
    # Resetting an untrusted baseline also retires stale lifecycle evidence;
    # otherwise a later stable poll could reuse it to authorize a rerun.
    state.pop("last_transition", None)
    state["last_baseline_reset"] = record
    _append_jsonl(EVIDENCE_LOG_PATH, record)
    return record


def _unknown_probe_report(
    topology: dict[str, Any],
    previous: dict[str, Any],
    *,
    reason: str,
    evidence: dict[str, Any] | None = None,
    blockers: list[str] | None = None,
) -> dict[str, Any]:
    observed_at = _now_iso()
    fingerprint_payload = {
        "topology_vm": topology["vm"]["name"],
        "reason": reason,
        "evidence": evidence or {},
        "blockers": blockers or [],
    }
    fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    previous_vm = previous.get("vm") if isinstance(previous.get("vm"), dict) else {}
    record = {
        "timestamp": observed_at,
        "observed_at": observed_at,
        "event": "hermes-ci-lifecycle-health",
        "classification": "unknown",
        "health": "UNKNOWN",
        "reason": reason,
        "blockers": blockers or [],
        "probe_fingerprint": fingerprint,
        "canonical_boot_uuid": None,
        "prior_boot_id": previous_vm.get("boot_id"),
        "current_boot_id": None,
        "orbstack_evidence": evidence or {},
        "state_preserved": True,
        "recovery_eligible": False,
    }
    _append_jsonl(EVIDENCE_LOG_PATH, record)
    return {
        "checked": 0,
        "red": None,
        "newly_red": 0,
        "recovered": 0,
        "vm_alerted": False,
        "runner_alerted": 0,
        "runner_recovered": 0,
        "rerun_ids": [],
        "health": "UNKNOWN",
        "classification": "unknown",
        "reason": reason,
        "probe_fingerprint": fingerprint,
        "canonical_boot_uuid": None,
        "observed_at": observed_at,
        "state_preserved": True,
        "blockers": blockers or [],
    }


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


def _recovery_candidates(topology: dict[str, Any], runner: CommandRunner) -> list[dict[str, Any]] | None:
    repos = sorted({item["repo"] for item in topology.get("recovery_allowlist", [])})
    candidates: list[dict[str, Any]] = []
    for repo in repos:
        runs = _gh_runs(repo, runner=runner)
        if runs is None:
            return None
        for run in runs:
            run = dict(run)
            run["repo"] = repo
            if run.get("status") == "completed" and run.get("conclusion") in {"failure", "cancelled", "timed_out"}:
                candidates.append(run)
    return candidates


def _maybe_recover(
    topology: dict[str, Any],
    state: dict[str, Any],
    statuses: dict[str, RunnerStatus] | None,
    latest_transition: dict[str, Any] | None,
    *,
    runner: CommandRunner,
    send: Callable[[str], bool],
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
            "attempt": 1,
            "persisted_before_dispatch_at": _now_iso(),
            "correlated_transition_at": latest_transition.get("timestamp"),
        }
        _save_state(state)
        result = _run(["gh", "run", "rerun", str(run_id), "--repo", str(run.get("repo"))], runner=runner, timeout=GH_TIMEOUT)
        recovery_state[key]["dispatch_rc"] = result.returncode
        recovery_state[key]["dispatched_at"] = _now_iso() if result.returncode == 0 else None
        if result.returncode == 0:
            recovered.append(int(run_id))
            send(f"🟡 *CI run rerun dispatched*\n`{run.get('repo')}` / {run.get('workflowName')} original run {run_id} overlapped hermes-ci restart and runner recovered.")
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
        state = _load_state(state_path)
        previous = copy.deepcopy(state)
        send = lambda msg: _send_slack(msg, runner=runner)

        try:
            vm = _probe_vm(topology, runner=runner)
        except MonitorError as exc:
            return _unknown_probe_report(
                topology,
                previous,
                reason="vm-probe-invalid",
                evidence={"error": str(exc)},
            )

        canonical_current_boot = _canonical_boot_uuid(vm.boot_id)
        if vm.available and canonical_current_boot is None:
            return _unknown_probe_report(
                topology,
                previous,
                reason="current-boot-id-invalid",
                evidence={
                    **vm.orbstack_evidence,
                    "raw_boot_id": vm.boot_id,
                },
            )
        if canonical_current_boot != vm.boot_id:
            vm = VmStatus(
                available=vm.available,
                boot_id=canonical_current_boot,
                uptime_seconds=vm.uptime_seconds,
                runner_status=vm.runner_status,
                orbstack_evidence=vm.orbstack_evidence,
            )

        old_vm = previous.get("vm") if isinstance(previous.get("vm"), dict) else {}
        raw_stored_boot = old_vm.get("boot_id")
        canonical_stored_boot = _canonical_boot_uuid(raw_stored_boot)
        if raw_stored_boot is not None and canonical_stored_boot is None:
            blockers = _active_baseline_reset_blockers(state)
            if old_vm.get("available") is False and "outage-active" not in blockers:
                blockers.insert(0, "outage-active")
            if not vm.available or canonical_current_boot is None:
                blockers.append("current-valid-boot-required")
            if blockers:
                return _unknown_probe_report(
                    topology,
                    previous,
                    reason="baseline-reset-blocked",
                    evidence=vm.orbstack_evidence,
                    blockers=blockers,
                )
            classification = "baseline-reset"
        else:
            classification = (
                "unavailable"
                if not vm.available
                else "baseline"
                if not old_vm
                else "stable"
                if canonical_stored_boot == canonical_current_boot
                and old_vm.get("available") == vm.available
                else "transition"
            )
            if canonical_stored_boot is not None and canonical_stored_boot != raw_stored_boot:
                previous["vm"]["boot_id"] = canonical_stored_boot

        observed_at = _now_iso()
        observation = _lifecycle_observation(
            topology,
            vm,
            canonical_boot_uuid=canonical_current_boot,
            observed_at=observed_at,
            classification=classification,
        )
        statuses = _runner_statuses_with_fallback(topology, runner=runner)
        runner_summary = "unknown" if statuses is None else ",".join(f"{key}={value.status}" for key, value in sorted(statuses.items()))
        if classification == "baseline-reset":
            _record_baseline_reset(previous, vm, state, runner_summary, observation)
            transition = None
        else:
            transition = _record_transition(previous, vm, state, runner_summary, observation)
        vm_alerted = _maybe_alert_vm_transition(state, transition, send=send)
        runner_alerted, runner_recovered = _update_runner_alerts(state, statuses, send=send)
        saved_transition = state.get("last_transition") if isinstance(state.get("last_transition"), dict) else None
        # A baseline reset repairs untrusted persisted identity; it is not
        # evidence of an interruption and must never reuse a stale transition
        # to authorize recovery.
        correlation_transition = None if classification == "baseline-reset" else transition or saved_transition
        rerun_ids = _maybe_recover(topology, state, statuses, correlation_transition, runner=runner, send=send)

        repos = _repos()
        current = _current_red(repos) if repos else {}
        if current is None:
            newly_red = ci_recovered = 0
        else:
            newly_red, ci_recovered = _ci_red_alerts(state, current, send=send)
        state["generated_at"] = _now_iso()
        state["vm"] = {"available": vm.available, "boot_id": vm.boot_id, "uptime_seconds": vm.uptime_seconds}
        state["lifecycle"] = observation
        if transition is not None:
            state["last_transition"] = transition
        _save_state(state, state_path)
        return {
            "checked": len(repos),
            "red": len(current) if current is not None else None,
            "newly_red": newly_red,
            "recovered": ci_recovered,
            "vm_alerted": vm_alerted,
            "runner_alerted": len(runner_alerted),
            "runner_recovered": len(runner_recovered),
            "rerun_ids": rerun_ids,
            "health": "OK",
            "classification": classification,
            "probe_fingerprint": observation["probe_fingerprint"],
            "canonical_boot_uuid": canonical_current_boot,
            "observed_at": observed_at,
            "state_preserved": False,
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
