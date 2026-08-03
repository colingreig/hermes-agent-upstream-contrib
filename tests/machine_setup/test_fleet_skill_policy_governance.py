"""Governance contract for skills nothing in skills-policy.json classifies.

Hermes writes skills for itself from the background self-improvement review
fork.  On 2026-08-02 that loop created ``promise-validation`` under
``~/.hermes/skills/software-development/``, which pushed the default profile to
25 active manifests against a policy expecting 24 and hard-blocked every
``install_fleet_config.py`` run the next day (task 86e2kxk52).

The governed behaviour: an ungoverned skill is recoverably quarantined and
reported, never silently activated and never a blocked install.  Promotion into
the active fleet surface stays an explicit ``skills-policy.json`` edit.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_ROOT = REPO_ROOT / "machine-setup" / "fleet-config"
SCRIPT = FLEET_ROOT / "install_fleet_config.py"
POLICY_PATH = FLEET_ROOT / "skills-policy.json"

_spec = importlib.util.spec_from_file_location("fleet_skill_governance_under_test", SCRIPT)
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)


SELF_AUTHORED_REL = "software-development/promise-validation"


def _write_skill(path: Path, name: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


@pytest.fixture(scope="module")
def loaded_policy() -> dict:
    return install_mod.load_skill_policy(POLICY_PATH, bundle_root=FLEET_ROOT)


@pytest.fixture
def policy(loaded_policy) -> dict:
    # Deep-copy everything except the resolved Path handles the installer stores.
    clone = {
        key: copy.deepcopy(value)
        for key, value in loaded_policy.items()
        if not isinstance(value, Path)
    }
    for key, value in loaded_policy.items():
        if isinstance(value, Path):
            clone[key] = value
    return clone


@pytest.fixture
def home(tmp_path: Path, policy: dict) -> Path:
    """A minimal but policy-complete installed skill surface.

    Stub manifests stand in for the real bundled trees: this suite is about the
    counting/classification contract, not about bundled skill content.
    """
    home_dir = tmp_path / "home"
    all_bundled = {**policy["bundled"]["remove"], **policy["bundled"]["keep"]}
    for profile in install_mod.SKILL_POLICY_PROFILES:
        skills_dir = install_mod._profile_home(home_dir, profile) / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for name, rel in all_bundled.items():
            _write_skill(skills_dir / rel, name)

    default_skills = home_dir / ".hermes" / "skills"
    for name, rel in {**policy["local_remove"], **policy["required_local_keep"]}.items():
        _write_skill(default_skills / rel, name)
    for row in policy["local_reference_consolidations"]:
        data = f"historical reference fixture {row['source_rel']}\n".encode("utf-8")
        row["source_sha256"] = hashlib.sha256(data).hexdigest()
        source = default_skills / policy["local_remove"][row["source_skill"]] / row["source_rel"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
    installed = {}
    for name, spec in policy["hub_shadow_remove"].items():
        _write_skill(default_skills / spec["install_path"], name)
        installed[name] = {
            "source": spec["source"],
            "identifier": spec["identifier"],
            "trust_level": "community",
            "install_path": spec["install_path"],
        }
    lock_path = default_skills / ".hub" / "lock.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"version": 1, "installed": installed}), encoding="utf-8")
    return home_dir


def _apply(policy: dict, actions: list[dict], home: Path, stamp: str = "20260803T120000Z") -> list[dict]:
    steps: list[dict] = []
    install_mod._apply_skill_policy(
        policy,
        actions,
        destination_root=home / ".hermes",
        snapshot_dir=home / ".hermes" / "logs" / "fleet-config-installs" / stamp,
        stamp=stamp,
        receipt_steps=steps,
    )
    return steps


def _default_action(actions: list[dict]) -> dict:
    return next(action for action in actions if action["profile"] == "default")


def test_baseline_surface_has_no_ungoverned_skills(policy, home):
    actions = install_mod.build_skill_policy_plan(policy, home=home)

    assert all(action["ungoverned"] == [] for action in actions)
    assert _default_action(actions)["predicted_count"] == 24


def test_self_authored_skill_is_quarantined_instead_of_blocking_the_install(policy, home):
    """The exact 86e2kxk52 break: 25 active manifests against an expected 24."""
    default_skills = home / ".hermes" / "skills"
    self_authored = default_skills / SELF_AUTHORED_REL
    _write_skill(self_authored, "promise-validation")
    assert len(install_mod._active_skill_manifests(default_skills)) == 81

    actions = install_mod.build_skill_policy_plan(policy, home=home)
    action = _default_action(actions)

    assert [row["rel"] for row in action["ungoverned"]] == [SELF_AUTHORED_REL]
    assert action["ungoverned"][0]["name"] == "promise-validation"
    assert action["ungoverned"][0]["disposition"] == "discard"
    assert action["current_count"] == 81
    assert action["predicted_count"] == 24

    steps = _apply(policy, actions, home)

    assert not self_authored.exists()
    assert len(install_mod._active_skill_manifests(default_skills)) == 24
    archived = (
        home / ".hermes" / "archives" / "fleet-skill-policy" / policy["policy_id"]
        / "20260803T120000Z" / "default" / install_mod.UNGOVERNED_SKILL_KIND
        / SELF_AUTHORED_REL / "SKILL.md"
    )
    assert archived.is_file()

    quarantine_steps = [
        step
        for step in steps
        if step["step"] == "skill_policy_archive"
        and step["kind"] == install_mod.UNGOVERNED_SKILL_KIND
    ]
    assert len(quarantine_steps) == 1
    assert quarantine_steps[0]["name"] == "promise-validation"
    assert quarantine_steps[0]["disposition"] == "discard"
    assert "86e2kxk52" in quarantine_steps[0]["disposition_reason"]

    # A quarantine is as recoverable as any other policy archive.
    install_mod._rollback(steps)
    assert (self_authored / "SKILL.md").is_file()


def test_ungoverned_skill_without_a_recorded_disposition_still_quarantines(policy, home):
    default_skills = home / ".hermes" / "skills"
    _write_skill(default_skills / "operations" / "invented-later", "invented-later")

    actions = install_mod.build_skill_policy_plan(policy, home=home)
    action = _default_action(actions)

    assert action["ungoverned"][0]["disposition"] == install_mod.UNGOVERNED_SKILL_UNREVIEWED
    assert action["ungoverned"][0]["disposition_reason"] is None
    assert action["predicted_count"] == 24

    _apply(policy, actions, home)
    assert not (default_skills / "operations" / "invented-later").exists()


def test_ungoverned_skill_in_a_non_default_profile_is_quarantined(policy, home):
    coder_skills = install_mod._profile_home(home, "coder") / "skills"
    _write_skill(coder_skills / "self-authored-coder-skill", "self-authored-coder-skill")

    actions = install_mod.build_skill_policy_plan(policy, home=home)
    coder = next(action for action in actions if action["profile"] == "coder")

    assert [row["rel"] for row in coder["ungoverned"]] == ["self-authored-coder-skill"]
    assert coder["predicted_count"] == 22

    _apply(policy, actions, home)
    assert len(install_mod._active_skill_manifests(coder_skills)) == 22


def test_fail_mode_still_refuses_an_ungoverned_skill(policy, home):
    policy["ungoverned_active"]["mode"] = "fail"
    _write_skill(home / ".hermes" / "skills" / SELF_AUTHORED_REL, "promise-validation")

    with pytest.raises(install_mod.InstallError, match="is not classified by skill policy"):
        install_mod.build_skill_policy_plan(policy, home=home)


def test_manifest_vendored_inside_a_governed_skill_is_not_a_separate_skill(policy, home):
    """A reference package inside a kept skill is its content, not a 25th skill."""
    default_skills = home / ".hermes" / "skills"
    vendored = default_skills / "clickup-queue-poller" / "references" / "archived-pack"
    _write_skill(vendored, "archived-pack")

    actions = install_mod.build_skill_policy_plan(policy, home=home)
    action = _default_action(actions)

    assert action["ungoverned"] == []
    assert action["predicted_count"] == 24

    _apply(policy, actions, home)
    assert (vendored / "SKILL.md").is_file()


def test_nested_ungoverned_skills_quarantine_as_one_outermost_tree(policy, home):
    default_skills = home / ".hermes" / "skills"
    outer = default_skills / "self-authored"
    _write_skill(outer, "self-authored")
    _write_skill(outer / "nested", "self-authored-nested")

    actions = install_mod.build_skill_policy_plan(policy, home=home)
    action = _default_action(actions)

    assert [row["rel"] for row in action["ungoverned"]] == ["self-authored"]
    assert action["predicted_count"] == 24

    _apply(policy, actions, home)
    assert not outer.exists()


def test_dry_run_plan_names_every_quarantine(policy, home, capsys):
    _write_skill(home / ".hermes" / "skills" / SELF_AUTHORED_REL, "promise-validation")
    actions = install_mod.build_skill_policy_plan(policy, home=home)

    install_mod._print_skill_policy_plan(policy, actions)

    out = capsys.readouterr().out
    assert f"quarantine ungoverned skill {SELF_AUTHORED_REL}" in out
    assert "disposition=discard" in out


def test_shipped_policy_quarantines_and_resolves_promise_validation(loaded_policy):
    ungoverned = loaded_policy["ungoverned_active"]

    assert ungoverned["mode"] == "quarantine"
    disposition = ungoverned["dispositions"]["promise-validation"]
    assert disposition["decision"] == "discard"
    assert "86e2kxk52" in disposition["reason"]


def test_absent_ungoverned_section_keeps_the_historical_hard_stop():
    assert install_mod._validate_ungoverned_skills(None) == {"mode": "fail", "dispositions": {}}


@pytest.mark.parametrize(
    "value, match",
    [
        ([], "must be an object"),
        ({"mode": "quarantine", "extra": 1}, "unknown keys"),
        ({"mode": "ignore"}, "must be one of"),
        ({"mode": "quarantine", "dispositions": []}, "must be an object"),
        ({"mode": "quarantine", "dispositions": {"x": {"decision": "discard"}}}, "exactly decision and reason"),
        (
            {"mode": "quarantine", "dispositions": {"x": {"decision": "keep", "reason": "r"}}},
            "invalid decision",
        ),
        (
            {"mode": "quarantine", "dispositions": {"x": {"decision": "discard", "reason": "  "}}},
            "non-empty reason",
        ),
    ],
)
def test_ungoverned_section_fails_closed_on_malformed_input(value, match):
    with pytest.raises(install_mod.InstallError, match=match):
        install_mod._validate_ungoverned_skills(value)


def test_disposition_for_an_already_governed_name_is_refused(tmp_path):
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw["ungoverned_active"]["dispositions"]["clickup-queue-poller"] = {
        "decision": "discard",
        "reason": "a governed keep cannot also be an ungoverned disposition",
    }
    forged = tmp_path / "skills-policy.json"
    forged.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(install_mod.InstallError, match="dispositions for names it already governs"):
        install_mod.load_skill_policy(forged, bundle_root=FLEET_ROOT)
