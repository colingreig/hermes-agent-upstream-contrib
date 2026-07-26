"""Tests for the Mini-only PR trust-boundary verifier."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
RECONCILER = SCRIPTS / "reconcile_pr_pipeline.py"
PIPELINE = SCRIPTS / "pr_pipeline"


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
        self.reconciler.install(self.destination, source_commit="a" * 40)

    def test_deployed_pipeline_verifier_proves_the_shadow_boundary(self):
        report = self.verifier.verify(self.destination)

        self.assertTrue(report["ok"])
        self.assertEqual(report["deployment_mode"], "shadow")
        self.assertIn("verify-hermes-patches.sh", self.reconciler.verify(self.destination)["expected_files"])
        self.assertIn("sqlite-wal-fence", report["checks"])
        self.assertIn("sandbox-default-deny", report["checks"])

    def test_deployed_pipeline_verifier_rejects_tampering_and_unmanaged_modules(self):
        (self.destination / "pr_pipeline" / "store.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaises(self.verifier.VerificationError):
            self.verifier.verify(self.destination)

        self.reconciler.install(self.destination, source_commit="a" * 40)
        (self.destination / "pr_pipeline" / "unmanaged.py").write_text("# unsafe\n", encoding="utf-8")
        with self.assertRaises(self.verifier.VerificationError):
            self.verifier.verify(self.destination)


if __name__ == "__main__":
    unittest.main()
