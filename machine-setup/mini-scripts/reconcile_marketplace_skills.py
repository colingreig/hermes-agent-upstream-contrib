#!/usr/bin/env python3
"""Transactionally install the Mac mini's canonical external-skill wiring."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
from typing import Any

IGNITE_LABEL = "com.colingreig.ignite-skills-pull"
ANTHROPIC_LABEL = "com.colingreig.pull_anthropic_skills"
STALE_LABEL = "com.ignite.skills-sync"
INDEX_FLOOR = 150
CANONICAL_EXTERNAL_DIRS = (
    "/Users/colingreig/dev/ignite-skills-live/skills",
    "/Users/colingreig/dev/ignite-skills-live/ignite-content/skills",
    "/Users/colingreig/.claude/plugins/marketplaces/anthropic-agent-skills/skills",
    "/Users/colingreig/brain/packs/social-hub",
    "/Users/colingreig/brain/packs/local-seo-brain",
    "/Users/colingreig/brain/packs/marketing-brain",
    "/Users/colingreig/brain/packs/website-brain",
    "/Users/colingreig/dev/ignite-marketplace/plugins/claude-ads",
    "/Users/colingreig/dev/ignite-marketplace/plugins/claude-seo",
    "/Users/colingreig/dev/ignite-marketplace/plugins/claude-blog",
)
SCRIPT_ASSETS = (
    "ignite-skills-pull.sh",
    "pull_anthropic_agent_skills.sh",
    "record_skill_pull_success.py",
    "skill_pull_guard.py",
    "reconcile_marketplace_skills.py",
)
PLIST_ASSETS = {
    IGNITE_LABEL: "com.colingreig.ignite-skills-pull.plist",
    ANTHROPIC_LABEL: "com.colingreig.pull_anthropic_skills.plist",
}
LAUNCHCTL_TIMEOUT_SECONDS = 30
LAUNCHCTL_BOOTSTRAP_EIO = 5
LAUNCHCTL_BOOTSTRAP_ATTEMPTS = 3


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.swap.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def default_source_root(*, script_path: Path, hermes_home: Path) -> Path:
    """Select the canonical source tree without trusting deployed script copies.

    A reconciler invoked from its deployed location must use the immutable
    runtime source rather than treating ``~/.hermes/scripts`` as canonical.
    Development and explicitly copied fixtures continue to use their own
    directory.  The intentional ``runtime-current`` ancestor symlink is
    allowed, but the terminal ``mini-scripts`` directory must exist and must
    not itself be a symlink.
    """
    script_path = script_path.absolute()
    hermes_home = hermes_home.absolute()
    if script_path.parent != hermes_home / "scripts":
        return script_path.parent

    candidate = hermes_home / "runtime-current" / "machine-setup" / "mini-scripts"
    if candidate.is_symlink() or not candidate.is_dir():
        raise RuntimeError(
            f"canonical terminal mini-scripts directory missing or symlinked: {candidate}"
        )
    return candidate


class Reconciler:
    def __init__(
        self,
        *,
        source_root: Path,
        home: Path,
        hermes_home: Path | None = None,
        launch_agents_dir: Path | None = None,
        state_dir: Path | None = None,
        external_dirs: tuple[str, ...] = CANONICAL_EXTERNAL_DIRS,
    ) -> None:
        if source_root.is_symlink() or not source_root.is_dir():
            raise RuntimeError(
                f"canonical terminal source directory missing or symlinked: {source_root}"
            )
        self.source_root = source_root.resolve(strict=True)
        self.home = home.resolve()
        self.hermes_home = (
            hermes_home.resolve() if hermes_home else self.home / ".hermes"
        )
        self.scripts_dir = self.hermes_home / "scripts"
        self.launch_agents_dir = (
            launch_agents_dir.resolve()
            if launch_agents_dir
            else self.home / "Library" / "LaunchAgents"
        )
        self.state_dir = (
            state_dir.resolve()
            if state_dir
            else self.hermes_home / "releases" / "marketplace-skills"
        )
        self.config_path = self.hermes_home / "config.yaml"
        self.external_dirs = external_dirs
        self.stale_plist = self.launch_agents_dir / f"{STALE_LABEL}.plist"

    def source_path(self, name: str) -> Path:
        return self.source_root / name

    def target_map(self) -> dict[Path, tuple[Path | None, int]]:
        targets = {
            self.scripts_dir / name: (self.source_path(name), 0o755)
            for name in SCRIPT_ASSETS
        }
        for label, name in PLIST_ASSETS.items():
            targets[self.launch_agents_dir / f"{label}.plist"] = (
                self.source_root / "launchd" / name,
                0o644,
            )
        targets[self.config_path] = (None, 0o600)
        targets[self.stale_plist] = (None, 0o644)
        targets[self.state_dir / "last-receipt.json"] = (None, 0o644)
        return targets

    def validate_sources(self) -> None:
        for target, (source, _) in self.target_map().items():
            if source is None:
                continue
            try:
                resolved_source = source.resolve(strict=True)
                resolved_source.relative_to(self.source_root)
            except (FileNotFoundError, RuntimeError, ValueError):
                raise RuntimeError(
                    f"canonical source missing, invalid, or outside source root: {source}"
                ) from None
            if source.is_symlink() or not resolved_source.is_file():
                raise RuntimeError(f"canonical source missing or symlinked: {source}")
            if target.suffix == ".plist":
                payload = plistlib.loads(source.read_bytes())
                label = target.stem
                if payload.get("Label") != label:
                    raise RuntimeError(f"plist label mismatch: {source}")
                args = payload.get("ProgramArguments")
                expected_wrapper = (
                    self.scripts_dir / "ignite-skills-pull.sh"
                    if label == IGNITE_LABEL
                    else self.scripts_dir / "pull_anthropic_agent_skills.sh"
                )
                if args != ["/bin/bash", str(expected_wrapper)]:
                    raise RuntimeError(f"plist wrapper mismatch: {source}")

    def validate_external_roots(self) -> None:
        for raw in self.external_dirs:
            path = Path(raw)
            if not path.is_dir():
                raise RuntimeError(f"canonical external skill root missing: {path}")
            resolved = path.resolve(strict=True)
            if resolved != path:
                raise RuntimeError(
                    f"external skill root escapes canonical path: {path} -> {resolved}"
                )

    def _desired_config(self) -> bytes:
        if not self.config_path.is_file() or self.config_path.is_symlink():
            raise RuntimeError(f"config missing or symlinked: {self.config_path}")
        try:
            import yaml

            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise RuntimeError(f"could not parse config: {exc}") from exc
        if not isinstance(config, dict):
            raise RuntimeError("config root must be a mapping")
        skills = config.setdefault("skills", {})
        if not isinstance(skills, dict):
            raise RuntimeError("skills config must be a mapping")
        skills["external_dirs"] = list(self.external_dirs)
        skills["index_floor"] = INDEX_FLOOR
        return yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode()

    def _verify_generated_config(self) -> None:
        """Verify only the config fields this reconciler owns.

        ``config.yaml`` is shared user configuration.  Its unrelated keys and
        skills settings must survive a reconciliation and must not make a
        later verification fail merely because YAML formatting changed.
        """
        try:
            import yaml

            config = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise RuntimeError(f"could not parse config: {exc}") from exc
        if not isinstance(config, dict):
            raise RuntimeError("config root must be a mapping")
        skills = config.get("skills")
        if not isinstance(skills, dict):
            raise RuntimeError("skills config must be a mapping")
        if skills.get("external_dirs") != list(self.external_dirs):
            raise RuntimeError("generated config external_dirs mismatch")
        if skills.get("index_floor") != INDEX_FLOOR:
            raise RuntimeError("generated config index_floor mismatch")

    def desired(self) -> dict[Path, tuple[bytes, int]]:
        desired = {
            target: (source.read_bytes(), mode)
            for target, (source, mode) in self.target_map().items()
            if source is not None
        }
        desired[self.config_path] = (self._desired_config(), 0o600)
        return desired

    def _snapshot(self) -> str:
        files: dict[str, Any] = {}
        for target in sorted(self.target_map(), key=str):
            if target.is_symlink():
                raise RuntimeError(f"refusing symlinked live target: {target}")
            if target.exists():
                if not target.is_file():
                    raise RuntimeError(f"live target is not a regular file: {target}")
                files[str(target)] = {
                    "present": True,
                    "mode": stat.S_IMODE(target.stat().st_mode),
                    "content": base64.b64encode(target.read_bytes()).decode("ascii"),
                }
            else:
                files[str(target)] = {"present": False}
        payload = _json_bytes({"schema_version": 1, "files": files})
        digest = _sha256(payload)
        snapshot = self.state_dir / "snapshots" / f"{digest}.json"
        if not snapshot.exists():
            _atomic_write(snapshot, payload, 0o600)
        elif snapshot.read_bytes() != payload:
            raise RuntimeError("content-addressed snapshot hash collision")
        _atomic_write(self.state_dir / "previous", f"{digest}\n".encode(), 0o600)
        return digest

    def _restore(self, digest: str) -> None:
        snapshot = self.state_dir / "snapshots" / f"{digest}.json"
        payload = snapshot.read_bytes()
        if _sha256(payload) != digest:
            raise RuntimeError("rollback snapshot hash mismatch")
        for raw_target, record in json.loads(payload)["files"].items():
            target = Path(raw_target)
            if target not in self.target_map():
                raise RuntimeError(f"snapshot contains unexpected target: {target}")
            if record["present"]:
                _atomic_write(
                    target,
                    base64.b64decode(record["content"]),
                    int(record["mode"]),
                )
            elif target.exists():
                if target.is_symlink() or not target.is_file():
                    raise RuntimeError(f"refusing to remove non-regular target: {target}")
                target.unlink()

    def _write_receipt(self, desired: dict[Path, tuple[bytes, int]]) -> Path:
        files = []
        for target, (data, mode) in sorted(desired.items(), key=lambda item: str(item[0])):
            source = self.target_map()[target][0]
            source_hash = _sha256(source.read_bytes()) if source else _sha256(data)
            files.append(
                {
                    "target": str(target),
                    "source": str(source) if source else "generated-config",
                    "source_sha256": source_hash,
                    "deployed_sha256": _sha256(target.read_bytes()),
                    "mode": f"{mode:04o}",
                }
            )
        payload = _json_bytes({"schema_version": 1, "files": files})
        digest = _sha256(payload)
        receipt = self.state_dir / "receipts" / f"{digest}.json"
        if not receipt.exists():
            _atomic_write(receipt, payload, 0o644)
        _atomic_write(self.state_dir / "last-receipt.json", payload, 0o644)
        return receipt

    def install(self) -> Path:
        self.validate_sources()
        self.validate_external_roots()
        desired = self.desired()
        snapshot = self._snapshot()
        try:
            for target, (data, mode) in desired.items():
                _atomic_write(target, data, mode)
            if self.stale_plist.exists():
                if self.stale_plist.is_symlink() or not self.stale_plist.is_file():
                    raise RuntimeError(f"refusing stale non-regular plist: {self.stale_plist}")
                self.stale_plist.unlink()
            self.verify(check_receipt=False)
            return self._write_receipt(desired)
        except Exception:
            self._restore(snapshot)
            raise

    def verify(self, *, check_receipt: bool = True) -> None:
        self.validate_sources()
        self.validate_external_roots()
        desired = self.desired()
        for target, (expected, mode) in desired.items():
            if not target.is_file() or target.is_symlink():
                raise RuntimeError(f"deployed target missing or symlinked: {target}")
            if target != self.config_path and target.read_bytes() != expected:
                raise RuntimeError(f"source/deployed identity mismatch: {target}")
            if stat.S_IMODE(target.stat().st_mode) != mode:
                raise RuntimeError(f"deployed mode mismatch: {target}")
        self._verify_generated_config()
        if self.stale_plist.exists():
            raise RuntimeError(f"stale launchd wiring remains: {self.stale_plist}")
        if check_receipt:
            receipt = self.state_dir / "last-receipt.json"
            if not receipt.is_file() or receipt.is_symlink():
                raise RuntimeError("marketplace skill receipt missing or symlinked")
            if stat.S_IMODE(receipt.stat().st_mode) != 0o644:
                raise RuntimeError("marketplace skill receipt mode mismatch")
            try:
                payload = json.loads(receipt.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"marketplace skill receipt malformed: {exc}") from exc
            files = payload.get("files") if isinstance(payload, dict) else None
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or not isinstance(files, list)
            ):
                raise RuntimeError("marketplace skill receipt malformed")
            records: dict[str, dict[str, Any]] = {}
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("target"), str):
                    raise RuntimeError("marketplace skill receipt malformed")
                target_name = item["target"]
                if target_name in records:
                    raise RuntimeError(f"duplicate receipt entry: {target_name}")
                records[target_name] = item
            expected_targets = {str(target) for target in desired}
            if set(records) != expected_targets:
                raise RuntimeError("marketplace skill receipt target set mismatch")
            for target, (data, mode) in desired.items():
                record = records[str(target)]
                if record.get("mode") != f"{mode:04o}":
                    raise RuntimeError(f"receipt mode mismatch: {target}")
                if target == self.config_path:
                    if record.get("source") != "generated-config":
                        raise RuntimeError(f"receipt source mismatch: {target}")
                    continue
                source = self.target_map()[target][0]
                if source is None or record.get("source") != str(source):
                    raise RuntimeError(f"receipt source mismatch: {target}")
                if record.get("source_sha256") != _sha256(data):
                    raise RuntimeError(f"receipt source hash mismatch: {target}")
                if record.get("deployed_sha256") != _sha256(target.read_bytes()):
                    raise RuntimeError(f"receipt deployed hash mismatch: {target}")

    def rollback(self) -> None:
        pointer = self.state_dir / "previous"
        if not pointer.is_file():
            raise RuntimeError("no marketplace skill rollback snapshot")
        digest = pointer.read_text(encoding="utf-8").strip()
        if len(digest) != 64:
            raise RuntimeError("invalid marketplace skill rollback pointer")
        self._restore(digest)

    @staticmethod
    def _bootout(domain: str, label: str) -> None:
        subprocess.run(
            ["launchctl", "bootout", f"{domain}/{label}"],
            check=False,
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def _bootstrap(self, domain: str, label: str, plist: Path) -> None:
        last_error: BaseException | None = None
        for attempt in range(1, LAUNCHCTL_BOOTSTRAP_ATTEMPTS + 1):
            try:
                subprocess.run(
                    ["launchctl", "bootstrap", domain, str(plist)],
                    check=True,
                    timeout=LAUNCHCTL_TIMEOUT_SECONDS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                last_error = None
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if exc.returncode == LAUNCHCTL_BOOTSTRAP_EIO:
                    self._bootout(domain, label)
            except (subprocess.TimeoutExpired, OSError) as exc:
                last_error = exc
            if self._registered(domain, label):
                return
            if attempt < LAUNCHCTL_BOOTSTRAP_ATTEMPTS:
                self._bootout(domain, label)
        message = f"launchctl failed to register {domain}/{label}"
        if last_error is not None:
            raise RuntimeError(message) from last_error
        raise RuntimeError(message)

    def reload(self) -> None:
        domain = f"gui/{os.getuid()}"
        for label in (IGNITE_LABEL, ANTHROPIC_LABEL, STALE_LABEL):
            self._bootout(domain, label)
        for label in (IGNITE_LABEL, ANTHROPIC_LABEL):
            plist = self.launch_agents_dir / f"{label}.plist"
            if plist.is_file():
                self._bootstrap(domain, label, plist)
        # Rollback may have restored the retired job.
        if self.stale_plist.is_file():
            self._bootstrap(domain, STALE_LABEL, self.stale_plist)

    def install_and_reload(self) -> Path:
        receipt = self.install()
        try:
            self.reload()
        except Exception:
            self.rollback()
            self.reload()
            raise
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "verify", "rollback"))
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--launch-agents-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    hermes_home = args.hermes_home or Path(
        os.environ.get("HERMES_HOME", args.home / ".hermes")
    )
    source_root = args.source_root or default_source_root(
        script_path=Path(__file__), hermes_home=hermes_home
    )
    reconciler = Reconciler(
        source_root=source_root,
        home=args.home,
        hermes_home=hermes_home,
        launch_agents_dir=args.launch_agents_dir,
        state_dir=args.state_dir,
    )
    try:
        if args.action == "install":
            receipt = (
                reconciler.install_and_reload() if args.reload else reconciler.install()
            )
            print(f"marketplace skills installed receipt={receipt.name}")
        elif args.action == "verify":
            reconciler.verify()
            print("marketplace skills verified")
        else:
            reconciler.rollback()
            if args.reload:
                reconciler.reload()
            print("marketplace skills rolled back")
    except Exception as exc:
        print(f"reconcile_marketplace_skills: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
