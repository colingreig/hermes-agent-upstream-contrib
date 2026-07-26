"""Behavioral tests for the source-controlled Mini PR-pipeline deployer."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


SCRIPTS = Path(__file__).resolve().parent.parent
RECONCILER = SCRIPTS / "reconcile_pr_pipeline.py"
PIPELINE = SCRIPTS / "pr_pipeline"
_COUNTER = 0


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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = Path(self.tmp.name) / "scripts"

    def _install(self):
        return self.mod.install(self.destination, source_commit=self.source_commit)

    def test_install_has_hash_parity_and_records_source_commit(self):
        report = self._install()

        self.assertTrue(report["ok"])
        self.assertEqual(report["recorded_source_commit"], self.source_commit)
        self.assertFalse(report["missing"])
        self.assertFalse(report["hash_mismatches"])
        self.assertFalse(report["extra"])
        for relative in report["expected_files"]:
            self.assertTrue((self.destination / relative).is_file(), relative)
        self.assertTrue((self.destination / ".pr_pipeline_deployment.json").is_file())

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

            self.assertTrue(autonomous_merge._shadow())
            self.assertEqual(
                Path(autonomous_merge.validator_verdict.__file__).resolve(),
                (self.destination / "validator_verdict.py").resolve(),
            )
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
            self.assertIn("fenced MergeActor plan", detail)
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


class ShadowOnlyMergeTests(unittest.TestCase):
    def setUp(self):
        self.old_path = list(os.sys.path)
        os.sys.path.insert(0, str(PIPELINE))
        self.addCleanup(lambda: setattr(os.sys, "path", self.old_path))

    def test_vendored_merge_surfaces_refuse_mutation_even_if_env_disables_shadow(self):
        old_shadow = os.environ.get("VALIDATE_SHADOW")
        os.environ["VALIDATE_SHADOW"] = "false"
        self.addCleanup(
            lambda: os.environ.pop("VALIDATE_SHADOW", None)
            if old_shadow is None
            else os.environ.__setitem__("VALIDATE_SHADOW", old_shadow)
        )
        autonomous_merge = _load(PIPELINE / "autonomous_merge.py", "autonomous_merge_shadow")
        merge_guard = _load(PIPELINE / "merge_guard.py", "merge_guard_shadow")
        validate_ops = _load(PIPELINE / "hermes_validate_ops.py", "validate_ops_shadow")

        self.assertTrue(autonomous_merge._shadow())
        self.assertTrue(merge_guard._shadow())
        self.assertTrue(validate_ops.VALIDATE_SHADOW)
        self.assertEqual(validate_ops.cmd_merge_pr(SimpleNamespace(repo="org/repo", pr_number=1)), 1)


if __name__ == "__main__":
    unittest.main()
