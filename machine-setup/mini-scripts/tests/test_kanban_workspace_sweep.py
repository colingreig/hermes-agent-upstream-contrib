from __future__ import annotations

import importlib.util
import sqlite3
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS / "kanban_workspace_sweep.py"
_COUNTER = 0


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"kanban_workspace_sweep_ut_{_COUNTER}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


def _make_db(path: Path, rows: list[tuple]):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
        "workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT)"
    )
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def _touch_dir(path: Path, age_days: float = 0):
    path.mkdir(parents=True, exist_ok=True)
    (path / "artifact.txt").write_text("x" * 100, encoding="utf-8")
    if age_days:
        ts = time.time() - age_days * 86400
        import os
        os.utime(path, (ts, ts))


def test_active_task_never_swept_regardless_of_age(tmp_path, mod):
    root = tmp_path
    _make_db(root / "kanban.db", [("t_active", "running", "scratch", None)])
    ws = root / "kanban" / "workspaces" / "t_active"
    _touch_dir(ws, age_days=999)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=1, dry_run=False)

    assert ws.exists()
    assert stats["removed"] == 0
    assert stats["skipped_active"] == 1


def test_terminal_task_removed_once_old_enough(tmp_path, mod):
    root = tmp_path
    _make_db(root / "kanban.db", [("t_done", "done", "scratch", None)])
    ws = root / "kanban" / "workspaces" / "t_done"
    _touch_dir(ws, age_days=30)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=14, dry_run=False)

    assert not ws.exists()
    assert stats["removed"] == 1


def test_terminal_task_protected_while_recent(tmp_path, mod):
    root = tmp_path
    _make_db(root / "kanban.db", [("t_done", "done", "scratch", None)])
    ws = root / "kanban" / "workspaces" / "t_done"
    _touch_dir(ws, age_days=1)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=14, dry_run=False)

    assert ws.exists()
    assert stats["skipped_recent"] == 1


def test_non_scratch_workspace_never_touched(tmp_path, mod):
    root = tmp_path
    _make_db(root / "kanban.db", [("t_wt", "done", "worktree", None)])
    ws = root / "kanban" / "workspaces" / "t_wt"
    _touch_dir(ws, age_days=999)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=1, dry_run=False)

    assert ws.exists()
    assert stats["skipped_non_scratch"] == 1


def test_orphan_directory_swept_by_age_only(tmp_path, mod):
    root = tmp_path
    _make_db(root / "kanban.db", [])
    ws = root / "kanban" / "workspaces" / "t_orphan"
    _touch_dir(ws, age_days=30)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=14, dry_run=False)

    assert not ws.exists()
    assert stats["orphan_removed"] == 1


def test_dry_run_never_mutates(tmp_path, mod):
    root = tmp_path
    _make_db(root / "kanban.db", [("t_done", "done", "scratch", None)])
    ws = root / "kanban" / "workspaces" / "t_done"
    _touch_dir(ws, age_days=30)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=14, dry_run=True)

    assert ws.exists()
    assert stats["removed"] == 0


def test_board_skipped_when_db_missing(tmp_path, mod, capsys):
    root = tmp_path
    ws_root = root / "kanban" / "boards" / "content" / "workspaces"
    ws = ws_root / "t_x"
    _touch_dir(ws, age_days=999)

    stats = mod.sweep_board(
        "content", root / "kanban" / "boards" / "content" / "kanban.db", ws_root, days=1, dry_run=False,
    )

    assert ws.exists()
    assert stats == {
        "removed": 0, "removed_bytes": 0, "orphan_removed": 0, "errors": 1,
        "skipped_active": 0, "skipped_non_scratch": 0, "skipped_path_mismatch": 0,
        "skipped_recent": 0,
    }
    assert "BOARD_SKIP_NO_DB: content" in capsys.readouterr().out

    rc = mod.main(["--root", str(root), "--days", "1"])
    output = capsys.readouterr().out
    assert rc == 1
    assert ws.exists()
    assert "BOARD_SKIP_NO_DB: content" in output
    assert "sweep-finish" in output
    assert "errors=1" in output


