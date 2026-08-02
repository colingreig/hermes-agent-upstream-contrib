"""Manifest / install semantics for the hermes-disk-lifecycle installer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install_disk_lifecycle.py"
_spec = importlib.util.spec_from_file_location("install_disk_lifecycle_under_test", SCRIPT)
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    mirror = tmp_path / "mirror"
    launchd = mirror / "launchd"
    launchd.mkdir(parents=True)
    home = tmp_path / "home"
    scripts_dir = home / ".hermes" / "scripts"
    scripts_dir.mkdir(parents=True)
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (scripts_dir / "slack_msg_builder.py").write_text("# co-exist\n")

    script_specs = [
        ("disk_space_alert.py", "~/.hermes/scripts/disk_space_alert.py", "script"),
        ("kanban_workspace_sweep.py", "~/.hermes/scripts/kanban_workspace_sweep.py", "script"),
    ]
    plist_specs = [
        (
            "launchd/com.colingreig.hermes.disk-space-alert.plist",
            "~/Library/LaunchAgents/com.colingreig.hermes.disk-space-alert.plist",
            "launch_agent",
        ),
        (
            "launchd/com.colingreig.hermes.kanban-workspace-sweep.plist",
            "~/Library/LaunchAgents/com.colingreig.hermes.kanban-workspace-sweep.plist",
            "launch_agent",
        ),
        (
            "launchd/com.colingreig.hermes.worktree-backstop-sweep.plist",
            "~/Library/LaunchAgents/com.colingreig.hermes.worktree-backstop-sweep.plist",
            "launch_agent",
        ),
    ]

    files = []
    for rel, dest_abs, deploy_mode in script_specs + plist_specs:
        data = f"# synthetic {rel}\n".encode()
        path = mirror / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files.append(
            {
                "src_rel": rel,
                "src_base": "mirror",
                "dest_abs": dest_abs,
                "sha256": _sha(data),
                "role": f"synthetic {rel}",
                "deploy_mode": deploy_mode,
            }
        )

    manifest = {
        "bundle": "hermes-disk-lifecycle",
        "source_task": "86e2k6j3c",
        "coexist_required": [
            {
                "dest_abs": "~/.hermes/scripts/slack_msg_builder.py",
                "reason": "disk_space_alert dependency",
            }
        ],
        "files": files,
    }
    manifest_path = mirror / "disk_lifecycle_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {
        "mirror": mirror,
        "home": home,
        "manifest": manifest,
        "manifest_path": manifest_path,
    }


def _install(bundle, **kw):
    return install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        mirror_root=bundle["mirror"],
        manifest_path=bundle["manifest_path"],
        dry_run=kw.pop("dry_run", False),
    )


def _receipt(home) -> dict:
    installs = sorted((home / ".hermes" / "logs" / "disk-lifecycle-installs").iterdir())
    return json.loads((installs[-1] / "install-receipt.json").read_text())


def test_dry_run_verifies_hashes_without_writing(bundle):
    assert _install(bundle, dry_run=True) == 0
    assert not (bundle["home"] / ".hermes" / "scripts" / "disk_space_alert.py").exists()
    assert not (
        bundle["home"] / "Library" / "LaunchAgents" / "com.colingreig.hermes.disk-space-alert.plist"
    ).exists()


def test_install_writes_scripts_and_launch_agents(bundle):
    assert _install(bundle) == 0
    home = bundle["home"]
    assert (home / ".hermes" / "scripts" / "disk_space_alert.py").is_file()
    assert (home / ".hermes" / "scripts" / "kanban_workspace_sweep.py").is_file()
    assert (
        home / "Library" / "LaunchAgents" / "com.colingreig.hermes.disk-space-alert.plist"
    ).is_file()
    receipt = _receipt(home)
    assert receipt["result"] == "success"
    assert len(receipt["files"]) == 5


def test_refuses_hash_drift(bundle):
    target = bundle["mirror"] / "disk_space_alert.py"
    target.write_text("# mutated\n")
    with pytest.raises(install_mod.InstallError, match="hash drift"):
        install_mod.build_plan(
            bundle["manifest"],
            home=bundle["home"],
            mirror_root=bundle["mirror"],
        )


def test_refuses_launch_agent_outside_label_prefix(bundle):
    entry = bundle["manifest"]["files"][-1].copy()
    entry["dest_abs"] = "~/Library/LaunchAgents/evil.plist"
    bad = {**bundle["manifest"], "files": [entry]}
    with pytest.raises(install_mod.InstallError, match="LaunchAgents label prefix"):
        install_mod.build_plan(
            bad,
            home=bundle["home"],
            mirror_root=bundle["mirror"],
        )


def test_repository_manifest_matches_live_sources(tmp_path):
    manifest_path = (
        Path(__file__).resolve().parents[1] / "disk_lifecycle_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mirror_root = manifest_path.parent
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    plan = install_mod.build_plan(
        manifest,
        home=home,
        mirror_root=mirror_root,
    )
    assert len(plan) == 7


def test_repository_manifest_verify_hashes_passes():
    """--verify-manifest-hashes must pass against the checked-in manifest.

    Regression guard for the incident class this bundle exists to close:
    editing a manifest-governed file without regenerating its sha256 pin
    silently rolls back every deploy of the whole bundle.
    """
    manifest_path = (
        Path(__file__).resolve().parents[1] / "disk_lifecycle_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mismatches = install_mod.verify_manifest_hashes(manifest, manifest_path.parent)
    assert mismatches == []


def test_resolve_src_repo_base(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / "scripts").mkdir()
    target = repo_root / "scripts" / "worktree_safety.py"
    target.write_text("# repo-relative source\n")

    mirror_root = repo_root / "machine-setup" / "mini-scripts"
    mirror_root.mkdir(parents=True)

    entry = {"src_rel": "scripts/worktree_safety.py", "src_base": "repo"}
    resolved = install_mod._resolve_src(entry, mirror_root)
    assert resolved == target
    assert resolved.read_text() == "# repo-relative source\n"


def test_resolve_src_repo_base_no_git_raises(tmp_path):
    mirror_root = tmp_path / "no-repo-here" / "mini-scripts"
    mirror_root.mkdir(parents=True)
    entry = {"src_rel": "scripts/worktree_safety.py", "src_base": "repo"}
    with pytest.raises(install_mod.InstallError, match="could not locate repo root"):
        install_mod._resolve_src(entry, mirror_root)


def test_resolve_src_unknown_base_raises(tmp_path):
    entry = {"src_rel": "x.py", "src_base": "bogus"}
    with pytest.raises(install_mod.InstallError, match="unknown src_base"):
        install_mod._resolve_src(entry, tmp_path)


def test_verify_manifest_hashes_detects_drift(bundle):
    mismatches = install_mod.verify_manifest_hashes(bundle["manifest"], bundle["mirror"])
    assert mismatches == []

    target = bundle["mirror"] / "disk_space_alert.py"
    target.write_text("# mutated after pin was recorded\n")
    mismatches = install_mod.verify_manifest_hashes(bundle["manifest"], bundle["mirror"])
    assert len(mismatches) == 1
    assert "disk_space_alert.py" in mismatches[0]


def test_write_manifest_hashes_updates_only_drifted_entries(tmp_path, bundle):
    manifest_path = tmp_path / "written_manifest.json"
    manifest_path.write_text(json.dumps(bundle["manifest"], indent=2))
    manifest = json.loads(manifest_path.read_text())

    # No drift yet: zero entries change.
    changed = install_mod.write_manifest_hashes(manifest, manifest_path, bundle["mirror"])
    assert changed == 0

    # Mutate one source, then confirm write picks up exactly that one entry
    # and verify then passes clean.
    target = bundle["mirror"] / "kanban_workspace_sweep.py"
    target.write_text("# mutated\n")
    manifest = json.loads(manifest_path.read_text())
    changed = install_mod.write_manifest_hashes(manifest, manifest_path, bundle["mirror"])
    assert changed == 1

    reloaded = json.loads(manifest_path.read_text())
    assert install_mod.verify_manifest_hashes(reloaded, bundle["mirror"]) == []


def test_cli_verify_manifest_hashes_exit_codes(tmp_path, bundle, capsys):
    rc = install_mod.main(
        [
            "--manifest",
            str(bundle["manifest_path"]),
            "--verify-manifest-hashes",
        ]
    )
    assert rc == 0
    assert "OK" in capsys.readouterr().out

    (bundle["mirror"] / "disk_space_alert.py").write_text("# mutated\n")
    rc = install_mod.main(
        [
            "--manifest",
            str(bundle["manifest_path"]),
            "--verify-manifest-hashes",
        ]
    )
    assert rc == 1
    assert "FAILED" in capsys.readouterr().err
