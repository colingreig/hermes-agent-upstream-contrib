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


def test_board_skipped_when_db_missing(tmp_path, mod):
    root = tmp_path
    ws_root = root / "kanban" / "boards" / "content" / "workspaces"
    ws = ws_root / "t_x"
    _touch_dir(ws, age_days=999)

    stats = mod.sweep_board(
        "content", root / "kanban" / "boards" / "content" / "kanban.db", ws_root, days=1, dry_run=False,
    )

    assert ws.exists()
    assert stats == {
        "removed": 0, "removed_bytes": 0, "orphan_removed": 0, "errors": 0,
        "skipped_active": 0, "skipped_non_scratch": 0, "skipped_path_mismatch": 0,
        "skipped_recent": 0,
    }


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
