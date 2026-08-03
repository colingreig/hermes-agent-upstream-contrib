"""Behavioral tests for the source-controlled Mini PR-pipeline deployer."""
from __future__ import annotations

import contextlib
import importlib.util
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
RECONCILER = SCRIPTS / "reconcile_pr_pipeline.py"
PIPELINE = SCRIPTS / "pr_pipeline"
PATCH_VERIFIER = SCRIPTS / "verify-hermes-patches.sh"
_COUNTER = 0

# Autonomous-merge activation made shadow/tier gates env-derived instead of
# hard-coded. Tests that assert a specific gate outcome must sandbox ALL of
# these so the ambient shell environment (e.g. a dev box with
# HERMES_AUTONOMOUS_MERGE=1 already exported) can never make them flaky.
_MERGE_ENV_KEYS = (
    "HERMES_MERGE_SHADOW",
    "HERMES_MERGE_ACTIVE",
    "VALIDATE_SHADOW",
    "HERMES_AUTONOMOUS_MERGE",
    "HERMES_AUTONOMOUS_MERGE_LOW",
    "HERMES_AUTONOMOUS_MERGE_MEDIUM",
    "HERMES_AUTONOMOUS_MERGE_HIGH",
)


def _cleared_merge_env(**overrides):
    """A patch.dict payload that blanks every merge-activation env var, then
    applies ``overrides`` on top."""
    cleared = {key: "" for key in _MERGE_ENV_KEYS}
    cleared.update(overrides)
    return cleared


