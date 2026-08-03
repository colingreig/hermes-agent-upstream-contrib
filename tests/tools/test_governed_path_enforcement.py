from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import model_tools
from tools import governed_paths
from tools.governed_paths import check_file_mutation, wrap_terminal_command


def _mini_production_context(monkeypatch, hermes):
    monkeypatch.setattr(governed_paths, "_MINI_PRODUCTION_HOME", hermes)
    monkeypatch.setattr(sys, "platform", "darwin")


def test_write_file_and_patch_targets_are_denied_without_lease(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert check_file_mutation("write_file", {"path": str(hermes / "runtime-current")}, "session")
    patch = "*** Begin Patch\n*** Update File: %s\n-old\n+new\n*** End Patch" % (hermes / "scripts" / "x.py")
    assert check_file_mutation("patch", {"mode": "patch", "patch": patch}, "session")


def test_runtime_current_subtree_is_governed_even_when_pointer_targets_elsewhere(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    outside = tmp_path / "outside-release"
    outside.mkdir()
    hermes.mkdir()
    (hermes / "runtime-current").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert check_file_mutation("write_file", {"path": str(hermes / "runtime-current" / "model_tools.py")}, "ordinary")


def test_arbitrary_symlink_aliases_into_governed_roots_are_denied(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    scripts = hermes / "scripts"
    releases = hermes / "releases"
    scripts.mkdir(parents=True)
    releases.mkdir()
    aliases = (tmp_path / "script-alias", tmp_path / "release-alias")
    aliases[0].symlink_to(scripts, target_is_directory=True)
    aliases[1].symlink_to(releases, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    for target in (aliases[0] / "x.py", aliases[1] / "v-next" / "new.py"):
        assert check_file_mutation("write_file", {"path": str(target)}, "ordinary")


def test_nonexistent_leaf_under_symlinked_governed_parent_is_denied(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    scripts = hermes / "scripts"
    scripts.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(scripts, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    target = alias / "missing" / "leaf.py"
    assert not target.exists()
    assert check_file_mutation("write_file", {"path": str(target)}, "ordinary")


def test_relative_target_uses_file_tool_task_workspace(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    workspace = tmp_path / "task-workspace"
    workspace.mkdir()
    (hermes / "scripts").mkdir(parents=True)
    (workspace / "alias").symlink_to(hermes / "scripts", target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr("tools.file_tools._authoritative_workspace_root", lambda task_id: str(workspace))
    assert check_file_mutation("write_file", {"path": "alias/new.py"}, "ordinary", task_id="task-42")


def test_real_dispatch_blocks_write_file_before_mutation(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    target = hermes / "runtime-current"
    target.write_text("safe", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    result = json.loads(model_tools.handle_function_call(
        "write_file", {"path": str(target), "content": "changed"},
        session_id="ordinary", skip_pre_tool_call_hook=True,
    ))
    assert "GOVERNED PATH WRITE BLOCKED" in result["error"]
    assert target.read_text(encoding="utf-8") == "safe"


def test_matching_cutter_lease_allows_file_mutation_policy(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    state = hermes / "state"
    state.mkdir(parents=True)
    database = state / "production-write-lease.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE active_leases (actor TEXT, resources_json TEXT, session_id TEXT, expires_at TEXT)")
        connection.execute(
            "INSERT INTO active_leases VALUES (?,?,?,?)",
            ("mini-release-cut", json.dumps(["runtime-release"]), "cutter",
             (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()),
        )
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    assert check_file_mutation(
        "write_file", {"path": str(hermes / "runtime-current")}, "cutter"
    ) is None


def test_actual_os_sandbox_blocks_shell_variable_and_python_concatenation(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    pointer = hermes / "runtime-current"
    pointer.write_text("safe", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    _mini_production_context(monkeypatch, hermes)
    commands = [
        "r=runtime-; rm \"$HERMES_HOME/${r}current\"",
        "python3 -c 'import os; os.unlink(os.environ[\"HERMES_HOME\"] + \"/runtime-\" + \"current\")'",
    ]
    for command in commands:
        wrapped, error = wrap_terminal_command(command, session_id="ordinary")
        assert error is None
        result = subprocess.run(wrapped, shell=True, text=True, capture_output=True, env={"HERMES_HOME": str(hermes), "PATH": "/usr/bin:/bin"})
        assert result.returncode != 0
        assert pointer.read_text(encoding="utf-8") == "safe"


def test_digest_os_sandbox_is_read_only_without_parent_identity(tmp_path, monkeypatch):
    target = tmp_path / "ordinary.txt"
    target.write_text("safe", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    _mini_production_context(monkeypatch, tmp_path / ".hermes")
    wrapped, error = wrap_terminal_command(f"printf changed > {target}", session_id="cron_f23a03e9d1b2_tick")
    assert error is None
    result = subprocess.run(wrapped, shell=True, text=True, capture_output=True)
    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "safe"


def test_terminal_sandbox_is_not_activated_on_unsupported_platform(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(governed_paths, "_MINI_PRODUCTION_HOME", hermes)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(governed_paths.shutil, "which", lambda _name: None)

    command = "printf unchanged"
    assert wrap_terminal_command(command, session_id="ordinary") == (command, None)


def test_terminal_sandbox_is_not_activated_for_other_macos_home(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(governed_paths.shutil, "which", lambda _name: None)

    command = "printf unchanged"
    assert wrap_terminal_command(command, session_id="ordinary") == (command, None)


def test_mini_production_context_fails_closed_without_sandbox(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    _mini_production_context(monkeypatch, hermes)
    monkeypatch.setattr(governed_paths.shutil, "which", lambda _name: None)

    wrapped, error = wrap_terminal_command("true", session_id="ordinary")
    assert wrapped == ""
    assert error == "governed path enforcement unavailable: sandbox-exec not found"


def _install_release_cutter(tmp_path: Path, hermes: Path) -> tuple[Path, Path]:
    release = hermes / "releases" / ("v0.18.2-" + "a" * 12)
    cutter = release / "scripts" / "mini-release-cut.sh"
    cutter.parent.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "scripts" / "mini-release-cut.sh"
    cutter.write_bytes(source.read_bytes())
    cutter.chmod(0o755)
    (hermes / "runtime-current").symlink_to(release, target_is_directory=True)
    return cutter, source


def test_exact_trusted_cutter_bootstraps_before_internal_lease(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    _install_release_cutter(tmp_path, hermes)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    _mini_production_context(monkeypatch, hermes)
    monkeypatch.setattr(governed_paths.shutil, "which", lambda _name: None)
    command = f"{hermes}/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches"
    assert wrap_terminal_command(command, session_id="ordinary") == (command, None)


def test_cutter_bootstrap_rejects_compound_alias_suffix_and_modified_bytes(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    cutter, _source = _install_release_cutter(tmp_path, hermes)
    alias = tmp_path / "cutter"
    alias.symlink_to(cutter)
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    _mini_production_context(monkeypatch, hermes)
    monkeypatch.setattr(governed_paths.shutil, "which", lambda _name: "/usr/bin/sandbox-exec")
    exact = f"{hermes}/runtime-current/scripts/mini-release-cut.sh --ref prod-live-patches"
    for command in (f"{exact}; touch /tmp/pwn", f"bash {exact}", f"{alias} --ref prod-live-patches", f"{exact} unexpected"):
        wrapped, error = wrap_terminal_command(command, session_id="ordinary")
        assert error is None
        assert wrapped != command
    cutter.write_bytes(cutter.read_bytes() + b"\n# modified\n")
    wrapped, error = wrap_terminal_command(exact, session_id="ordinary")
    assert error is None
    assert wrapped != exact
