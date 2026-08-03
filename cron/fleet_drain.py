"""Profile-local fleet drain marker and cron admission policy.

A drain is an atomic marker under the active ``HERMES_HOME``.  While present,
all agent jobs are denied.  No-agent jobs are fail-closed unless the governed
production mutation registry identifies the cron ID as a read-only actor.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import uuid

from hermes_constants import get_hermes_home


@dataclass(frozen=True)
class DrainDecision:
    allowed: bool
    reason: str
    actor_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _marker_path() -> Path:
    return get_hermes_home().resolve() / "state" / "fleet-drain.json"


def _registry_path() -> Path:
    return Path(__file__).resolve().parents[1] / "machine-setup" / "production_mutation_registry.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
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


def begin_fleet_drain(*, reason: str = "operator requested", actor: str = "hermes-cli") -> dict[str, Any]:
    """Create or return the active profile's durable drain marker."""
    current = fleet_drain_status()
    if current["active"]:
        return current
    marker = {
        "schema_version": 1,
        "drain_id": uuid.uuid4().hex,
        "profile_home": str(get_hermes_home().resolve()),
        "began_at": _utc_now(),
        "actor": str(actor or "unknown"),
        "reason": str(reason or "operator requested"),
    }
    _atomic_json(_marker_path(), marker)
    return {"active": True, "marker": marker, "error": None}


def cancel_fleet_drain() -> dict[str, Any]:
    """Remove the active profile's marker; cancellation never touches jobs."""
    path = _marker_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return {"active": False, "marker": None, "error": None}


def fleet_drain_status(*, marker_path: Path | None = None) -> dict[str, Any]:
    path = marker_path or _marker_path()
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"active": False, "marker": None, "error": None}
    except (OSError, json.JSONDecodeError) as exc:
        # An unreadable marker cannot be safely treated as absent.
        return {"active": True, "marker": None, "error": str(exc)}
    if not isinstance(marker, dict):
        return {"active": True, "marker": None, "error": "drain marker is not an object"}
    return {"active": True, "marker": marker, "error": None}


def _read_only_actor_for_job(job_id: str) -> str | None:
    try:
        payload = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    actors = payload.get("actors") if isinstance(payload, dict) else None
    if not isinstance(actors, list):
        return None
    matches = [
        actor for actor in actors
        if isinstance(actor, dict) and job_id in (actor.get("cron_job_ids") or [])
    ]
    if len(matches) != 1 or matches[0].get("mutability") != "read-only":
        return None
    actor_id = matches[0].get("id")
    return actor_id if isinstance(actor_id, str) and actor_id else None


def cron_job_admission(job: dict[str, Any]) -> DrainDecision:
    """Return the fleet-drain decision without mutating scheduler state."""
    status = fleet_drain_status()
    if not status["active"]:
        return DrainDecision(True, "fleet_not_draining")
    if job.get("no_agent") is not True:
        return DrainDecision(False, "fleet_draining_llm_blocked")
    job_id = str(job.get("id") or "")
    actor_id = _read_only_actor_for_job(job_id)
    if actor_id:
        return DrainDecision(True, "fleet_draining_read_only_monitor", actor_id)
    return DrainDecision(False, "fleet_draining_mutating_or_unknown_no_agent")
