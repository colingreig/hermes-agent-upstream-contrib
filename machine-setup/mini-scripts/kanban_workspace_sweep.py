#!/usr/bin/env python3
"""kanban_workspace_sweep.py — age-based disk-lifecycle backstop for Hermes kanban
scratch workspaces (ClickUp 86e2k3ryc: worktree/kanban/release disk lifecycle).

WHY THIS EXISTS: the kanban swarm creates a per-task scratch working directory
under ``<hermes_home>/kanban/workspaces/`` (default board) or
``<hermes_home>/kanban/boards/<slug>/workspaces/`` (named boards) for every task
whose ``workspace_kind`` is ``"scratch"`` (see ``hermes_cli/kanban_db.py``). Two
lifecycle hooks already reclaim SOME of that space:
  - ``complete_task()`` removes a scratch workspace immediately on completion,
    unless an active child task still needs it as a handoff artifact (#33774).
  - ``hermes kanban gc`` removes scratch workspaces for tasks already in the
    ``archived`` status, plus prunes old events/logs.

Neither is a full safety net. ``hermes kanban gc`` only ever targets ONE board
(whichever ``get_current_board()`` resolves to in its process env) and has no
``--board`` option and no scheduler wired to it anywhere in this repo, so a
multi-board swarm (5 profiles observed live: coder/content/design/research/ops)
can silently accumulate scratch workspaces for ``done`` tasks that were never
individually archived, board by board, for as long as nobody happens to invoke
``hermes kanban gc`` in that specific board's context. This is the SEPARATE,
dumber, per-board, age-based backstop — same philosophy as
``scripts/worktree_backstop_sweep.py`` — that sweeps every board's
``workspaces/`` directory directly on disk, deliberately independent of which
board is "current" for any one process, and independent of the hermes package
import (standalone script, only stdlib + `du`, same design constraint as the
worktree backstop so it keeps working under launchd's minimal environment).

Safety rules (fail closed):
  - Only ever considers directories one level directly under a workspaces/
    root discovered via the SAME board-enumeration convention as
    ``kanban_db.py``'s ``_managed_scratch_path_info`` (the default board's
    ``kanban/workspaces/``, plus ``kanban/boards/<slug>/workspaces/`` for
    every board directory present on disk) — never touches a workspaces root
    itself, ``kanban.db``, ``kanban/logs/``, or any sibling directory.
  - Looks up each candidate directory's owning task by id (== directory name,
    the on-disk convention every scratch workspace without an explicit
    ``workspace_path`` override uses) in that board's own ``kanban.db`` via a
    read-only sqlite connection. If a board's DB can't be opened, the WHOLE
    BOARD is skipped this run (fail closed) rather than guessing from disk
    state alone.
  - A task whose ``workspace_kind`` isn't ``scratch`` is never touched, even
    if its directory happens to sit under a ``workspaces/`` root — defends
    against the same class of bug the app-side code guards against (#28818):
    a ``dir``/``worktree`` workspace can point at real user data.
  - A task whose ``workspace_path`` is set and doesn't resolve to this exact
    directory is never touched (it isn't actually this candidate's scratch
    dir).
  - A task whose status isn't terminal (``done`` or ``archived``) is never
    touched, regardless of age — active/blocked/in-review work is always
    protected. The ONE exception is the stale-``blocked`` backstop below,
    and even that never does a bare delete — see below.
  - A directory with NO matching task row at all (orphan — task deleted from
    the DB, or a leftover from before a schema change) is swept by age alone.
  - Age gate: only removes dirs whose mtime is older than --days (default 14
    — a bit more conservative than the worktree backstop's 7, since a scratch
    workspace can be the only copy of a swarm's handoff artifacts).
  - Stale-``blocked`` backstop: a task can sit in ``blocked`` status
    indefinitely (e.g. a fenced lease that never got unblocked), which makes
    its scratch workspace immortal under the terminal-status rule above.
    ``--stale-blocked-days`` (default 30, env
    ``HERMES_KANBAN_SWEEP_STALE_BLOCKED_DAYS``, ``0`` disables) makes a
    ``blocked`` workspace archive-eligible once its mtime age exceeds N days
    — the SAME age basis (workspace directory mtime) used everywhere else in
    this script. Because this is the one case where we give up a non-terminal
    workspace, it is never a bare delete: the workspace is first archived to
    ``<root>/db-backups/kanban-archive/<name>-<date>.tar.zst`` (inside the
    restic backup include-set) with a JSON sidecar recording the task id,
    status, original workspace path, archive path, and timestamp — the
    read-only-DB-safe stand-in for a DB breadcrumb, so a later-resumed task
    can trace where its workspace went even though this script never writes
    to kanban.db. The archive is verified (non-empty file + a successful
    ``tar -tf`` listing) BEFORE the workspace directory is removed; any
    failure (missing tar/zstd, empty archive, failed verification) skips the
    removal entirely (``SKIP_ARCHIVE_FAILED``) rather than ever deleting an
    unarchived workspace.
  - --dry-run lists candidates without removing anything, and for
    stale-blocked candidates reports what WOULD be archived/removed without
    creating any tar file.
  - Idempotent: safe to run repeatedly / on a schedule; a clean sweep with
    nothing to do is a normal, silent-ish outcome (still logs a summary line).

Usage:
  python3 kanban_workspace_sweep.py [--dry-run] [--days 14]
      [--stale-blocked-days 30] [--root ~/.hermes]
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

TERMINAL_STATUSES = {"done", "archived"}


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"[{ts}] {msg}", flush=True)


def _du_bytes(path: Path) -> int:
    """Best-effort recursive size of *path* in bytes, via `du -sk`. Reporting
    only — never a deletion-safety input. Returns 0 on any failure."""
    try:
        proc = subprocess.run(
            ["du", "-sk", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            return 0
        return int(proc.stdout.split()[0]) * 1024
    except Exception:
        return 0


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _archive_workspace(wdir: Path, archive_root: Path, archive_name: str):
    """Create+verify a `tar --zstd` archive of *wdir* at
    ``archive_root/<archive_name>.tar.zst`` BEFORE any caller is allowed to
    delete *wdir*. Never raises. Returns (ok, archive_path, size_bytes); on
    any failure (tar/zstd unavailable, empty archive, failed `tar -tf`
    verification) returns ok=False and best-effort removes the partial
    archive file so a broken artifact never sits next to good ones."""
    archive_path = archive_root / f"{archive_name}.tar.zst"
    try:
        archive_root.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["tar", "--zstd", "-cf", str(archive_path), "-C", str(wdir.parent), wdir.name],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"tar create rc={proc.returncode}: {proc.stderr.strip()}")
        if not archive_path.is_file() or archive_path.stat().st_size == 0:
            raise RuntimeError("archive missing or empty after create")
        verify = subprocess.run(
            ["tar", "--zstd", "-tf", str(archive_path)],
            capture_output=True, text=True, timeout=300, check=False,
        )
        if verify.returncode != 0:
            raise RuntimeError(f"tar verify rc={verify.returncode}: {verify.stderr.strip()}")
        return True, archive_path, archive_path.stat().st_size
    except Exception:
        try:
            if archive_path.exists():
                archive_path.unlink()
        except Exception:
            pass
        return False, archive_path, 0


def _write_archive_sidecar(archive_root: Path, archive_name: str, payload: dict) -> None:
    """Best-effort JSON breadcrumb next to the tar archive. The sweep is
    deliberately query_only on kanban.db (never writes there), so this
    sidecar is the only durable record tying a removed stale-blocked
    workspace back to its task id/status/original path for later tracing."""
    sidecar_path = archive_root / f"{archive_name}.json"
    try:
        sidecar_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        _log(f"WARN_SIDECAR_WRITE_FAILED: {archive_name} ({exc})")


def _discover_boards(root: Path):
    """Yield (board_label, db_path, workspaces_root) for the default board and
    every named board found on disk. Mirrors kanban_db.py's
    `_managed_scratch_path_info` root-enumeration convention, reimplemented
    standalone (no import of the hermes package — same design constraint as
    worktree_backstop_sweep.py not importing kanban internals)."""
    yield "default", root / "kanban.db", root / "kanban" / "workspaces"
    boards_parent = root / "kanban" / "boards"
    if not boards_parent.is_dir():
        return
    entries = sorted(os.listdir(boards_parent))
    for slug in entries:
        bdir = boards_parent / slug
        if not bdir.is_dir():
            continue
        yield slug, bdir / "kanban.db", bdir / "workspaces"


def _open_ro(db_path: Path):
    """Query-only sqlite connection, or None if the DB can't be opened/used.

    ``mode=ro`` cannot open a WAL database when SQLite needs to create or
    attach its ``-wal``/``-shm`` sidecars (the live default-board database is
    such a database).  Open the *existing* file with ``mode=rw`` so SQLite can
    participate in WAL locking, then enable ``query_only`` before the first
    schema read.  ``mode=rw`` still refuses to create a missing database and
    ``query_only`` makes every SQL write fail, preserving this sweep's
    read-only safety contract without using ``immutable=1`` (which could hide
    committed WAL content from deletion decisions).
    """
    if not db_path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        # Cheap probe that the schema is actually usable before trusting it.
        conn.execute("SELECT 1 FROM tasks LIMIT 1")
        return conn
    except Exception:
        if conn is not None:
            conn.close()
        return None


def _task_row(conn, task_id: str):
    return conn.execute(
        "SELECT id, status, workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def sweep_board(
    label: str, db_path: Path, workspaces_root: Path, days: int, dry_run: bool,
    stale_blocked_days: int = 0, archive_root: Path | None = None,
) -> dict:
    stats = {
        "removed": 0, "removed_bytes": 0, "orphan_removed": 0, "errors": 0,
        "skipped_active": 0, "skipped_non_scratch": 0, "skipped_path_mismatch": 0,
        "skipped_recent": 0, "stale_blocked_removed": 0, "stale_blocked_archived_bytes": 0,
        "skipped_archive_failed": 0, "stale_blocked_would_remove": 0,
        "stale_blocked_would_archive_bytes": 0,
    }
    try:
        has_workspaces_root = workspaces_root.is_dir()
    except OSError as exc:
        stats["errors"] += 1
        _log(f"BOARD_SKIP_UNLISTABLE: {label} ({exc})")
        return stats
    if not has_workspaces_root:
        _log(f"BOARD_SKIP: {label} (no workspaces root at {workspaces_root})")
        return stats

    conn = _open_ro(db_path)
    if conn is None:
        stats["errors"] += 1
        _log(f"BOARD_SKIP_NO_DB: {label} (cannot open {db_path} read-only — refusing to guess from disk state alone)")
        return stats

    try:
        now = time.time()
        try:
            names = sorted(os.listdir(workspaces_root))
        except OSError as exc:
            stats["errors"] += 1
            _log(f"BOARD_SKIP_UNLISTABLE: {label} ({exc})")
            return stats

        for name in names:
            wdir = workspaces_root / name
            try:
                is_candidate_dir = not wdir.is_symlink() and wdir.is_dir()
            except OSError as exc:
                stats["errors"] += 1
                _log(f"BOARD_SKIP_UNLISTABLE: {label}/{name} ({exc})")
                return stats
            if not is_candidate_dir:
                continue

            try:
                row = _task_row(conn, name)
            except Exception as exc:
                stats["errors"] += 1
                _log(f"BOARD_SKIP_TASK_LOOKUP_ERROR: {label}/{name} ({exc})")
                return stats

            if row is None:
                # Orphan: no task row at all for this directory name (deleted task,
                # or a leftover from before a schema change). Age-gate only.
                try:
                    age_days = (now - wdir.stat().st_mtime) / 86400
                except OSError as exc:
                    stats["errors"] += 1
                    _log(f"BOARD_SKIP_UNLISTABLE: {label}/{name} ({exc})")
                    return stats
                if age_days < days:
                    stats["skipped_recent"] += 1
                    _log(f"SKIP_RECENT_ORPHAN: {label}/{name} | age_days={age_days:.1f}")
                    continue
                size = _du_bytes(wdir)
                if dry_run:
                    _log(f"WOULD_REMOVE_ORPHAN: {label}/{name} | age_days={age_days:.1f} size={_fmt_bytes(size)}")
                    continue
                try:
                    shutil.rmtree(wdir)
                    stats["orphan_removed"] += 1
                    stats["removed_bytes"] += size
                    _log(f"REMOVED_ORPHAN: {label}/{name} | age_days={age_days:.1f} size={_fmt_bytes(size)}")
                except Exception as exc:
                    stats["errors"] += 1
                    _log(f"ERROR_REMOVING_ORPHAN: {label}/{name} | {exc}")
                continue

            if (row["workspace_kind"] or "scratch") != "scratch":
                stats["skipped_non_scratch"] += 1
                _log(f"SKIP_NON_SCRATCH: {label}/{name} | kind={row['workspace_kind']}")
                continue

            wpath = row["workspace_path"]
            if wpath:
                try:
                    if Path(wpath).expanduser().resolve() != wdir.resolve():
                        stats["skipped_path_mismatch"] += 1
                        _log(f"SKIP_PATH_MISMATCH: {label}/{name}")
                        continue
                except OSError as exc:
                    # Resolution failure is not evidence of a mismatch. Treat it
                    # as the same fail-closed inspection error used elsewhere so
                    # the fleet outcome probe sees its canonical failure marker
                    # and main returns nonzero without touching the workspace.
                    stats["errors"] += 1
                    _log(
                        f"BOARD_SKIP_UNLISTABLE: {label}/{name} "
                        f"(unresolvable workspace_path: {exc})"
                    )
                    continue

            status = row["status"]
            if status not in TERMINAL_STATUSES:
                if status == "blocked" and stale_blocked_days > 0:
                    try:
                        # Same age basis as everywhere else in this script: the
                        # workspace directory's own mtime.
                        blocked_age_days = (now - wdir.stat().st_mtime) / 86400
                    except OSError as exc:
                        stats["errors"] += 1
                        _log(f"BOARD_SKIP_UNLISTABLE: {label}/{name} ({exc})")
                        return stats
                    if blocked_age_days > stale_blocked_days:
                        if dry_run:
                            size = _du_bytes(wdir)
                            stats["stale_blocked_would_remove"] += 1
                            stats["stale_blocked_would_archive_bytes"] += size
                            _log(
                                f"WOULD_REMOVE_STALE_BLOCKED: {label}/{name} | "
                                f"age_days={blocked_age_days:.1f} status=blocked size={_fmt_bytes(size)}"
                            )
                            continue
                        date_str = time.strftime("%Y%m%d", time.gmtime())
                        archive_name = f"{name}-{date_str}"
                        if archive_root is None:
                            stats["skipped_archive_failed"] += 1
                            _log(f"SKIP_ARCHIVE_FAILED: {label}/{name} | no archive_root configured")
                            continue
                        ok, archive_path, size = _archive_workspace(wdir, archive_root, archive_name)
                        if not ok:
                            stats["skipped_archive_failed"] += 1
                            _log(
                                f"SKIP_ARCHIVE_FAILED: {label}/{name} | "
                                f"age_days={blocked_age_days:.1f} status=blocked "
                                f"(tar/zstd archive+verify failed — workspace left in place)"
                            )
                            continue
                        _write_archive_sidecar(archive_root, archive_name, {
                            "task_id": row["id"],
                            "status": row["status"],
                            "board": label,
                            "workspace_path": str(wdir),
                            "archive_path": str(archive_path),
                            "archived_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        })
                        try:
                            shutil.rmtree(wdir)
                            stats["stale_blocked_removed"] += 1
                            stats["stale_blocked_archived_bytes"] += size
                            _log(
                                f"REMOVED_STALE_BLOCKED: {label}/{name} | "
                                f"age_days={blocked_age_days:.1f} archive={archive_path}"
                            )
                        except Exception as exc:
                            stats["errors"] += 1
                            _log(f"ERROR_REMOVING: {label}/{name} | {exc}")
                        continue
                stats["skipped_active"] += 1
                _log(f"SKIP_ACTIVE: {label}/{name} | status={status}")
                continue

            try:
                age_days = (now - wdir.stat().st_mtime) / 86400
            except OSError as exc:
                stats["errors"] += 1
                _log(f"BOARD_SKIP_UNLISTABLE: {label}/{name} ({exc})")
                return stats
            if age_days < days:
                stats["skipped_recent"] += 1
                _log(f"SKIP_RECENT: {label}/{name} | age_days={age_days:.1f} status={status}")
                continue

            size = _du_bytes(wdir)
            if dry_run:
                _log(f"WOULD_REMOVE: {label}/{name} | age_days={age_days:.1f} status={status} size={_fmt_bytes(size)}")
                continue
            try:
                shutil.rmtree(wdir)
                stats["removed"] += 1
                stats["removed_bytes"] += size
                _log(f"REMOVED: {label}/{name} | age_days={age_days:.1f} status={status} size={_fmt_bytes(size)}")
            except Exception as exc:
                stats["errors"] += 1
                _log(f"ERROR_REMOVING: {label}/{name} | {exc}")
    finally:
        conn.close()

    return stats


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--days", type=int, default=int(os.environ.get("HERMES_KANBAN_SWEEP_DAYS", "14")),
        help="age threshold in days for terminal-status/orphan workspaces (default 14)",
    )
    p.add_argument(
        "--root", default=os.environ.get("HERMES_KANBAN_HOME") or os.environ.get("HERMES_HOME", "~/.hermes"),
        help="Hermes home the kanban tree lives under (default ~/.hermes)",
    )
    p.add_argument(
        "--stale-blocked-days", type=int,
        default=int(os.environ.get("HERMES_KANBAN_SWEEP_STALE_BLOCKED_DAYS", "30")),
        help=(
            "age threshold in days for archiving+removing a workspace whose task is "
            "stuck in non-terminal 'blocked' status (0=disabled, default 30)"
        ),
    )
    args = p.parse_args(argv or sys.argv[1:])

    root = Path(os.path.expanduser(args.root)).resolve()
    archive_root = root / "db-backups" / "kanban-archive"

    # Hard safety fence, same shape as worktree_backstop_sweep.py: never let this
    # backstop be pointed at the user's home or a canonical dev tree.
    home = Path(os.path.expanduser("~")).resolve()
    forbidden = {home, home / "dev", home / "Projects"}
    if root in forbidden:
        _log(f"REFUSING: --root {root} is a protected canonical location, not a Hermes home")
        return 2

    if not root.is_dir():
        _log(f"no-root-dir: {root}")
        return 0

    totals = {
        "removed": 0, "removed_bytes": 0, "orphan_removed": 0, "errors": 0,
        "skipped_active": 0, "skipped_non_scratch": 0, "skipped_path_mismatch": 0,
        "skipped_recent": 0, "boards_swept": 0, "stale_blocked_removed": 0,
        "stale_blocked_archived_bytes": 0, "skipped_archive_failed": 0,
        "stale_blocked_would_remove": 0, "stale_blocked_would_archive_bytes": 0,
    }
    try:
        boards = list(_discover_boards(root))
    except OSError as exc:
        totals["errors"] += 1
        _log(
            f"BOARD_DISCOVERY_UNLISTABLE: {root / 'kanban' / 'boards'} ({exc})"
        )
        boards = []

    for label, db_path, workspaces_root in boards:
        stats = sweep_board(
            label, db_path, workspaces_root, args.days, args.dry_run,
            stale_blocked_days=args.stale_blocked_days, archive_root=archive_root,
        )
        totals["boards_swept"] += 1
        for key in (
            "removed", "removed_bytes", "orphan_removed", "errors",
            "skipped_active", "skipped_non_scratch", "skipped_path_mismatch",
            "skipped_recent", "stale_blocked_removed", "stale_blocked_archived_bytes",
            "skipped_archive_failed", "stale_blocked_would_remove",
            "stale_blocked_would_archive_bytes",
        ):
            totals[key] += stats.get(key, 0)

    _log(
        f"sweep-finish root={root} boards_swept={totals['boards_swept']} "
        f"removed={totals['removed']} orphan_removed={totals['orphan_removed']} "
        f"removed_bytes={totals['removed_bytes']} removed_size={_fmt_bytes(totals['removed_bytes'])} "
        f"stale_blocked_removed={totals['stale_blocked_removed']} "
        f"stale_blocked_archived_bytes={totals['stale_blocked_archived_bytes']} "
        f"stale_blocked_archived_size={_fmt_bytes(totals['stale_blocked_archived_bytes'])} "
        f"stale_blocked_would_remove={totals['stale_blocked_would_remove']} "
        f"stale_blocked_would_archive_bytes={totals['stale_blocked_would_archive_bytes']} "
        f"skipped_archive_failed={totals['skipped_archive_failed']} "
        f"skipped_active={totals['skipped_active']} skipped_non_scratch={totals['skipped_non_scratch']} "
        f"skipped_path_mismatch={totals['skipped_path_mismatch']} skipped_recent={totals['skipped_recent']} "
        f"errors={totals['errors']} days={args.days} stale_blocked_days={args.stale_blocked_days} "
        f"dry_run={args.dry_run}"
    )
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
