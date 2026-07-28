import importlib.util
import importlib.machinery
import subprocess
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
