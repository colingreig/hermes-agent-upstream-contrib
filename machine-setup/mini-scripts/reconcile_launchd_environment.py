#!/usr/bin/env python3
"""Install and verify the canonical Hermes launchd environment transactionally."""
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
import time
from typing import Any

SCRIPT_ASSETS = (
    "reconcile_launchd_environment.py",
    "gateway_secrets_wrap.sh",
    "dashboard_secrets_wrap.sh",
    "gateway_launch_inner.sh",
    "github_app_token.py",
    "op_sdk_resolve.py",
)
REFERENCE_SOURCE = "launchd-secrets.op-env.template"
REFERENCE_TARGET = "launchd-secrets.op-env"
COMPREHENSIVE_REFERENCE_SOURCE = "op-secrets.env"
GATEWAY_LABEL = "ai.hermes.gateway"
DASHBOARD_LABEL = "com.colingreig.hermes-dashboard"
EXPECTED_REFERENCE_KEYS = {
    "GH_APP_PRIVATE_KEY",
    "GH_APP_ID",
    "GH_APP_INSTALLATION_ID",
    "OPENAI_API_KEY_HERMES",
}
LAUNCHCTL_BOOTSTRAP_EIO = 5
LAUNCHCTL_BOOTSTRAP_IN_PROGRESS = 37
LAUNCHCTL_TRANSIENT_BOOTSTRAP_CODES = {
    LAUNCHCTL_BOOTSTRAP_EIO,
    LAUNCHCTL_BOOTSTRAP_IN_PROGRESS,
}
LAUNCHCTL_BOOTSTRAP_ATTEMPTS = 3
LAUNCHCTL_TIMEOUT_SECONDS = 30
LAUNCHCTL_STATE_POLL_ATTEMPTS = 60
LAUNCHCTL_STATE_POLL_INTERVAL_SECONDS = 0.1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.swap.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class Reconciler:
    def __init__(
        self,
        *,
        source_root: Path,
        home: Path,
        hermes_home: Path | None = None,
        launch_agents_dir: Path | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self.source_root = source_root.resolve()
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
            else self.hermes_home / "releases" / "launchd-environment"
        )
        self.gateway_wrapper = self.scripts_dir / "gateway_secrets_wrap.sh"
        self.dashboard_wrapper = self.scripts_dir / "dashboard_secrets_wrap.sh"
        self.reference_target = self.scripts_dir / REFERENCE_TARGET
        self.comprehensive_reference_source = (
            self.scripts_dir / COMPREHENSIVE_REFERENCE_SOURCE
        )
        self.gateway_plist = self.launch_agents_dir / f"{GATEWAY_LABEL}.plist"
        self.dashboard_plist = self.launch_agents_dir / f"{DASHBOARD_LABEL}.plist"

    def source_path(self, name: str) -> Path:
        return self.source_root / name

    def target_map(self) -> dict[Path, tuple[Path | None, int]]:
        result = {
            self.scripts_dir / name: (self.source_path(name), 0o755)
            for name in SCRIPT_ASSETS
        }
        # Keep the canonical template alongside the rendered launchd reference
        # file so an installed scripts directory is itself a valid source root
        # for verification and recovery.  It is deliberately data, not an
        # executable asset.
        result[self.scripts_dir / REFERENCE_SOURCE] = (
            self.source_path(REFERENCE_SOURCE),
            0o600,
        )
        result[self.reference_target] = (
            self.source_path(REFERENCE_SOURCE),
            0o600,
        )
        result[self.gateway_plist] = (None, 0o644)
        result[self.dashboard_plist] = (None, 0o644)
        return result

    @staticmethod
    def _parse_references(path: Path) -> dict[str, str]:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"secret reference file missing or symlinked: {path}")
        refs: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise RuntimeError(f"secret reference file contains malformed line: {path}")
            key, value = (part.strip() for part in line.split("=", 1))
            if (
                not key
                or not (key[0].isalpha() or key[0] == "_")
                or not all(char.isalnum() or char == "_" for char in key)
            ):
                raise RuntimeError(f"secret reference file contains invalid key: {path}")
            if key in refs:
                raise RuntimeError(
                    f"secret reference file contains duplicate key {key}: {path}"
                )
            if not value.startswith("op://"):
                raise RuntimeError(f"secret reference file contains a value for {key}")
            refs[key] = value
        return refs

    def _reference_inventory(self) -> bytes:
        required = self._parse_references(self.source_path(REFERENCE_SOURCE))
        refs: dict[str, str] = {}
        if (
            self.comprehensive_reference_source.exists()
            or self.comprehensive_reference_source.is_symlink()
        ):
            refs.update(self._parse_references(self.comprehensive_reference_source))
        # Source-controlled launch requirements always win over an older
        # comprehensive manifest while every other validated reference is
        # retained for configured gateway platforms and integrations.
        refs.update(required)
        return "".join(f"{key}={refs[key]}\n" for key in sorted(refs)).encode()

    def validate_sources(self) -> None:
        for name in (*SCRIPT_ASSETS, REFERENCE_SOURCE):
            source = self.source_path(name)
            if not source.is_file() or source.is_symlink():
                raise RuntimeError(f"canonical source missing or symlinked: {source}")
        refs = self._parse_references(self.source_path(REFERENCE_SOURCE))
        if set(refs) != EXPECTED_REFERENCE_KEYS:
            raise RuntimeError(
                "secret reference template keys differ from canonical contract"
            )
        if (
            self.comprehensive_reference_source.exists()
            or self.comprehensive_reference_source.is_symlink()
        ):
            self._parse_references(self.comprehensive_reference_source)

    def validate_gateway_config(self) -> None:
        config_path = self.hermes_home / "config.yaml"
        if not config_path.is_file():
            raise RuntimeError(f"gateway config missing: {config_path}")
        try:
            import yaml

            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise RuntimeError(f"could not parse gateway config: {exc}") from exc
        configured = (config.get("gateway") or {}).get("launchd_secrets_wrapper")
        if configured != str(self.gateway_wrapper):
            raise RuntimeError(
                "gateway.launchd_secrets_wrapper must equal "
                f"{self.gateway_wrapper}; refusing a config mutation"
            )

    def _plist(self, *, label: str, wrapper: Path) -> bytes:
        environment = {
            "HERMES_HOME": str(self.hermes_home),
            "PATH": (
                f"{self.home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:"
                "/usr/bin:/bin:/usr/sbin:/sbin"
            ),
            "VIRTUAL_ENV": str(self.hermes_home / "runtime-current" / "venv"),
        }
        log_stem = "gateway" if label == GATEWAY_LABEL else "dashboard"
        payload = {
            "Label": label,
            "ProgramArguments": ["/bin/bash", str(wrapper)],
            "WorkingDirectory": str(self.hermes_home / "runtime-current"),
            "EnvironmentVariables": environment,
            "RunAtLoad": True,
            # A permanent-auth failure is mapped by the wrapper to exit 0,
            # parking the job. Transient failures remain nonzero and retry.
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 30,
            "StandardOutPath": str(self.hermes_home / "logs" / f"{log_stem}.log"),
            "StandardErrorPath": str(
                self.hermes_home / "logs" / f"{log_stem}.error.log"
            ),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    def desired(self) -> dict[Path, tuple[bytes, int]]:
        desired = {
            target: (source.read_bytes(), mode)
            for target, (source, mode) in self.target_map().items()
            if source is not None
        }
        desired[self.gateway_plist] = (
            self._plist(label=GATEWAY_LABEL, wrapper=self.gateway_wrapper),
            0o644,
        )
        desired[self.dashboard_plist] = (
            self._plist(label=DASHBOARD_LABEL, wrapper=self.dashboard_wrapper),
            0o644,
        )
        desired[self.reference_target] = (self._reference_inventory(), 0o600)
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
        snapshots = self.state_dir / "snapshots"
        snapshot = snapshots / f"{digest}.json"
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
        files = json.loads(payload)["files"]
        for raw_target, record in files.items():
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
        entries = []
        for target, (data, mode) in sorted(desired.items(), key=lambda item: str(item[0])):
            source = self.target_map()[target][0]
            entries.append(
                {
                    "target": str(target),
                    "source": str(source) if source else "generated-plist",
                    "source_sha256": _sha256(data),
                    "deployed_sha256": _sha256(target.read_bytes()),
                    "mode": f"{mode:04o}",
                }
            )
        payload = _json_bytes({"schema_version": 1, "files": entries})
        digest = _sha256(payload)
        receipt = self.state_dir / "receipts" / f"{digest}.json"
        if not receipt.exists():
            _atomic_write(receipt, payload, 0o644)
        _atomic_write(self.state_dir / "last-receipt.json", payload, 0o644)
        return receipt

    def install(self) -> Path:
        self.validate_sources()
        self.validate_gateway_config()
        desired = self.desired()
        snapshot = self._snapshot()
        try:
            for target, (data, mode) in desired.items():
                _atomic_write(target, data, mode)
            self.verify()
            return self._write_receipt(desired)
        except Exception:
            self._restore(snapshot)
            raise

    def install_and_reload(self) -> Path:
        """Install, reload, and restore the exact snapshot if reload fails."""
        receipt = self.install()
        try:
            self.reload()
        except Exception:
            self.rollback()
            self.reload()
            raise
        return receipt

    def verify(self) -> None:
        self.validate_sources()
        self.validate_gateway_config()
        desired = self.desired()
        for target, (expected, mode) in desired.items():
            if not target.is_file() or target.is_symlink():
                raise RuntimeError(f"deployed target missing or symlinked: {target}")
            if target.read_bytes() != expected:
                raise RuntimeError(f"source/deployed identity mismatch: {target}")
            if stat.S_IMODE(target.stat().st_mode) != mode:
                raise RuntimeError(f"deployed mode mismatch: {target}")
        for plist_path, wrapper in (
            (self.gateway_plist, self.gateway_wrapper),
            (self.dashboard_plist, self.dashboard_wrapper),
        ):
            plist = plistlib.loads(plist_path.read_bytes())
            if plist["ProgramArguments"] != ["/bin/bash", str(wrapper)]:
                raise RuntimeError(f"plist does not point only to canonical wrapper: {plist_path}")
            if plist.get("KeepAlive") != {"SuccessfulExit": False}:
                raise RuntimeError(f"plist retry contract mismatch: {plist_path}")
            serialized = plist_path.read_text(encoding="utf-8")
            forbidden = (
                "github_app_token.py",
                "GH_TOKEN",
                "OPENAI_API_KEY",
                "VALIDATOR_LOW_CHAIN",
                "op://",
            )
            if any(item in serialized for item in forbidden):
                raise RuntimeError(f"plist contains inline secret/mint logic: {plist_path}")

    def rollback(self) -> None:
        pointer = self.state_dir / "previous"
        if not pointer.is_file():
            raise RuntimeError("no launchd environment rollback snapshot")
        digest = pointer.read_text(encoding="utf-8").strip()
        if len(digest) != 64:
            raise RuntimeError("invalid launchd rollback pointer")
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

    def _wait_for_registration_state(
        self,
        domain: str,
        label: str,
        *,
        registered: bool,
        attempts: int = LAUNCHCTL_STATE_POLL_ATTEMPTS,
    ) -> bool:
        if attempts < 1:
            raise ValueError("launchctl state poll attempts must be positive")
        for attempt in range(1, attempts + 1):
            if self._registered(domain, label) is registered:
                return True
            if attempt < attempts:
                time.sleep(LAUNCHCTL_STATE_POLL_INTERVAL_SECONDS)
        return False

    def _wait_until_unregistered(self, domain: str, label: str) -> None:
        if not self._wait_for_registration_state(
            domain,
            label,
            registered=False,
        ):
            raise RuntimeError(
                f"launchctl did not unregister {domain}/{label} after bounded wait"
            )

    def _wait_until_registered(self, domain: str, label: str) -> bool:
        return self._wait_for_registration_state(
            domain,
            label,
            registered=True,
        )

    def _bootstrap_until_registered(
        self,
        domain: str,
        label: str,
        plist: Path,
        *,
        attempts: int = LAUNCHCTL_BOOTSTRAP_ATTEMPTS,
    ) -> None:
        """Bound bootstrap retries and prove this generation registered."""
        if attempts < 1:
            raise ValueError("launchctl bootstrap attempts must be positive")
        last_error: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                subprocess.run(
                    ["launchctl", "bootstrap", domain, str(plist)],
                    check=True,
                    timeout=LAUNCHCTL_TIMEOUT_SECONDS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if exc.returncode not in LAUNCHCTL_TRANSIENT_BOOTSTRAP_CODES:
                    break
                # EIO and EINPROGRESS both occur while launchd is still
                # tearing down the prior generation. The bounded cleanup
                # below prepares the next attempt.
            except (subprocess.TimeoutExpired, OSError) as exc:
                last_error = exc
            else:
                # Only a zero-exit bootstrap can establish provenance for the
                # registration observed here. A stale pre-bootstrap label can
                # never satisfy this branch.
                if self._wait_until_registered(domain, label):
                    return
                last_error = RuntimeError(
                    f"launchctl bootstrap succeeded but {domain}/{label} "
                    "did not register after bounded wait"
                )
            if attempt < attempts:
                self._bootout(domain, label)
                self._wait_until_unregistered(domain, label)

        message = (
            f"launchctl failed to register {domain}/{label} after "
            f"{attempts} bounded bootstrap attempt(s)"
        )
        if last_error is not None:
            raise RuntimeError(message) from last_error
        raise RuntimeError(message)

    def reload(self) -> None:
        domain = f"gui/{os.getuid()}"
        for label, plist in (
            (GATEWAY_LABEL, self.gateway_plist),
            (DASHBOARD_LABEL, self.dashboard_plist),
        ):
            self._bootout(domain, label)
            self._wait_until_unregistered(domain, label)
            if not plist.is_file():
                continue
            self._bootstrap_until_registered(domain, label, plist)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "verify", "rollback"))
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--hermes-home", type=Path)
    parser.add_argument("--launch-agents-dir", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--reload", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    reconciler = Reconciler(
        source_root=args.source_root,
        home=args.home,
        hermes_home=args.hermes_home,
        launch_agents_dir=args.launch_agents_dir,
        state_dir=args.state_dir,
    )
    try:
        if args.action == "install":
            receipt = (
                reconciler.install_and_reload()
                if args.reload
                else reconciler.install()
            )
            print(f"launchd environment installed receipt={receipt.name}")
        elif args.action == "verify":
            reconciler.verify()
            print("launchd environment verified")
        else:
            reconciler.rollback()
            print("launchd environment rolled back")
        if args.reload and args.action != "install":
            reconciler.reload()
    except Exception as exc:
        print(f"reconcile_launchd_environment: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
