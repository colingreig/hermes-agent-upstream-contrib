"""Regression coverage for collection-time ``cron.jobs`` path caching."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _snapshot_tree(root: Path) -> dict[str, bytes | None]:
    """Capture every directory and file below *root* using portable names."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
    }


def test_direct_multifile_collection_cannot_mutate_outer_cron_store(tmp_path):
    """Every test gets fresh cron paths even when the module was imported first.

    The child process deliberately imports ``cron.jobs`` while HERMES_HOME points
    at an outer sentinel profile, then directly collects the three files from the
    audit's failing import order.  A hook writes both ticker markers during each
    test, making stale heartbeat/success bindings observable as well as stale
    ``jobs.json`` bindings.  The outer profile is temporary, so even a fixture
    regression cannot reach a developer's real profile.
    """
    repo_root = Path(__file__).resolve().parents[2]
    outer_home = tmp_path / "outer-profile"
    outer_cron = outer_home / "cron"
    outer_cron.mkdir(parents=True)

    sentinels = {
        outer_cron / "jobs.json": b"[]\n",
        outer_cron / "ticker_heartbeat": b"outer-heartbeat-sentinel\n",
        outer_cron / "ticker_last_success": b"outer-success-sentinel\n",
    }
    for path, contents in sentinels.items():
        path.write_bytes(contents)
    outer_tree_before = _snapshot_tree(outer_cron)

    bootstrap = """
import sys

import pytest

import cron
import cron.jobs as cron_jobs


class HeartbeatProbe:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        assert cron.JOBS_FILE == cron_jobs.JOBS_FILE
        with cron_jobs._jobs_lock():
            pass
        cron_jobs.save_job_output("pytest-isolation-probe", "isolated output")
        cron_jobs.record_ticker_heartbeat(success=True)


raise SystemExit(pytest.main(sys.argv[1:], plugins=[HeartbeatProbe()]))
"""
    targets = [
        "tests/cron/test_jobs_changed_notify.py",
        "tests/hermes_cli/test_console_engine.py",
        "tests/cron/test_claim_job_for_fire.py",
    ]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(outer_home)
    env["PYTEST_ADDOPTS"] = ""
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap,
            "-q",
            "-p",
            "no:cacheprovider",
            *targets,
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, (
        "direct multi-file pytest run failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert _snapshot_tree(outer_cron) == outer_tree_before
