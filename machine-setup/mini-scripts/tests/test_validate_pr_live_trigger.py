"""Tests for the deterministic validator live trigger (task 86e2m44np).

Covers the three load-bearing properties:
  1. candidate discipline — only fully CI-green, unheld, open, unverdicted
     heads are ever handed to the fenced validator (immutable-verdict safety);
  2. the trust boundary — the trigger refuses to run without the finalize
     token, and everything it validates goes through validate_pr.validate()
     with shadow=False/allow_panel=True (the fenced review flow), producing a
     real non-shadow SQLite finalization the merge sweep can act on;
  3. governance — the new entrypoint is manifest-governed, the fleet cron job
     exists with a job-scoped finalize-token declaration, the fleet-outcome
     contract covers the job, and every governed checksum matches its bytes.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
REPO_ROOT = SCRIPTS.parent.parent
FLEET_CONFIG = REPO_ROOT / "machine-setup" / "fleet-config"
for path in (SCRIPTS, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from pr_pipeline import validate_pr_live_trigger as vplt  # noqa: E402
from pr_pipeline import validate_pr  # noqa: E402
from pr_pipeline import validator_verdict  # noqa: E402
from pr_pipeline.identity import TrustedMergeIdentity  # noqa: E402

JOB_ID = "4b8e1d97c3a2"
JOB_NAME = "validator-live-trigger"

_MERGE_ENV_KEYS = (
    "HERMES_MERGE_SHADOW",
    "HERMES_MERGE_ACTIVE",
    "VALIDATE_SHADOW",
    "HERMES_AUTONOMOUS_MERGE",
    "HERMES_AUTONOMOUS_MERGE_LOW",
    "HERMES_AUTONOMOUS_MERGE_MEDIUM",
    "HERMES_AUTONOMOUS_MERGE_HIGH",
    "HERMES_VALIDATOR_FINALIZE_TOKEN",
    "HERMES_VALIDATOR_TRIGGER_MAX",
    "DRY_RUN",
)


def _env(**overrides):
    cleared = {key: "" for key in _MERGE_ENV_KEYS}
    cleared.update(overrides)
    return cleared


def _info(**overrides):
    base = {
        "state": "OPEN",
        "head": "f" * 40,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "draft": False,
        "labels": [],
        "failing": [],
        "pending": [],
        "ignored": [],
        "gating_green": ["ci/test"],
    }
    base.update(overrides)
    return base


class CandidateSkipReasonTests(unittest.TestCase):
    def test_green_unheld_open_pr_is_a_candidate(self):
        self.assertEqual(vplt.candidate_skip_reason(_info()), "")

    def test_non_open_pr_is_skipped(self):
        self.assertIn("not OPEN", vplt.candidate_skip_reason(_info(state="MERGED")))

    def test_draft_pr_is_skipped(self):
        self.assertIn("draft", vplt.candidate_skip_reason(_info(draft=True)))

    def test_hold_labels_are_honored(self):
        for label in ("hold-for-colin", "do-not-merge", "do-not-auto-merge", "hold", "HOLD"):
            reason = vplt.candidate_skip_reason(_info(labels=[label]))
            self.assertIn("hold label", reason, label)

    def test_conflicting_pr_is_skipped(self):
        self.assertIn("conflict", vplt.candidate_skip_reason(_info(mergeable="CONFLICTING")))
        self.assertIn("conflict", vplt.candidate_skip_reason(_info(merge_state="DIRTY")))

    def test_failing_gating_checks_block_candidacy(self):
        self.assertIn("failing", vplt.candidate_skip_reason(_info(failing=["ci/test"])))

    def test_pending_gating_checks_block_candidacy(self):
        # THE immutable-verdict footgun: validating a head whose CI has not
        # finished would permanently BLOCK that head. Must never be a candidate.
        self.assertIn("pending", vplt.candidate_skip_reason(_info(pending=["ci/test"])))

    def test_no_green_gating_check_blocks_candidacy(self):
        self.assertIn("no green gating check",
                      vplt.candidate_skip_reason(_info(gating_green=[])))


class ScanCandidatesTests(unittest.TestCase):
    def test_alias_spellings_are_deduped_by_pr_and_head(self):
        prs = [{"number": 316, "title": "t", "body": "clickup.com/t/86e2m44np",
                "headRefName": "agent/86e2m44np"}]
        with (
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(), None)),
            mock.patch.object(vplt.validator_verdict, "verdict_for", return_value=None),
        ):
            candidates, skipped, errors = vplt.scan_candidates(
                {"colingreig/hermes-agent", "colingreig/hermes-agent-upstream-contrib"})
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["repo"], "colingreig/hermes-agent")
        self.assertEqual(candidates[0]["task_id"], "86e2m44np")
        self.assertEqual(errors, [])

    def test_existing_immutable_verdict_is_skipped(self):
        prs = [{"number": 9, "title": "", "body": "", "headRefName": ""}]
        with (
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(), None)),
            mock.patch.object(vplt.validator_verdict, "verdict_for",
                              return_value={"verdict": "PASS"}),
        ):
            candidates, skipped, _ = vplt.scan_candidates({"acme/widget"})
        self.assertEqual(candidates, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("immutable verdict already recorded", skipped[0]["reason"])

    def test_pr_state_error_is_nonfatal(self):
        prs = [{"number": 1, "title": "", "body": "", "headRefName": ""}]
        with (
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(None, "gh boom")),
        ):
            candidates, skipped, errors = vplt.scan_candidates({"acme/widget"})
        self.assertEqual(candidates, [])
        self.assertEqual(len(errors), 1)


class RunTriggerTests(unittest.TestCase):
    def test_missing_finalize_token_refuses_loudly(self):
        with (
            mock.patch.dict(os.environ, _env()),
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            rc, summary = vplt.run()
        self.assertEqual(rc, 1)
        self.assertIn("finalize-token-absent", summary["errors"])
        validate.assert_not_called()

    def test_candidates_are_validated_non_shadow_with_panel(self):
        candidate = {"repo": "acme/widget", "pr": 7, "head": "f" * 40,
                     "task_id": "86e2m44np"}
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=([candidate], [], [])),
            mock.patch.object(vplt.validate_pr, "validate",
                              return_value=(0, {"verdict": "PASS", "tier": "low",
                                                "shadow": False})) as validate,
        ):
            rc, summary = vplt.run()
        self.assertEqual(rc, 0)
        validate.assert_called_once_with(
            "acme/widget", 7, task="86e2m44np", shadow=False, allow_panel=True,
            expected_repo="acme/widget")
        self.assertEqual(summary["validated"][0]["verdict"], "PASS")
        self.assertIs(summary["validated"][0]["shadow"], False)

    def test_per_tick_cap_bounds_validations(self):
        candidates = [
            {"repo": "acme/widget", "pr": n, "head": f"{n:040x}", "task_id": ""}
            for n in range(1, 6)
        ]
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=(candidates, [], [])),
            mock.patch.object(vplt.validate_pr, "validate",
                              return_value=(0, {"verdict": "PASS", "tier": "low",
                                                "shadow": False})) as validate,
        ):
            rc, summary = vplt.run(cap=2)
        self.assertEqual(rc, 0)
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(len(summary["validated"]), 2)

    def test_dry_run_never_validates(self):
        candidate = {"repo": "acme/widget", "pr": 7, "head": "f" * 40, "task_id": ""}
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=([candidate], [], [])),
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            rc, summary = vplt.run(dry_run=True)
        self.assertEqual(rc, 0)
        validate.assert_not_called()
        self.assertEqual(summary["validated"], [])

    def test_validation_crash_is_nonfatal_and_recorded(self):
        candidate = {"repo": "acme/widget", "pr": 7, "head": "f" * 40, "task_id": ""}
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=([candidate], [], [])),
            mock.patch.object(vplt.validate_pr, "validate",
                              side_effect=RuntimeError("boom")),
        ):
            rc, summary = vplt.run()
        self.assertEqual(rc, 0)
        self.assertEqual(summary["validated"], [])
        self.assertTrue(any("boom" in err for err in summary["errors"]))

    def test_max_validations_env_parsing(self):
        with mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_TRIGGER_MAX="5")):
            self.assertEqual(vplt.max_validations(), 5)
        with mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_TRIGGER_MAX="0")):
            self.assertEqual(vplt.max_validations(), vplt.DEFAULT_MAX_VALIDATIONS)
        with mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_TRIGGER_MAX="bogus")):
            self.assertEqual(vplt.max_validations(), vplt.DEFAULT_MAX_VALIDATIONS)


class EndToEndLedgerTests(unittest.TestCase):
    """The trigger's whole reason to exist: a real non-shadow finalization row."""

    def test_trigger_produces_a_merge_eligible_finalization(self):
        identity = TrustedMergeIdentity(
            canonical_repo="acme/widget",
            pr_number=7,
            trusted_task_id="86e2m44np",
            base_sha="a" * 40,
            head_sha="b" * 40,
            tested_merge_sha="c" * 40,
            ci_policy_id="sha256:" + "d" * 64,
            ci_run_ids=("ci:lint", "ci:unit"),
        )
        candidate = {"repo": "acme/widget", "pr": 7, "head": identity.head_sha,
                     "task_id": "86e2m44np"}
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "trust.sqlite3"
            fenced_validate = functools.partial(
                validate_pr.validate,
                trusted_identity=identity,
                trust_store_path=ledger,
            )
            with (
                mock.patch.dict(os.environ, _env(
                    HERMES_MERGE_ACTIVE="1",
                    HERMES_VALIDATOR_FINALIZE_TOKEN="test-token",
                )),
                mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                                  return_value={"acme/widget"}),
                mock.patch.object(vplt, "scan_candidates",
                                  return_value=([candidate], [], [])),
                mock.patch.object(vplt, "validate_pr") as trigger_validate_pr,
                mock.patch.object(validate_pr.vc, "fetch_pr_diff",
                                  return_value="diff --git a/a.py b/a.py\n"),
                mock.patch.object(validate_pr.vc, "pr_head_sha",
                                  return_value=identity.head_sha),
                mock.patch.object(validate_pr.vt, "run",
                                  return_value={"tier": "low", "findings": []}),
                mock.patch.object(validate_pr.via, "run",
                                  return_value={"findings": []}),
                mock.patch.object(validate_pr.ar, "check_missing_ci",
                                  return_value=[]),
            ):
                trigger_validate_pr.validate = fenced_validate
                rc, summary = vplt.run()

            self.assertEqual(rc, 0)
            self.assertEqual(len(summary["validated"]), 1)
            self.assertEqual(summary["validated"][0]["verdict"], "PASS")
            self.assertIs(summary["validated"][0]["shadow"], False)

            with sqlite3.connect(ledger) as conn:
                count = conn.execute("SELECT COUNT(*) FROM finalizations").fetchone()[0]
            self.assertEqual(count, 1)

            allowed, why = validator_verdict.is_pass_fresh(
                "acme/widget", 7, identity.head_sha, path=ledger)
            self.assertTrue(allowed, why)


