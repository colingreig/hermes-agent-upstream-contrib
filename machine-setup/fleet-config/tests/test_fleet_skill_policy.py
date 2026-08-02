"""Behavior contracts for the governed Hermes Mini skill surface policy."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest


FLEET_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = FLEET_ROOT / "install_fleet_config.py"
POLICY_PATH = FLEET_ROOT / "skills-policy.json"
_spec = importlib.util.spec_from_file_location("fleet_skill_policy_under_test", SCRIPT)
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)


def _load_policy():
    return install_mod.load_skill_policy(POLICY_PATH, bundle_root=FLEET_ROOT)


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _seed_home(tmp_path: Path, policy: dict) -> tuple[Path, Path]:
    home = tmp_path / "home"
    external = tmp_path / "ignite-skills" / "vehicle-image-qc"
    _write_skill(external, "vehicle-image-qc")
    sentry_repo_skill = home / ".hermes" / "repos" / "ignite-sentinel" / "hermes" / "SKILL.md"
    sentry_repo_skill.parent.mkdir(parents=True, exist_ok=True)
    sentry_repo_skill.write_text(
        "---\nname: sentry-monitor\n---\noperational monitor wrapper\n",
        encoding="utf-8",
    )

    all_bundled = {**policy["bundled"]["remove"], **policy["bundled"]["keep"]}
    for profile in install_mod.SKILL_POLICY_PROFILES:
        profile_home = install_mod._profile_home(home, profile)
        skills_dir = profile_home / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        if policy["profiles"][profile]["bundled_mode"] == "keep":
            for name, rel in all_bundled.items():
                source = policy["_source_root"] / rel
                shutil.copytree(source, skills_dir / rel)

    default_skills = home / ".hermes" / "skills"
    for name, rel in {**policy["local_remove"], **policy["required_local_keep"]}.items():
        path = default_skills / rel
        if name == "sentry-monitor":
            path.mkdir(parents=True, exist_ok=True)
            (path / "SKILL.md").symlink_to(sentry_repo_skill)
        else:
            _write_skill(path, name)
    for index, row in enumerate(policy["local_reference_consolidations"]):
        data = f"historical reference fixture {index}\n".encode("utf-8")
        row["source_sha256"] = hashlib.sha256(data).hexdigest()
        source = (
            default_skills
            / policy["local_remove"][row["source_skill"]]
            / row["source_rel"]
        )
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
    for name, spec in policy["hub_shadow_remove"].items():
        rel = spec["install_path"]
        _write_skill(default_skills / rel, name)
        lock = {
            "version": 1,
            "installed": {
                name: {
                    "source": spec["source"],
                    "identifier": spec["identifier"],
                    "trust_level": "community",
                    "install_path": rel,
                }
            },
        }
        lock_path = default_skills / ".hub" / "lock.json"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps(lock), encoding="utf-8")
    (default_skills / ".curator_suppressed").write_text("legacy-suppression\n", encoding="utf-8")
    return home, external


def _replace_with_description_residue(home: Path, policy: dict) -> Path:
    rel = policy["bundled"]["remove"]["ocr-and-documents"]
    target = home / ".hermes" / "skills" / rel
    shutil.rmtree(target)
    target.mkdir(parents=True)
    shutil.copy2(
        policy["_source_root"] / rel / "DESCRIPTION.md",
        target / "DESCRIPTION.md",
    )
    return target


def test_policy_exactly_classifies_current_bundled_catalog():
    policy = _load_policy()
    remove = policy["bundled"]["remove"]
    keep = policy["bundled"]["keep"]

    assert len(remove) == 52
    assert len(keep) == 22
    assert set(remove).isdisjoint(keep)
    assert set(remove) | set(keep) == {
        install_mod._frontmatter_name(path)
        for path in policy["_source_root"].rglob("SKILL.md")
    }
    assert policy["local_remove"] == {
        "clickup-queue-poller-merged-before-claim": (
            "autonomous-ai-agents/clickup-queue-poller-merged-before-claim"
        ),
        "clickup-task-capture": "operations/clickup-task-capture",
        "sentry-monitor": "sentry-monitor",
    }
    assert policy["required_local_keep"] == {
        "clickup-queue-poller": "clickup-queue-poller",
        "hermes-self-report": "hermes-self-report",
    }
    assert policy["profiles"]["default"]["expected_active_manifests"] == 24
    assert policy["profiles"]["coder"]["expected_active_manifests"] == 22
    assert policy["profiles"]["content"]["expected_active_manifests"] == 22
    assert policy["profiles"]["ops"]["expected_active_manifests"] == 22
    assert policy["profiles"]["design"]["expected_active_manifests"] == 0
    assert policy["profiles"]["research"]["expected_active_manifests"] == 0


def test_policy_is_sha_pinned_by_fleet_manifest(tmp_path):
    manifest = install_mod.load_manifest(FLEET_ROOT / "fleet_config_manifest.json")
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    plan = install_mod.build_plan(manifest, home=home, bundle_root=FLEET_ROOT)

    assert plan["skill_policy"] is not None
    assert plan["skill_policy"]["src"].resolve() == POLICY_PATH.resolve()
    assert plan["skill_policy"]["sha256"] == install_mod._sha256(POLICY_PATH)


def test_policy_apply_is_recoverable_idempotent_and_keeps_external_skill(tmp_path):
    policy = _load_policy()
    home, external = _seed_home(tmp_path, policy)
    destination_root = home / ".hermes"
    snapshot_dir = destination_root / "logs" / "fleet-config-installs" / "first"
    actions = install_mod.build_skill_policy_plan(policy, home=home)
    steps: list[dict] = []

    install_mod._apply_skill_policy(
        policy,
        actions,
        destination_root=destination_root,
        snapshot_dir=snapshot_dir,
        stamp="20260801T120000Z",
        receipt_steps=steps,
    )

    for action in actions:
        assert len(install_mod._active_skill_manifests(action["skills_dir"])) == action["expected_count"]
        suppressed = install_mod._read_suppressed(action["suppression_path"])
        expected_suppressed = set(policy["bundled"]["remove"])
        if policy["profiles"][action["profile"]]["bundled_mode"] == "empty":
            expected_suppressed |= set(policy["bundled"]["keep"])
        assert expected_suppressed <= suppressed
        assert not (action["home"] / ".no-bundled-skills").exists()

    default_skills = destination_root / "skills"
    sentry_repo_skill = destination_root / "repos" / "ignite-sentinel" / "hermes" / "SKILL.md"
    sentry_repo_sha = install_mod._sha256(sentry_repo_skill)
    assert (default_skills / "clickup-queue-poller" / "SKILL.md").is_file()
    assert (default_skills / "hermes-self-report" / "SKILL.md").is_file()
    assert not (default_skills / policy["local_remove"]["clickup-task-capture"]).exists()
    assert not (default_skills / policy["local_remove"]["sentry-monitor"]).exists()
    sentry_archive = (
        destination_root
        / "archives"
        / "fleet-skill-policy"
        / policy["policy_id"]
        / "20260801T120000Z"
        / "default"
        / "local"
        / "sentry-monitor"
        / "SKILL.md"
    )
    assert sentry_archive.is_symlink()
    assert sentry_archive.readlink() == sentry_repo_skill
    assert sentry_repo_skill.is_file(), "ignite-sentinel operational monitor must remain installed"
    assert install_mod._sha256(sentry_repo_skill) == sentry_repo_sha
    vehicle_path = policy["hub_shadow_remove"]["vehicle-image-qc"]["install_path"]
    assert not (default_skills / vehicle_path).exists()
    for row in policy["local_reference_consolidations"]:
        destination = (
            default_skills
            / policy["required_local_keep"][row["destination_skill"]]
            / row["destination_rel"]
        )
        assert install_mod._sha256(destination) == row["source_sha256"]
    lock = json.loads((default_skills / ".hub" / "lock.json").read_text(encoding="utf-8"))
    assert "vehicle-image-qc" not in lock["installed"]
    assert (external / "SKILL.md").is_file(), "canonical external skill must be untouched"
    assert len([step for step in steps if step["step"] == "skill_policy_backup"]) == 6
    assert len([step for step in steps if step["step"] == "skill_policy_archive"]) == 212
    sentry_step = next(
        step
        for step in steps
        if step["step"] == "skill_policy_archive" and step["name"] == "sentry-monitor"
    )
    assert sentry_step["kind"] == "local"
    assert Path(sentry_step["archive_dest"]) == sentry_archive.parent
    consolidations = [
        step for step in steps if step["step"] == "skill_policy_reference_consolidation"
    ]
    assert len(consolidations) == 2
    first_local_archive = next(
        index for index, step in enumerate(steps)
        if step["step"] == "skill_policy_archive" and step["kind"] == "local"
    )
    assert all(steps.index(step) < first_local_archive for step in consolidations)

    # A second application has no moves, metadata writes, or backups.
    second_actions = install_mod.build_skill_policy_plan(policy, home=home)
    second_steps: list[dict] = []
    install_mod._apply_skill_policy(
        policy,
        second_actions,
        destination_root=destination_root,
        snapshot_dir=destination_root / "logs" / "fleet-config-installs" / "second",
        stamp="20260801T120100Z",
        receipt_steps=second_steps,
    )
    assert second_steps == []
    assert sentry_archive.is_symlink()
    assert sentry_repo_skill.is_file()
    assert install_mod._sha256(sentry_repo_skill) == sentry_repo_sha

    # Installer rollback restores all active skills and hub/suppression metadata.
    install_mod._rollback(steps)
    assert len(install_mod._active_skill_manifests(default_skills)) == 80
    restored_lock = json.loads((default_skills / ".hub" / "lock.json").read_text(encoding="utf-8"))
    assert "vehicle-image-qc" in restored_lock["installed"]
    assert install_mod._read_suppressed(default_skills / ".curator_suppressed") == {"legacy-suppression"}
    restored_sentry = default_skills / policy["local_remove"]["sentry-monitor"] / "SKILL.md"
    assert restored_sentry.is_symlink()
    assert restored_sentry.readlink() == sentry_repo_skill
    assert sentry_repo_skill.is_file()
    assert install_mod._sha256(sentry_repo_skill) == sentry_repo_sha
    for row in policy["local_reference_consolidations"]:
        source = (
            default_skills
            / policy["local_remove"][row["source_skill"]]
            / row["source_rel"]
        )
        destination = (
            default_skills
            / policy["required_local_keep"][row["destination_skill"]]
            / row["destination_rel"]
        )
        assert install_mod._sha256(source) == row["source_sha256"]
        assert not destination.exists()
    assert (external / "SKILL.md").is_file()


def test_policy_dry_run_reports_plan_without_mutation(tmp_path, capsys, monkeypatch):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    before = sorted(str(path.relative_to(home)) for path in home.rglob("SKILL.md"))
    monkeypatch.setattr(install_mod, "load_skill_policy", lambda *_args, **_kwargs: policy)

    manifest_path = FLEET_ROOT / "fleet_config_manifest.json"
    manifest = install_mod.load_manifest(manifest_path)
    assert install_mod.install(
        manifest,
        home=home,
        bundle_root=FLEET_ROOT,
        manifest_path=manifest_path,
        dry_run=True,
        skill_policy_path=POLICY_PATH,
    ) == 0

    assert sorted(str(path.relative_to(home)) for path in home.rglob("SKILL.md")) == before
    assert not (home / ".hermes" / "config.yaml").exists()
    assert not (home / ".hermes" / "cron" / "jobs.json").exists()
    out = capsys.readouterr().out
    assert "default: archive 56, consolidate 2, suppress +52, active 80 -> 24" in out
    assert "design: archive 0, consolidate 0, suppress +74, active 0 -> 0" in out
    assert "broad .no-bundled-skills marker: untouched" in out


def test_policy_accepts_exact_bundled_description_only_residue(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)

    actions = install_mod.build_skill_policy_plan(policy, home=home)
    default = next(action for action in actions if action["profile"] == "default")
    expected_current = (
        len(policy["bundled"]["remove"])
        + len(policy["bundled"]["keep"])
        - 1  # metadata-only residue has no active manifest
        + len(policy["local_remove"])
        + len(policy["required_local_keep"])
        + len(policy["hub_shadow_remove"])
    )

    assert residue not in {row["path"] for row in default["targets"]}
    assert default["current_count"] == expected_current
    assert default["predicted_count"] == default["expected_count"] == 24


def test_policy_refuses_symlinked_bundled_residue(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)
    shutil.rmtree(residue)
    symlink_target = home / ".hermes" / "symlink-target"
    symlink_target.mkdir()
    residue.symlink_to(symlink_target)

    with pytest.raises(install_mod.InstallError, match="symlinked skill path"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_non_directory_bundled_residue(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)
    shutil.rmtree(residue)
    residue.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="not a skill directory"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_symlinked_bundled_residue_description(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)
    description = residue / "DESCRIPTION.md"
    description.unlink()
    description.symlink_to(
        policy["_source_root"]
        / policy["bundled"]["remove"]["ocr-and-documents"]
        / "DESCRIPTION.md"
    )

    with pytest.raises(
        install_mod.InstallError,
        match="DESCRIPTION.md is not a regular file",
    ):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_extra_bundled_residue_bytes(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)
    (residue / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="contain only DESCRIPTION.md"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_nested_skill_in_bundled_residue(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)
    _write_skill(residue / "nested", "unexpected-nested-skill")

    with pytest.raises(install_mod.InstallError, match="nested skill manifests"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_tampered_bundled_description_residue(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    residue = _replace_with_description_residue(home, policy)
    (residue / "DESCRIPTION.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="does not match source"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_bundled_residue_without_source_metadata(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    _replace_with_description_residue(home, policy)
    replacement_source = tmp_path / "source"
    replacement_source.mkdir()
    policy["_source_root"] = replacement_source

    with pytest.raises(
        install_mod.InstallError,
        match="no regular source DESCRIPTION.md",
    ):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_local_vehicle_skill_without_matching_hub_provenance(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    lock_path = home / ".hermes" / "skills" / ".hub" / "lock.json"
    lock_path.write_text('{"version":1,"installed":{}}\n', encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="no matching hub provenance"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_symlinked_local_skill_manifest_except_sentry_wrapper(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    skill_dir = (
        home
        / ".hermes"
        / "skills"
        / policy["local_remove"]["clickup-task-capture"]
    )
    manifest = skill_dir / "SKILL.md"
    manifest.unlink()
    outside_manifest = tmp_path / "outside-skill.md"
    outside_manifest.write_text(
        "---\nname: clickup-task-capture\n---\noutside\n",
        encoding="utf-8",
    )
    manifest.symlink_to(outside_manifest)

    with pytest.raises(install_mod.InstallError, match="symlinked manifest"):
        install_mod.build_skill_policy_plan(policy, home=home)

    # The separately governed Sentry wrapper is intentionally different: it
    # is a recoverable wrapper around the operational Sentinel checkout.
    sentry_manifest = (
        home
        / ".hermes"
        / "skills"
        / policy["local_remove"]["sentry-monitor"]
        / "SKILL.md"
    )
    assert sentry_manifest.is_symlink()


@pytest.mark.parametrize("field", ["source", "identifier"])
def test_policy_refuses_vehicle_shadow_with_wrong_hub_identity(tmp_path, field):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    lock_path = home / ".hermes" / "skills" / ".hub" / "lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["installed"]["vehicle-image-qc"][field] = "wrong-provenance"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="no matching hub provenance"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_policy_refuses_historical_reference_hash_drift(tmp_path):
    policy = _load_policy()
    home, _external = _seed_home(tmp_path, policy)
    row = policy["local_reference_consolidations"][0]
    source = (
        home / ".hermes" / "skills"
        / policy["local_remove"][row["source_skill"]]
        / row["source_rel"]
    )
    source.write_text("changed historical bytes\n", encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="reference consolidation source.*pinned"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_supplied_policy_requires_matching_manifest_entry_before_mutation(tmp_path):
    manifest = install_mod.load_manifest(FLEET_ROOT / "fleet_config_manifest.json")
    manifest["files"] = [
        entry for entry in manifest["files"] if entry.get("deploy_mode") != "skill_policy"
    ]
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)

    with pytest.raises(install_mod.InstallError, match="has no skill_policy entry"):
        install_mod.install(
            manifest,
            home=home,
            bundle_root=FLEET_ROOT,
            manifest_path=FLEET_ROOT / "fleet_config_manifest.json",
            dry_run=False,
            skill_policy_path=POLICY_PATH,
        )

    assert list((home / ".hermes").iterdir()) == []
