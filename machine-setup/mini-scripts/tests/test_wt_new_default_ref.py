import importlib.util
import importlib.machinery
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "wt-new"


def load_wt_new():
    loader = importlib.machinery.SourceFileLoader("wt_new", str(SCRIPT))
    spec = importlib.util.spec_from_loader("wt_new", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def create_source_repo(path: Path) -> None:
    run("git", "init", "--initial-branch=main", str(path))
    run("git", "config", "user.name", "wt-new test", cwd=path)
    run("git", "config", "user.email", "wt-new@example.invalid", cwd=path)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    run("git", "add", "README.md", cwd=path)
    run("git", "commit", "-m", "initial", cwd=path)


class DefaultRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wt_new = load_wt_new()

    def tearDown(self):
        self.tmp.cleanup()

    def test_true_mirror_refs_heads_main(self):
        source = self.root / "source"
        mirror = self.root / "mirror.git"
        create_source_repo(source)

        run("git", "clone", "--mirror", str(source), str(mirror))

        self.assertEqual(self.wt_new.default_ref(mirror), "main")

    def test_ordinary_origin_head_when_present(self):
        source = self.root / "source"
        clone = self.root / "clone"
        create_source_repo(source)

        run("git", "clone", str(source), str(clone))

        run("git", "-C", str(clone), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")

        self.assertEqual(self.wt_new.default_ref(clone), "origin/main")

    def test_missing_default_ref_fails_clearly(self):
        broken = self.root / "broken.git"
        run("git", "init", "--bare", str(broken))

        with self.assertRaises(SystemExit) as raised:
            self.wt_new.default_ref(broken)

        message = str(raised.exception)
        self.assertIn("could not resolve mirror default branch", message)
        self.assertIn("Pass --base", message)

    def test_git_env_uses_hermes_gitconfig_when_present(self):
        gitconfig = self.root / ".hermes" / "gitconfig"
        gitconfig.parent.mkdir()
        gitconfig.write_text("[credential]\n\thelper = /fake/helper\n", encoding="utf-8")
        self.wt_new.GIT_CONFIG_PATH = gitconfig

        with mock.patch.dict(os.environ, {}, clear=True):
            env = self.wt_new._git_env()

        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(gitconfig))

    def test_git_env_preserves_explicit_git_config_override(self):
        gitconfig = self.root / ".hermes" / "gitconfig"
        gitconfig.parent.mkdir()
        gitconfig.write_text("[credential]\n\thelper = /fake/helper\n", encoding="utf-8")
        self.wt_new.GIT_CONFIG_PATH = gitconfig

        with mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": "/tmp/custom-gitconfig",
                "GIT_TERMINAL_PROMPT": "1",
            },
            clear=True,
        ):
            env = self.wt_new._git_env()

        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/tmp/custom-gitconfig")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "1")

    def test_try_fetch_passes_git_auth_env(self):
        gitconfig = self.root / ".hermes" / "gitconfig"
        gitconfig.parent.mkdir()
        gitconfig.write_text("[credential]\n\thelper = /fake/helper\n", encoding="utf-8")
        self.wt_new.GIT_CONFIG_PATH = gitconfig
        mirror = self.root / "mirror.git"
        mirror.mkdir()

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            self.wt_new.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run_mock:
            self.wt_new.try_fetch(mirror)

        env = run_mock.call_args.kwargs["env"]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(gitconfig))


if __name__ == "__main__":
    unittest.main()
