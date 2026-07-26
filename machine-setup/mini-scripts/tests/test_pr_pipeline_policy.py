"""Fail-closed policy-manifest and CI evidence tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from pr_pipeline.policy import CIRun, PolicyError, parse_policy_manifest  # noqa: E402


MERGE = "c" * 40


def manifest() -> str:
    return '{"schema_version":1,"repositories":["Acme/Widget"],"required_checks":["unit","lint"]}'


class PolicyManifestTests(unittest.TestCase):
    def test_manifest_is_content_addressed_and_rejects_unknown_or_duplicate_shape(self) -> None:
        policy = parse_policy_manifest(manifest())
        self.assertEqual(policy.repositories, ("acme/widget",))
        self.assertTrue(policy.policy_id.startswith("sha256:"))
        with self.assertRaises(PolicyError):
            parse_policy_manifest('{"schema_version":1,"repositories":["acme/widget"],"required_checks":["unit"],"skip_failures":true}')
        with self.assertRaises(PolicyError):
            parse_policy_manifest('{"schema_version":1,"repositories":["acme/widget"],"repositories":["other/repo"],"required_checks":["unit"]}')
        with self.assertRaises(PolicyError):
            parse_policy_manifest('{"schema_version":1,"repositories":["acme/widget"],"required_checks":[]}')

    def test_ci_evidence_must_cover_every_required_check_at_exact_tested_merge(self) -> None:
        policy = parse_policy_manifest(manifest())
        runs = (
            CIRun("101", "unit", "success", MERGE),
            CIRun("102", "lint", "success", MERGE),
        )
        identity = policy.bind_identity(
            canonical_repo="acme/widget", pr_number=7, trusted_task_id="86e2gh04e",
            base_sha="a" * 40, head_sha="b" * 40, tested_merge_sha=MERGE, runs=runs,
        )
        self.assertEqual(identity.ci_policy_id, policy.policy_id)
        self.assertEqual(identity.ci_run_ids, ("101", "102"))
        with self.assertRaises(PolicyError):
            policy.successful_run_ids(repository="acme/widget", tested_merge_sha=MERGE, runs=(runs[0],))
        with self.assertRaises(PolicyError):
            policy.successful_run_ids(
                repository="acme/widget", tested_merge_sha=MERGE,
                runs=(runs[0], CIRun("102", "lint", "success", "d" * 40)),
            )

    def test_skipped_neutral_and_failed_ci_are_not_configurable_as_success(self) -> None:
        for conclusion in ("skipped", "neutral", "cancelled", "failure"):
            with self.subTest(conclusion=conclusion), self.assertRaises(PolicyError):
                CIRun("101", "unit", conclusion, MERGE)


if __name__ == "__main__":
    unittest.main()
