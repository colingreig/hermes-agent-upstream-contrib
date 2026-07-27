"""Tests for the single privileged exact-merge actor."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PIPELINE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sandbox = _load("sandbox")
merge_actor = _load("merge_actor")


def core_identity(**overrides):
    value = {
        "canonical_repo": "acme/widget",
        "pr_number": 41,
        "trusted_task_id": "86e2gh04e",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "tested_merge_sha": "c" * 40,
        "ci_policy_id": "required-v3",
        "ci_run_ids": ("101", "102"),
    }
    value.update(overrides)
    return types.SimpleNamespace(**value)


def snapshot(identity, **overrides):
    value = {
        "repository": identity.canonical_repo,
        "pull_number": identity.pr_number,
        "is_open": True,
        "base_sha": identity.base_sha,
        "head_sha": identity.head_sha,
        "tested_merge_sha": identity.tested_merge_sha,
        "tested_merge_parents": (identity.base_sha, identity.head_sha),
        "ci_policy_id": identity.ci_policy_id,
        "ci_run_ids": identity.ci_run_ids,
        "required_checks_passed": True,
    }
    value.update(overrides)
    return merge_actor.MergeSnapshot(**value)


class NonForceCasPushTests(unittest.TestCase):
    def test_command_has_a_exact_lease_and_no_force_refspec(self):
        candidate = sandbox.coerce_candidate(core_identity())
        pusher = merge_actor.NonForceCasPusher({"PATH": "/usr/bin:/bin"})
        command = pusher.command_for(candidate, Path("/tmp/exact"))
        branch_ref = "refs/heads/main"
        self.assertIn(f"--force-with-lease={branch_ref}:{candidate.base_sha}", command)
        refspec = command[-1]
        self.assertFalse(refspec.startswith("+"))
        self.assertEqual(refspec, f"{candidate.tested_merge_sha}:{branch_ref}")

    def test_pusher_is_shadow_safe_without_explicit_execute(self):
        candidate = sandbox.coerce_candidate(core_identity())
        result = merge_actor.NonForceCasPusher({"PATH": "/usr/bin:/bin"}).push(candidate, Path("/tmp/exact"))
        self.assertFalse(result.success)
        self.assertIn("disabled", result.reason_evidence)


class MergeActorTests(unittest.TestCase):
    def test_default_is_shadow_and_never_calls_the_privileged_dependencies(self):
        identity = core_identity()
        provider = types.SimpleNamespace(snapshot_for=lambda _identity: self.fail("snapshot should not be called"))
        actor = merge_actor.MergeActor(provider, lambda *_args: self.fail("permit should not be called"))
        result = actor.merge(identity)
        self.assertEqual(result.status, "shadow")

    def test_candidate_drift_is_blocked_before_checkout_or_push(self):
        identity = core_identity()
        provider = types.SimpleNamespace(snapshot_for=lambda supplied: snapshot(supplied, head_sha="d" * 40))

        class CheckoutRunner:
            def materialize(self, *_args, **_kwargs):
                self.called = True
                raise AssertionError("checkout must not happen after drift")

        class Pusher:
            def push(self, *_args, **_kwargs):
                raise AssertionError("push must not happen after drift")

        with tempfile.TemporaryDirectory() as directory:
            actor = merge_actor.MergeActor(provider, lambda *_args: True, pusher=Pusher(), checkout_runner=CheckoutRunner(), lock_path=Path(directory) / "actor.lock")
            result = actor.merge(identity, lease=object(), execute=True)
        self.assertEqual(result.status, "blocked")

    def test_snapshot_ci_runs_in_policy_order_match_the_canonical_identity(self):
        identity = core_identity()
        candidate = sandbox.coerce_candidate(identity)
        current = snapshot(identity, ci_run_ids=("102", "101"))

        self.assertTrue(merge_actor._matches_exact_candidate(current, candidate, identity))

    def test_exact_snapshot_then_nonforce_push_and_post_merge_verification_succeeds(self):
        identity = core_identity()
        snapshots = [snapshot(identity), snapshot(identity, base_sha=identity.tested_merge_sha)]

        class Provider:
            def snapshot_for(self, _identity):
                return snapshots.pop(0)

        class Checkout:
            ready = True
            workspace = Path(tempfile.gettempdir())
            reason_evidence = '""'
            def __enter__(self): return self
            def __exit__(self, *_exc): return None

        class CheckoutRunner:
            def materialize(self, *_args, **_kwargs): return Checkout()

        class Pusher:
            def __init__(self): self.calls = []
            def push(self, supplied_candidate, workspace, **_kwargs):
                self.calls.append((supplied_candidate, workspace))
                return merge_actor.CasPushResult(True)

        pusher = Pusher()
        with tempfile.TemporaryDirectory() as directory:
            actor = merge_actor.MergeActor(Provider(), lambda supplied_identity, _lease: supplied_identity is identity, pusher=pusher, checkout_runner=CheckoutRunner(), lock_path=Path(directory) / "actor.lock")
            result = actor.merge(identity, lease=object(), execute=True)
        self.assertTrue(result.merged)
        self.assertEqual(pusher.calls[0][0].tested_merge_sha, identity.tested_merge_sha)

    def test_no_pusher_means_no_mutation_even_when_execution_is_requested(self):
        identity = core_identity()
        with tempfile.TemporaryDirectory() as directory:
            actor = merge_actor.MergeActor(types.SimpleNamespace(snapshot_for=lambda supplied: snapshot(supplied)), lambda *_args: True, lock_path=Path(directory) / "actor.lock")
            result = actor.merge(identity, lease=object(), execute=True)
        self.assertEqual(result.status, "blocked")


if __name__ == "__main__":
    unittest.main()
