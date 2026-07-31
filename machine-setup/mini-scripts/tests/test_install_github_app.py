"""Manifest / install semantics for the hermes-github-app installer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install_github_app.py"
_spec = importlib.util.spec_from_file_location("install_github_app_under_test", SCRIPT)
install_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = install_mod
_spec.loader.exec_module(install_mod)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True)
    home = tmp_path / "home"
    scripts_dir = home / ".hermes" / "scripts"
    scripts_dir.mkdir(parents=True)
    (home / ".hermes" / "github-app.op-env").write_text("# co-exist\n")

    script_specs = [
        ("github_app_token.py", "~/.hermes/scripts/github_app_token.py", "script"),
        ("github_app_cred.sh", "~/.hermes/scripts/github_app_cred.sh", "script"),
        (
            "thermal_github_app_probe.py",
            "~/.hermes/scripts/thermal_github_app_probe.py",
            "script",
        ),
    ]

    files = []
    for rel, dest_abs, deploy_mode in script_specs:
        data = f"# synthetic {rel}\n".encode()
        path = mirror / rel
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
        "bundle": "hermes-github-app",
        "source_task": "86e2k42qu",
        "coexist_required": [
            {
                "dest_abs": "~/.hermes/github-app.op-env",
                "reason": "credential env file",
            }
        ],
        "files": files,
    }
    manifest_path = mirror / "github_app_manifest.json"
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
    installs = sorted((home / ".hermes" / "logs" / "github-app-installs").iterdir())
    return json.loads((installs[-1] / "install-receipt.json").read_text())


def test_dry_run_verifies_hashes_without_writing(bundle):
    assert _install(bundle, dry_run=True) == 0
    assert not (bundle["home"] / ".hermes" / "scripts" / "github_app_token.py").exists()


def test_install_writes_all_scripts(bundle):
    assert _install(bundle) == 0
    home = bundle["home"]
    assert (home / ".hermes" / "scripts" / "github_app_token.py").is_file()
    assert (home / ".hermes" / "scripts" / "github_app_cred.sh").is_file()
    assert (home / ".hermes" / "scripts" / "thermal_github_app_probe.py").is_file()
    receipt = _receipt(home)
    assert receipt["result"] == "success"
    assert len(receipt["files"]) == 3


def test_refuses_hash_drift(bundle):
    target = bundle["mirror"] / "github_app_token.py"
    target.write_text("# mutated\n")
    with pytest.raises(install_mod.InstallError, match="hash drift"):
        install_mod.build_plan(
            bundle["manifest"],
            home=bundle["home"],
            mirror_root=bundle["mirror"],
        )


def test_repository_manifest_matches_live_sources(tmp_path):
    manifest_path = Path(__file__).resolve().parents[1] / "github_app_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mirror_root = manifest_path.parent
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes" / "github-app.op-env").write_text("# co-exist\n")
    plan = install_mod.build_plan(
        manifest,
        home=home,
        mirror_root=mirror_root,
    )
    assert len(plan) == 3
