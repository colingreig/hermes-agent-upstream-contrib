#!/usr/bin/env python3
"""Crash-safe locking and prompt-visible worktree guards for skill pulls."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Callable

STALE_INVALID_LOCK_SECONDS = 6 * 60 * 60
PROMPT_INDEX_FILENAMES = frozenset(("SKILL.md", "DESCRIPTION.md"))
EXCLUDED_SKILL_DIRS = frozenset(
    (
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
)
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


class LockBusyError(RuntimeError):
    """Raised when another live pull owns the lock."""


@dataclass(frozen=True)
class LockOwner:
    pid: int
    process_start: str
    created_at: float
    token: str


def _process_start(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        check=False,
        text=True,
        capture_output=True,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _write_owner(path: Path, owner: LockOwner) -> None:
    payload = {
        "schema_version": 1,
        "pid": owner.pid,
        "process_start": owner.process_start,
        "created_at": owner.created_at,
        "token": owner.token,
    }
    fd, temporary_name = tempfile.mkstemp(prefix=".owner.swap.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_owner(path: Path) -> LockOwner | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            return None
        return LockOwner(
            pid=int(payload["pid"]),
            process_start=str(payload["process_start"]),
            created_at=float(payload["created_at"]),
            token=str(payload["token"]),
        )
    except Exception:
        return None


def acquire_lock(
    lock_dir: Path,
    *,
    owner_pid: int,
    now: float | None = None,
    stale_invalid_after: float = STALE_INVALID_LOCK_SECONDS,
    pid_alive: Callable[[int], bool] = _pid_alive,
    process_start: Callable[[int], str] = _process_start,
) -> str:
    """Acquire an atomic directory lock, safely reclaiming dead/stale owners."""
    observed_at = time.time() if now is None else now
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    owner = LockOwner(
        pid=owner_pid,
        process_start=process_start(owner_pid),
        created_at=observed_at,
        token=token,
    )
    if not owner.process_start:
        raise RuntimeError(f"could not identify lock owner process {owner_pid}")

    for _attempt in range(3):
        try:
            lock_dir.mkdir(mode=0o700)
        except FileExistsError:
            current = _read_owner(lock_dir / "owner.json")
            if current is not None:
                alive = pid_alive(current.pid)
                live_start = process_start(current.pid) if alive else ""
                # A live PID with an unreadable start token is ambiguous, so
                # fail closed instead of risking concurrent pulls.
                if alive and (
                    not live_start or live_start == current.process_start
                ):
                    raise LockBusyError(
                        f"skill pull lock held by live pid {current.pid}"
                    )
                reclaimable = True
            else:
                try:
                    age = max(0.0, observed_at - lock_dir.stat().st_mtime)
                except OSError:
                    continue
                reclaimable = age >= stale_invalid_after
                if not reclaimable:
                    raise LockBusyError(
                        f"skill pull lock has incomplete fresh owner metadata ({age:.0f}s)"
                    )
            if reclaimable:
                quarantine = lock_dir.with_name(
                    f".{lock_dir.name}.stale.{owner_pid}.{token}"
                )
                try:
                    os.replace(lock_dir, quarantine)
                except (FileNotFoundError, OSError):
                    continue
                shutil.rmtree(quarantine)
                continue
        else:
            try:
                _write_owner(lock_dir / "owner.json", owner)
            except Exception:
                shutil.rmtree(lock_dir, ignore_errors=True)
                raise
            return token
    raise LockBusyError("skill pull lock changed repeatedly during acquisition")


def release_lock(lock_dir: Path, *, token: str) -> None:
    """Release only the generation owned by ``token``."""
    owner = _read_owner(lock_dir / "owner.json")
    if owner is None or owner.token != token:
        raise LockBusyError("refusing to release a skill pull lock owned by another process")
    quarantine = lock_dir.with_name(f".{lock_dir.name}.release.{os.getpid()}.{token}")
    os.replace(lock_dir, quarantine)
    shutil.rmtree(quarantine)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_prompt_path(path: Path, repository_root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"could not resolve prompt-visible path: {path}") from exc
    if not _is_within(resolved, repository_root):
        raise RuntimeError(
            f"prompt-visible skill path escapes repository: {path} -> {resolved}"
        )
    return resolved


def _reject_prompt_symlink(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"prompt-visible skill path is a symlink: {path}")


def _prompt_index_paths(repository_root: Path, relative_roots: list[str]) -> set[str]:
    """Validate the exact followlinks=True prompt walk and return its index files."""
    index_paths: set[str] = set()
    visited_directories: set[tuple[int, int]] = set()

    for relative_root in relative_roots:
        prompt_root = repository_root / relative_root
        _reject_prompt_symlink(prompt_root)
        resolved_root = _resolve_prompt_path(prompt_root, repository_root)
        if not resolved_root.is_dir():
            raise RuntimeError(f"prompt-visible root is not a directory: {prompt_root}")

        for current, dirs, files in os.walk(prompt_root, followlinks=True):
            current_path = Path(current)
            resolved_current = _resolve_prompt_path(current_path, repository_root)
            stat = resolved_current.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in visited_directories:
                dirs[:] = []
                continue
            visited_directories.add(identity)

            has_skill_md = "SKILL.md" in files
            traversed_dirs: list[str] = []
            for directory in dirs:
                if directory in EXCLUDED_SKILL_DIRS or (
                    has_skill_md and directory in SKILL_SUPPORT_DIRS
                ):
                    continue
                child = current_path / directory
                _reject_prompt_symlink(child)
                resolved_child = _resolve_prompt_path(child, repository_root)
                if not resolved_child.is_dir():
                    raise RuntimeError(
                        f"prompt-visible directory is not a directory: {child}"
                    )
                child_stat = resolved_child.stat()
                if (child_stat.st_dev, child_stat.st_ino) not in visited_directories:
                    traversed_dirs.append(directory)
            dirs[:] = traversed_dirs

            for filename in PROMPT_INDEX_FILENAMES.intersection(files):
                index_file = current_path / filename
                _reject_prompt_symlink(index_file)
                _resolve_prompt_path(index_file, repository_root)
                index_paths.add(index_file.relative_to(repository_root).as_posix())

    return index_paths


def _nul_paths(stdout: bytes | str) -> set[str]:
    separator = b"\0" if isinstance(stdout, bytes) else "\0"
    entries = stdout.split(separator)
    result: set[str] = set()
    for entry in entries:
        if not entry:
            continue
        if isinstance(entry, bytes):
            result.add(os.fsdecode(entry))
        else:
            result.add(entry)
    return result


def assert_prompt_content_clean(
    root: Path,
    relative_roots: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    """Fail when tracked or untracked prompt-visible content is dirty."""
    resolved = root.resolve(strict=True)
    if resolved != root:
        raise RuntimeError(f"repository escapes canonical root: {root} -> {resolved}")
    paths: list[str] = []
    for raw in relative_roots:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or not raw.strip():
            raise ValueError(f"invalid prompt-visible path: {raw!r}")
        paths.append(raw)
    visible_index_paths = _prompt_index_paths(resolved, paths)
    result = runner(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *paths,
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("could not inspect prompt-visible skill worktree state")
    if result.stdout:
        raise RuntimeError("prompt-visible skill content has tracked or untracked changes")

    ignored = runner(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            *paths,
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if ignored.returncode != 0:
        raise RuntimeError("could not inspect ignored prompt-visible skill content")
    ignored_prompt_files = visible_index_paths.intersection(_nul_paths(ignored.stdout))
    if ignored_prompt_files:
        raise RuntimeError(
            "prompt-visible skill content contains ignored index files: "
            + ", ".join(sorted(ignored_prompt_files))
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    acquire = subparsers.add_parser("acquire")
    acquire.add_argument("--lock", type=Path, required=True)
    acquire.add_argument("--owner-pid", type=int, required=True)
    release = subparsers.add_parser("release")
    release.add_argument("--lock", type=Path, required=True)
    release.add_argument("--token", required=True)
    clean = subparsers.add_parser("clean")
    clean.add_argument("--root", type=Path, required=True)
    clean.add_argument("--path", action="append", required=True)
    args = parser.parse_args()
    if args.action == "acquire":
        print(acquire_lock(args.lock, owner_pid=args.owner_pid))
    elif args.action == "release":
        release_lock(args.lock, token=args.token)
    else:
        assert_prompt_content_clean(args.root, args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
