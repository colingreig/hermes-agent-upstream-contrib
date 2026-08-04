#!/usr/bin/env python3
"""Read-only conformance checks for Hermes Mini governed write paths.

The verifier deliberately checks only the fields and paths owned by the three
governed writers.  It therefore catches a missing, symlinked, tampered, or
directly-written managed value without treating user configuration, scheduler
run metadata, or other runtime state as drift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any


SCRIPT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_ROOT.parents[1]
FLEET_ROOT = REPO_ROOT / "machine-setup" / "fleet-config"
RELEASE_EVENTS = {"noop", "rejected", "advanced", "cut", "rollback"}
# These scheduler-owned fields change after installation and are intentionally
# not fleet configuration.  Unknown fields remain significant: a direct write
# to a governed job must not be silently accepted.
RUNTIME_JOB_FIELDS = {
    "last_run_at",
    "next_run_at",
    "last_status",
    "last_error",
    "last_delivery_error",
    "paused_at",
    "paused_reason",
    "fire_claim",
    "run_claim",
    "lane_state",
    "route_health",
    "provider_snapshot",
    "model_snapshot",
    "state",
}


@dataclass(frozen=True)
class Finding:
    check: str
    detail: str


class VerificationError(RuntimeError):
    """A governed path is missing, unsafe, or has drifted."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"{label} missing or symlinked: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be a JSON object: {path}")
    return value


def _materialize_fleet_jobs(
    path: Path, helper_path: Path, helper_sha256: object
) -> dict[str, Any]:
    """Load source jobs through the same pure expansion contract as the installer."""
    _plain_file(path, "fleet jobs")
    _plain_file(helper_path, "fleet jobs payload helper")
    try:
        helper_bytes = helper_path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"cannot read fleet jobs payload helper: {helper_path}: {exc}") from exc
    if hashlib.sha256(helper_bytes).hexdigest() != helper_sha256:
        raise VerificationError(f"fleet config source hash drift: {helper_path}")
    namespace: dict[str, Any] = {
        "__file__": str(helper_path),
        "__name__": "_verified_fleet_job_payload",
    }
    try:
        exec(compile(helper_bytes, str(helper_path), "exec"), namespace)
        error_type = namespace["FleetJobPayloadError"]
        materialize = namespace["materialize_jobs_payload"]
    except Exception as exc:
        raise VerificationError(f"fleet jobs payload helper cannot be loaded: {exc}") from exc
    if (
        not isinstance(error_type, type)
        or not issubclass(error_type, Exception)
        or not callable(materialize)
    ):
        raise VerificationError("fleet jobs payload helper does not expose its required contract")
    try:
        return materialize(path.read_bytes())
    except error_type as exc:
        raise VerificationError(f"fleet jobs cannot be materialized: {exc}") from exc
    except Exception as exc:
        raise VerificationError(f"fleet jobs payload helper failed: {exc}") from exc