class GovernanceTests(unittest.TestCase):
    """The wiring must be deployable: manifests, cron job, contract, checksums."""

    def test_pipeline_manifest_governs_both_trigger_surfaces(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "reconcile_pr_pipeline_trigger_test", SCRIPTS / "reconcile_pr_pipeline.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        manifest = module.resolve_manifest()
        destinations = {item.destination.as_posix() for item in manifest.files}
        self.assertIn("validate_pr_live_trigger.py", destinations)
        self.assertIn("pr_pipeline/validate_pr_live_trigger.py", destinations)

    def test_fleet_jobs_declares_the_trigger_with_scoped_token(self):
        payload = json.loads((FLEET_CONFIG / "jobs.json").read_text(encoding="utf-8"))
        jobs = {job["name"]: job for job in payload["jobs"]}
        job = jobs.get(JOB_NAME)
        self.assertIsNotNone(job, "validator-live-trigger job missing from fleet jobs.json")
        self.assertEqual(job["id"], JOB_ID)
        self.assertTrue(job["enabled"])
        self.assertTrue(job["no_agent"])
        self.assertEqual(job["script"], "validate_pr_live_trigger.py")
        self.assertEqual(job["required_environment_variables"],
                         ["HERMES_VALIDATOR_FINALIZE_TOKEN"])
        # The finalize token must be job-scoped, never boot-wide: only this
        # job and hermes-pr-validate may declare it.
        declaring = sorted(
            j["name"] for j in payload["jobs"]
            if "HERMES_VALIDATOR_FINALIZE_TOKEN"
            in (j.get("required_environment_variables") or [])
        )
        self.assertEqual(declaring, ["hermes-pr-validate", JOB_NAME])

    def test_fleet_config_manifest_checksum_matches_jobs_json(self):
        manifest = json.loads(
            (FLEET_CONFIG / "fleet_config_manifest.json").read_text(encoding="utf-8"))
        entry = next(f for f in manifest["files"] if f["src_rel"] == "jobs.json")
        actual = hashlib.sha256((FLEET_CONFIG / "jobs.json").read_bytes()).hexdigest()
        self.assertEqual(entry["sha256"], actual,
                         "fleet_config_manifest.json jobs.json sha256 is stale")

    def test_fleet_outcome_contract_covers_the_trigger(self):
        contracts = json.loads(
            (SCRIPTS / "fleet_outcome_contracts.json").read_text(encoding="utf-8"))
        entry = next((c for c in contracts["cron_jobs"] if c.get("id") == JOB_ID), None)
        self.assertIsNotNone(entry, "no fleet-outcome contract for validator-live-trigger")
        self.assertEqual(entry["name"], JOB_NAME)
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["outcome"]["kind"], "cron_output")
        self.assertIn("FINALIZE TOKEN ABSENT", entry["outcome"]["failure_patterns"])

    def test_fleet_outcome_manifest_checksum_matches_contracts(self):
        manifest = json.loads(
            (SCRIPTS / "fleet_outcome_manifest.json").read_text(encoding="utf-8"))
        entry = next(f for f in manifest["files"]
                     if f["source"] == "fleet_outcome_contracts.json")
        actual = hashlib.sha256(
            (SCRIPTS / "fleet_outcome_contracts.json").read_bytes()).hexdigest()
        self.assertEqual(entry["sha256"], actual,
                         "fleet_outcome_manifest.json contracts sha256 is stale — "
                         "silent-rollback trap (see 2026-07-31 incident)")

    def test_root_shim_import_smoke(self):
        env = dict(os.environ)
        env["HERMES_VALIDATOR_TRIGGER_IMPORT_SMOKE"] = "1"
        env["PYTHONPATH"] = str(SCRIPTS)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "validate_pr_live_trigger.py")],
            capture_output=True, text=True, timeout=30, env=env, cwd=str(SCRIPTS))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "validator-live-trigger-import-smoke: ok")


if __name__ == "__main__":
    unittest.main()