def _load(path: Path, prefix: str):
    global _COUNTER
    _COUNTER += 1
    name = f"{prefix}_{_COUNTER}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PipelineDeploymentTests(unittest.TestCase):
    source_commit = "a" * 40

    def setUp(self):
        self.mod = _load(RECONCILER, "reconcile_pr_pipeline_test")
        self.real_resolve_manifest = self.mod.resolve_manifest
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = Path(self.tmp.name) / "scripts"
        self.runtime_python = Path(sys.executable)
        self.local_patch_bytes = b"test-local verify-hermes-patches.sh\n"
        self.fixture_manifest = self._fixture_manifest_for_local_patch(self.local_patch_bytes)
        self.mod.resolve_manifest = lambda path=self.mod.DEFAULT_MANIFEST: self.fixture_manifest
        self.addCleanup(lambda: setattr(self.mod, "resolve_manifest", self.real_resolve_manifest))

    def _install(self, *, runtime_python=None):
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        self._write_jobs([self._stable_ci_health_job()])
        return self.mod.install(
            self.destination,
            source_commit=self.source_commit,
            runtime_python=runtime_python or self.runtime_python,
        )

    def _jobs_path(self):
        return self.destination.parent / "cron" / "jobs.json"

    def _stable_ci_health_job(self, **updates):
        job = {
            "id": "stable-ci-health-id",
            "name": "ci-health-watch",
            "schedule": {"kind": "cron", "expr": "17 */2 * * *", "display": "17 */2 * * *"},
            "schedule_display": "17 */2 * * *",
            "script": "ci-health-watch-cron.py",
            "no_agent": True,
            "enabled": True,
            "state": "scheduled",
            "next_run_at": "2026-01-01T00:17:00+00:00",
            "custom": "preserved",
        }
        job.update(updates)
        return job

    def _write_jobs(self, jobs, **metadata):
        self._jobs_path().parent.mkdir(parents=True, exist_ok=True)
        document = {"jobs": jobs, "metadata": {"owner": "unit-test"}}
        document.update(metadata)
        self._jobs_path().write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    def _read_jobs_document(self):
        return json.loads(self._jobs_path().read_text(encoding="utf-8"))

    def _read_jobs(self):
        return self._read_jobs_document()["jobs"]

    def _fixture_manifest_for_local_patch(self, payload: bytes):
        manifest_data = json.loads((PIPELINE / "manifest.json").read_text(encoding="utf-8"))
        expected = hashlib.sha256(payload).hexdigest()
        files = []
        seen: set[Path] = set()

        def add(source: Path, destination: Path, *, install: bool = True) -> None:
            if destination in seen:
                raise AssertionError(f"duplicate fixture destination: {destination}")
            seen.add(destination)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            files.append(
                self.mod.ResolvedFile(
                    source=source,
                    destination=destination,
                    sha256=expected if not install else source_hash,
                    mode=source.stat().st_mode & 0o777,
                    install=install,
                    source_sha256=source_hash if not install else None,
                    local_patch_reason="test local patch" if not install else None,
                )
            )

        for name in manifest_data["source_root_entrypoints"]:
            add(SCRIPTS / name, Path(name), install=name != "verify-hermes-patches.sh")
        for name in manifest_data["legacy_flat_entrypoints"]:
            add(PIPELINE / name, Path(name))
        for source in sorted(PIPELINE.glob(manifest_data["package_glob"])):
            if source.is_file():
                add(source, Path(manifest_data["package_destination"]) / source.name)
        for name in manifest_data.get("package_files", []):
            add(PIPELINE / name, Path(manifest_data["package_destination"]) / name)
        return self.mod.ResolvedManifest(
            path=PIPELINE / "manifest.json",
            sha256=hashlib.sha256((PIPELINE / "manifest.json").read_bytes()).hexdigest(),
            files=tuple(files),
            root_patterns=tuple(manifest_data["managed_root_patterns"]),
            unmanaged_root_exclusions=tuple(manifest_data["unmanaged_root_exclusions"]),
            package_destination=manifest_data["package_destination"],
            runner_vm_assets=self.mod._runner_vm_assets(manifest_data, files),
        )

    def test_default_manifest_declares_verify_patches_as_expected_local_patch(self):
        manifest = json.loads((PIPELINE / "manifest.json").read_text(encoding="utf-8"))
        spec = manifest["expected_local_patches"]["verify-hermes-patches.sh"]

        current_source_sha = hashlib.sha256(PATCH_VERIFIER.read_bytes()).hexdigest()
        self.assertEqual(spec["source_sha256"], current_source_sha)
        self.assertTrue(self.real_resolve_manifest().files)
        self.assertEqual(
            spec["deployed_sha256"],
            "ce51ca818e1d127a83b61694545c0fb673e749688a72dd7b0ae909522431aff5",
        )
        self.assertIn("Slack patch-05 MPIM/DM", spec["reason"])

    def test_install_has_hash_parity_and_records_source_commit(self):
        report = self._install()

        self.assertTrue(report["ok"])
        self.assertEqual(report["recorded_source_commit"], self.source_commit)
        self.assertRegex(report["reconciliation_receipt_id"], r"^[0-9a-f]{64}$")
        self.assertTrue(report["review_gate_smoke"]["ok"])
        self.assertEqual(
            report["review_gate_smoke"]["output"],
            "review-poll-gate-import-smoke: ok",
        )
        self.assertEqual(report["review_gate_smoke"]["cwd"], "/")
        self.assertEqual(
            report["review_gate_smoke"]["runtime_python"],
            str(self.runtime_python),
        )
        self.assertEqual(
            report["review_gate_smoke"]["runtime_sys_executable"],
            str(self.runtime_python),
        )
        self.assertIsInstance(
            report["review_gate_smoke"]["runtime_sys_prefix"],
            str,
        )
        self.assertFalse(report["missing"])
        self.assertFalse(report["hash_mismatches"])
        self.assertFalse(report["extra"])
        for relative in report["expected_files"]:
            self.assertTrue((self.destination / relative).is_file(), relative)
        self.assertTrue((self.destination / "verify-hermes-patches.sh").is_file())
        self.assertEqual(
            (self.destination / "verify-hermes-patches.sh").read_bytes(),
            self.local_patch_bytes,
        )
        self.assertIn("verify-hermes-patches.sh", report["expected_local_patches"])
        self.assertTrue((self.destination / "review_poll_gate.py").is_file())
        self.assertTrue((self.destination / "pr_pipeline" / "__init__.py").is_file())
        self.assertTrue((self.destination / "pr_pipeline" / "review_poll_gate.py").is_file())
        self.assertTrue((self.destination / "validator_repo_guard.py").is_file())
        self.assertTrue((self.destination / "pr_pipeline" / "validator_repo_guard.py").is_file())
        self.assertTrue((self.destination / ".pr_pipeline_deployment.json").is_file())
        jobs = self._read_jobs()
        managed = [job for job in jobs if job.get("name") == self.mod.CI_HEALTH_CRON_NAME]
        self.assertEqual(len(managed), 1)
        self.assertEqual(managed[0]["id"], "stable-ci-health-id")
        self.assertEqual(managed[0]["schedule"], {"kind": "cron", "expr": "*/5 * * * *", "display": "*/5 * * * *"})
        self.assertEqual(managed[0]["schedule_display"], "*/5 * * * *")
        self.assertEqual(managed[0]["script"], "ci-health-watch-cron.py")
        self.assertTrue(managed[0]["no_agent"])
        self.assertTrue(managed[0]["enabled"])
        self.assertEqual(managed[0]["state"], "scheduled")
        self.assertEqual(managed[0]["custom"], "preserved")
        self.assertFalse(report["cron_errors"])
        self.assertFalse(report["runner_vm_managed"])
        self.assertFalse(report["runner_vm_errors"])
        self.assertEqual(
            set(report["runner_vm_assets"]),
            self.mod.RUNNER_ASSET_DESTINATIONS,
        )

    def test_short_or_wrong_source_commit_is_rejected_exactly(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        self._write_jobs([self._stable_ci_health_job()])

        with self.assertRaisesRegex(self.mod.ManifestError, "full lowercase"):
            self.mod.install(
                self.destination,
                source_commit="a" * 12,
                runtime_python=self.runtime_python,
            )

        self.mod.install(
            self.destination,
            source_commit=self.source_commit,
            runtime_python=self.runtime_python,
        )
        report = self.mod.verify(
            self.destination,
            expected_source_commit="b" * 40,
        )
        self.assertFalse(report["ok"])
        self.assertIn(
            "deployment-marker-source-commit-mismatch",
            report["marker_errors"],
        )

    def test_install_requires_explicit_runtime_python_before_writing(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        self._write_jobs([self._stable_ci_health_job()])

        with self.assertRaisesRegex(
            self.mod.ManifestError,
            "--runtime-python is required",
        ):
            self.mod.install(
                self.destination,
                source_commit=self.source_commit,
                runtime_python=None,
            )

        self.assertFalse((self.destination / self.mod.MARKER_NAME).exists())

    def test_remote_reconcile_requires_runtime_python_before_staging(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(self.mod, "_remote_run") as remote_run,
            contextlib.redirect_stderr(stderr),
        ):
            result = self.mod.main(
                [
                    "reconcile",
                    "--host",
                    "mini",
                    "--source-commit",
                    self.source_commit,
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("--runtime-python is required", stderr.getvalue())
        remote_run.assert_not_called()

    def test_smoke_preserves_supplied_absolute_python_symlink(self):
        runtime_root = Path(self.tmp.name) / "release" / "venv"
        subprocess.run(
            [
                str(self.runtime_python),
                "-m",
                "venv",
                "--without-pip",
                str(runtime_root),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        runtime_python = runtime_root / "bin" / "python"
        self.assertTrue(runtime_python.is_symlink())

        report = self._install(runtime_python=runtime_python)

        smoke = report["review_gate_smoke"]
        self.assertEqual(smoke["runtime_python"], str(runtime_python))
        self.assertTrue(smoke["runtime_executable_samefile"])
        self.assertTrue(
            os.path.samefile(smoke["runtime_sys_executable"], runtime_python)
        )
        self.assertNotEqual(smoke["runtime_python"], str(runtime_python.resolve()))
        self.assertIsInstance(smoke["runtime_sys_prefix"], str)
        self.assertIsInstance(smoke["runtime_base_prefix"], str)
        self.assertEqual(
            smoke["runtime_is_venv"],
            smoke["runtime_sys_prefix"] != smoke["runtime_base_prefix"],
        )

    def test_smoke_receipt_tampering_fails_verification(self):
        self._install()
        marker_path = self.destination / self.mod.MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["review_gate_smoke"]["cwd"] = str(self.destination)
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        report = self.mod.verify(
            self.destination,
            expected_source_commit=self.source_commit,
        )

        self.assertFalse(report["ok"])
        self.assertIn(
            "deployment-marker-review-gate-smoke-contract-drift",
            report["marker_errors"],
        )
        self.assertIn("deployment-marker-receipt-invalid", report["marker_errors"])

    def test_root_review_gate_write_failure_uses_shared_safe_boundary(self):
        self._install()
        injection = Path(self.tmp.name) / "injection"
        injection.mkdir()
        missing_snapshot = Path(self.tmp.name) / "missing-parent" / "snapshot.json"
        (injection / "sitecustomize.py").write_text(
            "\n".join(
                [
                    "import pr_pipeline.review_poll_gate as gate",
                    "gate._merge_sweep = lambda: 0",
                    "gate._revalidation_sweep = lambda: 0",
                    "gate._human_merge_sweep = lambda: 0",
                    "gate._orphan_pr_sweep = lambda: 0",
                    "gate._scan = lambda: []",
                    f"gate.SNAPSHOT_PATH = {str(missing_snapshot)!r}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [str(self.runtime_python), str(self.destination / "review_poll_gate.py")],
            cwd="/",
            env={
                "CLICKUP_API_TOKEN": "fixture-token",
                "HOME": str(Path(self.tmp.name) / "home"),
                "PATH": os.defpath,
                "PYTHONPATH": os.pathsep.join(
                    [str(self.destination), str(injection)]
                ),
                "PYTHONNOUSERSITE": "1",
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"wakeAgent": False})
        self.assertIn("[review-gate] unexpected: FileNotFoundError", result.stderr)

    def test_verify_accepts_correct_managed_ci_health_schedule(self):
        self._install()

        report = self.mod.verify(self.destination, expected_source_commit=self.source_commit)

        self.assertTrue(report["ok"])
        self.assertEqual(report["managed_cron"]["name"], self.mod.CI_HEALTH_CRON_NAME)
        self.assertEqual(report["managed_cron"]["max_interval_seconds"], 300)

    def test_verify_detects_managed_ci_health_schedule_drift(self):
        self._install()
        jobs = self._read_jobs()
        jobs[0]["schedule"] = {"kind": "cron", "expr": "17 */2 * * *", "display": "17 */2 * * *"}
        jobs[0]["schedule_display"] = "17 */2 * * *"
        self._write_jobs(jobs)

        report = self.mod.verify(self.destination, expected_source_commit=self.source_commit)

        self.assertFalse(report["ok"])
        self.assertEqual(report["cron_errors"], ["ci-health-cron-schedule-drift"])

    def test_install_repairs_only_managed_ci_health_job(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        unrelated = {
            "id": "keep-me",
            "name": "unrelated",
            "schedule": {"kind": "cron", "expr": "0 */2 * * *", "display": "0 */2 * * *"},
            "script": "/tmp/other.py",
            "enabled": False,
        }
        self._write_jobs([
            unrelated,
            self._stable_ci_health_job(
                schedule={"kind": "cron", "expr": "0 */2 * * *", "display": "0 */2 * * *"},
                schedule_display="0 */2 * * *",
                script="wrong.py",
                no_agent=False,
                enabled=False,
                state="paused",
            ),
        ])

        report = self.mod.install(
            self.destination,
            source_commit=self.source_commit,
            runtime_python=self.runtime_python,
        )

        self.assertTrue(report["ok"])
        document = self._read_jobs_document()
        self.assertEqual(document["metadata"], {"owner": "unit-test"})
        jobs = self._read_jobs()
        self.assertEqual(jobs[0], unrelated)
        self.assertEqual(jobs[1]["name"], self.mod.CI_HEALTH_CRON_NAME)
        self.assertEqual(jobs[1]["id"], "stable-ci-health-id")
        self.assertEqual(jobs[1]["schedule"], {"kind": "cron", "expr": "*/5 * * * *", "display": "*/5 * * * *"})
        self.assertEqual(jobs[1]["schedule_display"], "*/5 * * * *")
        self.assertEqual(jobs[1]["script"], "ci-health-watch-cron.py")
        self.assertTrue(jobs[1]["no_agent"])
        self.assertTrue(jobs[1]["enabled"])
        self.assertEqual(jobs[1]["state"], "scheduled")
        self.assertIn("next_run_at", jobs[1])
        self.assertEqual(jobs[1]["custom"], "preserved")

    def test_schedule_repair_uses_scheduler_lock_and_preserves_concurrent_update(self):
        if self.mod.fcntl is None:
            self.skipTest("POSIX fcntl/flock required")
        self._write_jobs([self._stable_ci_health_job()])
        jobs_path = self._jobs_path()
        lock_path = jobs_path.parent / ".jobs.lock"
        ready = Path(self.tmp.name) / "holder-ready"
        release = Path(self.tmp.name) / "holder-release"
        holder_code = """
import fcntl, json, sys, time
from pathlib import Path
lock_path, jobs_path, ready, release = map(Path, sys.argv[1:])
with lock_path.open("a+", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    document = json.loads(jobs_path.read_text(encoding="utf-8"))
    document.setdefault("metadata", {})["concurrent_update"] = "preserved"
    jobs_path.write_text(json.dumps(document), encoding="utf-8")
    ready.touch()
    while not release.exists():
        time.sleep(0.01)
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
"""
        holder = subprocess.Popen(
            [
                str(self.runtime_python),
                "-c",
                holder_code,
                str(lock_path),
                str(jobs_path),
                str(ready),
                str(release),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), "concurrent writer did not acquire jobs lock")

        failures = []

        def repair():
            try:
                self.mod._repair_ci_health_schedule(self.destination)
            except Exception as exc:
                failures.append(exc)

        repair_thread = threading.Thread(target=repair)
        repair_thread.start()
        time.sleep(0.15)
        self.assertTrue(repair_thread.is_alive(), "repair bypassed held .jobs.lock")
        release.touch()
        repair_thread.join(timeout=5)
        stdout, stderr = holder.communicate(timeout=5)

        self.assertFalse(repair_thread.is_alive())
        self.assertEqual(holder.returncode, 0, stderr or stdout)
        self.assertEqual(failures, [])
        document = self._read_jobs_document()
        self.assertEqual(
            document["metadata"]["concurrent_update"],
            "preserved",
        )
        self.assertEqual(
            document["jobs"][0]["schedule"],
            self.mod.CI_HEALTH_CRON_SCHEDULE,
        )

    def test_install_fails_closed_when_ci_health_job_is_missing(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        self._write_jobs([{"id": "other", "name": "other"}])

        with self.assertRaises(self.mod.ManifestError):
            self.mod.install(
                self.destination,
                source_commit=self.source_commit,
                runtime_python=self.runtime_python,
            )

    def test_install_fails_closed_when_ci_health_job_is_duplicated(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        self._write_jobs([self._stable_ci_health_job(id="one"), self._stable_ci_health_job(id="two")])

        with self.assertRaises(self.mod.ManifestError):
            self.mod.install(
                self.destination,
                source_commit=self.source_commit,
                runtime_python=self.runtime_python,
            )

    def test_install_reports_wrong_local_patch_without_resyncing_it(self):
        self.destination.mkdir(parents=True, exist_ok=True)
        wrong = b"wrong local patch\n"
        (self.destination / "verify-hermes-patches.sh").write_bytes(wrong)
        self._write_jobs([self._stable_ci_health_job()])

        report = self.mod.install(
            self.destination,
            source_commit=self.source_commit,
            runtime_python=self.runtime_python,
        )

        self.assertFalse(report["ok"])
        self.assertIn("verify-hermes-patches.sh", report["hash_mismatches"])
        self.assertEqual((self.destination / "verify-hermes-patches.sh").read_bytes(), wrong)

    def test_manifest_includes_validator_repo_guard_in_flat_and_package_surfaces(self):
        manifest = self.mod.resolve_manifest()
        destinations = {item.destination.as_posix() for item in manifest.files}

        self.assertIn("validator_repo_guard.py", destinations)
        self.assertIn("pr_pipeline/validator_repo_guard.py", destinations)
        self.assertIn("validator_repo_guard.py", self.mod._expected_hashes(manifest))

    def test_manifest_routes_review_gate_through_root_shim_and_package_closure(self):
        manifest_data = json.loads((PIPELINE / "manifest.json").read_text(encoding="utf-8"))
        manifest = self._fixture_manifest_for_local_patch(self.local_patch_bytes)
        destinations = {item.destination.as_posix() for item in manifest.files}

        self.assertIn("review_poll_gate.py", manifest_data["source_root_entrypoints"])
        self.assertNotIn("review_poll_gate.py", manifest_data["legacy_flat_entrypoints"])
        self.assertNotIn("pr_pipeline_event_driven.py", manifest_data["legacy_flat_entrypoints"])
        self.assertNotIn("pr_pipeline_improvements.py", manifest_data["legacy_flat_entrypoints"])
        self.assertIn("review_poll_gate.py", destinations)
        for required in self.mod.REVIEW_GATE_PACKAGE_CLOSURE:
            self.assertIn(required.as_posix(), destinations)

    def test_installed_root_review_gate_runs_from_real_cwd_without_flat_helpers(self):
        self._install()
        for helper in (
            "autonomous_merge.py",
            "pr_pipeline_event_driven.py",
            "pr_pipeline_improvements.py",
            "validator_verdict.py",
        ):
            (self.destination / helper).unlink(missing_ok=True)

        isolated_home = Path(self.tmp.name) / "sanitized-home"
        isolated_home.mkdir()
        result = subprocess.run(
            [sys.executable, str(self.destination / "review_poll_gate.py")],
            cwd=self.destination,
            env={
                "HOME": str(isolated_home),
                "PATH": os.defpath,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "TZ": "UTC",
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLICKUP_API_TOKEN not set", result.stderr)
        self.assertEqual(json.loads(result.stdout), {"wakeAgent": False})

    def test_verify_reports_review_gate_package_marker_and_transitive_import_missing(self):
        self._install()
        (self.destination / "pr_pipeline" / "__init__.py").unlink()
        (self.destination / "pr_pipeline" / "pr_pipeline_event_driven.py").unlink()

        report = self.mod.verify(self.destination, expected_source_commit=self.source_commit)

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["review_gate_errors"],
            [
                "review-gate-runtime-missing:pr_pipeline/__init__.py",
                "review-gate-runtime-missing:pr_pipeline/pr_pipeline_event_driven.py",
            ],
        )

    def test_verify_reports_review_gate_shim_hash_mismatch(self):
        self._install()
        (self.destination / "review_poll_gate.py").write_text(
            "raise SystemExit('tampered')\n",
            encoding="utf-8",
        )

        report = self.mod.verify(self.destination, expected_source_commit=self.source_commit)

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["review_gate_errors"],
            ["review-gate-hash-mismatch:review_poll_gate.py"],
        )

    def test_manifest_governs_runner_assets_modes_and_exact_vm_destinations(self):
        manifest = self.real_resolve_manifest()
        assets = {item.destination: item for item in manifest.runner_vm_assets}

        self.assertEqual(set(assets), self.mod.RUNNER_ASSET_DESTINATIONS)
        self.assertEqual(assets["/etc/systemd/system/hermes-runner@.service"].mode, 0o644)
        self.assertEqual(assets["/etc/systemd/system/hermes-runner@.service"].owner, "root")
        self.assertEqual(assets["/home/colingreig/.hermes-ci/runner-config.json"].mode, 0o600)
        for destination in (
            "/home/colingreig/.hermes-ci/hooks/acquire.sh",
            "/home/colingreig/.hermes-ci/hooks/release.sh",
            "/home/colingreig/.hermes-ci/run-runner-loop.sh",
        ):
            self.assertEqual(assets[destination].mode, 0o755)
            self.assertEqual(assets[destination].owner, "colingreig")

    def test_runner_assets_encode_single_slot_bounded_private_diagnostics(self):
        acquire = (PIPELINE / "hermes-runner-acquire.sh").read_text(encoding="utf-8")
        loop = (PIPELINE / "hermes-runner-loop.sh").read_text(encoding="utf-8")
        unit = (PIPELINE / "hermes-runner@.service").read_text(encoding="utf-8")
        config = json.loads((PIPELINE / "hermes-runner-config.json").read_text(encoding="utf-8"))

        self.assertEqual(config["concurrency_slots"], 1)
        self.assertEqual(config["semaphore_timeout_seconds"], 1800)
        self.assertEqual(config["diagnostic_retention_days"], 7)
        self.assertEqual(config["diagnostic_max_jobs_per_repo"], 20)
        self.assertIn('select(type == "number" and . == 1)', acquire)
        self.assertIn('select(type == "number" and . == 1800)', acquire)
        self.assertIn("chmod 0700", loop)
        self.assertIn('cp -a "$DIR/_diag"', loop)
        self.assertIn("journalctl -u", loop)
        self.assertIn("memory.events", loop)
        self.assertIn("diagnostic_retention_days", loop)
        self.assertIn("diagnostic_max_jobs_per_repo", loop)
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED", unit)
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_COMPLETED", unit)

    def test_runner_vm_installer_applies_resources_assets_and_daemon_reload(self):
        self._install()
        topology = self.mod._topology_for_manifest(self.fixture_manifest)
        calls = []

        def fake_run(command, **kwargs):
            calls.append((list(command), kwargs.get("input")))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run):
            self.mod._apply_runner_vm_assets(self.destination, self.fixture_manifest, topology)

        commands = [command for command, _payload in calls]
        self.assertIn(
            ["/usr/local/bin/orb", "config", "set", "machine.hermes-ci.cpu", "4"],
            commands,
        )
        self.assertIn(
            ["/usr/local/bin/orb", "config", "set", "machine.hermes-ci.memory_mib", "6144"],
            commands,
        )
        installs = [command for command in commands if "runner-asset-install" in command]
        self.assertEqual(len(installs), 5)
        self.assertTrue(any("0600" in command and any(part.endswith("runner-config.json") for part in command) for command in installs))
        self.assertTrue(any("0644" in command and any(part.endswith("hermes-runner@.service") for part in command) for command in installs))
        self.assertTrue(all(payload for command, payload in calls if "runner-asset-install" in command))
        self.assertEqual(
            commands[-1],
            ["/usr/local/bin/orb", "exec", "-m", "hermes-ci", "sudo", "systemctl", "daemon-reload"],
        )

    def _runner_test_tree(self):
        root = Path(self.tmp.name) / "hermes-ci"
        home = Path(self.tmp.name) / "home"
        runner_dir = home / "actions-runner" / "thermal"
        (root / "sem" / "slots").mkdir(parents=True, exist_ok=True)
        (root / "runtime").mkdir(parents=True, exist_ok=True)
        runner_dir.mkdir(parents=True, exist_ok=True)
        (root / "runner-config.json").write_text(
            (PIPELINE / "hermes-runner-config.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir(exist_ok=True)
        flock = fake_bin / "flock"
        flock.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        flock.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "HERMES_RUNNER_TEST_MODE": "1",
            "HERMES_RUNNER_TEST_ROOT": str(root),
            "HERMES_RUNNER_TEST_HOME": str(home),
            "RUNNER_NAME": "hermes-thermal",
            "INVOCATION_ID": "invocation-current",
        }
        return root, home, runner_dir, env

    def test_acquire_and_release_execute_with_a_single_private_lease(self):
        root, _home, _runner_dir, env = self._runner_test_tree()
        env.update(
            {
                "GITHUB_REPOSITORY": "colingreig/thermal",
                "GITHUB_WORKFLOW": "E2E Functional",
                "GITHUB_RUN_ID": "12345",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_JOB": "e2e-functional",
            }
        )
        acquire = subprocess.run(
            [str(PIPELINE / "hermes-runner-acquire.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(acquire.returncode, 0, acquire.stderr)
        lease = root / "sem" / "slots" / "hermes-thermal"
        self.assertTrue(lease.is_file())
        self.assertIn("invocation_id=invocation-current", lease.read_text(encoding="utf-8"))
        self.assertEqual(lease.stat().st_mode & 0o777, 0o600)

        release = subprocess.run(
            [str(PIPELINE / "hermes-runner-release.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(release.returncode, 0, release.stderr)
        self.assertFalse(lease.exists())
        metadata = (root / "runtime" / "hermes-thermal.env").read_text(encoding="utf-8")
        self.assertIn("run_id=12345", metadata)
        self.assertIn("job_completed_at=", metadata)

    def test_runner_startup_reclaims_only_proven_stale_own_lease(self):
        root, _home, _runner_dir, env = self._runner_test_tree()
        slots = root / "sem" / "slots"
        own = slots / "hermes-thermal"
        other = slots / "hermes-jdmbuysell-v4"
        own.write_text("boot_id=old\ninvocation_id=invocation-old\nworker_pid=999999\n", encoding="utf-8")
        other.write_text("boot_id=current\ninvocation_id=active-other\nworker_pid=1\n", encoding="utf-8")
        env["HERMES_RUNNER_TEST_ACTION"] = "reconcile-lease"

        result = subprocess.run(
            [str(PIPELINE / "hermes-runner-loop.sh"), "thermal"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(own.exists())
        self.assertTrue(other.exists())

    def test_shared_slot_reclaims_dead_other_runner_but_preserves_live_owner(self):
        root, _home, _runner_dir, env = self._runner_test_tree()
        slots = root / "sem" / "slots"
        dead = slots / "hermes-dead-repo"
        live = slots / "hermes-live-repo"
        dead.write_text(
            "boot_id=\ninvocation_id=other-dead\nworker_pid=999999\n",
            encoding="utf-8",
        )
        live.write_text(
            f"boot_id=\ninvocation_id=other-live\nworker_pid={os.getpid()}\n",
            encoding="utf-8",
        )
        env["HERMES_RUNNER_TEST_ACTION"] = "reconcile-leases"

        result = subprocess.run(
            [str(PIPELINE / "hermes-runner-acquire.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())

    def test_shared_slot_reclaims_only_aged_empty_legacy_lease(self):
        root, _home, _runner_dir, env = self._runner_test_tree()
        slots = root / "sem" / "slots"
        aged = slots / "hermes-topdynamicspartners"
        fresh = slots / "hermes-jdmbuysell-v4"
        aged.touch()
        fresh.touch()
        os.utime(aged, (1, 1))
        env["HERMES_RUNNER_TEST_ACTION"] = "reconcile-leases"

        result = subprocess.run(
            [str(PIPELINE / "hermes-runner-acquire.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(aged.exists())
        self.assertTrue(fresh.exists())

    def test_empty_restart_cycles_do_not_archive_or_prune_real_job_evidence(self):
        root, _home, runner_dir, env = self._runner_test_tree()
        archives = root / "diagnostics" / "thermal"
        for index in range(20):
            evidence = archives / f"20260701T0000{index:02d}Z-real"
            evidence.mkdir(parents=True)
            (evidence / "job.env").write_text(f"run_id={index}\njob=e2e\n", encoding="utf-8")
        before = sorted(path.name for path in archives.iterdir())
        env["HERMES_RUNNER_TEST_ACTION"] = "cleanup"
        for _ in range(5):
            (runner_dir / "_diag").mkdir(exist_ok=True)
            result = subprocess.run(
                [str(PIPELINE / "hermes-runner-loop.sh"), "thermal"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(sorted(path.name for path in archives.iterdir()), before)
        self.assertFalse((root / "diagnostics-failures").exists())

    def test_job_cleanup_archives_evidence_privately_and_enforces_retention(self):
        root, _home, runner_dir, env = self._runner_test_tree()
        archives = root / "diagnostics" / "thermal"
        for index in range(20):
            (archives / f"20260701T0000{index:02d}Z-real").mkdir(parents=True)
        diag = runner_dir / "_diag"
        diag.mkdir()
        (diag / "Runner.log").write_text("diagnostic\n", encoding="utf-8")
        work = runner_dir / "_work"
        work.mkdir()
        metadata = root / "runtime" / "hermes-thermal.env"
        metadata.write_text("run_id=999\njob=e2e-functional\n", encoding="utf-8")
        env["HERMES_RUNNER_TEST_ACTION"] = "cleanup"

        result = subprocess.run(
            [str(PIPELINE / "hermes-runner-loop.sh"), "thermal"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        remaining = sorted(path for path in archives.iterdir() if path.is_dir())
        self.assertEqual(len(remaining), 20)
        self.assertFalse((archives / "20260701T000000Z-real").exists())
        newest = remaining[-1]
        self.assertTrue((newest / "_diag" / "Runner.log").is_file())
        self.assertTrue((newest / "job.env").is_file())
        self.assertEqual(newest.stat().st_mode & 0o777, 0o700)
        self.assertFalse(work.exists())
        self.assertFalse(diag.exists())

    def test_nonempty_registration_diagnostics_use_separate_bounded_archive(self):
        root, _home, runner_dir, env = self._runner_test_tree()
        job_archives = root / "diagnostics" / "thermal"
        real_job = job_archives / "20260701T000000Z-real-job"
        real_job.mkdir(parents=True)
        (real_job / "job.env").write_text("run_id=1\njob=e2e\n", encoding="utf-8")
        failure_archives = root / "diagnostics-failures" / "thermal"
        for index in range(20):
            (failure_archives / f"20260701T0000{index:02d}Z-failure").mkdir(parents=True)
        diag = runner_dir / "_diag"
        diag.mkdir()
        (diag / "Runner.log").write_text("registration failed\n", encoding="utf-8")
        env["HERMES_RUNNER_TEST_ACTION"] = "cleanup"

        result = subprocess.run(
            [str(PIPELINE / "hermes-runner-loop.sh"), "thermal"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(real_job.is_dir())
        self.assertEqual(len([path for path in failure_archives.iterdir() if path.is_dir()]), 20)
        self.assertFalse((failure_archives / "20260701T000000Z-failure").exists())
        newest = sorted(path for path in failure_archives.iterdir() if path.is_dir())[-1]
        self.assertTrue((newest / "_diag" / "Runner.log").is_file())
        self.assertIn(
            "archive_kind=registration-failure",
            (newest / "runner.env").read_text(encoding="utf-8"),
        )
        self.assertFalse(diag.exists())

    def test_installed_merge_surface_loads_as_a_standalone_entrypoint(self):
        self._install()
        old_path = list(sys.path)
        tracked = {
            name: module
            for name, module in sys.modules.items()
            if name == "validator_verdict" or name == "pr_pipeline" or name.startswith("pr_pipeline.")
        }
        for name in list(tracked):
            sys.modules.pop(name, None)
        # Do not let the source package mask a missing deployed-script import.
        source_paths = {str(SCRIPTS.resolve()), str(PIPELINE.resolve())}
        sys.path[:] = [path for path in sys.path if str(Path(path or ".").resolve()) not in source_paths]
        try:
            autonomous_merge = _load(self.destination / "autonomous_merge.py", "installed_autonomous_merge")

            # Default contract: SHADOW (fail-closed) when no env is set;
            # activation requires an explicit HERMES_MERGE_ACTIVE truthy. Clear
            # the ambient env so this is deterministic regardless of the host
            # shell's own HERMES_AUTONOMOUS_MERGE* exports.
            with mock.patch.dict(os.environ, _cleared_merge_env()):
                self.assertTrue(autonomous_merge._shadow())
            with mock.patch.dict(os.environ, _cleared_merge_env(HERMES_MERGE_ACTIVE="1")):
                self.assertFalse(autonomous_merge._shadow())
            self.assertEqual(
                Path(autonomous_merge.validator_verdict.__file__).resolve(),
                (self.destination / "validator_verdict.py").resolve(),
            )
            validator_repo_guard = _load(
                self.destination / "validator_repo_guard.py",
                "installed_validator_repo_guard",
            )
            self.assertEqual(
                validator_repo_guard.parse_repo_ref("https://github.com/acme/widget/pull/7"),
                "acme/widget",
            )
            # Pipeline explicitly activated but no tier-autonomy env enabled ->
            # the fenced MergeActor shadow plan still runs cleanly (proving the
            # deployed wiring is intact), and the merge is refused at the
            # tier-autonomy gate (the verdict has no tier -> defaults to
            # 'high', which is never autonomously mergeable).
            with mock.patch.dict(os.environ, _cleared_merge_env(HERMES_MERGE_ACTIVE="1")):
                action, detail = autonomous_merge.evaluate(
                    "acme/widget",
                    7,
                    {
                        "identity": {
                            "canonical_repo": "acme/widget",
                            "pr_number": 7,
                            "trusted_task_id": "86e2gh04e",
                            "base_sha": "a" * 40,
                            "head_sha": "b" * 40,
                            "tested_merge_sha": "c" * 40,
                            "ci_policy_id": "ci-policy-v1",
                            "ci_run_ids": ["ci:unit"],
                        },
                    },
                    {"acme/widget"},
                )
            self.assertEqual(action, "skip")
            self.assertIn("tier", detail)
            self.assertIn("autonomy not enabled", detail)
        finally:
            for name in list(sys.modules):
                if name == "validator_verdict" or name == "pr_pipeline" or name.startswith("pr_pipeline."):
                    sys.modules.pop(name, None)
            sys.modules.update(tracked)
            sys.path[:] = old_path

    def test_verify_reports_missing_manifest_file(self):
        self._install()
        (self.destination / "autonomous_merge.py").unlink()

        report = self.mod.verify(self.destination, expected_source_commit=self.source_commit)

        self.assertFalse(report["ok"])
        self.assertEqual(report["missing"], ["autonomous_merge.py"])

    def test_verify_reports_unmanaged_pipeline_extras_without_touching_them(self):
        self._install()
        root_extra = self.destination / "pr_unlisted.py"
        excluded_non_pipeline_script = self.destination / "validator_autonomy.py"
        validator_extra = self.destination / "validator_unlisted.py"
        package_extra = self.destination / "pr_pipeline" / "unlisted.py"
        root_extra.write_text("# unexpected\n", encoding="utf-8")
        excluded_non_pipeline_script.write_text("# separate manual ledger\n", encoding="utf-8")
        validator_extra.write_text("# unexpected\n", encoding="utf-8")
        package_extra.write_text("# unexpected\n", encoding="utf-8")

        report = self.mod.verify(self.destination, expected_source_commit=self.source_commit)

        self.assertFalse(report["ok"])
        self.assertEqual(
            report["extra"],
            ["pr_pipeline/unlisted.py", "pr_unlisted.py", "validator_unlisted.py"],
        )
        self.assertTrue(root_extra.exists())
        self.assertTrue(excluded_non_pipeline_script.exists())
        self.assertTrue(validator_extra.exists())
        self.assertTrue(package_extra.exists())

    def test_verify_reports_hash_and_recorded_commit_mismatches(self):
        self._install()
        target = self.destination / "merge_guard.py"
        target.write_text("tampered", encoding="utf-8")

        report = self.mod.verify(self.destination, expected_source_commit="b" * 40)

        self.assertFalse(report["ok"])
        self.assertIn("merge_guard.py", report["hash_mismatches"])
        self.assertIn("deployment-marker-source-commit-mismatch", report["marker_errors"])


class MergeActivationTests(unittest.TestCase):
    """Autonomous-merge activation contract (fail-closed): absence of env =
    SHADOW; activation requires an explicit HERMES_MERGE_ACTIVE truthy;
    HERMES_MERGE_SHADOW is the emergency override that forces shadow back on
    even when HERMES_MERGE_ACTIVE is set (shadow wins); tier 'high' is NEVER
    autonomously mergeable by any switch; and activation is necessary but
    never sufficient — a merge is still refused without a fresh non-shadow
    PASS verdict for the exact head.
    """

    def setUp(self):
        self.old_path = list(os.sys.path)
        os.sys.path.insert(0, str(PIPELINE))
        self.addCleanup(lambda: setattr(os.sys, "path", self.old_path))

    def test_unset_env_means_shadow_fail_closed(self):
        with mock.patch.dict(os.environ, _cleared_merge_env()):
            autonomous_merge = _load(PIPELINE / "autonomous_merge.py", "autonomous_merge_default")
            merge_guard = _load(PIPELINE / "merge_guard.py", "merge_guard_default")
            validate_ops = _load(PIPELINE / "hermes_validate_ops.py", "validate_ops_default")

            self.assertTrue(autonomous_merge._shadow())
            self.assertTrue(merge_guard._shadow())
            self.assertTrue(validate_ops.VALIDATE_SHADOW)
            self.assertEqual(
                validate_ops.cmd_merge_pr(SimpleNamespace(repo="org/repo", pr_number=1)), 1
            )

    def test_hermes_merge_active_truthy_activates(self):
        with mock.patch.dict(os.environ, _cleared_merge_env(HERMES_MERGE_ACTIVE="1")):
            autonomous_merge = _load(PIPELINE / "autonomous_merge.py", "autonomous_merge_active")
            merge_guard = _load(PIPELINE / "merge_guard.py", "merge_guard_active")
            validate_ops = _load(PIPELINE / "hermes_validate_ops.py", "validate_ops_active")

            self.assertFalse(autonomous_merge._shadow())
            self.assertFalse(merge_guard._shadow())
            self.assertFalse(validate_ops.VALIDATE_SHADOW)

    def test_hermes_merge_shadow_override_beats_hermes_merge_active(self):
        with mock.patch.dict(
            os.environ, _cleared_merge_env(HERMES_MERGE_ACTIVE="1", HERMES_MERGE_SHADOW="1")
        ):
            autonomous_merge = _load(PIPELINE / "autonomous_merge.py", "autonomous_merge_forced_shadow")
            merge_guard = _load(PIPELINE / "merge_guard.py", "merge_guard_forced_shadow")
            validate_ops = _load(PIPELINE / "hermes_validate_ops.py", "validate_ops_forced_shadow")

            self.assertTrue(autonomous_merge._shadow())
            self.assertTrue(merge_guard._shadow())
            self.assertTrue(validate_ops.VALIDATE_SHADOW)
            self.assertEqual(
                validate_ops.cmd_merge_pr(SimpleNamespace(repo="org/repo", pr_number=1)), 1
            )

    def test_high_tier_is_never_autonomous_even_with_every_switch_set(self):
        every_switch = _cleared_merge_env(
            HERMES_MERGE_ACTIVE="1",
            HERMES_AUTONOMOUS_MERGE="1",
            HERMES_AUTONOMOUS_MERGE_HIGH="1",
        )
        with mock.patch.dict(os.environ, every_switch):
            autonomous_merge = _load(PIPELINE / "autonomous_merge.py", "autonomous_merge_high_tier")
            merge_guard = _load(PIPELINE / "merge_guard.py", "merge_guard_high_tier")

            for module in (autonomous_merge, merge_guard):
                self.assertFalse(module._tier_autonomy_enabled("high"))
                self.assertFalse(module._tier_autonomy_enabled("HIGH"))
                # Unknown/unparseable tier defaults to high => refused.
                self.assertFalse(module._tier_autonomy_enabled(""))
                self.assertFalse(module._tier_autonomy_enabled(None))
                self.assertFalse(module._tier_autonomy_enabled("weird"))
                # low/medium remain master-switch enabled.
                self.assertTrue(module._tier_autonomy_enabled("low"))
                self.assertTrue(module._tier_autonomy_enabled("medium"))

    def test_activated_merge_still_refused_without_a_fresh_pass_verdict(self):
        """Green CI is necessary but never sufficient. With the pipeline fully
        activated (not shadow) and tier autonomy enabled, a PR whose trust
        store has NO fresh non-shadow PASS verdict for the exact head must
        still be refused — the verdict-freshness gate is independent of, and
        never bypassed by, the shadow/tier gates opening."""
        with tempfile.TemporaryDirectory() as directory:
            empty_ledger = Path(directory) / "empty.sqlite3"
            with mock.patch.dict(
                os.environ,
                _cleared_merge_env(HERMES_MERGE_ACTIVE="1", HERMES_AUTONOMOUS_MERGE="1"),
            ):
                autonomous_merge = _load(PIPELINE / "autonomous_merge.py", "autonomous_merge_no_verdict")
                self.assertFalse(autonomous_merge._shadow())

                green_info = {
                    "state": "OPEN", "head": "b" * 40, "mergeable": "MERGEABLE",
                    "merge_state": "CLEAN", "draft": False, "labels": [],
                    "failing": [], "pending": [], "ignored": [],
                    "gating_green": ["Lint, typecheck, test"],
                }
                verdict = {
                    "tier": "low",
                    "head_sha": "b" * 40,
                    "identity": {
                        "canonical_repo": "acme/widget",
                        "pr_number": 41,
                        "trusted_task_id": "86e2gh04e",
                        "base_sha": "a" * 40,
                        "head_sha": "b" * 40,
                        "tested_merge_sha": "c" * 40,
                        "ci_policy_id": "ci-policy-v1",
                        "ci_run_ids": ["ci:unit"],
                    },
                }
                with (
                    mock.patch.object(autonomous_merge, "_pr_state", return_value=(green_info, None)),
                    mock.patch.object(autonomous_merge.validator_verdict, "STORE_PATH", str(empty_ledger)),
                ):
                    action, detail = autonomous_merge.evaluate(
                        "acme/widget", 41, verdict, {"acme/widget"}
                    )
            self.assertEqual(action, "skip")
            self.assertIn("no fenced SQLite verdict", detail)


if __name__ == "__main__":
    unittest.main()
