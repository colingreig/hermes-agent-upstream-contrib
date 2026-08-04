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
import io
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
from pr_pipeline import validator_repo_guard  # noqa: E402
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


def _cross_process_context():
    """Use fork so full-suite mock state never has to be pickled into children."""
    if "fork" not in multiprocessing.get_all_start_methods():
        raise unittest.SkipTest("cross-process lock tests require POSIX fork")
    return multiprocessing.get_context("fork")


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


def _select_and_reserve_in_process(state_path, candidates, cap, start_barrier, results):
    """Exercise the real reservation transaction in an independent child."""
    try:
        vplt.STATE_PATH = Path(state_path)
        start_barrier.wait(timeout=10)
        transaction = getattr(vplt, "_select_and_reserve_candidates")
        selected = transaction(candidates, cap)
        results.put(("ok", [vplt._candidate_key(item) for item in selected]))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _run_trigger_with_blocking_validator_in_process(
        state_path, candidates, cap, started, entered_validation, release, results,
        entered_scan=None):
    """Exercise the real run path with a blocking validator in another process."""
    try:
        vplt.STATE_PATH = Path(state_path)

        def observed_scan(_allowlist):
            if entered_scan is not None:
                entered_scan.set()
            return candidates, [], []

        def blocking_validate(*_args, **_kwargs):
            entered_validation.set()
            if not release.wait(timeout=10):
                raise TimeoutError("test validator was not released")
            return 0, {"verdict": "PASS", "tier": "low", "shadow": False}

        with (
            mock.patch.dict(
                os.environ,
                _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok"),
            ),
            mock.patch.object(
                vplt.autonomous_merge, "_load_allowlist",
                return_value={"acme/widget"},
            ),
            mock.patch.object(
                vplt, "scan_candidates", side_effect=observed_scan,
            ),
            mock.patch.object(
                vplt.validate_pr, "validate", side_effect=blocking_validate,
            ),
        ):
            started.set()
            rc, summary = vplt.run(cap=cap)
        results.put(("ok", rc, len(summary["validated"])))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


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
        aliases = {"colingreig/hermes-agent", "colingreig/hermes-agent-upstream-contrib"}

        def canonical_identity(repo):
            self.assertIn(repo, aliases)
            return {
                "node_id": "R_hermes_agent",
                "full_name": "colingreig/hermes-agent",
                "source": "test",
            }

        with (
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(), None)),
            mock.patch.object(vplt.validator_verdict, "verdict_for", return_value=None),
            mock.patch.object(validator_repo_guard, "canonical_identity",
                              side_effect=canonical_identity),
        ):
            candidates, skipped, errors = vplt.scan_candidates(aliases)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["repo"], "colingreig/hermes-agent")
        self.assertEqual(candidates[0]["task_id"], "86e2m44np")
        self.assertEqual(errors, [])

    def test_same_pr_head_is_deduped_only_within_canonical_repository(self):
        prs = [{"number": 316, "title": "t", "body": "", "headRefName": "agent/test"}]
        repo_nodes = {
            "acme/gadget": "R_gadget",
            "acme/widget": "R_widget",
            "acme/widget-old-name": "R_widget",
        }

        def canonical_identity(repo):
            full_name = "acme/widget" if repo == "acme/widget-old-name" else repo
            return {"node_id": repo_nodes[repo], "full_name": full_name, "source": "test"}

        with (
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(), None)),
            mock.patch.object(vplt.validator_verdict, "verdict_for", return_value=None),
            mock.patch.object(validator_repo_guard, "canonical_identity",
                              side_effect=canonical_identity),
        ):
            candidates, skipped, errors = vplt.scan_candidates(set(repo_nodes))

        self.assertEqual([candidate["repo"] for candidate in candidates],
                         ["acme/gadget", "acme/widget"])
        self.assertEqual(skipped, [])
        self.assertEqual(errors, [])

    def test_existing_immutable_verdict_is_skipped(self):
        prs = [{"number": 9, "title": "", "body": "", "headRefName": ""}]
        with (
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(), None)),
            mock.patch.object(
                validator_repo_guard,
                "canonical_identity",
                return_value={
                    "node_id": "R_widget",
                    "full_name": "acme/widget",
                    "source": "test",
                },
            ),
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
    def setUp(self):
        self._state_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_directory.cleanup)
        self.state_path = Path(self._state_directory.name) / "validator-trigger-state.json"
        state_patch = mock.patch.object(vplt, "STATE_PATH", self.state_path)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_unresolved_canonical_repository_identity_fails_closed_before_validation(self):
        repo = "acme/widget-alias"
        prs = [{"number": 7, "title": "", "body": "", "headRefName": "agent/test"}]
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist", return_value={repo}),
            mock.patch.object(validator_repo_guard, "canonical_identity", return_value=None),
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(), None)),
            mock.patch.object(vplt.validator_verdict, "verdict_for", return_value=None),
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            rc, summary = vplt.run()

        self.assertEqual(rc, 0)
        self.assertEqual(summary["candidates"], [])
        observations = summary["errors"] + [item["reason"] for item in summary["skipped"]]
        self.assertTrue(
            any(repo in observation and "canonical" in observation.lower()
                for observation in observations),
            observations,
        )
        validate.assert_not_called()

    def test_conflicting_node_ids_for_one_canonical_name_fail_closed_before_pr_listing(self):
        aliases = {"acme/widget", "acme/widget-alias"}
        identities = {
            "acme/widget": {
                "node_id": "R_widget_primary",
                "full_name": "acme/widget",
                "source": "test",
            },
            "acme/widget-alias": {
                "node_id": "R_widget_conflict",
                "full_name": "acme/widget",
                "source": "test",
            },
        }

        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist", return_value=aliases),
            mock.patch.object(
                validator_repo_guard,
                "canonical_identity",
                side_effect=lambda repo: identities[repo],
            ),
            mock.patch.object(vplt, "_list_open_prs", return_value=([], None)) as list_open_prs,
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            rc, summary = vplt.run()

        self.assertEqual(rc, 0)
        self.assertEqual(summary["candidates"], [])
        self.assertTrue(
            any("acme/widget" in error and "conflict" in error.lower()
                for error in summary["errors"]),
            summary["errors"],
        )
        list_open_prs.assert_not_called()
        validate.assert_not_called()

    def test_allowlisted_alias_is_canonicalized_before_becoming_a_ledger_subject(self):
        alias = "acme/widget-old-name"
        canonical_repo = "acme/widget"
        head = "f" * 40
        prs = [{"number": 7, "title": "", "body": "", "headRefName": "agent/test"}]
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={alias}),
            mock.patch.object(
                validator_repo_guard,
                "canonical_identity",
                return_value={
                    "node_id": "R_widget",
                    "full_name": canonical_repo,
                    "source": "test",
                },
            ) as canonical_identity,
            mock.patch.object(vplt, "_list_open_prs",
                              return_value=(prs, None)) as list_open_prs,
            mock.patch.object(vplt.autonomous_merge, "pr_state",
                              return_value=(_info(head=head), None)) as pr_state,
            mock.patch.object(vplt.validator_verdict, "verdict_for",
                              return_value=None) as verdict_for,
            mock.patch.object(
                vplt.validate_pr,
                "validate",
                return_value=(0, {"verdict": "PASS", "tier": "low", "shadow": False}),
            ) as validate,
        ):
            rc, summary = vplt.run()

        self.assertEqual(rc, 0)
        canonical_identity.assert_called_once_with(alias)
        list_open_prs.assert_called_once_with(canonical_repo)
        pr_state.assert_called_once_with(canonical_repo, 7)
        verdict_for.assert_called_once_with(canonical_repo, 7, head_sha=head)
        self.assertEqual(summary["candidates"], [{
            "repo": canonical_repo,
            "pr": 7,
            "head": head,
            "task_id": "",
        }])
        validate.assert_called_once_with(
            canonical_repo, 7, task="", shadow=False, allow_panel=True,
            expected_repo=canonical_repo,
        )
        self.assertNotIn(alias, repr(summary))

    def test_mixed_case_canonical_repo_is_normalized_before_cursor_resume(self):
        canonical_repo = "acme/widget"
        prs = [
            {"number": number, "title": "", "body": "", "headRefName": "agent/test"}
            for number in (2, 1)
        ]
        validated = []

        def pr_state(_repo, number):
            return _info(head=f"{number:040x}"), None

        def validate(repo, pr, **_kwargs):
            validated.append((repo, pr))
            return 0, {"verdict": "PASS", "tier": "low", "shadow": False}

        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={canonical_repo}),
            mock.patch.object(
                validator_repo_guard,
                "canonical_identity",
                return_value={
                    "node_id": "R_widget",
                    "full_name": "Acme/Widget",
                    "source": "test",
                },
            ),
            mock.patch.object(vplt, "_list_open_prs", return_value=(prs, None)),
            mock.patch.object(vplt.autonomous_merge, "pr_state", side_effect=pr_state),
            mock.patch.object(vplt.validator_verdict, "verdict_for", return_value=None),
            mock.patch.object(vplt.validate_pr, "validate", side_effect=validate),
        ):
            first_rc, _ = vplt.run(cap=1)
            first_cursor = json.loads(self.state_path.read_text(encoding="utf-8"))
            second_rc, second_summary = vplt.run(cap=1)

        self.assertEqual(first_rc, 0)
        self.assertEqual(first_cursor, {
            "last_reserved_candidate": [canonical_repo, 1, f"{1:040x}"],
        })
        self.assertEqual(second_rc, 0, second_summary["errors"])
        self.assertEqual(validated, [(canonical_repo, 1), (canonical_repo, 2)])

    def test_round_robin_resumes_after_last_reserved_candidate(self):
        candidates = [
            {"repo": "acme/widget", "pr": n, "head": f"{n:040x}", "task_id": ""}
            for n in range(5, 0, -1)
        ]
        calls = []

        def blocked(repo, pr, **kwargs):
            calls.append(pr)
            return 2, {"verdict": "BLOCK", "tier": None, "shadow": None,
                       "reason": "trusted identity unavailable"}

        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=(candidates, [], [])),
            mock.patch.object(vplt.validate_pr, "validate", side_effect=blocked),
        ):
            first_rc, _ = vplt.run(cap=3)
            second_rc, _ = vplt.run(cap=3)

        self.assertEqual((first_rc, second_rc), (0, 0))
        self.assertEqual(calls, [1, 2, 3, 4, 5, 1])

    def test_concurrent_selection_and_reservation_transactions_are_disjoint(self):
        candidates = [
            {"repo": "acme/widget", "pr": n, "head": f"{n:040x}", "task_id": ""}
            for n in range(1, 7)
        ]
        self.assertFalse(self.state_path.exists())

        # Fork avoids serializing full-suite mock state while still exercising
        # real OS processes and file-lock coordination. A three-party barrier
        # releases both children at the transaction boundary together.
        context = _cross_process_context()
        start_barrier = context.Barrier(3)
        results = context.Queue()
        processes = [
            context.Process(
                target=_select_and_reserve_in_process,
                args=(str(self.state_path), candidates, 2, start_barrier, results),
            )
            for _ in range(2)
        ]
        started_processes = []

        def stop_children():
            for process in started_processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)

        self.addCleanup(stop_children)
        for process in processes:
            process.start()
            started_processes.append(process)
        start_barrier.wait(timeout=10)

        outcomes = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)

        self.assertEqual([status for status, _ in outcomes], ["ok", "ok"], outcomes)
        batches = [set(keys) for _, keys in outcomes]
        self.assertEqual([len(batch) for batch in batches], [2, 2])
        self.assertTrue(batches[0].isdisjoint(batches[1]), outcomes)
        self.assertEqual(
            batches[0] | batches[1],
            {vplt._candidate_key(candidate) for candidate in candidates[:4]},
        )

        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted,
            {"last_reserved_candidate": list(vplt._candidate_key(candidates[3]))},
        )

    def test_overlapping_run_processes_do_not_validate_concurrently_when_selection_wraps(self):
        candidates = [
            {"repo": "acme/widget", "pr": n, "head": f"{n:040x}", "task_id": ""}
            for n in range(1, 3)
        ]
        cap = 3
        self.assertLessEqual(len(candidates), cap)
        # Put the cursor at the end so each current reservation wraps and, since
        # the candidate set is no larger than the cap, selects the same full set.
        self.state_path.write_text(
            json.dumps({
                "last_reserved_candidate": list(vplt._candidate_key(candidates[-1])),
            }),
            encoding="utf-8",
        )

        context = _cross_process_context()
        release = context.Event()
        first_started = context.Event()
        second_started = context.Event()
        first_entered = context.Event()
        second_entered = context.Event()
        results = context.Queue()
        first = context.Process(
            target=_run_trigger_with_blocking_validator_in_process,
            args=(str(self.state_path), candidates, cap, first_started,
                  first_entered, release, results),
        )
        second = context.Process(
            target=_run_trigger_with_blocking_validator_in_process,
            args=(str(self.state_path), candidates, cap, second_started,
                  second_entered, release, results),
        )
        processes = [first, second]
        started_processes = []

        def stop_children():
            release.set()
            for process in started_processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)

        self.addCleanup(stop_children)
        first.start()
        started_processes.append(first)
        self.assertTrue(first_started.wait(timeout=10), "first trigger did not start")
        self.assertTrue(first_entered.wait(timeout=10),
                        "first trigger did not enter validation")

        second.start()
        started_processes.append(second)
        self.assertTrue(second_started.wait(timeout=10), "second trigger did not start")
        self.assertFalse(
            second_entered.wait(timeout=1),
            "second trigger entered validation while the first validator was blocked",
        )

        release.set()
        self.assertTrue(second_entered.wait(timeout=10),
                        "second trigger did not proceed after the first was released")
        outcomes = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)

        self.assertEqual([outcome[0] for outcome in outcomes], ["ok", "ok"], outcomes)
        self.assertEqual(sorted(outcome[2] for outcome in outcomes), [2, 2], outcomes)

    def test_live_candidate_scan_waits_for_prior_full_run_to_release(self):
        candidates = [
            {"repo": "acme/widget", "pr": 7, "head": "f" * 40, "task_id": ""},
        ]
        context = _cross_process_context()
        release = context.Event()
        first_started = context.Event()
        first_entered_validation = context.Event()
        second_started = context.Event()
        second_entered_scan = context.Event()
        second_entered_validation = context.Event()
        results = context.Queue()
        first = context.Process(
            target=_run_trigger_with_blocking_validator_in_process,
            args=(str(self.state_path), candidates, 1, first_started,
                  first_entered_validation, release, results),
        )
        second = context.Process(
            target=_run_trigger_with_blocking_validator_in_process,
            args=(str(self.state_path), candidates, 1, second_started,
                  second_entered_validation, release, results, second_entered_scan),
        )
        processes = [first, second]
        started_processes = []

        def stop_children():
            release.set()
            for process in started_processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)

        self.addCleanup(stop_children)
        first.start()
        started_processes.append(first)
        self.assertTrue(first_started.wait(timeout=10), "first trigger did not start")
        self.assertTrue(
            first_entered_validation.wait(timeout=10),
            "first trigger did not enter validation",
        )

        second.start()
        started_processes.append(second)
        self.assertTrue(second_started.wait(timeout=10), "second trigger did not start")
        self.assertFalse(
            second_entered_scan.wait(timeout=1),
            "second trigger scanned live candidates while the first run held the run lock",
        )

        release.set()
        self.assertTrue(
            second_entered_scan.wait(timeout=10),
            "second trigger did not scan after the first run released the run lock",
        )
        self.assertTrue(
            second_entered_validation.wait(timeout=10),
            "second trigger did not validate after its post-lock live scan",
        )
        outcomes = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(timeout=10)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        self.assertEqual([outcome[0] for outcome in outcomes], ["ok", "ok"], outcomes)
        self.assertEqual(sorted(outcome[2] for outcome in outcomes), [1, 1], outcomes)

    def test_corrupt_cursor_state_fails_closed_before_validation(self):
        candidate = {"repo": "acme/widget", "pr": 7, "head": "f" * 40,
                     "task_id": "86e2m44np"}
        self.state_path.write_text("{not-json", encoding="utf-8")

        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=([candidate], [], [])),
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            rc, summary = vplt.run()

        self.assertNotEqual(rc, 0)
        self.assertTrue(any("round-robin state unreadable" in error
                            for error in summary["errors"]))
        validate.assert_not_called()

    def test_semantically_malformed_cursor_state_fails_closed_before_validation(self):
        candidate = {"repo": "acme/widget", "pr": 7, "head": "f" * 40,
                     "task_id": "86e2m44np"}
        self.state_path.write_text(
            json.dumps({"last_reserved_candidate": ["acme/widget", 0, "not-a-sha"]}),
            encoding="utf-8",
        )

        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=([candidate], [], [])),
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            rc, summary = vplt.run()

        self.assertNotEqual(rc, 0)
        self.assertTrue(any("round-robin state malformed" in error
                            for error in summary["errors"]))
        validate.assert_not_called()

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

    def test_dry_run_reports_bounded_selection_without_persisting_cursor(self):
        candidates = [
            {"repo": "acme/widget", "pr": n, "head": f"{n:040x}", "task_id": ""}
            for n in range(3, 0, -1)
        ]
        first_stdout = io.StringIO()
        second_stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, _env(HERMES_VALIDATOR_FINALIZE_TOKEN="tok")),
            mock.patch.object(vplt.autonomous_merge, "_load_allowlist",
                              return_value={"acme/widget"}),
            mock.patch.object(vplt, "scan_candidates",
                              return_value=(candidates, [], [])),
            mock.patch.object(vplt.validate_pr, "validate") as validate,
        ):
            with redirect_stdout(first_stdout):
                rc, summary = vplt.run(dry_run=True, cap=2)
            self.assertEqual(rc, 0)
            self.assertEqual([item["pr"] for item in summary["candidates"]], [3, 2, 1])
            self.assertIn("would validate acme/widget#1", first_stdout.getvalue())
            self.assertIn("would validate acme/widget#2", first_stdout.getvalue())
            self.assertNotIn("would validate acme/widget#3", first_stdout.getvalue())
            self.assertFalse(self.state_path.exists())

            self.state_path.write_text(
                json.dumps({"last_reserved_candidate": list(vplt._candidate_key(candidates[-1]))}),
                encoding="utf-8",
            )
            cursor_bytes = self.state_path.read_bytes()
            with redirect_stdout(second_stdout):
                rc, summary = vplt.run(dry_run=True, cap=2)

        self.assertEqual(rc, 0)
        self.assertEqual([item["pr"] for item in summary["candidates"]], [3, 2, 1])
        self.assertIn("would validate acme/widget#2", second_stdout.getvalue())
        self.assertIn("would validate acme/widget#3", second_stdout.getvalue())
        self.assertNotIn("would validate acme/widget#1", second_stdout.getvalue())
        self.assertEqual(self.state_path.read_bytes(), cursor_bytes)
        validate.assert_not_called()

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
