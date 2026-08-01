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
    protected.
  - A directory with NO matching task row at all (orphan — task deleted from
    the DB, or a leftover from before a schema change) is swept by age alone.
  - Age gate: only removes dirs whose mtime is older than --days (default 14
    — a bit more conservative than the worktree backstop's 7, since a scratch
    workspace can be the only copy of a swarm's handoff artifacts).
  - --dry-run lists candidates without removing anything.
  - Idempotent: safe to run repeatedly / on a schedule; a clean sweep with
    nothing to do is a normal, silent-ish outcome (still logs a summary line).

Usage:
  python3 kanban_workspace_sweep.py [--dry-run] [--days 14] [--root ~/.hermes]
"""
import argparse
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
    """Read-only sqlite connection, or None if the DB can't be opened/used.
    Never creates a DB file — a missing or unusable DB means this board is
    skipped entirely by the caller (fail closed)."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        # Cheap probe that the schema is actually usable before trusting it.
        conn.execute("SELECT 1 FROM tasks LIMIT 1")
        return conn
    except Exception:
        return None


def _task_row(conn, task_id: str):
    return conn.execute(
        "SELECT id, status, workspace_kind, workspace_path FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()


def sweep_board(label: str, db_path: Path, workspaces_root: Path, days: int, dry_run: bool) -> dict:
    stats = {
        "removed": 0, "removed_bytes": 0, "orphan_removed": 0, "errors": 0,
        "skipped_active": 0, "skipped_non_scratch": 0, "skipped_path_mismatch": 0,
        "skipped_recent": 0,
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
    args = p.parse_args(argv or sys.argv[1:])

    root = Path(os.path.expanduser(args.root)).resolve()

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
        "skipped_recent": 0, "boards_swept": 0,
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
        stats = sweep_board(label, db_path, workspaces_root, args.days, args.dry_run)
        totals["boards_swept"] += 1
        for key in (
            "removed", "removed_bytes", "orphan_removed", "errors",
            "skipped_active", "skipped_non_scratch", "skipped_path_mismatch",
            "skipped_recent",
        ):
            totals[key] += stats.get(key, 0)

    _log(
        f"sweep-finish root={root} boards_swept={totals['boards_swept']} "
        f"removed={totals['removed']} orphan_removed={totals['orphan_removed']} "
        f"removed_bytes={totals['removed_bytes']} removed_size={_fmt_bytes(totals['removed_bytes'])} "
        f"skipped_active={totals['skipped_active']} skipped_non_scratch={totals['skipped_non_scratch']} "
        f"skipped_path_mismatch={totals['skipped_path_mismatch']} skipped_recent={totals['skipped_recent']} "
        f"errors={totals['errors']} days={args.days} dry_run={args.dry_run}"
    )
    return 1 if totals["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