@pytest.mark.parametrize("failure", ["is_dir", "listdir"])
def test_unlistable_board_fails_closed(tmp_path, mod, monkeypatch, capsys, failure):
    root = tmp_path
    db_path = root / "kanban.db"
    _make_db(db_path, [("t_done", "done", "scratch", None)])
    ws_root = root / "kanban" / "workspaces"
    ws = ws_root / "t_done"
    _touch_dir(ws, age_days=999)
    if failure == "is_dir":
        real_is_dir = mod.Path.is_dir

        def uninspectable(path):
            if path == ws_root:
                raise OSError("permission denied")
            return real_is_dir(path)

        monkeypatch.setattr(mod.Path, "is_dir", uninspectable)
    else:
        real_listdir = mod.os.listdir

        def unlistable(path):
            if Path(path) == ws_root:
                raise OSError("permission denied")
            return real_listdir(path)

        monkeypatch.setattr(mod.os, "listdir", unlistable)

    rc = mod.main(["--root", str(root), "--days", "1"])
    output = capsys.readouterr().out
    assert rc == 1
    assert ws.exists()
    assert "BOARD_SKIP_UNLISTABLE: default" in output
    assert "sweep-finish" in output
    assert "errors=1" in output


def test_task_directory_inspection_error_fails_closed(tmp_path, mod, monkeypatch, capsys):
    root = tmp_path
    db_path = root / "kanban.db"
    _make_db(db_path, [("t_done", "done", "scratch", None)])
    ws = root / "kanban" / "workspaces" / "t_done"
    _touch_dir(ws, age_days=999)
    real_is_dir = mod.Path.is_dir

    def uninspectable(path):
        if path == ws:
            raise OSError("permission denied")
        return real_is_dir(path)

    monkeypatch.setattr(mod.Path, "is_dir", uninspectable)

    rc = mod.main(["--root", str(root), "--days", "1"])
    output = capsys.readouterr().out

    assert rc == 1
    assert ws.exists()
    assert "BOARD_SKIP_UNLISTABLE: default/t_done" in output
    assert "sweep-finish" in output
    assert "errors=1" in output


