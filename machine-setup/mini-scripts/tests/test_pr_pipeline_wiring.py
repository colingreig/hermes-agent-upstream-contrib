"""Runtime wiring proof for the fenced PR-validator verdict boundary."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
for path in (SCRIPTS, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import validate_pr  # noqa: E402
import validator_verdict  # noqa: E402
import autonomous_merge  # noqa: E402
import merge_guard  # noqa: E402
import hermes_validate_ops  # noqa: E402
from pr_pipeline.identity import TrustedMergeIdentity  # noqa: E402


# Sandbox every merge-activation env var: the ambient dev shell exports
# HERMES_AUTONOMOUS_MERGE and tier vars, which must never leak into these
# assertions.
_MERGE_ENV_KEYS = (
    "HERMES_MERGE_SHADOW",
    "HERMES_MERGE_ACTIVE",
    "VALIDATE_SHADOW",
    "HERMES_AUTONOMOUS_MERGE",
    "HERMES_AUTONOMOUS_MERGE_LOW",
    "HERMES_AUTONOMOUS_MERGE_MEDIUM",
    "HERMES_AUTONOMOUS_MERGE_HIGH",
)


def _merge_env(**overrides) -> dict[str, str]:
    cleared = {key: "" for key in _MERGE_ENV_KEYS}
    cleared.update(overrides)
    return cleared


def identity(head: str = "b" * 40) -> TrustedMergeIdentity:
    return TrustedMergeIdentity(
        canonical_repo="acme/widget",
        pr_number=7,
        trusted_task_id="86e2gh04e",
        base_sha="a" * 40,
        head_sha=head,
        tested_merge_sha="c" * 40,
        ci_policy_id="sha256:" + "d" * 64,
        ci_run_ids=("ci:lint", "ci:unit"),
    )


class RuntimeWiringTests(unittest.TestCase):
    def test_retired_flat_pipeline_duplicates_are_absent_from_repo_and_manifest(self) -> None:
        retired = {"pr_pipeline_improvements.py", "pr_staleness_alert.py"}
        manifest = json.loads((PIPELINE / "manifest.json").read_text(encoding="utf-8"))

        for name in retired:
            self.assertFalse((SCRIPTS / name).exists(), name)
            self.assertTrue((PIPELINE / name).is_file(), name)
        self.assertFalse(retired.intersection(manifest["legacy_flat_entrypoints"]))

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
                mock.patch.object(validate_pr.ar, "check_missing_ci", return_value=[]),
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


class VerdictForgeryTests(unittest.TestCase):
    """A merge-eligible (non-shadow) verdict must be mintable ONLY by the
    validator's own fenced review flow — never by a bare CLI/compat write,
    regardless of env."""

    def test_bare_compat_write_is_always_shadow_even_when_pipeline_is_active(self) -> None:
        trusted = identity()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            with mock.patch.dict(os.environ, _merge_env(HERMES_MERGE_ACTIVE="1")):
                self.assertFalse(validator_verdict.merge_shadow_active())
                kept, immutable = validator_verdict.record_verdict(
                    "acme/widget",
                    7,
                    {
                        "verdict": "PASS",
                        "tier": "low",
                        "task_id": "86e2gh04e",
                        "head_sha": trusted.head_sha,
                        "expected_repo": "acme/widget",
                        "model_used": "forged",
                        "findings": [],
                        "shadow": False,  # attacker-supplied value must be discarded
                    },
                    identity=trusted,
                    path=ledger,
                )
            self.assertFalse(immutable)
            self.assertIs(kept["shadow"], True)
            allowed, why = validator_verdict.is_pass_fresh(
                "acme/widget", 7, trusted.head_sha, path=ledger
            )
            self.assertFalse(allowed)
            self.assertIn("shadow-only", why)

    def test_direct_finalize_without_validator_attestation_is_shadow(self) -> None:
        trusted = identity()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            with mock.patch.dict(os.environ, _merge_env(HERMES_MERGE_ACTIVE="1")):
                session = validator_verdict.begin_shadow_review(trusted, path=ledger)
                kept, _ = validator_verdict.finalize_shadow_review(
                    session,
                    {
                        "verdict": "PASS",
                        "tier": "low",
                        "head_sha": trusted.head_sha,
                        "expected_repo": "acme/widget",
                        "shadow": False,
                    },
                )
            self.assertIs(kept["shadow"], True)

    def test_validator_review_flow_without_a_live_lease_fails_closed_loudly(self) -> None:
        trusted = identity()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            session = validator_verdict.begin_shadow_review(trusted, path=ledger)
            # Simulate the lease dying (expiry/supersession) before finalize.
            session.store.release(session.lease)
            # HERMES_VALIDATOR_FINALIZE_TOKEN present: this exercises the real
            # validator-identity path (2026-07-27 anti-forgery hardening) so
            # the assertion below still proves the lease-liveness check itself
            # fails closed, rather than exercising the token-missing fallback.
            with mock.patch.dict(os.environ, _merge_env(
                HERMES_MERGE_ACTIVE="1", HERMES_VALIDATOR_FINALIZE_TOKEN="test-token",
            )):
                with self.assertRaises(validator_verdict.VerdictStoreError) as caught:
                    validator_verdict.finalize_shadow_review(
                        session,
                        {
                            "verdict": "PASS",
                            "tier": "low",
                            "head_sha": trusted.head_sha,
                            "expected_repo": "acme/widget",
                            "shadow": False,
                        },
                        validator_review=True,
                    )
            self.assertIn("fail-closed", str(caught.exception))
            # Nothing was finalized.
            self.assertIsNone(
                validator_verdict.verdict_for("acme/widget", 7, path=ledger, head_sha=trusted.head_sha)
            )

    def test_validator_review_flow_finalizes_non_shadow_when_active(self) -> None:
        trusted = identity()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            with (
                # HERMES_VALIDATOR_FINALIZE_TOKEN present: this test asserts
                # what a REAL validator process (2026-07-27 anti-forgery
                # hardening: the token models "this process is the validator")
                # finalizes when active — not the missing-token fallback,
                # which is covered by test_pr_pipeline_reereview_fixes.py.
                mock.patch.dict(os.environ, _merge_env(
                    HERMES_MERGE_ACTIVE="1", HERMES_VALIDATOR_FINALIZE_TOKEN="test-token",
                )),
                mock.patch.object(validate_pr.vc, "fetch_pr_diff", return_value="diff --git a/a.py b/a.py\n"),
                mock.patch.object(validate_pr.vc, "pr_head_sha", return_value=trusted.head_sha),
                mock.patch.object(validate_pr.vt, "run", return_value={"tier": "low", "findings": []}),
                mock.patch.object(validate_pr.via, "run", return_value={"findings": []}),
                mock.patch.object(validate_pr.ar, "check_missing_ci", return_value=[]),
                mock.patch.object(
                    validate_pr,
                    "_run_content_lens",
                    return_value={"verdict": "PASS", "prose_pct": 0.0, "reason": "not prose-dominant"},
                ),
            ):
                code, result = validate_pr.validate(
                    "acme/widget", 7, task="86e2gh04e", shadow=False,
                    trusted_identity=trusted, trust_store_path=ledger,
                )
                self.assertEqual(code, 0)
                self.assertIs(result["shadow"], False)
                allowed, why = validator_verdict.is_pass_fresh(
                    "acme/widget", 7, trusted.head_sha, path=ledger
                )
            self.assertTrue(allowed, why)

    def test_no_panel_run_always_finalizes_shadow_regardless_of_tier_and_env(self) -> None:
        trusted = identity()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            with (
                mock.patch.dict(os.environ, _merge_env(HERMES_MERGE_ACTIVE="1")),
                mock.patch.object(validate_pr.vc, "fetch_pr_diff", return_value="diff --git a/a.py b/a.py\n"),
                mock.patch.object(validate_pr.vc, "pr_head_sha", return_value=trusted.head_sha),
                mock.patch.object(validate_pr.vt, "run", return_value={"tier": "medium", "findings": []}),
                mock.patch.object(validate_pr.via, "run", return_value={"findings": []}),
                mock.patch.object(validate_pr.ar, "check_missing_ci", return_value=[]),
            ):
                code, result = validate_pr.validate(
                    "acme/widget", 7, task="86e2gh04e", shadow=False,
                    allow_panel=False, trusted_identity=trusted, trust_store_path=ledger,
                )
                self.assertEqual(code, 0)
                self.assertEqual(result["verdict"], "PASS")
                # Panel-suppressed => shadow verdict => never merge-eligible.
                self.assertIs(result["shadow"], True)
                allowed, why = validator_verdict.is_pass_fresh(
                    "acme/widget", 7, trusted.head_sha, path=ledger
                )
            self.assertFalse(allowed)
            self.assertIn("shadow-only", why)

    def test_merge_guard_blocks_verdict_write_invocations_unconditionally(self) -> None:
        blocked = (
            "python3 ~/.hermes/scripts/validator_verdict.py write --repo acme/widget "
            "--pr 7 --identity /tmp/id.json --verdict PASS --tier low",
            "python3 -m pr_pipeline.validator_verdict write --repo acme/widget --pr 7",
            "python3 -c 'import validator_verdict as v; v.record_verdict(\"acme/widget\", 7, {})'",
            "python3 -c 'from validator_verdict import finalize_shadow_review; finalize_shadow_review(s, r)'",
        )
        allowed = (
            "python3 ~/.hermes/scripts/validator_verdict.py get --repo acme/widget --pr 7",
            "gh pr view 7 --json mergeable",
            "git merge main",
        )
        for command in blocked:
            self.assertTrue(merge_guard._is_verdict_write_command(command), command)
            self.assertTrue(merge_guard._is_merge_command(command), command)
        for command in allowed:
            self.assertFalse(merge_guard._is_verdict_write_command(command), command)
            self.assertFalse(merge_guard._is_merge_command(command), command)


class MergeExecutorGateTests(unittest.TestCase):
    """cmd_merge_pr must enforce the tier gate and pin the merge to the
    validated head SHA."""

    def _run_cmd_merge_pr(self, verdict, env_overrides=None):
        head = "b" * 40
        green = {
            "state": "OPEN", "head": head, "mergeable": "MERGEABLE",
            "merge_state": "CLEAN", "draft": False, "labels": [],
            "failing": [], "pending": [], "ignored": [],
            "gating_green": ["Lint, typecheck, test"],
        }
        fake_store = SimpleNamespace(
            is_pass_fresh=lambda repo, pr, head_sha="", *a, **k: (True, "fresh"),
            verdict_for=lambda repo, pr, path=None, head_sha="", *a, **k: verdict,
        )
        stdout = io.StringIO()
        env = _merge_env(HERMES_MERGE_ACTIVE="1", HERMES_AUTONOMOUS_MERGE="1")
        env.update(env_overrides or {})
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(hermes_validate_ops, "VALIDATE_SHADOW", False),
            mock.patch.object(hermes_validate_ops, "DRY_RUN", True),
            mock.patch.object(hermes_validate_ops, "load_allowlist", lambda: {"acme/widget"}),
            mock.patch.object(hermes_validate_ops, "validator_verdict", fake_store),
            # cmd_merge_pr resolves autonomous_merge via sys.modules; pin it to
            # the REAL module (other test files install empty stubs there).
            mock.patch.dict(sys.modules, {"autonomous_merge": autonomous_merge}),
            mock.patch.object(autonomous_merge, "_pr_state", lambda repo, pr: (green, None)),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            rc = hermes_validate_ops.cmd_merge_pr(
                SimpleNamespace(repo="acme/widget", pr_number=7, squash=True)
            )
        return rc, stdout.getvalue(), head

    def test_merge_command_is_pinned_to_the_validated_head_sha(self) -> None:
        rc, out, head = self._run_cmd_merge_pr({"tier": "low", "head_sha": "b" * 40})
        self.assertEqual(rc, 0)
        self.assertIn(f"--match-head-commit {head}", out)

    def test_high_tier_is_refused_even_with_master_switch(self) -> None:
        rc, _, _ = self._run_cmd_merge_pr(
            {"tier": "high", "head_sha": "b" * 40},
            env_overrides={"HERMES_AUTONOMOUS_MERGE_HIGH": "1"},
        )
        self.assertEqual(rc, 1)

    def test_undeterminable_tier_is_refused_fail_closed(self) -> None:
        for verdict in (None, {}, {"tier": "weird"}):
            rc, _, _ = self._run_cmd_merge_pr(verdict)
            self.assertEqual(rc, 1, verdict)


if __name__ == "__main__":
    unittest.main()
