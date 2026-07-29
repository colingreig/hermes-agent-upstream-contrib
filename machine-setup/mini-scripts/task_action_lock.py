#!/usr/bin/env python3
"""Shared per-task advisory lock for ClickUp task finalizers/reconcilers.

This mirrors the cron/jobs.py lock pattern: a stable lock file per task id
under ~/.hermes/cron/locks/, acquired with a non-blocking flock so the caller
can skip rather than race another worker on the same task.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows
    msvcrt = None

LOCK_DIR = Path(os.path.expanduser("~/.hermes/cron/locks"))


def lock_path(task_id: str) -> Path:
    text = str(task_id or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"invalid task id for lock path: {task_id!r}")
    return LOCK_DIR / f"{text}.lock"


@contextlib.contextmanager
def task_lock(task_id: str):
    """Yield True when the lock was acquired, False when another worker holds it."""
    path = lock_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    locked = False
    try:
        handle = open(path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except (BlockingIOError, OSError):
            locked = False
        yield locked
    finally:
        if handle is not None:
            if locked:
                try:
                    if fcntl is not None:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                handle.close()
            except OSError:
                pass