@pytest.mark.parametrize("row", [None, ("t_done", "done", "scratch", None)])
def test_task_stat_error_fails_closed(tmp_path, mod, monkeypatch, capsys, row):
    root = tmp_path
    db_path = root / "kanban.db"
    _make_db(db_path, [] if row is None else [row])
    task_id = "t_orphan" if row is None else row[0]
    ws = root / "kanban" / "workspaces" / task_id
    _touch_dir(ws, age_days=999)
    real_is_dir = mod.Path.is_dir
    real_stat = mod.Path.stat

    def inspectable_directory(path):
        if path == ws:
            return True
        return real_is_dir(path)

    def uninspectable(path, *args, **kwargs):
        if path == ws:
            raise OSError("permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(mod.Path, "is_dir", inspectable_directory)
    monkeypatch.setattr(mod.Path, "stat", uninspectable)

    rc = mod.main(["--root", str(root), "--days", "1"])
    output = capsys.readouterr().out

    assert rc == 1
    assert real_stat(ws)
    assert f"BOARD_SKIP_UNLISTABLE: default/{task_id}" in output
    assert "sweep-finish" in output
    assert "errors=1" in output


def test_task_lookup_schema_error_fails_closed(tmp_path, mod, capsys):
    root = tmp_path
    db_path = root / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks VALUES (?, ?)", ("t_done", "done"))
    conn.commit()
    conn.close()
    ws_root = root / "kanban" / "workspaces"
    ws = ws_root / "t_done"
    _touch_dir(ws, age_days=999)

    stats = mod.sweep_board(
        "default", db_path, ws_root, days=1, dry_run=False,
    )

    assert ws.exists()
    assert stats["errors"] == 1
    assert stats["removed"] == 0
    assert stats["orphan_removed"] == 0
    assert "BOARD_SKIP_TASK_LOOKUP_ERROR: default/t_done" in capsys.readouterr().out

    rc = mod.main(["--root", str(root), "--days", "1"])
    output = capsys.readouterr().out
    assert rc == 1
    assert ws.exists()
    assert "BOARD_SKIP_TASK_LOOKUP_ERROR: default/t_done" in output
    assert "sweep-finish" in output
    assert "errors=1" in output


@pytest.mark.parametrize("failure", ["parent_is_dir", "listdir", "nested_is_dir"])
def test_named_board_discovery_error_fails_closed(tmp_path, mod, monkeypatch, capsys, failure):
    root = tmp_path
    db_path = root / "kanban.db"
    _make_db(db_path, [("t_done", "done", "scratch", None)])
    ws = root / "kanban" / "workspaces" / "t_done"
    _touch_dir(ws, age_days=999)
    boards_parent = root / "kanban" / "boards"
    named_board = boards_parent / "content"
    named_board.mkdir(parents=True)
    if failure == "listdir":
        real_listdir = mod.os.listdir

        def unlistable(path):
            if Path(path) == boards_parent:
                raise OSError("permission denied")
            return real_listdir(path)

        monkeypatch.setattr(mod.os, "listdir", unlistable)
    else:
        target = boards_parent if failure == "parent_is_dir" else named_board
        real_is_dir = mod.Path.is_dir

        def uninspectable(path):
            if path == target:
                raise OSError("permission denied")
            return real_is_dir(path)

        monkeypatch.setattr(mod.Path, "is_dir", uninspectable)

    rc = mod.main(["--root", str(root), "--days", "1"])
    output = capsys.readouterr().out

    assert rc == 1
    assert ws.exists()
    assert "BOARD_DISCOVERY_UNLISTABLE:" in output
    assert "sweep-finish" in output
    assert "boards_swept=0" in output
    assert "errors=1" in output


def test_path_mismatch_is_protected(tmp_path, mod):
    root = tmp_path
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _make_db(root / "kanban.db", [("t_custom", "done", "scratch", str(elsewhere))])
    ws = root / "kanban" / "workspaces" / "t_custom"
    _touch_dir(ws, age_days=30)

    stats = mod.sweep_board("default", root / "kanban.db", root / "kanban" / "workspaces", days=14, dry_run=False)

    assert ws.exists()
    assert stats["skipped_path_mismatch"] == 1


def test_workspace_path_resolution_error_fails_closed(tmp_path, mod, monkeypatch, capsys):
    root = tmp_path
    db_path = root / "kanban.db"
    ws = root / "kanban" / "workspaces" / "t_done"
    _make_db(db_path, [("t_done", "done", "scratch", str(ws))])
    _touch_dir(ws, age_days=30)
    real_resolve = mod.Path.resolve

    def unresolvable(path, *args, **kwargs):
        if path == ws:
            raise OSError("permission denied")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(mod.Path, "resolve", unresolvable)

    stats = mod.sweep_board(
        "default", db_path, root / "kanban" / "workspaces", days=14, dry_run=False,
    )
    direct_output = capsys.readouterr().out
    assert ws.exists()
    assert stats["removed"] == 0
    assert stats["errors"] == 1
    assert "BOARD_SKIP_UNLISTABLE: default/t_done" in direct_output

    rc = mod.main(["--root", str(root), "--days", "14"])
    output = capsys.readouterr().out
    assert rc == 1
    assert ws.exists()
    assert "BOARD_SKIP_UNLISTABLE: default/t_done" in output
    assert "sweep-finish" in output
    assert "errors=1" in output


def test_discover_boards_finds_default_and_named_boards(tmp_path, mod):
    root = tmp_path
    (root / "kanban" / "boards" / "content").mkdir(parents=True)
    (root / "kanban" / "boards" / "research").mkdir(parents=True)

    boards = list(mod._discover_boards(root))
    labels = {b[0] for b in boards}

    assert "default" in labels
    assert "content" in labels
    assert "research" in labels


def test_main_refuses_protected_root(tmp_path, mod, monkeypatch):
    monkeypatch.setattr(mod.os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    rc = mod.main(["--root", str(tmp_path)])
    assert rc == 2
