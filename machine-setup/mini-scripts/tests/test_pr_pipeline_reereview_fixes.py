"""Regression tests for the 2026-07-27 re-review fixes on the autonomous-merge
activation branch:

  FIX 1: a validator_panel PASS produced by a degraded panel (no model chain
         resolved, every lens errored, or a partial-lens-error PASS resting on
         the surviving lenses) must never be finalized as a merge-eligible
         (non-shadow) verdict.
  FIX 2: merge_guard._verdict_gate must resolve the PR's live head SHA (via
         autonomous_merge._pr_state) and pass it to is_pass_fresh(), so the
         allow path is reachable again and the tier-specific refusal messages
         are not dead code.
  FIX 3: finalize_shadow_review(validator_review=True) must require
         HERMES_VALIDATOR_FINALIZE_TOKEN to be present (non-empty) in this
         process's env before it will stamp a non-shadow verdict; absent, it
         degrades to shadow=True rather than raising.

The dev shell ambiently exports HERMES_AUTONOMOUS_MERGE and tier vars, so
every test below sandboxes the merge-activation env explicitly.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
for _path in (SCRIPTS, PIPELINE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import validate_pr  # noqa: E402
import validator_verdict  # noqa: E402
import autonomous_merge  # noqa: E402
import merge_guard  # noqa: E402
from pr_pipeline.identity import TrustedMergeIdentity  # noqa: E402


_MERGE_ENV_KEYS = (
    "HERMES_MERGE_SHADOW",
    "HERMES_MERGE_ACTIVE",
    "VALIDATE_SHADOW",
    "HERMES_AUTONOMOUS_MERGE",
    "HERMES_AUTONOMOUS_MERGE_LOW",
    "HERMES_AUTONOMOUS_MERGE_MEDIUM",
    "HERMES_AUTONOMOUS_MERGE_HIGH",
    "HERMES_VALIDATOR_FINALIZE_TOKEN",
)


def _merge_env(**overrides) -> dict[str, str]:
    cleared = {key: "" for key in _MERGE_ENV_KEYS}
    cleared.update(overrides)
    return cleared


def _identity(head: str = "b" * 40, pr: int = 7) -> TrustedMergeIdentity:
    return TrustedMergeIdentity(
        canonical_repo="acme/widget",
        pr_number=pr,
        trusted_task_id="86e2gh04e",
        base_sha="a" * 40,
        head_sha=head,
        tested_merge_sha="c" * 40,
        ci_policy_id="sha256:" + "d" * 64,
        ci_run_ids=("ci:lint", "ci:unit"),
    )


# ---------------------------------------------------------------------------
# FIX 1 — a degraded panel PASS must finalize as shadow.
# ---------------------------------------------------------------------------
class DegradedPanelIsAlwaysShadowTests(unittest.TestCase):
    def _run(self, panel_result, trusted):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            with (
                # HERMES_VALIDATOR_FINALIZE_TOKEN set so FIX 3's validator-only
                # gate (a separate, orthogonal control) doesn't itself force
                # shadow here — this test class isolates FIX 1's behavior.
                mock.patch.dict(os.environ, _merge_env(
                    HERMES_MERGE_ACTIVE="1",
                    HERMES_VALIDATOR_FINALIZE_TOKEN="test-token",
                )),
                mock.patch.object(validate_pr.vc, "fetch_pr_diff",
                                   return_value="diff --git a/a.py b/a.py\n"),
                mock.patch.object(validate_pr.vc, "pr_head_sha",
                                   return_value=trusted.head_sha),
                mock.patch.object(validate_pr.vt, "run",
                                   return_value={"tier": "medium", "findings": []}),
                mock.patch.object(validate_pr.via, "run", return_value={"findings": []}),
                mock.patch.object(validate_pr.ar, "check_missing_ci", return_value=[]),
                mock.patch.object(validate_pr.validator_panel, "run",
                                   return_value=panel_result),
            ):
                code, result = validate_pr.validate(
                    "acme/widget", 7, task="86e2gh04e", shadow=False,
                    trusted_identity=trusted, trust_store_path=ledger,
                )
            allowed, why = validator_verdict.is_pass_fresh(
                "acme/widget", 7, trusted.head_sha, path=ledger
            )
        return code, result, allowed, why

    def test_no_model_chain_resolved_pass_is_shadow(self) -> None:
        trusted = _identity()
        panel = {
            "verdict": "PASS", "tier": "medium", "model_used": "NO-CHAIN",
            "panel_ran": False, "lenses": [], "infra_failure": True,
            "failure_class": "no-model-chain",
            "note": "PANEL INFRA FAILURE: no model chain resolved",
        }
        code, result, allowed, why = self._run(panel, trusted)
        self.assertEqual(code, 0)
        self.assertEqual(result["verdict"], "PASS")
        self.assertIs(result["shadow"], True)
        self.assertFalse(allowed)
        self.assertIn("shadow-only", why)

    def test_every_lens_errored_pass_is_shadow(self) -> None:
        trusted = _identity(head="e" * 40)
        panel = {
            "verdict": "PASS", "tier": "medium",
            "model_used": "m1,m2=ALL-UNAVAILABLE", "panel_ran": False,
            "lenses": [{"lens": "security", "verdict": "ERROR"}],
            "infra_failure": True, "failure_class": "model-chain-unavailable",
            "note": "PANEL INFRA FAILURE: every model in the chain was unavailable",
        }
        code, result, allowed, why = self._run(panel, trusted)
        self.assertEqual(code, 0)
        self.assertIs(result["shadow"], True)
        self.assertFalse(allowed)

    def test_partial_lens_error_pass_is_shadow(self) -> None:
        """The milder variant: panel_ran=True but SOME lenses errored and the
        PASS rests on the surviving lenses — still not merge-eligible."""
        trusted = _identity(head="f" * 40)
        panel = {
            "verdict": "PASS", "tier": "medium", "model_used": "m1,m2",
            "panel_ran": True,
            "lenses": [{"lens": "security", "verdict": "PASS"},
                       {"lens": "quality", "verdict": "ERROR"}],
            "infra_failure": True, "failure_class": "partial-lens-error",
            "note": "PANEL INFRA FAILURE on quality (the other lenses reviewed normally).",
        }
        code, result, allowed, why = self._run(panel, trusted)
        self.assertEqual(code, 0)
        self.assertIs(result["shadow"], True)
        self.assertFalse(allowed)

    def test_healthy_panel_pass_is_not_forced_shadow(self) -> None:
        """Control: a genuinely healthy panel PASS must still be able to
        finalize non-shadow when the pipeline is active — the fix must not
        overreach and blanket-shadow every PASS."""
        trusted = _identity(head="1" * 40)
        panel = {
            "verdict": "PASS", "tier": "medium", "model_used": "m1",
            "panel_ran": True,
            "lenses": [{"lens": "security", "verdict": "PASS"}],
            "infra_failure": False,
        }
        code, result, allowed, why = self._run(panel, trusted)
        self.assertEqual(code, 0)
        self.assertIs(result["shadow"], False)
        self.assertTrue(allowed, why)


# ---------------------------------------------------------------------------
# FIX 2 — merge_guard's verdict gate resolves the live head and can now allow.
# ---------------------------------------------------------------------------
class MergeGuardVerdictGateTests(unittest.TestCase):
    def _seed_pass(self, trusted, ledger, *, tier="medium", token="validator-secret"):
        """Finalize a real fenced non-shadow PASS for `trusted` via the
        validator's own review flow (the only path that can mint one)."""
        with mock.patch.dict(os.environ, _merge_env(
            HERMES_MERGE_ACTIVE="1", HERMES_VALIDATOR_FINALIZE_TOKEN=token,
        )):
            session = validator_verdict.begin_shadow_review(trusted, path=ledger)
            kept, _ = validator_verdict.finalize_shadow_review(
                session,
                {
                    "verdict": "PASS", "tier": tier, "head_sha": trusted.head_sha,
                    "expected_repo": trusted.canonical_repo, "model_used": "test",
                    "findings": [], "ts": validator_verdict._now_iso(),
                },
                validator_review=True,
            )
        self.assertIs(kept["shadow"], False)
        return kept

    def _gate(self, repo, pr, pr_state_head, pr_state_err=None, ledger=None):
        env = _merge_env(HERMES_MERGE_ACTIVE="1", HERMES_AUTONOMOUS_MERGE="1")
        info = None if pr_state_err else {"head": pr_state_head, "base": "a" * 40}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(merge_guard, "validator_verdict",
                               _bound_verdict_store(ledger)),
            mock.patch.object(autonomous_merge, "_pr_state",
                               lambda r, p: (info, pr_state_err)),
        ):
            return merge_guard._verdict_gate("merge_pull_request",
                                              {"owner": "acme", "repo": "widget",
                                               "pullNumber": pr}, None)

    def test_allow_path_is_reachable_when_head_resolves_to_a_fresh_pass(self) -> None:
        trusted = _identity(head="b" * 40)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            self._seed_pass(trusted, ledger, tier="medium")
            ok, why = self._gate("acme/widget", 7, trusted.head_sha, ledger=ledger)
        self.assertTrue(ok, why)
        self.assertIn("autonomy enabled", why)

    def test_high_tier_is_still_refused_even_with_a_fresh_pass(self) -> None:
        trusted = _identity(head="c" * 40)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            self._seed_pass(trusted, ledger, tier="high")
            ok, why = self._gate("acme/widget", 7, trusted.head_sha, ledger=ledger)
        self.assertFalse(ok)
        self.assertIn("NEVER", why)

    def test_force_push_back_to_high_risk_head_cannot_inherit_newer_low_risk_tier(self) -> None:
        """A PR can have verdicts for multiple heads. If B's newer low-risk
        verdict is latest but GitHub is force-pushed back to A, both freshness
        and tier authorization must be read from exact current head A.
        """
        high_head = _identity(head="6" * 40)
        low_head = _identity(head="7" * 40)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            self._seed_pass(high_head, ledger, tier="high")
            self._seed_pass(low_head, ledger, tier="low")
            ok, why = self._gate(
                "acme/widget", 7, high_head.head_sha, ledger=ledger
            )
        self.assertFalse(ok)
        self.assertIn("NEVER", why)

    def test_stale_head_is_refused(self) -> None:
        trusted = _identity(head="d" * 40)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            self._seed_pass(trusted, ledger, tier="medium")
            # Live PR head has moved past the PASS'd head.
            ok, why = self._gate("acme/widget", 7, "9" * 40, ledger=ledger)
        self.assertFalse(ok)

    def test_missing_verdict_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            ok, why = self._gate("acme/widget", 7, "b" * 40, ledger=ledger)
        self.assertFalse(ok)

    def test_unresolvable_head_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            ok, why = self._gate("acme/widget", 7, None,
                                  pr_state_err="gh api timed out", ledger=ledger)
        self.assertFalse(ok)
        self.assertIn("fail-closed", why)


