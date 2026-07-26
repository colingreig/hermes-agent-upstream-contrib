#!/usr/bin/env python3
"""Tests for validator_repo_guard.py (ClickUp cron 5a76e290811d).

RC1 coverage: the pure workdir guard against throwaway local git repos with
fake `origin` remotes.

RC2 coverage: alias-aware canonical repository identity — the
`hermes-agent` / `hermes-agent-upstream-contrib` rename must NOT read as a
wrong-repo, a genuinely different repo (`brain`) must still be caught, and an
unresolvable identity must SKIP (exit 3) rather than FAIL.

RC2 tests are hermetic by default: `HERMES_REPO_GUARD_NO_NETWORK=1` disables
the live `gh` call and identity comes from a seeded temp cache / alias file.
Set `HERMES_REPO_GUARD_LIVE=1` to additionally exercise the real `gh api`
rename-following path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

MINI_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
GUARD_PATH = MINI_SCRIPTS_DIR / "pr_pipeline" / "validator_repo_guard.py"

# Keep every test off the operator's real identity cache / alias file.
_ISOLATED = tempfile.mkdtemp(prefix="repo-guard-test-")
os.environ.setdefault("HERMES_REPO_IDENTITY_CACHE",
                      str(Path(_ISOLATED) / "cache.json"))
os.environ.setdefault("HERMES_REPO_ALIASES",
                      str(Path(_ISOLATED) / "aliases.json"))

HERMES_NODE = "R_kgDOS5KWsw"   # colingreig/hermes-agent-upstream-contrib
BRAIN_NODE = "R_kgDOS4oHNQ"    # colingreig/brain


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


guard = _load_module(GUARD_PATH, "validator_repo_guard")


def _make_repo(tmp: Path, name: str, remote: str) -> Path:
    repo_dir = tmp / name
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote], cwd=repo_dir, check=True
    )
    return repo_dir


def _seed_cache(path: Path, entries: dict) -> None:
    now = 2_000_000_000.0  # far future so nothing is ever "stale" in tests
    path.write_text(json.dumps(
        {slug: {"node_id": node, "full_name": full, "ts": now}
         for slug, (node, full) in entries.items()}
    ), encoding="utf-8")


def _offline_env(cache: Path = None, aliases: Path = None) -> dict:
    env = dict(os.environ)
    env["HERMES_REPO_GUARD_NO_NETWORK"] = "1"
    env["HERMES_REPO_IDENTITY_CACHE"] = str(
        cache or Path(_ISOLATED) / "empty-cache.json")
    env["HERMES_REPO_ALIASES"] = str(
        aliases or Path(_ISOLATED) / "empty-aliases.json")
    return env


class TestWorkdirRepoName(unittest.TestCase):
    def test_resolves_https_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(
                Path(tmp), "hermes-agent",
                "https://github.com/colingreig/hermes-agent.git",
            )
            self.assertEqual(guard.workdir_repo_name(str(repo)), "hermes-agent")

    def test_resolves_ssh_origin_without_dotgit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(
                Path(tmp), "thermal", "git@github.com:colingreig/thermal"
            )
            self.assertEqual(guard.workdir_repo_name(str(repo)), "thermal")

    def test_missing_directory_returns_none(self):
        self.assertIsNone(guard.workdir_repo_name("/nonexistent/path/xyz"))

    def test_not_a_git_repo_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(guard.workdir_repo_name(tmp))

    def test_no_origin_remote_returns_none(self):
        """repo-identity.sh would fall back to the DIRECTORY name here; that is
        not a repo identity, so the guard must still report None (fail closed)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "no-origin"
            repo_dir.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            self.assertIsNone(guard.workdir_repo_name(str(repo_dir)))

    def test_workdir_repo_ref_includes_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(
                Path(tmp), "hermes-agent",
                "https://github.com/colingreig/hermes-agent.git",
            )
            self.assertEqual(guard.workdir_repo_ref(str(repo)),
                             "colingreig/hermes-agent")


class TestParseRepoRef(unittest.TestCase):
    def test_pull_request_url(self):
        self.assertEqual(
            guard.parse_repo_ref(
                "https://github.com/colingreig/hermes-agent-upstream-contrib/pull/121"),
            "colingreig/hermes-agent-upstream-contrib")

    def test_actions_run_url(self):
        self.assertEqual(
            guard.parse_repo_ref(
                "github.com/colingreig/hermes-agent-upstream-contrib/actions/runs/30188898604"),
            "colingreig/hermes-agent-upstream-contrib")

    def test_ssh_remote(self):
        self.assertEqual(guard.parse_repo_ref("git@github.com:colingreig/brain.git"),
                         "colingreig/brain")

    def test_slug(self):
        self.assertEqual(guard.parse_repo_ref("colingreig/brain"),
                         "colingreig/brain")

    def test_bare_name_takes_default_owner(self):
        self.assertEqual(guard.parse_repo_ref("hermes-agent"),
                         "colingreig/hermes-agent")

    def test_garbage_is_unparseable(self):
        self.assertIsNone(guard.parse_repo_ref("../../etc/passwd"))
        self.assertIsNone(guard.parse_repo_ref(""))
        self.assertIsNone(guard.parse_repo_ref(None))


