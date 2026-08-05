"""Manifest / install / rollback semantics for the hermes-self-report installer.

FULLY SANDBOXED: every path (home, mirror root, brain checkout, logs) is a
``tmp_path`` fixture. Nothing here imports from, reads, or writes a real
``~/.hermes`` or the mini, and no live ledger/receipt is touched. A synthetic
manifest + synthetic source files stand in for the real bundle so the tests
verify semantics, not the specific canonical bytes.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install_self_report.py"
_spec = importlib.util.spec_from_file_location("install_self_report_under_test", SCRIPT)
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    """Build a synthetic mirror + brain checkout + manifest in tmp_path."""
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    brain = tmp_path / "brain"
    skill_src = brain / "hermes" / "skills" / "hermes-self-report"
    skill_src.mkdir(parents=True)
    home = tmp_path / "home"
    # co-exist files present so the happy path emits no warnings
    scripts_dir = home / ".hermes" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "queue_snapshot.json").write_text("{}\n")

    script_names = [
        "hermes_report_build.py",
        "hermes_report_build_v1lib.py",
        "hermes_report_build_v2.py",
        "hermes-metrics.py",
        "postmark_send_report.py",
        "hermes_self_report_delivery_probe.py",
    ]
    files = []
    for name in script_names:
        data = f"# synthetic {name}\nMARKER = {name!r}\n".encode()
        (mirror / name).write_bytes(data)
        files.append(
            {
                "src_rel": name,
                "src_base": "mirror",
                "dest_abs": f"~/.hermes/scripts/{name}",
                "sha256": _sha(data),
                "role": f"synthetic {name}",
                "deploy_mode": "script",
            }
        )
    skill_data = b"# synthetic SKILL.md\n"
    (skill_src / "SKILL.md").write_bytes(skill_data)
    files.append(
        {
            "src_rel": "hermes/skills/hermes-self-report/SKILL.md",
            "src_base": "brain",
            "dest_abs": "~/.hermes/skills/hermes-self-report/SKILL.md",
            "sha256": _sha(skill_data),
            "role": "synthetic skill",
            "deploy_mode": "skill",
        }
    )
    manifest = {
        "bundle": "hermes-self-report",
        "source_task": "86e2gnz60",
        "coexist_required": [
            {"dest_abs": "~/.hermes/scripts/queue_snapshot.json", "reason": "freshness"},
        ],
        "files": files,
    }
    manifest_path = mirror / "self_report_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {
        "mirror": mirror,
        "brain": brain,
        "home": home,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "script_names": script_names,
    }


def _install(bundle, **kw):
    return install_mod.install(
        bundle["manifest"],
        home=bundle["home"],
        mirror_root=bundle["mirror"],
        manifest_path=bundle["manifest_path"],
        brain_path=kw.pop("brain_path", None),
        include_skill=kw.pop("include_skill", False),
        dry_run=kw.pop("dry_run", False),
    )


def _receipt(home) -> dict:
    installs = sorted((home / ".hermes" / "logs" / "self-report-installs").iterdir())
    return json.loads((installs[-1] / "install-receipt.json").read_text())


# --- happy path -----------------------------------------------------------


def test_happy_path_installs_and_verifies(bundle):
    rc = _install(bundle)
    assert rc == 0
    scripts = bundle["home"] / ".hermes" / "scripts"
    for name in bundle["script_names"]:
        deployed = scripts / name
        assert deployed.is_file()
        entry = next(f for f in bundle["manifest"]["files"] if f["src_rel"] == name)
        assert _sha(deployed.read_bytes()) == entry["sha256"]
    assert (scripts / "self_report_manifest.json").read_bytes() == bundle[
        "manifest_path"
    ].read_bytes()
    receipt = _receipt(bundle["home"])
    assert receipt["result"] == "success"
    assert receipt["coexist_warnings"] == []
    assert {f["status"] for f in receipt["files"]} == {"installed"}
    # skill not requested → not installed
    assert not (bundle["home"] / ".hermes" / "skills").exists()


def test_include_skill_installs_skill(bundle):
    rc = _install(bundle, include_skill=True, brain_path=bundle["brain"])
    assert rc == 0
    skill = bundle["home"] / ".hermes" / "skills" / "hermes-self-report" / "SKILL.md"
    assert skill.is_file()


def test_include_skill_without_brain_path_refused(bundle):
    with pytest.raises(install_mod.InstallError):
        _install(bundle, include_skill=True)


# --- snapshot + rollback --------------------------------------------------


def test_snapshot_and_receipt_written(bundle):
    scripts = bundle["home"] / ".hermes" / "scripts"
    # a pre-existing dest so a snapshot copy + .bak are produced
    (scripts / "hermes_report_build.py").write_text("# OLD deployed bytes\n")
    _install(bundle)
    installs = sorted((bundle["home"] / ".hermes" / "logs" / "self-report-installs").iterdir())
    assert len(installs) == 1
    snap = installs[0]
    assert (snap / "install-receipt.json").is_file()
    assert (snap / "self_report_manifest.json").is_file()
    assert not (snap / "deployed-self_report_manifest.json").exists()
    assert (snap / "hermes_report_build.py").read_text() == "# OLD deployed bytes\n"
    baks = list(scripts.glob("hermes_report_build.py.bak-self-report-install-*"))
    assert len(baks) == 1
    assert baks[0].read_text() == "# OLD deployed bytes\n"


def test_source_hash_mismatch_fails_closed(bundle):
    # corrupt one source without updating the manifest sha
    (bundle["mirror"] / "hermes-metrics.py").write_bytes(b"# tampered\n")
    with pytest.raises(install_mod.InstallError):
        _install(bundle)
    # nothing installed
    assert not (bundle["home"] / ".hermes" / "scripts" / "hermes-metrics.py").exists()
    assert not (bundle["home"] / ".hermes" / "logs").exists()


def test_deployed_hash_mismatch_triggers_restore(bundle, monkeypatch):
    scripts = bundle["home"] / ".hermes" / "scripts"
    target = scripts / "hermes_report_build.py"
    target.write_text("# ORIGINAL deployed bytes\n")

    real_sha = install_mod._sha256

    def fake_sha(path):
        s = str(path)
        # only the freshly-deployed dest re-read returns a bogus hash;
        # source verification (mirror/) and snapshots stay truthful.
        if s == str(target):
            return "0" * 64
        return real_sha(path)

    monkeypatch.setattr(install_mod, "_sha256", fake_sha)
    rc = _install(bundle)
    assert rc == 1
    # the pre-existing dest was restored from its snapshot
    assert target.read_text() == "# ORIGINAL deployed bytes\n"
    receipt = _receipt(bundle["home"])
    assert receipt["result"] == "failed"
    statuses = {f["dest"]: f["status"] for f in receipt["files"]}
    assert statuses[str(target)] == "verify-failed"


def test_deployed_mismatch_removes_newly_created_files(bundle, monkeypatch):
    # no pre-existing report_build dest → on failure it must be deleted, not left
    scripts = bundle["home"] / ".hermes" / "scripts"
    target = scripts / "hermes_report_build.py"
    real_sha = install_mod._sha256

    def fake_sha(path):
        if str(path) == str(target):
            return "0" * 64
        return real_sha(path)

    monkeypatch.setattr(install_mod, "_sha256", fake_sha)
    rc = _install(bundle)
    assert rc == 1
    assert not target.exists()


# --- dry run --------------------------------------------------------------


def test_dry_run_writes_nothing(bundle):
    rc = _install(bundle, dry_run=True)
    assert rc == 0
    assert not (bundle["home"] / ".hermes" / "scripts" / "hermes_report_build.py").exists()
    assert not (bundle["home"] / ".hermes" / "scripts" / "self_report_manifest.json").exists()
    assert not (bundle["home"] / ".hermes" / "logs").exists()


def test_dry_run_still_verifies_source_hashes(bundle):
    (bundle["mirror"] / "postmark_send_report.py").write_bytes(b"# drift\n")
    with pytest.raises(install_mod.InstallError):
        _install(bundle, dry_run=True)


# --- out-of-bounds / protected paths --------------------------------------


def test_refuses_dest_outside_hermes(bundle):
    bundle["manifest"]["files"][0]["dest_abs"] = "~/evil/outside.py"
    with pytest.raises(install_mod.InstallError):
        _install(bundle)


def test_refuses_dest_with_dotdot_traversal(bundle):
    # Path.relative_to is lexical and does not collapse "..", so a naive
    # bounds check would let this manifest entry walk out of ~/.hermes into
    # ~/.ssh. Must be refused before anything is written.
    bundle["manifest"]["files"][0]["dest_abs"] = "~/.hermes/scripts/../../.ssh/x"
    with pytest.raises(install_mod.InstallError):
        _install(bundle)
    assert not (bundle["home"] / ".ssh").exists()
    assert not (bundle["home"] / ".hermes" / "logs").exists()


def test_refuses_symlinked_parent_escape(bundle):
    # Even without a literal ".." in the manifest, a symlinked intermediate
    # directory under ~/.hermes/scripts could redirect the write outside
    # ~/.hermes. The bounds check must resolve the closest existing ancestor
    # and catch that too.
    outside = bundle["home"].parent / "outside-target"
    outside.mkdir()
    scripts = bundle["home"] / ".hermes" / "scripts"
    (scripts / "evil-link").symlink_to(outside, target_is_directory=True)
    bundle["manifest"]["files"][0]["dest_abs"] = "~/.hermes/scripts/evil-link/x.py"
    with pytest.raises(install_mod.InstallError):
        _install(bundle)
    assert not (outside / "x.py").exists()
    assert not (bundle["home"] / ".hermes" / "logs").exists()


def test_refuses_protected_basename(bundle):
    bundle["manifest"]["files"][0]["dest_abs"] = "~/.hermes/scripts/queue_snapshot.json"
    with pytest.raises(install_mod.InstallError):
        _install(bundle)
    # the live runtime-state file is untouched
    assert (bundle["home"] / ".hermes" / "scripts" / "queue_snapshot.json").read_text() == "{}\n"


def test_missing_coexist_file_warns_not_fails(bundle):
    (bundle["home"] / ".hermes" / "scripts" / "queue_snapshot.json").unlink()
    rc = _install(bundle)
    assert rc == 0
    receipt = _receipt(bundle["home"])
    assert any("queue_snapshot.json" in w for w in receipt["coexist_warnings"])


# --- CLI entry ------------------------------------------------------------


def test_cli_dry_run(bundle, capsys):
    rc = install_mod.main(
        [
            "--manifest",
            str(bundle["manifest_path"]),
            "--home",
            str(bundle["home"]),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert not (bundle["home"] / ".hermes" / "logs").exists()