def _bound_verdict_store(ledger):
    """A thin wrapper binding validator_verdict's real functions to a fixed
    test ledger path, mirroring merge_guard's own module-level calls (which
    take no `path` kwarg and therefore hit the default STORE_PATH in prod)."""
    return SimpleNamespace(
        is_pass_fresh=lambda repo, pr, head_sha="", **kwargs: validator_verdict.is_pass_fresh(
            repo, pr, head_sha, path=ledger, **kwargs),
        verdict_for=lambda repo, pr, head_sha="": validator_verdict.verdict_for(
            repo, pr, path=ledger, head_sha=head_sha),
    )


# ---------------------------------------------------------------------------
# FIX 3 — validator-only secret gate on finalize_shadow_review.
# ---------------------------------------------------------------------------
class ValidatorFinalizeTokenTests(unittest.TestCase):
    def _finalize(self, trusted, ledger, *, token=None, force_shadow=False):
        env = _merge_env(HERMES_MERGE_ACTIVE="1")
        if token is not None:
            env["HERMES_VALIDATOR_FINALIZE_TOKEN"] = token
        with mock.patch.dict(os.environ, env):
            session = validator_verdict.begin_shadow_review(trusted, path=ledger)
            kept, _ = validator_verdict.finalize_shadow_review(
                session,
                {
                    "verdict": "PASS", "tier": "medium", "head_sha": trusted.head_sha,
                    "expected_repo": trusted.canonical_repo, "model_used": "test",
                    "findings": [], "ts": validator_verdict._now_iso(),
                },
                validator_review=True, force_shadow=force_shadow,
            )
        return kept

    def test_validator_review_without_token_stamps_shadow(self) -> None:
        trusted = _identity(head="a1" + "0" * 38)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            kept = self._finalize(trusted, ledger, token=None)
        self.assertIs(kept["shadow"], True)

    def test_validator_review_with_empty_token_stamps_shadow(self) -> None:
        trusted = _identity(head="a2" + "0" * 38)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            kept = self._finalize(trusted, ledger, token="")
        self.assertIs(kept["shadow"], True)

    def test_validator_review_with_token_present_stamps_per_merge_shadow_active(self) -> None:
        trusted = _identity(head="a3" + "0" * 38)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            kept = self._finalize(trusted, ledger, token="present-token")
        # HERMES_MERGE_ACTIVE=1 and no shadow override => merge_shadow_active() False.
        self.assertIs(kept["shadow"], False)

    def test_token_present_but_force_shadow_requested_is_still_shadow(self) -> None:
        trusted = _identity(head="a4" + "0" * 38)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            kept = self._finalize(trusted, ledger, token="present-token", force_shadow=True)
        self.assertIs(kept["shadow"], True)

    def test_token_value_is_never_compared_only_presence_checked(self) -> None:
        """Any non-empty value works — the token is not a shared secret being
        verified against a stored value, its mere presence in this process's
        env is the validator-identity signal (executor subprocesses have
        secret-shaped env vars scrubbed by Hermes)."""
        trusted = _identity(head="a5" + "0" * 38)
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "verdicts.sqlite3"
            kept = self._finalize(trusted, ledger, token="literally-anything-nonempty")
        self.assertIs(kept["shadow"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
