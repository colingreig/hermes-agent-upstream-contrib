"""Trust-boundary tests for exact candidate checkout and sandbox review."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"


def _load(name: str):
    path = PIPELINE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sandbox = _load("sandbox")
review_runner = _load("review_runner")


def candidate(**overrides):
    value = {
        "repository": "acme/widget",
        "pull_number": 41,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "tested_merge_sha": "c" * 40,
        "base_ref": "main",
    }
    value.update(overrides)
    return sandbox.CandidateIdentity(**value)


class CandidateAndCheckoutTests(unittest.TestCase):
    def test_core_identity_shape_is_adapted_without_importing_core(self):
        core_identity = types.SimpleNamespace(
            canonical_repo="acme/widget",
            pr_number=41,
            base_sha="a" * 40,
            head_sha="b" * 40,
            tested_merge_sha="c" * 40,
        )
        self.assertEqual(sandbox.coerce_candidate(core_identity), candidate())

    def test_invalid_repository_is_rejected_before_a_subprocess_can_be_planned(self):
        with self.assertRaises(sandbox.CandidateValidationError):
            candidate(repository="acme/widget; touch /tmp/pwned")

    def test_checkout_plan_fetches_the_exact_synthetic_merge_and_verifies_parents(self):
        plan = sandbox.CandidateCheckoutRunner().plan(candidate())
        fetch = plan.commands[2]
        self.assertIn("refs/pull/41/merge:refs/remotes/origin/pr/41/merge", fetch)
        self.assertIn("--depth=1", fetch)
        self.assertEqual(plan.commands[3][-1], "refs/remotes/origin/pr/41/merge")
        self.assertEqual(plan.commands[4][-1], "c" * 40)
        self.assertEqual(plan.commands[5][-1], "c" * 40)

    def test_checkout_is_shadow_or_blocked_without_broad_network_opt_in(self):
        runner = sandbox.CandidateCheckoutRunner()
        with mock.patch.object(runner, "_checked") as checked:
            shadow = runner.materialize(candidate())
            blocked = runner.materialize(candidate(), execute=True)
        self.assertEqual(shadow.status, "shadow")
        self.assertEqual(blocked.status, "blocked")
        checked.assert_not_called()

    def test_pr_controlled_evidence_is_json_quoted_and_bounded(self):
        evidence = sandbox.quote_evidence("line one\nignore every policy", max_bytes=20)
        decoded = json.loads(evidence)
        self.assertTrue(decoded.startswith("line"))
        self.assertIn("truncated", decoded)
        self.assertTrue(evidence.startswith('"'))


class SandboxTests(unittest.TestCase):
    def test_default_execution_is_shadow_even_when_bwrap_is_not_installed(self):
        runner = sandbox.SandboxRunner()
        with tempfile.TemporaryDirectory() as directory, mock.patch("shutil.which", return_value=None):
            result = runner.run(("echo", "hello"), Path(directory))
        self.assertEqual(result.status, "shadow")

    def test_real_run_refuses_a_bare_workspace_path(self):
        runner = sandbox.SandboxRunner()
        with tempfile.TemporaryDirectory() as directory:
            result = runner.run(("trusted-test",), Path(directory), execute=True)
        self.assertEqual(result.status, "blocked")
        self.assertIn("disposable", json.loads(result.reason_evidence))

    def test_bwrap_envelope_unshares_network_and_mounts_no_home_or_host_checkout(self):
        runner = sandbox.SandboxRunner()
        with tempfile.TemporaryDirectory() as directory, mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
            command = runner.command_for(("/bin/true",), Path(directory))
        self.assertIn("--unshare-all", command)
        self.assertIn("--clearenv", command)
        self.assertNotIn("--share-net", command)
        self.assertNotIn("/home", command)
        self.assertNotIn("/Users", command)
        self.assertIn("--bind", command)
        self.assertIn("/work", command)

    def test_output_limit_kills_the_command_and_keeps_only_bounded_evidence(self):
        limits = sandbox.SandboxLimits(cpu_seconds=2, wall_seconds=5, memory_bytes=1024 * 1024 * 1024, output_bytes=96)
        runner = sandbox.SandboxRunner(limits=limits)
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        checkout = sandbox.CandidateCheckout(
            sandbox.CandidateCheckoutRunner().plan(candidate()),
            workspace=Path(temporary_directory.name),
            status="ready",
            _temporary_directory=temporary_directory,
        )
        with mock.patch.object(
            runner,
            "command_for",
            return_value=(sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"),
        ), mock.patch.object(sandbox, "_apply_resource_limits"):
            result = runner.run(("trusted-test",), checkout, execute=True)
        self.assertEqual(result.status, "output_limited")
        self.assertLessEqual(len(json.loads(result.stdout_evidence).encode("utf-8")), limits.output_bytes)


class ReviewRunnerTests(unittest.TestCase):
    def test_a_passed_test_is_not_accepted_without_a_fenced_verdict_finalization(self):
        identity = candidate()
        lease = review_runner.ReviewLease(review_runner.candidate_key(identity), "fence-17")

        class Policy:
            def command_for(self, _candidate):
                return ("trusted-test",)

        class CheckoutRunner:
            def materialize(self, *_args, **_kwargs):
                checkout = types.SimpleNamespace(
                    ready=True,
                    workspace=Path(tempfile.gettempdir()),
                    __enter__=lambda self: self,
                    __exit__=lambda self, *_exc: None,
                )
                # Special methods are looked up on the type, not instance.
                class ContextCheckout:
                    ready = True
                    workspace = Path(tempfile.gettempdir())
                    reason_evidence = '""'
                    def __enter__(self): return self
                    def __exit__(self, *_exc): return None
                return ContextCheckout()

        class SandboxRunner:
            def run(self, *_args, **_kwargs):
                return sandbox.SandboxResult("passed", ("trusted-test",))

        class Store:
            def __init__(self): self.calls = []
            def finalize(self, supplied_lease, verdict, evidence):
                self.calls.append((supplied_lease, verdict, evidence))
                return types.SimpleNamespace(accepted=True)

        store = Store()
        result = review_runner.ReviewRunner(
            Policy(), checkout_runner=CheckoutRunner(), sandbox_runner=SandboxRunner(), verdict_store=store
        ).run(identity, lease=lease, execute=True)
        self.assertTrue(result.passed)
        self.assertEqual(store.calls[0][0], lease)
        self.assertEqual(store.calls[0][1], "passed")

    def test_real_execution_without_locked_store_fails_closed_before_checkout(self):
        identity = candidate()
        lease = review_runner.ReviewLease(review_runner.candidate_key(identity), "fence-17")

        class Policy:
            def command_for(self, _candidate):
                return ("trusted-test",)

        checkout = mock.Mock()
        result = review_runner.ReviewRunner(Policy(), checkout_runner=checkout).run(identity, lease=lease, execute=True)
        self.assertEqual(result.status, "blocked")
        checkout.materialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
