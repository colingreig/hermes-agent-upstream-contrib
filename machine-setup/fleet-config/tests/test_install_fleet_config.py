"""Manifest / install / rollback semantics for the fleet-config installer.

FULLY SANDBOXED: ``--home`` and ``--bundle-root`` are both ``tmp_path``
fixtures, so nothing here reads or writes a real ``~/.hermes``. A synthetic
manifest + synthetic config-overlay/profile/jobs.json files stand in for the
real bundle; these tests verify installer semantics, not the specific
canonical bytes shipped in this directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "install_fleet_config.py"
_spec = importlib.util.spec_from_file_location("install_fleet_config_under_test", SCRIPT)
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)

PROFILE_NAMES = ("coder", "content", "design", "research", "ops")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    """Build a synthetic bundle_root (source files + manifest) in tmp_path."""
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)

    overlay_data = yaml.safe_dump({"model": "synthetic-model"}).encode()
    (bundle_root / "config-overlay.yaml").write_bytes(overlay_data)

    profile_files = []
    for profile in PROFILE_NAMES:
        profile_cfg = f"# synthetic {profile} profile config\n".encode()
        profile_soul = f"# synthetic {profile} SOUL\n".encode()
        profile_dir = bundle_root / "profiles" / profile
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_bytes(profile_cfg)
        (profile_dir / "SOUL.md").write_bytes(profile_soul)
        profile_files.extend(
            [
                {
                    "src_rel": f"profiles/{profile}/config.yaml",
                    "dest_abs": f"~/.hermes/profiles/{profile}/config.yaml",
                    "sha256": _sha(profile_cfg),
                    "role": "synthetic profile config",
                    "deploy_mode": "profile_file",
                },
                {
                    "src_rel": f"profiles/{profile}/SOUL.md",
                    "dest_abs": f"~/.hermes/profiles/{profile}/SOUL.md",
                    "sha256": _sha(profile_soul),
                    "role": "synthetic profile soul",
                    "deploy_mode": "profile_file",
                },
            ]
        )

    jobs_data = json.dumps({"jobs": [{"id": "1", "name": "synthetic-job"}]}).encode()
    (bundle_root / "jobs.json").write_bytes(jobs_data)

    manifest = {
        "bundle": "fleet-config-test",
        "source_task": "test",
        "files": [
            {
                "src_rel": "config-overlay.yaml",
                "dest_abs": "~/.hermes/config.yaml",
                "sha256": _sha(overlay_data),
                "role": "synthetic overlay",
                "deploy_mode": "config_overlay",
            },
            *profile_files,
            {
                "src_rel": "jobs.json",
                "dest_abs": "~/.hermes/cron/jobs.json",
                "sha256": _sha(jobs_data),
                "role": "synthetic jobs",
                "deploy_mode": "jobs_json",
            },
        ],
    }
    manifest_path = bundle_root / "fleet_config_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "bundle_root": bundle_root,
        "home": home,
        "manifest_path": manifest_path,
        "manifest": manifest,
    }


def test_dry_run_writes_nothing(bundle, capsys):
    config_path = bundle["home"] / ".hermes" / "config.yaml"
    config_path.write_text("model: old\n", encoding="utf-8")
    os.chmod(config_path, 0o644)
    rc = install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        bundle_root=bundle["bundle_root"],
        manifest_path=bundle["manifest_path"],
        dry_run=True,
    )
    assert rc == 0
    assert yaml.safe_load(config_path.read_text()) == {"model": "old"}
    assert config_path.stat().st_mode & 0o777 == 0o644
    assert not (bundle["home"] / ".hermes" / "cron" / "jobs.json").exists()
    out = capsys.readouterr().out
    assert "mode: 0644 -> 0600" in out
    assert "dry-run: verified all source hashes; wrote nothing." in out


def test_real_install_writes_all_three_destinations(bundle):
    rc = install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        bundle_root=bundle["bundle_root"],
        manifest_path=bundle["manifest_path"],
        dry_run=False,
    )
    assert rc == 0
    # The test home's installer lease must not leak to the process/default
    # Hermes home; profile and --home isolation applies to its state DB too.
    assert (bundle["home"] / ".hermes" / "state" / "production-write-lease.db").is_file()
    cfg = yaml.safe_load((bundle["home"] / ".hermes" / "config.yaml").read_text())
    assert cfg["model"] == "synthetic-model"
    assert (bundle["home"] / ".hermes" / "config.yaml").stat().st_mode & 0o777 == 0o600
    for profile in PROFILE_NAMES:
        profile_dir = bundle["home"] / ".hermes" / "profiles" / profile
        assert (profile_dir / "config.yaml").is_file()
        assert (profile_dir / "config.yaml").stat().st_mode & 0o777 == 0o600
        assert (profile_dir / "SOUL.md").is_file()
    jobs = json.loads((bundle["home"] / ".hermes" / "cron" / "jobs.json").read_text())
    assert jobs["jobs"][0]["name"] == "synthetic-job"
    # profile bootstrap dirs got created too
    for sub in install_mod.PROFILE_BOOTSTRAP_DIRS:
        assert (bundle["home"] / ".hermes" / "profiles" / "ops" / sub).is_dir()


def test_atomic_write_replaces_existing_file_privately(tmp_path):
    dest = tmp_path / "private" / "config.yaml"
    dest.parent.mkdir()
    dest.write_text("old\n", encoding="utf-8")
    dest.chmod(0o644)

    install_mod._atomic_write(dest, b"new\n")

    assert dest.read_bytes() == b"new\n"
    assert dest.stat().st_mode & 0o777 == 0o600
    assert not list(dest.parent.glob(".config.yaml.tmp-*"))


def test_atomic_write_skips_hostile_temp_symlink(tmp_path, monkeypatch):
    dest = tmp_path / "private" / "config.yaml"
    dest.parent.mkdir()
    outside = tmp_path / "outside-secret.yaml"
    outside.write_text("outside\n", encoding="utf-8")
    hostile = dest.parent / ".config.yaml.tmp-taken"
    hostile.symlink_to(outside)
    names = iter(("taken", "safe"))
    monkeypatch.setattr(install_mod.secrets, "token_hex", lambda _bytes: next(names))

    install_mod._atomic_write(dest, b"safe\n")

    assert dest.read_bytes() == b"safe\n"
    assert dest.stat().st_mode & 0o777 == 0o600
    assert hostile.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_snapshot_dir_disambiguates_repeated_same_second_receipts(tmp_path):
    destination_root = tmp_path / ".hermes"
    destination_root.mkdir()

    first = install_mod._create_snapshot_dir(destination_root, "20260801T010101Z")
    second = install_mod._create_snapshot_dir(destination_root, "20260801T010101Z")

    assert first.name == "20260801T010101Z"
    assert second.name == "20260801T010101Z-01"
    assert first.is_dir()
    assert second.is_dir()


def test_same_second_full_installs_keep_distinct_sibling_backup_bytes(bundle, monkeypatch):
    config_path = bundle["home"] / ".hermes" / "config.yaml"
    config_path.write_bytes(b"model: before-first\n")
    stamp = "20260801T010101Z"
    stamps = iter((stamp, stamp))
    monkeypatch.setattr(install_mod, "_utc_stamp", lambda: next(stamps))

    assert install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        bundle_root=bundle["bundle_root"],
        manifest_path=bundle["manifest_path"],
        dry_run=False,
    ) == 0
    config_path.write_bytes(b"model: before-second\n")
    assert install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        bundle_root=bundle["bundle_root"],
        manifest_path=bundle["manifest_path"],
        dry_run=False,
    ) == 0

    first = config_path.with_name(f"config.yaml.bak-fleet-config-install-{stamp}")
    second = config_path.with_name(f"config.yaml.bak-fleet-config-install-{stamp}-01")
    assert first.read_bytes() == b"model: before-first\n"
    assert second.read_bytes() == b"model: before-second\n"


def test_direct_clickup_contract_rejects_synthetic_or_drifted_jobs_before_writing(bundle):
    bundle["manifest"]["fleet_contract"] = install_mod.FLEET_JOBS_CONTRACT

    with pytest.raises(install_mod.InstallError, match="missing 'clickup-executor'"):
        install_mod.build_plan(
            bundle["manifest"],
            home=bundle["home"],
            bundle_root=bundle["bundle_root"],
        )

    assert not (bundle["home"] / ".hermes" / "config.yaml").exists()


def test_existing_config_and_its_install_backups_are_repaired_to_0600(bundle):
    config_path = bundle["home"] / ".hermes" / "config.yaml"
    config_path.write_text("model: old\n", encoding="utf-8")
    config_path.chmod(0o644)

    assert install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        bundle_root=bundle["bundle_root"],
        manifest_path=bundle["manifest_path"],
        dry_run=False,
    ) == 0

    assert config_path.stat().st_mode & 0o777 == 0o600
    sibling_backups = list(config_path.parent.glob("config.yaml.bak-fleet-config-install-*"))
    assert len(sibling_backups) == 1
    assert sibling_backups[0].stat().st_mode & 0o777 == 0o600
    receipts = list((bundle["home"] / ".hermes" / "logs" / "fleet-config-installs").iterdir())
    central_backup = receipts[0] / "destinations" / "config.yaml"
    assert central_backup.stat().st_mode & 0o777 == 0o600


def test_dry_run_reports_multiple_legacy_root_backups_without_mutating(bundle, capsys):
    hermes_dir = bundle["home"] / ".hermes"
    first = hermes_dir / "config.yaml.bak-fleet-config-install-20260729T010203Z"
    second = hermes_dir / "config.yaml.bak-fleet-config-install-20260730T040506Z"
    first.write_text("secret: first\n", encoding="utf-8")
    second.write_text("secret: second\n", encoding="utf-8")
    first.chmod(0o644)
    second.chmod(0o640)

    # Similar and profile-scoped names are deliberately not governed.
    loose_match = hermes_dir / "config.yaml.bak-fleet-config-install-manual"
    loose_match.write_text("leave: alone\n", encoding="utf-8")
    loose_match.chmod(0o644)
    profile_backup = hermes_dir / "profiles" / "coder" / "config.yaml.bak-fleet-config-install-20260729T010203Z"
    profile_backup.parent.mkdir(parents=True)
    profile_backup.write_text("leave: profile-alone\n", encoding="utf-8")
    profile_backup.chmod(0o644)

    assert install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        bundle_root=bundle["bundle_root"],
        manifest_path=bundle["manifest_path"],
        dry_run=True,
    ) == 0

    assert first.stat().st_mode & 0o777 == 0o644
    assert second.stat().st_mode & 0o777 == 0o640
    assert loose_match.stat().st_mode & 0o777 == 0o644
    assert profile_backup.stat().st_mode & 0o777 == 0o644
    out = capsys.readouterr().out
    assert f"{first}: 0644 -> 0600" in out
    assert f"{second}: 0640 -> 0600" in out
    assert str(loose_match) not in out
    assert str(profile_backup) not in out


def test_legacy_root_backup_modes_are_normalized_idempotently(bundle, monkeypatch):
    hermes_dir = bundle["home"] / ".hermes"
    backups = [
        hermes_dir / "config.yaml.bak-fleet-config-install-20260729T010203Z",
        hermes_dir / "config.yaml.bak-fleet-config-install-20260730T040506Z",
    ]
    for index, backup in enumerate(backups):
        backup.write_text(f"secret: {index}\n", encoding="utf-8")
        backup.chmod(0o644 if index == 0 else 0o640)

    stamps = iter(("20260801T010101Z", "20260801T010102Z"))
    monkeypatch.setattr(install_mod, "_utc_stamp", lambda: next(stamps))
    for _ in range(2):
        assert install_mod.install(
            bundle["manifest"],
            home=bundle["home"],
            bundle_root=bundle["bundle_root"],
            manifest_path=bundle["manifest_path"],
            dry_run=False,
        ) == 0
        assert all(backup.stat().st_mode & 0o777 == 0o600 for backup in backups)

    receipts = sorted((hermes_dir / "logs" / "fleet-config-installs").glob("*/install-receipt.json"))
    assert len(receipts) == 2
    second_receipt = json.loads(receipts[1].read_text())
    governed = [
        step for step in second_receipt["steps"]
        if step["step"] == "legacy_config_backup_mode"
        and Path(step["dest"]) in backups
    ]
    assert len(governed) == 2
    assert all(step["prior_mode"] == "0600" for step in governed)
    assert all(step["status"] == "already-governed" for step in governed)


@pytest.mark.parametrize("unsafe_kind", ["symlink", "directory"])
def test_unsafe_legacy_root_backup_entry_fails_closed(bundle, tmp_path, unsafe_kind):
    hermes_dir = bundle["home"] / ".hermes"
    unsafe = hermes_dir / "config.yaml.bak-fleet-config-install-20260729T010203Z"
    if unsafe_kind == "symlink":
        outside = tmp_path / "outside-secret.yaml"
        outside.write_text("secret: untouched\n", encoding="utf-8")
        outside.chmod(0o644)
        unsafe.symlink_to(outside)
    else:
        unsafe.mkdir()

    with pytest.raises(install_mod.InstallError, match="legacy config backup|outside"):
        install_mod.install(
            bundle["manifest"],
            home=bundle["home"],
            bundle_root=bundle["bundle_root"],
            manifest_path=bundle["manifest_path"],
            dry_run=False,
        )

    assert not (hermes_dir / "logs" / "fleet-config-installs").exists()
    if unsafe_kind == "symlink":
        assert outside.stat().st_mode & 0o777 == 0o644


def test_late_failure_restores_exact_legacy_backup_modes(bundle, monkeypatch):
    hermes_dir = bundle["home"] / ".hermes"
    first = hermes_dir / "config.yaml.bak-fleet-config-install-20260729T010203Z"
    second = hermes_dir / "config.yaml.bak-fleet-config-install-20260730T040506Z"
    first.write_text("secret: first\n", encoding="utf-8")
    second.write_text("secret: second\n", encoding="utf-8")
    first.chmod(0o644)
    second.chmod(0o640)

    real_atomic_write = install_mod._atomic_write
    calls = {"count": 0}

    def fail_after_mode_normalization(dest, data):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated failure after legacy mode normalization")
        return real_atomic_write(dest, data)

    monkeypatch.setattr(install_mod, "_atomic_write", fail_after_mode_normalization)
    with pytest.raises(OSError, match="after legacy mode normalization"):
        install_mod.install(
            bundle["manifest"],
            home=bundle["home"],
            bundle_root=bundle["bundle_root"],
            manifest_path=bundle["manifest_path"],
            dry_run=False,
        )

    assert first.stat().st_mode & 0o777 == 0o644
    assert second.stat().st_mode & 0o777 == 0o640
    receipt_path = next((hermes_dir / "logs" / "fleet-config-installs").glob("*/install-receipt.json"))
    receipt = json.loads(receipt_path.read_text())
    mode_steps = [step for step in receipt["steps"] if step["step"] == "legacy_config_backup_mode"]
    assert {step["prior_mode"] for step in mode_steps} == {"0640", "0644"}
    assert all(step["status"] == "rolled-back" for step in mode_steps)


def test_forced_failure_mid_step_rolls_back_and_reraises(bundle, monkeypatch):
    """FIX 3 regression test: a non-InstallError exception raised during the
    write phase (e.g. an OSError from the filesystem) must still trigger
    rollback of whatever was already written, and must propagate rather than
    being silently swallowed.
    """
    home = bundle["home"]
    hermes_dir = home / ".hermes"

    # Pre-existing config.yaml that must survive the failed install untouched.
    original_cfg = {"model": "pre-existing-model", "untouched": True}
    (hermes_dir / "config.yaml").write_text(yaml.safe_dump(original_cfg), encoding="utf-8")

    real_atomic_write = install_mod._atomic_write
    calls = {"n": 0}

    def flaky_atomic_write(dest, data):
        calls["n"] += 1
        # Let step 1 (config_overlay) succeed so there is something on disk
        # to prove rollback restored it; blow up on the very next write
        # (a profile file), simulating an unexpected OSError mid-step.
        if calls["n"] == 2:
            raise OSError("simulated disk failure mid-step")
        return real_atomic_write(dest, data)

    monkeypatch.setattr(install_mod, "_atomic_write", flaky_atomic_write)

    with pytest.raises(OSError, match="simulated disk failure mid-step"):
        install_mod.install(
            bundle["manifest"],
            home=home,
            bundle_root=bundle["bundle_root"],
            manifest_path=bundle["manifest_path"],
            dry_run=False,
        )

    # Rollback must have restored the pre-existing config.yaml exactly.
    restored = yaml.safe_load((hermes_dir / "config.yaml").read_text())
    assert restored == original_cfg

    # The failed write must not have left jobs.json behind (it never ran).
    assert not (hermes_dir / "cron" / "jobs.json").exists()

    # A failure receipt must still have been written even though we re-raised.
    install_dirs = list((hermes_dir / "logs" / "fleet-config-installs").iterdir())
    assert len(install_dirs) == 1
    receipt = json.loads((install_dirs[0] / "install-receipt.json").read_text())
    assert receipt["result"] == "failed"
    assert "simulated disk failure" in receipt["failure_detail"]


def test_lost_fence_refuses_installer_rollback_after_successor_handoff(bundle, monkeypatch):
    """An old installer must not restore bytes once its write fence is gone."""
    home = bundle["home"]
    config = home / ".hermes" / "config.yaml"
    config.write_text("model: old-owner\n", encoding="utf-8")
    real_atomic_write = install_mod._atomic_write
    real_fence = install_mod._heartbeat_production_write_lease
    fence_calls = {"count": 0}
    handoff_done = {"value": False}

    def successor_has_fence(lease, *, home):
        fence_calls["count"] += 1
        # Let one rollback restoration run, then give the successor the next
        # fence before the remaining old-owner restoration boundary.
        if fence_calls["count"] >= 6 and not handoff_done["value"]:
            db = home / ".hermes" / "state" / "production-write-lease.db"
            install_mod.production_write_lease.release(
                lease_id=lease.lease_id, actor=lease.actor, session_id=lease.session_id,
                fencing_token=lease.fencing_token, database_path=db,
            )
            successor = install_mod.production_write_lease.acquire(
                ["fleet-config", "cron-jobs", "skills-policy"], "fleet-config-installer",
                "successor", str(home / ".hermes"), "hermes-agent", lease.commit_sha,
                "successor owns rollback", database_path=db,
            )
            assert successor.fencing_token > lease.fencing_token
            handoff_done["value"] = True
        return real_fence(lease, home=home)

    writes = {"count": 0}
    def fail_after_config(dest, data):
        writes["count"] += 1
        if writes["count"] == 2:
            raise OSError("simulated successor handoff")
        return real_atomic_write(dest, data)

    monkeypatch.setattr(install_mod, "_heartbeat_production_write_lease", successor_has_fence)
    monkeypatch.setattr(install_mod, "_atomic_write", fail_after_config)
    with pytest.raises(OSError, match="simulated successor handoff"):
        install_mod.install(bundle["manifest"], home=home, bundle_root=bundle["bundle_root"], manifest_path=bundle["manifest_path"], dry_run=False)
    assert "synthetic-model" in config.read_text(encoding="utf-8")


def test_late_failure_restores_every_distinct_profile_snapshot(bundle, monkeypatch):
    """A post-journal jobs failure restores all 12 non-aliasing snapshots."""
    home = bundle["home"]
    hermes_dir = home / ".hermes"
    original_cfg = b"model: original-main\n"
    (hermes_dir / "config.yaml").write_bytes(original_cfg)
    original_jobs = b'{"jobs":[{"id":"old","name":"original-job"}]}\n'
    jobs_dest = hermes_dir / "cron" / "jobs.json"
    jobs_dest.parent.mkdir(parents=True)
    jobs_dest.write_bytes(original_jobs)

    originals = {}
    for profile in PROFILE_NAMES:
        profile_dir = hermes_dir / "profiles" / profile
        profile_dir.mkdir(parents=True)
        config_bytes = f"model: original-{profile}\n".encode()
        soul_bytes = f"# Original {profile} soul\n".encode()
        (profile_dir / "config.yaml").write_bytes(config_bytes)
        (profile_dir / "SOUL.md").write_bytes(soul_bytes)
        originals[profile] = (config_bytes, soul_bytes)

    real_sha256 = install_mod._sha256

    def fail_after_jobs_replace(path):
        if path == jobs_dest:
            raise OSError("simulated late failure after jobs replacement")
        return real_sha256(path)

    monkeypatch.setattr(install_mod, "_sha256", fail_after_jobs_replace)

    with pytest.raises(OSError, match="simulated late failure after jobs replacement"):
        install_mod.install(
            bundle["manifest"],
            home=home,
            bundle_root=bundle["bundle_root"],
            manifest_path=bundle["manifest_path"],
            dry_run=False,
        )

    assert (hermes_dir / "config.yaml").read_bytes() == original_cfg
    for profile, (config_bytes, soul_bytes) in originals.items():
        profile_dir = hermes_dir / "profiles" / profile
        assert (profile_dir / "config.yaml").read_bytes() == config_bytes
        assert (profile_dir / "SOUL.md").read_bytes() == soul_bytes
    assert jobs_dest.read_bytes() == original_jobs

    install_dirs = list((hermes_dir / "logs" / "fleet-config-installs").iterdir())
    assert len(install_dirs) == 1
    receipt = json.loads((install_dirs[0] / "install-receipt.json").read_text())
    assert receipt["result"] == "failed"
    assert "simulated late failure after jobs replacement" in receipt["failure_detail"]
    snapshot_steps = [
        step
        for step in receipt["steps"]
        if step["step"] in {"config_overlay", "profile_file", "jobs_json"}
    ]
    snapshot_paths = [step["snapshot"] for step in snapshot_steps]
    assert len(snapshot_paths) == 12
    assert len(set(snapshot_paths)) == 12
    assert all(step["status"] == "rolled-back" for step in snapshot_steps)
