#!/usr/bin/env python3
"""Bounded cleanup of stale Hermes runtime artifacts.

This is deliberately separate from cleanup_hermes_baks.py and from worktree
retirement. It never traverses repositories, invokes no external commands, and
only considers direct children of fixed scratch, run, session, and OpenCode
artifact roots. Each candidate must be older than the configured age threshold,
must not be a symlink, and must match a disposable-name allowlist.

Run artifacts are stricter: directories are never candidates; PID files are
removed only when they contain a valid, definitely-dead PID; lock files are
removed only while an exclusive nonblocking advisory lock is held; socket
artifacts are retained conservatively.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import stat
import time
from pathlib import Path

SCRATCH_PREFIXES = ("tmp-", "tmp_", "scratch-", "scratch_", "run-", "run_", "session-", "session_", "opencode-")
RUN_SUFFIXES = (".pid", ".lock", ".sock", ".socket")
SESSION_PREFIX = "request_dump_"
OPENCODE_PREFIXES = ("tmp-", "tmp_", "scratch-", "scratch_", "session-", "session_")


def log(message: str) -> None:
    print(f"[runtime-artifact-cleanup] {message}", flush=True)


def collect(root: Path, kind: str, eligible, minimum_age_seconds: float) -> list[tuple[str, Path, int, int]]:
    if not root.exists():
        log(f"skip missing root kind={kind} path={root}")
        return []
    if root.is_symlink() or not root.is_dir():
        log(f"skip unsafe root kind={kind} path={root}")
        return []

    safe_root = root.resolve()
    found: list[tuple[str, Path, int, int]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        try:
            child.relative_to(root)
        except ValueError:
            continue
        try:
            info = child.lstat()
        except OSError:
            continue
        # Retain every non-regular artifact. In particular, a directory with a
        # disposable-looking name is never a cleanup candidate.
        if not stat.S_ISREG(info.st_mode):
            continue
        if not eligible(child) or time.time() - info.st_mtime < minimum_age_seconds:
            continue
        try:
            child.resolve().relative_to(safe_root)
        except ValueError:
            continue
        found.append((kind, child, info.st_dev, info.st_ino))
    return found


def remove_candidate(path: Path, expected_dev: int, expected_ino: int) -> bool:
    """Unlink only the regular file selected during this sweep.

    The lstat/inode check is repeated immediately before removal so a renamed,
    replaced, or newly-created nested artifact is retained rather than being
    removed based on a stale directory scan.
    """
    try:
        current = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(current.st_mode):
        log(f"retain changed-nonfile path={path}")
        return False
    if current.st_dev != expected_dev or current.st_ino != expected_ino:
        log(f"retain changed-file path={path}")
        return False
    path.unlink()
    return True


def regular_old_run_file(path: Path, minimum_age_seconds: float) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    return time.time() - info.st_mtime >= minimum_age_seconds


def pid_definitely_dead(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        log(f"retain unreadable-pid path={path}")
        return False
    if not raw.isdecimal():
        log(f"retain invalid-pid path={path}")
        return False
    pid = int(raw)
    if pid <= 1:
        log(f"retain invalid-pid path={path}")
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        log(f"retain unknown-pid path={path} pid={pid}")
        return False
    log(f"retain live-pid path={path} pid={pid}")
    return False


def sweep_pid_file(path: Path, minimum_age_seconds: float, dry_run: bool) -> int:
    if not regular_old_run_file(path, minimum_age_seconds):
        return 0
    if not pid_definitely_dead(path):
        return 0
    if dry_run:
        log(f"would-remove kind=run-dead-pid path={path}")
        return 1
    if not regular_old_run_file(path, minimum_age_seconds) or not pid_definitely_dead(path):
        log(f"retain changed-pid path={path}")
        return 0
    try:
        path.unlink()
    except OSError as exc:
        log(f"remove-failed kind=run-dead-pid path={path} error={exc}")
        return 0
    log(f"removed kind=run-dead-pid path={path}")
    return 1


def sweep_lock_file(
    path: Path, minimum_age_seconds: float, dry_run: bool, kind: str = "run-unlocked-lock"
) -> int:
    if not regular_old_run_file(path, minimum_age_seconds):
        return 0
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        log(f"retain unreadable-lock path={path}")
        return 0
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or time.time() - info.st_mtime < minimum_age_seconds:
            return 0
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            log(f"retain active-lock path={path}")
            return 0
        try:
            current = path.lstat()
        except OSError:
            return 0
        if current.st_dev != info.st_dev or current.st_ino != info.st_ino:
            log(f"retain changed-lock path={path}")
            return 0
        if dry_run:
            log(f"would-remove kind={kind} path={path}")
            return 1
        try:
            path.unlink()
        except OSError as exc:
            log(f"remove-failed kind=run-unlocked-lock path={path} error={exc}")
            return 0
        log(f"removed kind={kind} path={path}")
        return 1
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def sweep_run_root(root: Path, minimum_age_seconds: float, dry_run: bool) -> int:
    if not root.exists():
        log(f"skip missing root kind=run path={root}")
        return 0
    if root.is_symlink() or not root.is_dir():
        log(f"skip unsafe root kind=run path={root}")
        return 0

    count = 0
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.name.endswith(RUN_SUFFIXES):
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            log(f"retain non-file-run-artifact path={path}")
            continue
        if time.time() - info.st_mtime < minimum_age_seconds:
            continue
        if path.suffix == ".pid":
            count += sweep_pid_file(path, minimum_age_seconds, dry_run)
        elif path.suffix == ".lock":
            count += sweep_lock_file(path, minimum_age_seconds, dry_run)
        else:
            log(f"retain socket-artifact path={path}")
    return count


def sweep_direct_lock_root(
    root: Path, kind: str, minimum_age_seconds: float, dry_run: bool
) -> int:
    """Sweep only direct, stale, advisory-unlocked ``*.lock`` children of root.

    This is deliberately scoped to the two Hermes cron lock roots passed by
    ``main``. It does not recurse and therefore cannot discover repository or
    arbitrary nested lock files.
    """
    if not root.exists():
        log(f"skip missing root kind={kind} path={root}")
        return 0
    if root.is_symlink() or not root.is_dir():
        log(f"skip unsafe root kind={kind} path={root}")
        return 0

    count = 0
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.name.endswith(".lock"):
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            log(f"retain non-file-{kind} path={path}")
            continue
        if time.time() - info.st_mtime < minimum_age_seconds:
            continue
        count += sweep_lock_file(path, minimum_age_seconds, dry_run, kind)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-age-hours", type=float, default=72.0)
    parser.add_argument("--cron-lock-min-age-hours", type=float, default=6.0)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--opencode-home", type=Path, default=Path.home() / ".local" / "share" / "opencode")
    args = parser.parse_args()

    if args.min_age_hours < 1:
        parser.error("--min-age-hours must be at least 1")
    if args.cron_lock_min_age_hours < 1:
        parser.error("--cron-lock-min-age-hours must be at least 1")
    age_seconds = args.min_age_hours * 3600
    cron_lock_age_seconds = args.cron_lock_min_age_hours * 3600
    hermes_home = args.hermes_home.expanduser()
    opencode_home = args.opencode_home.expanduser()

    specs = (
        ("scratch", hermes_home / "tmp", lambda path: path.name.startswith(SCRATCH_PREFIXES)),
        ("session", hermes_home / "sessions", lambda path: path.name.startswith(SESSION_PREFIX) and path.suffix == ".json"),
        ("opencode-snapshot", opencode_home / "snapshot", lambda path: path.name.startswith(OPENCODE_PREFIXES)),
        ("opencode-log", opencode_home / "log", lambda path: path.is_file() and (path.name.startswith(OPENCODE_PREFIXES) or path.name.endswith(".tmp") or ".log." in path.name)),
    )

    candidates: list[tuple[str, Path, int, int]] = []
    for kind, root, eligible in specs:
        candidates.extend(collect(root, kind, eligible, age_seconds))

    handled = 0
    for kind, path, expected_dev, expected_ino in candidates:
        if args.dry_run:
            log(f"would-remove kind={kind} path={path}")
            handled += 1
            continue
        try:
            if remove_candidate(path, expected_dev, expected_ino):
                log(f"removed kind={kind} path={path}")
                handled += 1
        except OSError as exc:
            log(f"remove-failed kind={kind} path={path} error={exc}")

    handled += sweep_run_root(hermes_home / "run", age_seconds, args.dry_run)
    handled += sweep_direct_lock_root(
        hermes_home / "cron", "cron-direct-unlocked-lock", cron_lock_age_seconds, args.dry_run
    )
    handled += sweep_direct_lock_root(
        hermes_home / "cron" / "locks", "cron-locks-unlocked-lock", cron_lock_age_seconds, args.dry_run
    )
    log(
        f"complete dry_run={args.dry_run} candidates={handled} "
        f"min_age_hours={args.min_age_hours:g} "
        f"cron_lock_min_age_hours={args.cron_lock_min_age_hours:g}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