def _load_yaml(path: Path, label: str) -> dict[str, Any]:
    # Import only after the stdlib-only runtime-release preflight. The deployed
    # monitor therefore still reports a broken pointer when the release venv
    # (and its PyYAML) is unavailable.
    try:
        import yaml
    except ImportError as exc:
        raise VerificationError(
            "PyYAML unavailable after runtime-current preflight; full governed config verification cannot run"
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise VerificationError(f"{label} missing or symlinked: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise VerificationError(f"{label} is not valid YAML: {path}: {exc}") from exc
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be a YAML object: {path}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _failure_code(reason: str) -> str:
    if reason.startswith("runtime-current"):
        return "runtime_current_broken"
    if reason.startswith("releases root") or reason.startswith("release "):
        return "runtime_release_broken"
    return "governed_path_drift"


def _plain_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"{label} missing or symlinked: {path}")


def _plain_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or path == Path(".") or ".." in path.parts:
        raise VerificationError(f"{label} must not be absolute or contain '..'")
    return path


def _safe_join(root: Path, relative: Path, label: str) -> Path:
    """Join a manifest path without following a symlinked path component."""
    if root.is_symlink() or not root.is_dir():
        raise VerificationError(f"{label} root missing or symlinked: {root}")
    candidate = root
    for component in relative.parts:
        candidate = candidate / component
        if candidate.is_symlink():
            raise VerificationError(f"{label} has a symlinked path component: {candidate}")
    try:
        candidate.relative_to(root)
    except ValueError as exc:  # defensive: _plain_relative already rejects traversal
        raise VerificationError(f"{label} escapes its governed root") from exc
    return candidate


def _jobs_by_id(value: object, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise VerificationError(f"{label} has no object list")
    result: dict[str, dict[str, Any]] = {}
    for job in value:
        if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
            raise VerificationError(f"{label} contains an invalid job")
        if job["id"] in result:
            raise VerificationError(f"{label} contains duplicate job target: {job['id']}")
        result[job["id"]] = job
    return result


def _fleet_config_default(home: Path, source_root: Path) -> Path:
    """Locate the fleet bundle in a checkout or under a deployed release."""
    deployed_scripts = home / ".hermes" / "scripts"
    if source_root == deployed_scripts:
        return home / ".hermes" / "runtime-current" / "machine-setup" / "fleet-config"
    return source_root.parent / "fleet-config"


def _source_root_default(home: Path) -> Path:
    """Use release-owned sources when this verifier runs from ~/.hermes/scripts."""
    deployed_scripts = home / ".hermes" / "scripts"
    if SCRIPT_ROOT == deployed_scripts:
        return home / ".hermes" / "runtime-current" / "machine-setup" / "mini-scripts"
    return SCRIPT_ROOT


def _repo_root_default(home: Path) -> Path:
    """Root for fleet-outcome entries declaring "source_root": "repo".

    Deliberately independent of any --source-root override: a handful of
    tests point source_root at an isolated copy of just the mini-scripts
    subtree to exercise edge cases on entries that stay bundle-relative,
    but a repo-relative entry's single canonical source (e.g. scripts/
    clickup_poll_gate.py) never moves with that override — it is always
    either the real checkout (SCRIPT_ROOT's parent tree) or, when deployed,
    the active release directory (which contains scripts/ alongside
    machine-setup/, same as source_root's own deployed case above).
    """
    deployed_scripts = home / ".hermes" / "scripts"
    if SCRIPT_ROOT == deployed_scripts:
        return home / ".hermes" / "runtime-current"
    return REPO_ROOT


def _semantic_overlay_matches(live: Any, overlay: Any, path: str = "") -> None:
    """Check only overlay-owned semantic values, preserving all other config."""
    if isinstance(overlay, dict):
        if overlay == {}:
            if live != {}:
                raise VerificationError(f"managed config value drifted at {path or '<root>'}")
            return
        if not isinstance(live, dict):
            raise VerificationError(f"managed config mapping missing at {path or '<root>'}")
        for key, expected in overlay.items():
            next_path = f"{path}.{key}" if path else str(key)
            if key not in live:
                raise VerificationError(f"managed config value missing at {next_path}")
            _semantic_overlay_matches(live[key], expected, next_path)
        return
    if path == "hooks.pre_tool_call" and isinstance(overlay, list):
        if not isinstance(live, list) or any(item not in live for item in overlay):
            raise VerificationError("managed config hook missing at hooks.pre_tool_call")
        return
    if live != overlay:
        raise VerificationError(f"managed config value drifted at {path or '<root>'}")


def _managed_job(job: dict[str, Any]) -> dict[str, Any]:
    managed = {key: value for key, value in job.items() if key not in RUNTIME_JOB_FIELDS}
    # Scheduler progress is runtime metadata, but the configured repeat limit
    # remains governed.  Copy before removing the live completion count.
    if isinstance(managed.get("repeat"), dict):
        repeat = dict(managed["repeat"])
        repeat.pop("completed", None)
        managed["repeat"] = repeat
    # The scheduler persists normalized shares while the governed source may
    # express the same lane ratio as integer weights.  Compare the ratio, not
    # the incidental representation, without accepting malformed values.
    lane_weights = managed.get("lane_weights")
    if isinstance(lane_weights, dict) and set(lane_weights) == {"code", "content"}:
        values = (lane_weights["code"], lane_weights["content"])
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
            try:
                code, content = (Fraction(str(value)) for value in values)
                total = code + content
                if code > 0 and content > 0 and total > 0:
                    managed["lane_weights"] = {
                        "code": code / total,
                        "content": content / total,
                    }
            except (ValueError, ZeroDivisionError):
                pass
    return managed


class GovernedPathsVerifier:
    def __init__(
        self,
        *,
        home: Path,
        source_root: Path | None = None,
        fleet_root: Path | None = None,
        repo_root: Path | None = None,
        fixture_safe: bool = False,
    ) -> None:
        self.home = home.expanduser().resolve()
        self.hermes = self.home / ".hermes"
        self.source_root = (source_root or _source_root_default(self.home)).resolve()
        self.fleet_root = (fleet_root or _fleet_config_default(self.home, self.source_root)).resolve()
        self.repo_root = (repo_root or _repo_root_default(self.home)).resolve()
        self.fixture_safe = fixture_safe

    def verify(self) -> list[Finding]:
        findings: list[Finding] = []
        self._verify_release()
        findings.append(Finding("release", "runtime-current and its content-addressed receipt are coherent"))
        self._verify_fleet_outcomes()
        findings.append(Finding("fleet-outcomes", "declared script, plist, manifest, and cron fields match"))
        self._verify_fleet_config()
        findings.append(Finding("fleet-config", "managed overlay, profiles, and cron jobs match semantically"))
        return findings

    def _verify_release(self) -> None:
        releases = self.hermes / "releases"
        if releases.is_symlink() or not releases.is_dir():
            raise VerificationError(f"releases root missing or symlinked: {releases}")
        current = self.hermes / "runtime-current"
        if not current.is_symlink():
            raise VerificationError(f"runtime-current is missing or not a symlink: {current}")
        # Check the RAW link text, not only the resolved path (ClickUp
        # 86e2kt3yr). The 2026-08-02 corruption was a bare-name relative link
        # (`v0.18.2-<sha>`), which resolves against ~/.hermes rather than
        # releases/ and therefore dangled. A relative link that *does* resolve
        # (e.g. `releases/v0.18.2-<sha>`) is equally out of contract: the
        # release receipt records an absolute runtime_target, and every
        # consumer builds paths by string-joining onto this pointer.
        raw_target = Path(os.readlink(current))
        if not raw_target.is_absolute():
            raise VerificationError(
                f"runtime-current is a relative symlink (must be absolute): {current} -> {raw_target}"
            )
        try:
            target = current.resolve(strict=True)
        except OSError as exc:
            raise VerificationError(f"runtime-current target is missing: {current}") from exc
        if not target.is_dir() or target.parent != releases.resolve():
            raise VerificationError(f"runtime-current escapes releases: {target}")
        # Reject traversal/dot components on the raw text. Deliberately NOT a
        # string comparison against the resolved path: this verifier runs
        # inside every release cut, and a home under a symlinked prefix
        # (macOS /var -> /private/var) would then fail a healthy pointer and
        # roll the cut back.
        if any(part in {".", ".."} for part in raw_target.parts):
            raise VerificationError(
                f"runtime-current is not canonical: {current} -> {raw_target}"
            )

        previous = releases / ".previous"
        _plain_file(previous, "release previous pointer")
        previous_target = Path(previous.read_text(encoding="utf-8").strip())
        if not previous_target.is_absolute():
            previous_target = releases / previous_target
        try:
            previous_target = previous_target.resolve(strict=True)
        except OSError as exc:
            raise VerificationError("release previous pointer target is missing") from exc
        if not previous_target.is_dir() or previous_target.parent != releases.resolve():
            raise VerificationError(f"release previous pointer escapes releases: {previous_target}")

        last_receipt = releases / ".mini-release-last-receipt.json"
        payload = _load_json(last_receipt, "last release receipt")
        digest = _sha256(last_receipt)
        immutable = releases / f".mini-release-receipt-{digest}.json"
        _plain_file(immutable, "content-addressed release receipt")
        if immutable.read_bytes() != last_receipt.read_bytes():
            raise VerificationError("last release receipt differs from its content-addressed receipt")
        if payload.get("schema_version") != 2 or payload.get("event") not in RELEASE_EVENTS:
            raise VerificationError("release receipt has an invalid schema or event")
        if payload.get("runtime_target") != str(target):
            raise VerificationError("release receipt runtime_target does not match runtime-current")
        for field in ("from_commit", "to_commit"):
            value = payload.get(field)
            if not isinstance(value, str) or len(value) not in {40, 64} or any(c not in "0123456789abcdef" for c in value):
                raise VerificationError(f"release receipt {field} is not a full lowercase object id")

    def _verify_fleet_outcomes(self) -> None:
        manifest_path = _safe_join(
            self.source_root, Path("fleet_outcome_manifest.json"), "fleet outcome source manifest"
        )
        manifest = _load_json(manifest_path, "fleet outcome source manifest")
        if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
            raise VerificationError("fleet outcome source manifest is malformed")
        deployed_manifest = _safe_join(
            self.hermes, Path("scripts/fleet_outcome_manifest.json"), "deployed fleet outcome manifest"
        )
        _plain_file(deployed_manifest, "deployed fleet outcome manifest")
        if deployed_manifest.read_bytes() != manifest_path.read_bytes():
            raise VerificationError("deployed fleet outcome manifest differs from source")

        declared_destinations: set[Path] = set()
        for index, entry in enumerate(manifest["files"]):
            if not isinstance(entry, dict):
                raise VerificationError(f"fleet outcome manifest files[{index}] is invalid")
            entry_source_root = entry.get("source_root", "bundle")
            if entry_source_root == "bundle":
                source_base = self.source_root
            elif entry_source_root == "repo":
                source_base = self.repo_root
            else:
                raise VerificationError(
                    f"fleet outcome files[{index}].source_root is not governed: {entry_source_root!r}"
                )
            source = _safe_join(
                source_base,
                _plain_relative(entry.get("source"), f"fleet outcome files[{index}].source"),
                f"fleet outcome source files[{index}]",
            )
            _plain_file(source, f"fleet outcome source files[{index}]")
            expected_hash = entry.get("sha256")
            if _sha256(source) != expected_hash:
                raise VerificationError(f"fleet outcome source hash drift: {source}")
            root = entry.get("destination_root")
            if root == "scripts":
                destination_root = _safe_join(self.hermes, Path("scripts"), "fleet outcome scripts")
            elif root == "launch_agents":
                destination_root = _safe_join(self.home, Path("Library/LaunchAgents"), "fleet outcome LaunchAgents")
            else:
                raise VerificationError(f"fleet outcome destination root is ungoverned: {root!r}")
            destination = _safe_join(
                destination_root,
                _plain_relative(entry.get("destination"), f"fleet outcome files[{index}].destination"),
                f"deployed fleet outcome files[{index}]",
            )
            if destination in declared_destinations:
                raise VerificationError(f"fleet outcome manifest has duplicate destination: {destination}")
            declared_destinations.add(destination)
            _plain_file(destination, f"deployed fleet outcome files[{index}]")
            if destination.read_bytes() != source.read_bytes():
                raise VerificationError(f"fleet outcome deployed bytes drifted: {destination}")
            try:
                mode = int(str(entry.get("mode")), 8)
            except (TypeError, ValueError) as exc:
                raise VerificationError(f"fleet outcome mode is invalid: {destination}") from exc
            if stat.S_IMODE(destination.stat().st_mode) != mode:
                raise VerificationError(f"fleet outcome mode drifted: {destination}")

        jobs_path = _safe_join(self.hermes, Path("cron/jobs.json"), "live cron jobs")
        jobs = _load_json(jobs_path, "live cron jobs")
        live_jobs = _jobs_by_id(jobs.get("jobs"), "live cron jobs")
        updates = manifest.get("cron_updates")
        if not isinstance(updates, list):
            raise VerificationError("fleet outcome cron_updates must be a list")
        update_ids: set[str] = set()
        for update in updates:
            if (
                not isinstance(update, dict)
                or not isinstance(update.get("id"), str)
                or not update["id"]
                or not isinstance(update.get("name"), str)
                or not update["name"]
                or not isinstance(update.get("fields"), dict)
                or not update["fields"]
            ):
                raise VerificationError("fleet outcome cron update is malformed")
            if update["id"] in update_ids:
                raise VerificationError(f"fleet outcome cron update has duplicate job target: {update['id']}")
            update_ids.add(update["id"])
            job = live_jobs.get(update["id"])
            if job is None or job.get("name") != update["name"]:
                raise VerificationError(f"fleet outcome cron job is missing: {update.get('id')!r}")
            for field, expected in update["fields"].items():
                if job.get(field) != expected:
                    raise VerificationError(f"fleet outcome cron field drifted: {update['id']}.{field}")

    def _verify_fleet_config(self) -> None:
        manifest = _load_json(
            _safe_join(self.fleet_root, Path("fleet_config_manifest.json"), "fleet config manifest"),
            "fleet config manifest",
        )
        files = manifest.get("files")
        if not isinstance(files, list):
            raise VerificationError("fleet config manifest has no files list")
        entries: dict[str, dict[str, Any]] = {}
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("src_rel"), str):
                raise VerificationError("fleet config manifest entry is malformed")
            source_rel = _plain_relative(entry["src_rel"], "fleet config src_rel")
            source = _safe_join(self.fleet_root, source_rel, "fleet config source")
            _plain_file(source, "fleet config source")
            if _sha256(source) != entry.get("sha256"):
                raise VerificationError(f"fleet config source hash drift: {source}")
            entries[str(entry.get("deploy_mode"))] = entry

        overlay_entry = entries.get("config_overlay")
        if overlay_entry is None:
            raise VerificationError("fleet config manifest has no config overlay")
        _semantic_overlay_matches(
            _load_yaml(self.hermes / "config.yaml", "live config"),
            _load_yaml(
                _safe_join(
                    self.fleet_root,
                    _plain_relative(overlay_entry["src_rel"], "fleet config overlay src_rel"),
                    "fleet config overlay",
                ),
                "fleet config overlay",
            ),
        )

        for entry in files:
            if entry.get("deploy_mode") != "profile_file":
                continue
            source_rel = _plain_relative(entry["src_rel"], "fleet profile src_rel")
            if not source_rel.parts or source_rel.parts[0] != "profiles":
                raise VerificationError("fleet profile source must be beneath profiles/")
            profiles_root = _safe_join(self.hermes, Path("profiles"), "managed profiles")
            destination = _safe_join(profiles_root, Path(*source_rel.parts[1:]), "managed profile file")
            source = _safe_join(self.fleet_root, source_rel, "fleet profile source")
            _plain_file(destination, "managed profile file")
            if destination.read_bytes() != source.read_bytes():
                raise VerificationError(f"managed profile bytes drifted: {destination}")

        jobs_entry = entries.get("jobs_json")
        if jobs_entry is None:
            raise VerificationError("fleet config manifest has no jobs JSON")
        jobs_helper_entry = entries.get("jobs_payload_helper")
        if jobs_helper_entry is None:
            raise VerificationError("fleet config manifest has no jobs payload helper")
        source_jobs = _materialize_fleet_jobs(
            _safe_join(
                self.fleet_root,
                _plain_relative(jobs_entry["src_rel"], "fleet jobs src_rel"),
                "fleet jobs",
            ),
            _safe_join(
                self.fleet_root,
                _plain_relative(
                    jobs_helper_entry["src_rel"], "fleet jobs payload helper src_rel"
                ),
                "fleet jobs payload helper",
            ),
            jobs_helper_entry.get("sha256"),
        )
        live_jobs = _load_json(
            _safe_join(self.hermes, Path("cron/jobs.json"), "live cron jobs"), "live cron jobs"
        )
        source_by_id = _jobs_by_id(source_jobs.get("jobs"), "fleet jobs")
        live_by_id = _jobs_by_id(live_jobs.get("jobs"), "live cron jobs")
        for job_id, source in source_by_id.items():
            if job_id not in live_by_id:
                raise VerificationError(f"managed cron job missing: {job_id}")
            if _managed_job(live_by_id[job_id]) != _managed_job(source):
                raise VerificationError(f"managed cron job drifted: {job_id}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home(), help="home containing .hermes")
    parser.add_argument("--source-root", type=Path, help="governed mini-scripts source root")
    parser.add_argument("--fleet-root", type=Path, help="fleet bundle root; defaults to the active release when deployed")
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="repo checkout root for source_root=repo manifest entries; defaults to the active release when deployed",
    )
    parser.add_argument(
        "--fixture-safe",
        action="store_true",
        help="declare an isolated fixture run; never consult launchd, git, or external state",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable findings")
    parser.add_argument("--quiet", action="store_true", help="emit nothing on a healthy verification")
    parser.add_argument(
        "--receipt",
        type=Path,
        help="persist the exact outcome (defaults beneath <home>/.hermes/state)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    verifier = GovernedPathsVerifier(
        home=args.home,
        source_root=args.source_root,
        fleet_root=args.fleet_root,
        repo_root=args.repo_root,
        fixture_safe=args.fixture_safe,
    )
    receipt = args.receipt or args.home / ".hermes" / "state" / "governed-paths-verification.json"
    try:
        findings = verifier.verify()
    except VerificationError as exc:
        failure_code = _failure_code(str(exc))
        _atomic_json(
            receipt,
            {
                "schema_version": 1,
                "status": "failed",
                "failure_code": failure_code,
                "failure_reason": str(exc),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if args.json:
            print(json.dumps({"ok": False, "failure_code": failure_code, "error": str(exc)}, sort_keys=True))
        else:
            label = "RUNTIME_CURRENT_BROKEN" if failure_code == "runtime_current_broken" else failure_code.upper()
            print(f"FAIL verify-governed-paths [{label}]: {exc}", file=sys.stderr)
        return 1
    _atomic_json(
        receipt,
        {
            "schema_version": 1,
            "status": "ok",
            "failure_code": None,
            "failure_reason": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    if args.json:
        print(json.dumps({"ok": True, "checks": [item.__dict__ for item in findings]}, sort_keys=True))
    elif not args.quiet:
        for finding in findings:
            print(f"OK {finding.check}: {finding.detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
