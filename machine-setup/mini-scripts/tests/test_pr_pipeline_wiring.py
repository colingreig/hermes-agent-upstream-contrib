"""Runtime wiring proof for the fenced PR-validator verdict boundary."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
for path in (SCRIPTS, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import validate_pr  # noqa: E402
import validator_verdict  # noqa: E402
import autonomous_merge  # noqa: E402
from pr_pipeline.identity import TrustedMergeIdentity  # noqa: E402


def identity() -> TrustedMergeIdentity:
    return TrustedMergeIdentity(
        canonical_repo="acme/widget",
        pr_number=7,
        trusted_task_id="86e2gh04e",
        base_sha="a" * 40,
        head_sha="b" * 40,
        tested_merge_sha="c" * 40,
        ci_policy_id="sha256:" + "d" * 64,
        ci_run_ids=("ci:lint", "ci:unit"),
    )


class RuntimeWiringTests(unittest.TestCase):
    def test_finalization_count_is_read_only_and_distinguishes_unreadable_from_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite3"
            self.assertIsNone(validator_verdict.finalization_count(missing))
            self.assertFalse(missing.exists())

            schemaless = Path(directory) / "schemaless.sqlite3"
            with sqlite3.connect(schemaless):
                pass
            self.assertIsNone(validator_verdict.finalization_count(schemaless))

            ledger = Path(directory) / "ledger.sqlite3"
            with sqlite3.connect(ledger) as connection:
                connection.execute("CREATE TABLE finalizations (id INTEGER PRIMARY KEY)")
            before = ledger.stat().st_mtime_ns
            self.assertEqual(validator_verdict.finalization_count(ledger), 0)
            self.assertEqual(ledger.stat().st_mtime_ns, before)
            with sqlite3.connect(ledger) as connection:
                connection.execute("INSERT INTO finalizations DEFAULT VALUES")
            self.assertEqual(validator_verdict.finalization_count(ledger), 1)

    def test_legacy_status_reordering_keeps_the_trusted_identity_stable(self) -> None:
        base_sha, head_sha, merge_sha = "a" * 40, "b" * 40, "c" * 40
        path_prefix = "repos/acme/widget"
        first_statuses = [
            {
                "context": "legacy-ci", "state": "success", "sha": merge_sha,
                "target_url": "https://ci.example.test/runs/first", "description": "first duplicate",
            },
            {
                "context": "legacy-ci", "state": "success", "sha": merge_sha,
                "target_url": "https://ci.example.test/runs/second", "description": "second duplicate",
            },
        ]
        replies = {
            f"{path_prefix}/pulls/7": {
                "base": {"sha": base_sha, "ref": "main"},
                "head": {"sha": head_sha},
                "merge_commit_sha": merge_sha,
            },
            f"{path_prefix}/git/commits/{merge_sha}": {
                "parents": [{"sha": base_sha}, {"sha": head_sha}],
            },
            # The identity binds the base the synthetic merge was actually tested
            # against, and requires it to still be the live protected-branch tip.
            f"{path_prefix}/git/ref/heads/main": {"object": {"sha": base_sha}},
            f"{path_prefix}/branches/main/protection/required_status_checks": {"contexts": ["legacy-ci"]},
            f"{path_prefix}/commits/{merge_sha}/check-runs": {"check_runs": []},
        }

        def resolve(statuses: list[dict[str, str]]) -> TrustedMergeIdentity:
            with mock.patch.object(
                validator_verdict,
                "_gh_json",
                side_effect=lambda path: {**replies, f"{path_prefix}/commits/{merge_sha}/status": {"statuses": statuses}}[path],
            ):
                return validator_verdict.resolve_shadow_identity("acme/widget", 7, task_id="86e2gh04e")

        first = resolve(first_statuses)
        reordered = resolve(list(reversed(first_statuses)))

        self.assertEqual(first, reordered)
        self.assertEqual(first.fingerprint, reordered.fingerprint)
        self.assertTrue(first.ci_run_ids[0].startswith("status:sha256:"))

    def test_validator_exception_releases_the_exact_lease_for_an_immediate_retry(self) -> None:
        trusted = identity()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            with (
                mock.patch.object(validate_pr.vc, "fetch_pr_diff", return_value="diff --git a/a.py b/a.py\n"),
                mock.patch.object(validate_pr.vc, "pr_head_sha", return_value=trusted.head_sha),
                mock.patch.object(validate_pr.vt, "run", side_effect=RuntimeError("tripwire integration failed")),
            ):
                code, result = validate_pr.validate(
                    "acme/widget",
                    7,
                    task="86e2gh04e",
                    trusted_identity=trusted,
                    trust_store_path=ledger,
                )

            self.assertEqual(code, 2)
            self.assertEqual(result["verdict"], "BLOCK")
            self.assertIsNone(
                validator_verdict.verdict_for("acme/widget", 7, path=ledger, head_sha=trusted.head_sha)
            )
            retry = validator_verdict.begin_shadow_review(trusted, path=ledger, holder="immediate-retry")
            self.assertIsNotNone(retry.lease)
            validator_verdict.abort_shadow_review(retry)

    def test_validate_pr_finalizes_and_reads_only_the_fenced_sqlite_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            retired_json = Path(directory) / "verdicts.json"
            legacy_payload = json.dumps({"acme/widget#7": {"verdict": "PASS"}})
            retired_json.write_text(legacy_payload)
            trusted = identity()

            with (
                mock.patch.object(validate_pr.vc, "fetch_pr_diff", return_value="diff --git a/a.py b/a.py\n"),
                mock.patch.object(validate_pr.vc, "pr_head_sha", return_value=trusted.head_sha),
                mock.patch.object(validate_pr.vt, "run", return_value={"tier": "low", "findings": []}),
                mock.patch.object(validate_pr.via, "run", return_value={"findings": []}),
                mock.patch.object(
                    validate_pr,
                    "_run_content_lens",
                    return_value={"verdict": "PASS", "prose_pct": 0.0, "reason": "not prose-dominant"},
                ),
            ):
                code, result = validate_pr.validate(
                    "acme/widget",
                    7,
                    task="86e2gh04e",
                    trusted_identity=trusted,
                    trust_store_path=ledger,
                )

            self.assertEqual(code, 0)
            self.assertEqual(result["verdict"], "PASS")
            stored = validator_verdict.verdict_for(
                "acme/widget", 7, path=ledger, head_sha=trusted.head_sha
            )
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored["store"], "sqlite-fenced")
            self.assertEqual(stored["identity"], trusted.to_record())
            self.assertGreater(stored["fencing_token"], 0)
            self.assertTrue(ledger.exists())
            self.assertEqual(retired_json.read_text(), legacy_payload)

            # The production merge sweep is a real MergeActor caller. Force
            # HERMES_MERGE_SHADOW=1 for this assertion regardless of the
            # ambient environment (autonomous-merge activation made _shadow()
            # env-derived and default-activated) so this proof of the fenced
            # MergeActor plan stays deterministic: the shadow kill switch
            # prevents every privileged dependency and every live merge.
            with mock.patch.dict(os.environ, {"HERMES_MERGE_SHADOW": "1"}):
                action, detail = autonomous_merge.evaluate(
                    "acme/widget", 7, stored, {"acme/widget"}
                )
            self.assertEqual(action, "skip")
            self.assertIn("fenced MergeActor plan", detail)

            # A sibling retired JSON file cannot make a missing authoritative
            # ledger look valid; only the SQLite terminal finalization counts.
            absent_ledger = Path(directory) / "absent.sqlite3"
            absent_ledger.with_suffix(".json").write_text(json.dumps({"acme/widget#7": {"verdict": "PASS"}}))
            self.assertIsNone(
                validator_verdict.verdict_for("acme/widget", 7, path=absent_ledger, head_sha=trusted.head_sha)
            )
            self.assertEqual(validator_verdict.load_verdicts(path=absent_ledger), {})

            # Replaying a different conclusion for the same immutable candidate
            # cannot replace the already-finalized SQLite verdict.
            kept, immutable = validator_verdict.record_verdict(
                "acme/widget",
                7,
                {
                    "verdict": "BLOCK",
                    "tier": "low",
                    "task_id": "86e2gh04e",
                    "head_sha": trusted.head_sha,
                    "expected_repo": "acme/widget",
                    "model_used": "test",
                    "findings": [],
                },
                identity=trusted,
                path=ledger,
            )
            self.assertTrue(immutable)
            self.assertEqual(kept["verdict"], "PASS")
            with sqlite3.connect(ledger) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM finalizations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
