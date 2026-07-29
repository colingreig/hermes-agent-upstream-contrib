"""Tests for the Mini-only PR trust-boundary verifier."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
RECONCILER = SCRIPTS / "reconcile_pr_pipeline.py"
PIPELINE = SCRIPTS / "pr_pipeline"
PATCH_VERIFIER = SCRIPTS / "verify-hermes-patches.sh"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PipelineVerifierTests(unittest.TestCase):
    def setUp(self):
        self.reconciler = _load(RECONCILER, "reconcile_pr_pipeline_verify_test")
        self.old_path = list(sys.path)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: setattr(sys, "path", self.old_path))
        self.verifier = _load(PIPELINE / "pipeline_verify.py", "pipeline_verify_test")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.destination = Path(self.temporary.name) / "scripts"
        self.local_patch_bytes = b"pipeline-verifier local patch fixture\n"
        self.destination.mkdir(parents=True, exist_ok=True)
        jobs_path = self.destination.parent / "cron" / "jobs.json"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "ci-health-fixture",
                            "name": "ci-health-watch",
                            "schedule": {
                                "kind": "cron",
                                "expr": "*/5 * * * *",
                                "display": "*/5 * * * *",
                            },
                            "schedule_display": "*/5 * * * *",
                            "script": "ci_health_watch.py",
                            "no_agent": True,
                            "enabled": True,
                            "state": "scheduled",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self._install_fixture()

    def _fixture_manifest_for_local_patch(self, payload: bytes):
        manifest = self.reconciler.resolve_manifest()
        expected = hashlib.sha256(payload).hexdigest()
        files = []
        for item in manifest.files:
            if item.destination.as_posix() == "verify-hermes-patches.sh":
                files.append(
                    self.reconciler.ResolvedFile(
                        source=item.source,
                        destination=item.destination,
                        sha256=expected,
                        mode=item.mode,
                        install=False,
                        source_sha256=item.source_sha256,
                        local_patch_reason="test local patch",
                    )
                )
            else:
                files.append(item)
        return self.reconciler.ResolvedManifest(
            path=manifest.path,
            sha256=manifest.sha256,
            files=tuple(files),
            root_patterns=manifest.root_patterns,
            unmanaged_root_exclusions=manifest.unmanaged_root_exclusions,
            package_destination=manifest.package_destination,
            runner_vm_assets=manifest.runner_vm_assets,
        )

    def _install_fixture(self):
        (self.destination / "verify-hermes-patches.sh").write_bytes(self.local_patch_bytes)
        jobs_path = self.destination.parent / "cron" / "jobs.json"
        jobs_path.parent.mkdir(parents=True, exist_ok=True)
        jobs_path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "pipeline-verifier-ci-health",
                            "name": "ci-health-watch",
                            "script": "ci_health_watch.py",
                            "schedule": {"kind": "cron", "expr": "*/5 * * * *"},
                            "schedule_display": "*/5 * * * *",
                            "enabled": True,
                            "no_agent": True,
                            "state": "scheduled",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        manifest = self._fixture_manifest_for_local_patch(self.local_patch_bytes)
        with mock.patch.object(self.reconciler, "resolve_manifest", return_value=manifest):
            return self.reconciler.install(
                self.destination,
                source_commit="a" * 40,
                runtime_python=Path(sys.executable),
            )

    def test_deployed_pipeline_verifier_proves_the_shadow_boundary(self):
        report = self.verifier.verify(self.destination)

        self.assertTrue(report["ok"])
        self.assertEqual(report["deployment_mode"], "shadow")
        self.assertIn("verify-hermes-patches.sh", self.reconciler.verify(self.destination)["expected_files"])
        self.assertIn("sqlite-wal-fence", report["checks"])
        self.assertIn("sandbox-default-deny", report["checks"])

    def test_patch_verifier_uses_the_runtime_venv_not_host_python(self):
        source = PATCH_VERIFIER.read_text(encoding="utf-8")

        self.assertIn('PR_PIPELINE_PY="$REPO/venv/bin/python"', source)
        self.assertIn('"$PR_PIPELINE_PY" "$PR_PIPELINE_VERIFY"', source)

    def test_deployed_pipeline_verifier_rejects_tampering_and_unmanaged_modules(self):
        (self.destination / "pr_pipeline" / "store.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(self.verifier.VerificationError):
            self.verifier.verify(self.destination)

        self._install_fixture()
        (self.destination / "pr_pipeline_improvements.py.bak-pre-boundary").write_text("legacy backup\n", encoding="utf-8")
        self.assertTrue(self.verifier.verify(self.destination)["ok"])

        (self.destination / "pr_unlisted.py").write_text("# unsafe\n", encoding="utf-8")
        with self.assertRaises(self.verifier.VerificationError):
            self.verifier.verify(self.destination)

        self._install_fixture()
        (self.destination / "pr_pipeline" / "unmanaged.py").write_text("# unsafe\n", encoding="utf-8")
        with self.assertRaises(self.verifier.VerificationError):
            self.verifier.verify(self.destination)

    def test_deployed_pipeline_verifier_rejects_tampered_smoke_receipt(self):
        marker_path = self.destination / self.reconciler.MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["review_gate_smoke"]["runtime_python"] = "/usr/bin/python3"
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        with self.assertRaises(self.verifier.VerificationError):
            self.verifier.verify(self.destination)


if __name__ == "__main__":
    unittest.main()
