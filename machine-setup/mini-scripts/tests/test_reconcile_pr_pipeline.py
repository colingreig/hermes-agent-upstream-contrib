"""Behavioral tests for the source-controlled Mini PR-pipeline deployer."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
RECONCILER = SCRIPTS / "reconcile_pr_pipeline.py"
PIPELINE = SCRIPTS / "pr_pipeline"
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
        self.assertTrue((self.destination / "verify-hermes-patches.sh").is_file())
        self.assertTrue((self.destination / "validator_repo_guard.py").is_file())
        self.assertTrue((self.destination / "pr_pipeline" / "validator_repo_guard.py").is_file())
        self.assertTrue((self.destination / ".pr_pipeline_deployment.json").is_file())

    def test_manifest_includes_validator_repo_guard_in_flat_and_package_surfaces(self):
        manifest = self.mod.resolve_manifest()
        destinations = {item.destination.as_posix() for item in manifest.files}

        self.assertIn("validator_repo_guard.py", destinations)
        self.assertIn("pr_pipeline/validator_repo_guard.py", destinations)
        self.assertIn("validator_repo_guard.py", self.mod._expected_hashes(manifest))

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
