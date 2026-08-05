#!/usr/bin/env python3
"""Install, verify, and roll back the Mini fleet-outcome monitor bundle."""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
import warnings
from xml.parsers.expat import ExpatError


SCHEMA_VERSION = 1
LOCK_TIMEOUT_SECONDS = 30.0
LAUNCHCTL_TIMEOUT_SECONDS = 15
LAUNCHCTL_STATE_ATTEMPTS = 20
LAUNCHCTL_STATE_INTERVAL_SECONDS = 0.1
LAUNCHCTL_BOOTSTRAP_ATTEMPTS = 3
LAUNCHCTL_TRANSIENT_BOOTSTRAP_CODES = {5, 37}


class ReconcileError(RuntimeError):
    """The governed fleet-outcome deployment could not be proven safe."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ReconcileError(f"refusing symlinked target: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def _jobs_lock(jobs_path: Path):
    lock_path = jobs_path.parent / ".jobs.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ReconcileError(
                        f"timed out waiting for cron jobs lock {lock_path}"
                    ) from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Reconciler:
    def __init__(
        self,
        *,
        source_root: Path,
        manifest_path: Path,
        home: Path,
        hermes_home: Path | None = None,
        launch_agents_dir: Path | None = None,
        state_dir: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.source_root = source_root.resolve()
        # Most governed files are vendored directly under source_root
        # (machine-setup/mini-scripts). A few files (e.g. scripts/
        # clickup_poll_gate.py) have their single canonical source at the
        # repo root instead, so entries may opt in with
        # "source_root": "repo" rather than duplicating the file into
        # mini-scripts and risking silent fork drift. Defaults to
        # source_root so existing bundle-relative entries are unaffected.
        self.repo_root = repo_root.resolve() if repo_root is not None else self.source_root
        self.manifest_path = manifest_path.resolve()
        self.home = home.resolve()
        self.hermes_home = (
            hermes_home.resolve() if hermes_home else self.home / ".hermes"
        )
        self.scripts_dir = self.hermes_home / "scripts"
        self.jobs_path = self.hermes_home / "cron" / "jobs.json"
        self.launch_agents_dir = (
            launch_agents_dir.resolve()
            if launch_agents_dir
            else self.home / "Library" / "LaunchAgents"
        )
        self.state_dir = (
            state_dir.resolve()
            if state_dir
            else self.hermes_home / "releases" / "fleet-outcomes"
        )
        self.manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.is_file() or self.manifest_path.is_symlink():
            raise ReconcileError(
                f"manifest missing or symlinked: {self.manifest_path}"
            )
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconcileError(f"could not parse manifest: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise ReconcileError("manifest must be a schema_version=1 object")
        if not isinstance(value.get("files"), list) or not value["files"]:
            raise ReconcileError("manifest files must be a non-empty list")
        if not isinstance(value.get("cron_updates"), list):
            raise ReconcileError("manifest cron_updates must be a list")
        return value

    @staticmethod
    def _plain_relative(value: object, *, field: str) -> Path:
        if not isinstance(value, str) or not value:
            raise ReconcileError(f"{field} must be a non-empty relative path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ReconcileError(f"{field} must not be absolute or contain '..'")
        return path

    def _source_base(self, entry: dict[str, Any], *, index: int) -> Path:
        source_root_field = entry.get("source_root", "bundle")
        if source_root_field == "bundle":
            return self.source_root
        if source_root_field == "repo":
            return self.repo_root
        raise ReconcileError(f"files[{index}].source_root is not governed")

    def target_map(self) -> dict[Path, tuple[Path, int]]:
        targets: dict[Path, tuple[Path, int]] = {
            self.scripts_dir / "fleet_outcome_manifest.json": (
                self.manifest_path,
                0o644,
            )
        }
        for index, entry in enumerate(self.manifest["files"]):
            if not isinstance(entry, dict):
                raise ReconcileError(f"manifest files[{index}] must be an object")
            source_rel = self._plain_relative(
                entry.get("source"), field=f"files[{index}].source"
            )
            destination_rel = self._plain_relative(
                entry.get("destination"), field=f"files[{index}].destination"
            )
            destination_root = entry.get("destination_root")
            if destination_root == "scripts":
                root = self.scripts_dir
            elif destination_root == "launch_agents":
                root = self.launch_agents_dir
            else:
                raise ReconcileError(
                    f"files[{index}].destination_root is not governed"
                )
            source = self._source_base(entry, index=index) / source_rel
            target = root / destination_rel
            if target in targets:
                raise ReconcileError(f"duplicate manifest destination: {target}")
            try:
                mode = int(str(entry.get("mode")), 8)
            except (TypeError, ValueError) as exc:
                raise ReconcileError(f"files[{index}].mode is invalid") from exc
            if mode not in {0o644, 0o755}:
                raise ReconcileError(f"files[{index}].mode is not allowed")
            targets[target] = (source, mode)
        return targets

    def validate_sources(self) -> None:
        expected_by_source: dict[Path, str] = {}
        for index, entry in enumerate(self.manifest["files"]):
            source = self._source_base(entry, index=index) / self._plain_relative(
                entry.get("source"), field=f"files[{index}].source"
            )
            if not source.is_file() or source.is_symlink():
                raise ReconcileError(f"source missing or symlinked: {source}")
            expected = entry.get("sha256")
            if (
                not isinstance(expected, str)
                or len(expected) != 64
                or any(char not in "0123456789abcdef" for char in expected)
            ):
                raise ReconcileError(f"files[{index}].sha256 is invalid")
            actual = _sha256_path(source)
            if actual != expected:
                raise ReconcileError(
                    f"source hash drift for {source}: {actual} != {expected}"
                )
            expected_by_source[source] = expected
        if len(expected_by_source) != len(self.manifest["files"]):
            raise ReconcileError("manifest contains a duplicate source")

    def desired(self) -> dict[Path, tuple[bytes, int]]:
        return {
            target: (source.read_bytes(), mode)
            for target, (source, mode) in self.target_map().items()
        }

    def _read_jobs(self) -> dict[str, Any]:
        try:
            document = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReconcileError(f"cron jobs unreadable: {exc}") from exc
        jobs = document.get("jobs") if isinstance(document, dict) else None
        if not isinstance(jobs, list) or not all(
            isinstance(job, dict) for job in jobs
        ):
            raise ReconcileError("cron jobs document lacks an object list")
        return document

    def _desired_jobs(self, document: dict[str, Any]) -> dict[str, Any]:
        jobs = list(document["jobs"])
        for update in self.manifest["cron_updates"]:
            if not isinstance(update, dict):
                raise ReconcileError("cron update must be an object")
            job_id = str(update.get("id") or "")
            name = update.get("name")
            fields = update.get("fields")
            matches = [
                index
                for index, job in enumerate(jobs)
                if str(job.get("id") or "") == job_id
            ]
            if len(matches) != 1:
                raise ReconcileError(
                    f"cron update {job_id} matched {len(matches)} jobs"
                )
            if jobs[matches[0]].get("name") != name:
                raise ReconcileError(f"cron update {job_id} name mismatch")
            if not isinstance(fields, dict) or not fields:
                raise ReconcileError(f"cron update {job_id} fields are invalid")
            repaired = dict(jobs[matches[0]])
            repaired.update(fields)
            jobs[matches[0]] = repaired
        result = dict(document)
        result["jobs"] = jobs
        return result

    @staticmethod
    def _launchd_domains() -> tuple[str, str]:
        uid = os.getuid()
        return f"gui/{uid}", f"user/{uid}"

    @staticmethod
    def _bootout_domain(domain: str, label: str) -> None:
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            check=False,
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @classmethod
    def _bootout(cls, label: str) -> None:
        for domain in cls._launchd_domains():
            cls._bootout_domain(domain, label)

    @staticmethod
    def _registered(domain: str, label: str) -> bool:
        try:
            result = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                check=False,
                timeout=LAUNCHCTL_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    @staticmethod
    def _domain_available(domain: str) -> bool:
        try:
            result = subprocess.run(
                ["launchctl", "print", domain],
                check=False,
                timeout=LAUNCHCTL_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def _is_loaded(self, label: str) -> bool:
        return any(
            self._registered(domain, label) for domain in self._launchd_domains()
        )

    def _duplicate_loaded(self, label: str) -> bool:
        return (
            sum(
                1
                for domain in self._launchd_domains()
                if self._registered(domain, label)
            )
            > 1
        )

    def _resolve_launchd_domain(self, label: str) -> str:
        """Return the owning domain, preferring an available GUI session."""
        gui_domain, user_domain = self._launchd_domains()
        if self._registered(gui_domain, label):
            return gui_domain
        if self._registered(user_domain, label):
            return user_domain
        # An SSH caller reports a Background manager even while the logged-in
        # GUI domain exists, so probe the domain instead of the caller session.
        if self._domain_available(gui_domain):
            return gui_domain
        return user_domain

    def _wait_registered(self, domain: str, label: str, expected: bool) -> bool:
        for attempt in range(LAUNCHCTL_STATE_ATTEMPTS):
            if self._registered(domain, label) is expected:
                return True
            if attempt + 1 < LAUNCHCTL_STATE_ATTEMPTS:
                time.sleep(LAUNCHCTL_STATE_INTERVAL_SECONDS)
        return False

    def _labels(self) -> list[str]:
        labels: list[str] = []
        for target, (source, _mode) in self.target_map().items():
            if target.parent != self.launch_agents_dir:
                continue
            try:
                payload = plistlib.loads(source.read_bytes())
            except (OSError, ValueError, ExpatError) as exc:
                warnings.warn(
                    f"skipped unreadable plist {source.name}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            label = payload.get("Label")
            if not isinstance(label, str) or not label:
                raise ReconcileError(f"plist has no Label: {target}")
            labels.append(label)
        return sorted(labels)

    def _snapshot(self) -> str:
        files: dict[str, Any] = {}
        for target in sorted(self.target_map(), key=str):
            if target.is_symlink():
                raise ReconcileError(f"refusing symlinked live target: {target}")
            if target.exists():
                if not target.is_file():
                    raise ReconcileError(f"live target is not a file: {target}")
                files[str(target)] = {
                    "present": True,
                    "mode": stat.S_IMODE(target.stat().st_mode),
                    "content": base64.b64encode(target.read_bytes()).decode("ascii"),
                }
            else:
                files[str(target)] = {"present": False}
        with _jobs_lock(self.jobs_path):
            jobs_bytes = self.jobs_path.read_bytes()
            self._read_jobs()
        loaded = {label: self._is_loaded(label) for label in self._labels()}
        payload = _json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "files": files,
                "jobs": base64.b64encode(jobs_bytes).decode("ascii"),
                "loaded": loaded,
            }
        )
        digest = _sha256_bytes(payload)
        snapshot = self.state_dir / "snapshots" / f"{digest}.json"
        if snapshot.exists():
            if snapshot.read_bytes() != payload:
                raise ReconcileError("content-addressed snapshot collision")
        else:
            _atomic_write(snapshot, payload, 0o600)
        _atomic_write(self.state_dir / "previous", f"{digest}\n".encode(), 0o600)
        return digest

    def _load_snapshot(self, digest: str) -> dict[str, Any]:
        snapshot = self.state_dir / "snapshots" / f"{digest}.json"
        payload = snapshot.read_bytes()
        if _sha256_bytes(payload) != digest:
            raise ReconcileError("rollback snapshot hash mismatch")
        value = json.loads(payload)
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ReconcileError("rollback snapshot schema mismatch")
        return value

    def _restore(self, digest: str) -> dict[str, bool]:
        value = self._load_snapshot(digest)
        allowed = self.target_map()
        for raw_target, record in value["files"].items():
            target = Path(raw_target)
            if target not in allowed:
                raise ReconcileError(f"snapshot contains unexpected target: {target}")
            if record["present"]:
                _atomic_write(
                    target,
                    base64.b64decode(record["content"]),
                    int(record["mode"]),
                )
            elif target.exists():
                if target.is_symlink() or not target.is_file():
                    raise ReconcileError(f"refusing to remove target: {target}")
                target.unlink()
        with _jobs_lock(self.jobs_path):
            _atomic_write(
                self.jobs_path,
                base64.b64decode(value["jobs"]),
                0o600,
            )
        return {
            str(label): bool(loaded)
            for label, loaded in dict(value.get("loaded") or {}).items()
        }

    def _set_loaded(self, loaded: dict[str, bool]) -> None:
        plist_by_label: dict[str, Path] = {}
        for target in self.target_map():
            if target.parent != self.launch_agents_dir or not target.exists():
                continue
            try:
                payload = plistlib.loads(target.read_bytes())
            except (OSError, ValueError, ExpatError) as exc:
                warnings.warn(
                    f"skipped unreadable plist {target.name}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            plist_by_label[str(payload["Label"])] = target
        for label, should_load in sorted(loaded.items()):
            # Bootout erases the strongest ownership signal; retain it first.
            selected_domain = (
                self._resolve_launchd_domain(label) if should_load else None
            )
            self._bootout(label)
            for domain in self._launchd_domains():
                if not self._wait_registered(domain, label, False):
                    raise ReconcileError(
                        f"launchctl did not unload {label} from {domain}"
                    )
            if not should_load:
                continue
            plist = plist_by_label.get(label)
            if plist is None:
                raise ReconcileError(f"cannot reload {label}: plist is absent")
            if selected_domain is None:
                raise ReconcileError(f"cannot resolve launchd domain for {label}")
            last_error: BaseException | None = None
            for attempt in range(1, LAUNCHCTL_BOOTSTRAP_ATTEMPTS + 1):
                try:
                    subprocess.run(
                        [
                            "launchctl",
                            "bootstrap",
                            selected_domain,
                            str(plist),
                        ],
                        check=True,
                        timeout=LAUNCHCTL_TIMEOUT_SECONDS,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except subprocess.CalledProcessError as exc:
                    last_error = exc
                    if exc.returncode not in LAUNCHCTL_TRANSIENT_BOOTSTRAP_CODES:
                        break
                except (OSError, subprocess.TimeoutExpired) as exc:
                    last_error = exc
                else:
                    if self._wait_registered(selected_domain, label, True):
                        last_error = None
                        break
                    last_error = ReconcileError(
                        f"launchctl did not register {label} in {selected_domain}"
                    )
                if attempt < LAUNCHCTL_BOOTSTRAP_ATTEMPTS:
                    self._bootout(label)
                    for retry_domain in self._launchd_domains():
                        if not self._wait_registered(retry_domain, label, False):
                            raise ReconcileError(
                                f"launchctl did not reset {label} for retry"
                            )
            if last_error is not None:
                raise ReconcileError(f"launchctl could not load {label}") from last_error

    def verify(self, *, require_loaded: bool = False) -> None:
        self.validate_sources()
        for target, (expected, mode) in self.desired().items():
            if not target.is_file() or target.is_symlink():
                raise ReconcileError(f"deployed target missing or symlinked: {target}")
            if target.read_bytes() != expected:
                raise ReconcileError(f"source/deployed mismatch: {target}")
            if stat.S_IMODE(target.stat().st_mode) != mode:
                raise ReconcileError(f"deployed mode mismatch: {target}")
        with _jobs_lock(self.jobs_path):
            actual = self._read_jobs()
            expected = self._desired_jobs(actual)
            if actual != expected:
                raise ReconcileError("governed cron fields drifted")
        if require_loaded:
            duplicates = [
                label for label in self._labels() if self._duplicate_loaded(label)
            ]
            if duplicates:
                raise ReconcileError(
                    "governed LaunchAgents are loaded in duplicate domains: "
                    + ", ".join(duplicates)
                )
            missing = [label for label in self._labels() if not self._is_loaded(label)]
            if missing:
                raise ReconcileError(
                    "governed LaunchAgents are not loaded: " + ", ".join(missing)
                )

    def _write_receipt(self, snapshot: str) -> Path:
        files = []
        for target, (source, mode) in sorted(
            self.target_map().items(), key=lambda item: str(item[0])
        ):
            files.append(
                {
                    "source": str(source),
                    "target": str(target),
                    "sha256": _sha256_path(target),
                    "mode": f"{mode:04o}",
                }
            )
        payload = _json_bytes(
            {
                "schema_version": SCHEMA_VERSION,
                "manifest_sha256": _sha256_path(self.manifest_path),
                "snapshot": snapshot,
                "files": files,
                "cron_updates": self.manifest["cron_updates"],
                "loaded": self._labels(),
            }
        )
        digest = _sha256_bytes(payload)
        receipt = self.state_dir / "receipts" / f"{digest}.json"
        _atomic_write(receipt, payload, 0o644)
        _atomic_write(self.state_dir / "last-receipt.json", payload, 0o644)
        return receipt

    def install(self, *, reload: bool = False) -> Path:
        self.validate_sources()
        desired = self.desired()
        snapshot = self._snapshot()
        desired_loaded = {label: True for label in self._labels()}
        try:
            for target, (data, mode) in desired.items():
                _atomic_write(target, data, mode)
            with _jobs_lock(self.jobs_path):
                document = self._read_jobs()
                _atomic_write(
                    self.jobs_path,
                    _json_bytes(self._desired_jobs(document)),
                    0o600,
                )
            self.verify()
            if reload:
                self._set_loaded(desired_loaded)
                self.verify(require_loaded=True)
            return self._write_receipt(snapshot)
        except BaseException:
            loaded = self._restore(snapshot)
            if reload:
                self._set_loaded(loaded)
            raise

    def rollback(self, *, reload: bool = False) -> None:
        pointer = self.state_dir / "previous"
        if not pointer.is_file() or pointer.is_symlink():
            raise ReconcileError("no fleet-outcome rollback snapshot")
        digest = pointer.read_text(encoding="utf-8").strip()
        if len(digest) != 64:
            raise ReconcileError("invalid fleet-outcome rollback pointer")
        loaded = self._restore(digest)
        if reload:
            self._set_loaded(loaded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "verify", "rollback"))
    source_root = Path(__file__).resolve().parent
    parser.add_argument("--source-root", type=Path, default=source_root)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=source_root / "fleet_outcome_manifest.json",
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--launch-agents-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Repo checkout root for files whose manifest entry sets "
            "source_root=repo (e.g. scripts/clickup_poll_gate.py). "
            "Defaults to --source-root when omitted."
        ),
    )
    parser.add_argument("--reload", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        reconciler = Reconciler(
            source_root=args.source_root,
            manifest_path=args.manifest,
            home=args.home,
            hermes_home=args.hermes_home,
            launch_agents_dir=args.launch_agents_dir,
            state_dir=args.state_dir,
            repo_root=args.repo_root,
        )
        if args.action == "install":
            receipt = reconciler.install(reload=args.reload)
            print(f"fleet outcomes installed receipt={receipt.name}")
        elif args.action == "verify":
            reconciler.verify(require_loaded=args.reload)
            print("fleet outcomes verified")
        else:
            reconciler.rollback(reload=args.reload)
            print("fleet outcomes rolled back")
    except Exception as exc:
        print(f"reconcile_fleet_outcomes: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
