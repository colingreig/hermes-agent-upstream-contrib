"""Focused tests for the Mini validator repository-identity guard."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent
PIPELINE = SCRIPTS / "pr_pipeline"
_COUNTER = 0


def _load_guard():
    global _COUNTER
    _COUNTER += 1
    name = f"validator_repo_guard_test_{_COUNTER}"
    spec = importlib.util.spec_from_file_location(name, PIPELINE / "validator_repo_guard.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ValidatorRepoGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = _load_guard()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_path = Path(self.tmp.name) / "repo_identity_cache.json"
        self.alias_path = Path(self.tmp.name) / "repo-aliases.json"
        self.old_cache = os.environ.get("HERMES_REPO_IDENTITY_CACHE")
        self.old_alias = os.environ.get("HERMES_REPO_ALIASES")
        os.environ["HERMES_REPO_IDENTITY_CACHE"] = str(self.cache_path)
        os.environ["HERMES_REPO_ALIASES"] = str(self.alias_path)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self.old_cache is None:
            os.environ.pop("HERMES_REPO_IDENTITY_CACHE", None)
        else:
            os.environ["HERMES_REPO_IDENTITY_CACHE"] = self.old_cache
        if self.old_alias is None:
            os.environ.pop("HERMES_REPO_ALIASES", None)
        else:
            os.environ["HERMES_REPO_ALIASES"] = self.old_alias

    def test_parse_repo_ref_accepts_validator_evidence_shapes(self):
        cases = {
            "https://github.com/acme/widget/pull/7": "acme/widget",
            "git@github.com:acme/widget.git": "acme/widget",
            "https://github.com/acme/widget/actions/runs/123": "acme/widget",
            "acme/widget": "acme/widget",
            "widget": "colingreig/widget",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.guard.parse_repo_ref(raw), expected)
        self.assertIsNone(self.guard.parse_repo_ref("not a repo ref"))

    def test_compare_refs_uses_live_identity_before_stale_aliases(self):
        self.alias_path.write_text(
            json.dumps({"aliases": [["acme/old", "acme/new"]]}),
            encoding="utf-8",
        )

        def lookup(slug):
            identities = {
                "acme/old": {"node_id": "NODE_1", "full_name": "acme/old", "source": "gh"},
                "acme/new": {"node_id": "NODE_2", "full_name": "acme/new", "source": "gh"},
            }
            return self.guard.GH_OK, identities[slug]

        with mock.patch.object(self.guard, "_gh_lookup", side_effect=lookup):
            verdict, detail = self.guard.compare_refs("acme/old", "acme/new")

        self.assertEqual(verdict, self.guard.DIFFERENT)
        self.assertIn("genuinely different repositories", detail)

    def test_compare_refs_falls_back_to_alias_only_when_identity_unresolved(self):
        self.alias_path.write_text(
            json.dumps({"aliases": [["acme/old", "acme/new"]]}),
            encoding="utf-8",
        )
        with mock.patch.object(self.guard, "_gh_lookup", return_value=(self.guard.GH_ERROR, None)):
            verdict, detail = self.guard.compare_refs("acme/old", "acme/new")

        self.assertEqual(verdict, self.guard.SAME)
        self.assertIn("declared equivalent", detail)

    def test_compare_refs_fail_closed_when_identity_cannot_be_established(self):
        with mock.patch.object(self.guard, "_gh_lookup", return_value=(self.guard.GH_ERROR, None)):
            verdict, detail = self.guard.compare_refs("acme/target", "acme/evidence")

        self.assertEqual(verdict, self.guard.UNRESOLVED)
        self.assertIn("could not be established", detail)
        self.assertIn("NAME STRING", detail)

    def test_definite_404_inference_authorizes_different_only_after_live_success(self):
        def lookup(slug):
            if slug == "acme/target":
                return self.guard.GH_OK, {
                    "node_id": "NODE_TARGET",
                    "full_name": "acme/target",
                    "source": "gh",
                }
            return self.guard.GH_NOT_FOUND, None

        with mock.patch.object(self.guard, "_gh_lookup", side_effect=lookup):
            verdict, detail = self.guard.compare_refs("acme/target", "acme/private")

        self.assertEqual(verdict, self.guard.DIFFERENT)
        self.assertIn("returns HTTP 404 to the same working credential", detail)

    def test_stale_cache_prevents_transient_network_failure_from_forcing_skip(self):
        stale_ts = time.time() - self.guard.CACHE_TTL_SECONDS - 5
        self.cache_path.write_text(
            json.dumps({
                "acme/old": {"node_id": "NODE", "full_name": "acme/current", "ts": stale_ts},
                "acme/current": {"node_id": "NODE", "full_name": "acme/current", "ts": stale_ts},
            }),
            encoding="utf-8",
        )
        with mock.patch.object(self.guard, "_gh_lookup", return_value=(self.guard.GH_ERROR, None)):
            verdict, detail = self.guard.compare_refs("acme/old", "acme/current")

        self.assertEqual(verdict, self.guard.SAME)
        self.assertIn("same repository under different names", detail)

    def test_workdir_guard_fails_closed_when_actual_repo_identity_is_unresolved(self):
        with (
            mock.patch.object(self.guard, "workdir_repo_name", return_value="actual"),
            mock.patch.object(self.guard, "workdir_repo_ref", return_value="acme/actual"),
            mock.patch.object(self.guard, "compare_refs", return_value=(self.guard.UNRESOLVED, "no identity")),
        ):
            reason = self.guard.repo_mismatch_reason("expected", "/tmp/worktree", "acme")

        self.assertIsNotNone(reason)
        self.assertIn("could not be established", reason)
        self.assertIn("expected", reason)
        self.assertIn("actual", reason)

    def test_cli_compare_unresolved_exits_abort_not_wrong_repo(self):
        with (
            mock.patch.object(self.guard, "compare_refs", return_value=(self.guard.UNRESOLVED, "no identity")),
            mock.patch.object(sys, "argv", ["validator_repo_guard.py", "--compare", "target", "evidence"]),
        ):
            rc = self.guard.main()

        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