class TestRepoMismatchReason(unittest.TestCase):
    def test_match_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(
                Path(tmp), "hermes-agent",
                "https://github.com/colingreig/hermes-agent.git",
            )
            self.assertIsNone(guard.repo_mismatch_reason("hermes-agent", str(repo)))

    def test_mismatch_returns_reason(self):
        """The exact RC1 shape: workdir points at thermal, job expects hermes-agent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(
                Path(tmp), "thermal", "https://github.com/colingreig/thermal.git"
            )
            reason = guard.repo_mismatch_reason("hermes-agent", str(repo))
            self.assertIsNotNone(reason)
            self.assertIn("cannot validate: repo mismatch", reason)
            self.assertIn("thermal", reason)
            self.assertIn("hermes-agent", reason)

    def test_unresolvable_workdir_fails_closed(self):
        reason = guard.repo_mismatch_reason("hermes-agent", "/nonexistent/path/xyz")
        self.assertIsNotNone(reason)
        self.assertIn("cannot validate: repo mismatch", reason)


class TestAliasAwareCompare(unittest.TestCase):
    """RC2: rename alias must read SAME; a sibling repo must read DIFFERENT."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="repo-guard-case-")
        self.cache = Path(self.tmp) / "cache.json"
        self.aliases = Path(self.tmp) / "aliases.json"
        self.aliases.write_text(json.dumps({"aliases": []}))
        _seed_cache(self.cache, {
            "colingreig/hermes-agent":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
            "colingreig/hermes-agent-upstream-contrib":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
            "colingreig/brain": (BRAIN_NODE, "colingreig/brain"),
        })
        os.environ["HERMES_REPO_GUARD_NO_NETWORK"] = "1"
        os.environ["HERMES_REPO_IDENTITY_CACHE"] = str(self.cache)
        os.environ["HERMES_REPO_ALIASES"] = str(self.aliases)

    def tearDown(self):
        os.environ.pop("HERMES_REPO_GUARD_NO_NETWORK", None)

    def test_rename_pair_is_same(self):
        verdict, detail = guard.compare_refs(
            "hermes-agent",
            "https://github.com/colingreig/hermes-agent-upstream-contrib/pull/121")
        self.assertEqual(verdict, guard.SAME, detail)
        self.assertIn(HERMES_NODE, detail)

    def test_identical_spelling_is_same_without_lookup(self):
        verdict, _ = guard.compare_refs("colingreig/thermal", "colingreig/thermal")
        self.assertEqual(verdict, guard.SAME)

    def test_sibling_repo_is_different(self):
        verdict, detail = guard.compare_refs(
            "hermes-agent", "https://github.com/colingreig/brain/pull/4")
        self.assertEqual(verdict, guard.DIFFERENT, detail)
        self.assertIn("brain", detail)

    def test_unknown_repo_is_unresolved_not_different(self):
        verdict, detail = guard.compare_refs(
            "hermes-agent", "colingreig/some-repo-with-no-cached-identity")
        self.assertEqual(verdict, guard.UNRESOLVED, detail)

    def test_unparseable_ref_is_unresolved(self):
        verdict, _ = guard.compare_refs("hermes-agent", "../../etc/passwd")
        self.assertEqual(verdict, guard.UNRESOLVED)

    def test_alias_file_short_circuits_offline(self):
        self.aliases.write_text(json.dumps({"aliases": [
            ["colingreig/old-spelling", "colingreig/new-spelling"]]}))
        verdict, detail = guard.compare_refs("old-spelling", "new-spelling")
        self.assertEqual(verdict, guard.SAME, detail)

    def test_renamed_checkout_passes_the_chartered_guard(self):
        """Cron charters 'hermes-agent'; the checkout's origin is the renamed
        spelling. That must NOT abort."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_repo(
                Path(tmp), "hermes-agent-upstream-contrib",
                "https://github.com/colingreig/hermes-agent-upstream-contrib.git",
            )
            self.assertIsNone(
                guard.repo_mismatch_reason("hermes-agent", str(repo)))


class TestDefinite404Inference(unittest.TestCase):
    """A repo the mini's App token cannot see (real 404, e.g.
    colingreig/hermes-ops-scripts in 86e2eu4fp) must still read DIFFERENT —
    but a transport/auth failure must still read UNRESOLVED."""

    def _stub_gh(self, tmp: Path, mode: str) -> dict:
        """Fake `gh` on PATH: resolves hermes-agent*, and either 404s or errors
        for everything else."""
        bindir = tmp / "bin"
        bindir.mkdir()
        script = bindir / "gh"
        if mode == "404":
            other = (
                'echo \'{"message":"Not Found","status":"404"}\'\n'
                'echo "gh: Not Found (HTTP 404)" >&2\n'
                'exit 1\n'
            )
        else:  # transport failure, no HTTP status at all
            other = (
                'echo "error connecting to api.github.com" >&2\n'
                'exit 1\n'
            )
        script.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *hermes-agent*)\n"
            '    echo \'{"node_id":"%s","id":1267898035,'
            '"full_name":"colingreig/hermes-agent-upstream-contrib"}\'\n'
            "    exit 0;;\n"
            "esac\n"
            "%s" % (HERMES_NODE, other)
        )
        script.chmod(0o755)
        env = dict(os.environ)
        env.pop("HERMES_REPO_GUARD_NO_NETWORK", None)
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        env["HERMES_REPO_IDENTITY_CACHE"] = str(tmp / "cache.json")
        env["HERMES_REPO_ALIASES"] = str(tmp / "aliases.json")
        return env

    def test_definite_404_is_different(self):
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-404-"))
        env = self._stub_gh(tmp, "404")
        result = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "https://github.com/colingreig/hermes-ops-scripts/pull/6"],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("DIFFERENT:", result.stdout)
        self.assertIn("hermes-ops-scripts", result.stdout)

    def test_transport_failure_is_skip_not_fail(self):
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-err-"))
        env = self._stub_gh(tmp, "error")
        result = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "https://github.com/colingreig/hermes-ops-scripts/pull/6"],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("ABORT:", result.stdout)
        self.assertIn("SKIP this task", result.stdout)

    def test_404_against_a_cached_target_still_reprobes(self):
        """Target comes from a fresh cache hit (no live call), so the guard must
        re-probe live before trusting the 404."""
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-404c-"))
        env = self._stub_gh(tmp, "404")
        _seed_cache(Path(env["HERMES_REPO_IDENTITY_CACHE"]), {
            "colingreig/hermes-agent":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
        })
        result = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "colingreig/hermes-ops-scripts"],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)


class TestAliasNeverOverridesLiveLookup(unittest.TestCase):
    """RC3: a stale/wrong alias entry must never beat a successful live `gh`
    resolution. Network is available (a stubbed `gh` resolves BOTH sides to
    their real, genuinely different node ids); the alias file wrongly
    declares them the same repository. Live resolution must win: DIFFERENT,
    not SAME-via-alias."""

    def _stub_gh(self, tmp: Path) -> dict:
        """Fake `gh` that resolves hermes-agent* and brain* to their real,
        distinct node ids -- i.e. gh can fully answer both sides."""
        bindir = tmp / "bin"
        bindir.mkdir()
        script = bindir / "gh"
        script.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *hermes-agent*)\n"
            '    echo \'{"node_id":"%s","id":1267898035,'
            '"full_name":"colingreig/hermes-agent-upstream-contrib"}\'\n'
            "    exit 0;;\n"
            "  *brain*)\n"
            '    echo \'{"node_id":"%s","id":999,'
            '"full_name":"colingreig/brain"}\'\n'
            "    exit 0;;\n"
            "esac\n"
            'echo \'{"message":"Not Found","status":"404"}\'\n'
            'echo "gh: Not Found (HTTP 404)" >&2\n'
            "exit 1\n" % (HERMES_NODE, BRAIN_NODE)
        )
        script.chmod(0o755)
        env = dict(os.environ)
        env.pop("HERMES_REPO_GUARD_NO_NETWORK", None)
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
        env["HERMES_REPO_IDENTITY_CACHE"] = str(tmp / "cache.json")
        return env

    def test_wrong_alias_entry_does_not_override_live_different(self):
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-precedence-"))
        env = self._stub_gh(tmp)
        aliases = tmp / "aliases.json"
        # Deliberately WRONG: asserts hermes-agent and brain (genuinely
        # different repos, confirmed by the stub above) are equivalent.
        aliases.write_text(json.dumps({"aliases": [
            ["colingreig/hermes-agent", "colingreig/brain"]]}))
        env["HERMES_REPO_ALIASES"] = str(aliases)

        result = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "colingreig/brain"],
            capture_output=True, text=True, env=env)

        self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
        self.assertIn("DIFFERENT:", result.stdout)
        self.assertNotIn("declared equivalent", result.stdout)

    def test_alias_still_used_when_live_cannot_resolve_either_side(self):
        """Sanity check the same fixture still lets the alias serve as a
        genuine offline last resort when gh has nothing to say about EITHER
        side (unknown to the stub -> definite 404 on both -> no live
        evidence for either -> falls through to the alias)."""
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-precedence-ok-"))
        env = self._stub_gh(tmp)
        aliases = tmp / "aliases.json"
        aliases.write_text(json.dumps({"aliases": [
            ["colingreig/old-thing", "colingreig/new-thing"]]}))
        env["HERMES_REPO_ALIASES"] = str(aliases)

        result = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "old-thing",
             "new-thing"],
            capture_output=True, text=True, env=env)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SAME:", result.stdout)
        self.assertIn("declared equivalent", result.stdout)


class TestCli(unittest.TestCase):
    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            match_repo = _make_repo(
                Path(tmp), "hermes-agent",
                "https://github.com/colingreig/hermes-agent.git",
            )
            mismatch_repo = _make_repo(
                Path(tmp), "thermal", "https://github.com/colingreig/thermal.git"
            )

            ok = subprocess.run(
                ["python3", str(GUARD_PATH), "--expect-repo", "hermes-agent",
                 "--workdir", str(match_repo)],
                capture_output=True, text=True,
            )
            self.assertEqual(ok.returncode, 0)
            self.assertIn("OK:", ok.stdout)

            abort = subprocess.run(
                ["python3", str(GUARD_PATH), "--expect-repo", "hermes-agent",
                 "--workdir", str(mismatch_repo)],
                capture_output=True, text=True,
            )
            self.assertEqual(abort.returncode, 3)
            self.assertIn("ABORT:", abort.stdout)
            self.assertIn("cannot validate: repo mismatch", abort.stdout)

    def test_compare_cli_exit_codes_offline(self):
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-cli-"))
        cache = tmp / "cache.json"
        aliases = tmp / "aliases.json"
        aliases.write_text(json.dumps({"aliases": []}))
        _seed_cache(cache, {
            "colingreig/hermes-agent":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
            "colingreig/hermes-agent-upstream-contrib":
                (HERMES_NODE, "colingreig/hermes-agent-upstream-contrib"),
            "colingreig/brain": (BRAIN_NODE, "colingreig/brain"),
        })
        env = _offline_env(cache, aliases)

        same = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "https://github.com/colingreig/hermes-agent-upstream-contrib/pull/121"],
            capture_output=True, text=True, env=env)
        self.assertEqual(same.returncode, 0, same.stdout + same.stderr)
        self.assertIn("SAME:", same.stdout)

        diff = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "https://github.com/colingreig/brain/pull/4"],
            capture_output=True, text=True, env=env)
        self.assertEqual(diff.returncode, 4, diff.stdout + diff.stderr)
        self.assertIn("DIFFERENT:", diff.stdout)

        skip = subprocess.run(
            ["python3", str(GUARD_PATH), "--compare", "hermes-agent",
             "colingreig/never-heard-of-it"],
            capture_output=True, text=True, env=env)
        self.assertEqual(skip.returncode, 3, skip.stdout + skip.stderr)
        self.assertIn("ABORT:", skip.stdout)
        self.assertIn("SKIP this task", skip.stdout)

    def test_mode_selection_is_exclusive(self):
        bad = subprocess.run(
            ["python3", str(GUARD_PATH)], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)


@unittest.skipUnless(os.environ.get("HERMES_REPO_GUARD_LIVE") == "1",
                     "set HERMES_REPO_GUARD_LIVE=1 to hit the real gh API")
class TestLiveGithubRenameFollowing(unittest.TestCase):
    """Proves the generic mechanism: `gh api` follows the 2026-07-23 rename, so
    no alias entry is needed for hermes-agent."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="repo-guard-live-"))
        os.environ["HERMES_REPO_IDENTITY_CACHE"] = str(tmp / "cache.json")
        os.environ["HERMES_REPO_ALIASES"] = str(tmp / "aliases.json")
        os.environ.pop("HERMES_REPO_GUARD_NO_NETWORK", None)

    def test_old_name_resolves_to_new(self):
        ident = guard.canonical_identity("hermes-agent")
        self.assertIsNotNone(ident)
        self.assertEqual(ident["full_name"],
                         "colingreig/hermes-agent-upstream-contrib")

    def test_rename_pair_same_and_brain_different(self):
        self.assertEqual(
            guard.compare_refs("hermes-agent",
                               "colingreig/hermes-agent-upstream-contrib")[0],
            guard.SAME)
        self.assertEqual(
            guard.compare_refs("hermes-agent", "colingreig/brain")[0],
            guard.DIFFERENT)


if __name__ == "__main__":
    unittest.main()
