from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from skill_pull_guard import (  # noqa: E402
    LockBusyError,
    acquire_lock,
    assert_prompt_content_clean,
    release_lock,
)


class SkillPullLockTests(unittest.TestCase):
    def test_live_owner_remains_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp).resolve() / "pull.lock"
            token = acquire_lock(
                lock,
                owner_pid=101,
                now=1000,
                pid_alive=lambda pid: pid == 101,
                process_start=lambda pid: f"start-{pid}",
            )
            with self.assertRaisesRegex(LockBusyError, "live pid 101"):
                acquire_lock(
                    lock,
                    owner_pid=202,
                    now=2000,
                    pid_alive=lambda pid: pid == 101,
                    process_start=lambda pid: f"start-{pid}",
                )
            release_lock(lock, token=token)

    def test_dead_or_pid_reused_owner_is_reclaimed(self):
        for live_start in ("", "different-start"):
            with self.subTest(live_start=live_start), tempfile.TemporaryDirectory() as tmp:
                lock = Path(tmp).resolve() / "pull.lock"
                acquire_lock(
                    lock,
                    owner_pid=101,
                    now=1000,
                    pid_alive=lambda _pid: True,
                    process_start=lambda pid: "old-start" if pid == 101 else "new-start",
                )
                token = acquire_lock(
                    lock,
                    owner_pid=202,
                    now=1001,
                    pid_alive=lambda pid: bool(live_start) if pid == 101 else True,
                    process_start=lambda pid: live_start if pid == 101 else "new-start",
                )
                owner = json.loads((lock / "owner.json").read_text())
                self.assertEqual(owner["pid"], 202)
                release_lock(lock, token=token)

    def test_incomplete_lock_requires_age_before_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp).resolve() / "pull.lock"
            lock.mkdir()
            os.utime(lock, (1000, 1000))
            with self.assertRaisesRegex(LockBusyError, "incomplete fresh"):
                acquire_lock(
                    lock,
                    owner_pid=202,
                    now=1001,
                    stale_invalid_after=60,
                    process_start=lambda _pid: "new-start",
                )
            token = acquire_lock(
                lock,
                owner_pid=202,
                now=1061,
                stale_invalid_after=60,
                process_start=lambda _pid: "new-start",
            )
            release_lock(lock, token=token)

    def test_live_pid_with_unreadable_start_token_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp).resolve() / "pull.lock"
            acquire_lock(
                lock,
                owner_pid=101,
                process_start=lambda _pid: "known-start",
            )
            with self.assertRaisesRegex(LockBusyError, "live pid 101"):
                acquire_lock(
                    lock,
                    owner_pid=202,
                    pid_alive=lambda _pid: True,
                    process_start=lambda pid: "" if pid == 101 else "new-start",
                )

    def test_old_owner_cannot_release_reclaimed_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp).resolve() / "pull.lock"
            old_token = acquire_lock(
                lock,
                owner_pid=101,
                process_start=lambda _pid: "old-start",
            )
            new_token = acquire_lock(
                lock,
                owner_pid=202,
                pid_alive=lambda _pid: False,
                process_start=lambda _pid: "new-start",
            )
            with self.assertRaisesRegex(LockBusyError, "another process"):
                release_lock(lock, token=old_token)
            release_lock(lock, token=new_token)


class PromptVisibleCleanlinessTests(unittest.TestCase):
    def test_clean_fixture_includes_untracked_content_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "skills").mkdir()
            (root / "ignite-content" / "skills").mkdir(parents=True)
            calls = []

            def clean_runner(*args, **kwargs):
                calls.append((args, kwargs))
                return subprocess.CompletedProcess(args[0], 0, stdout=b"", stderr=b"")

            assert_prompt_content_clean(
                root, ["skills", "ignite-content/skills"], runner=clean_runner
            )
            command = calls[0][0][0]
            self.assertIn("--untracked-files=all", command)
            self.assertEqual(command[-2:], ["skills", "ignite-content/skills"])
            ignored_command = calls[1][0][0]
            self.assertIn("--ignored", ignored_command)
            self.assertIn("--exclude-standard", ignored_command)

    def test_tracked_and_untracked_dirty_fixtures_fail_closed(self):
        dirty_outputs = (
            b" M skills/blog/SKILL.md\0",
            b"?? skills/new/SKILL.md\0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "skills").mkdir()
            for dirty in dirty_outputs:
                with self.subTest(dirty=dirty):
                    def dirty_runner(*args, **kwargs):
                        return subprocess.CompletedProcess(
                            args[0], 0, stdout=dirty, stderr=b""
                        )

                    with self.assertRaisesRegex(RuntimeError, "tracked or untracked"):
                        assert_prompt_content_clean(
                            root, ["skills"], runner=dirty_runner
                        )

    def test_ignored_prompt_index_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skill = root / "skills" / "ignored"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: ignored\n---\n")

            def ignored_runner(*args, **kwargs):
                command = args[0]
                stdout = (
                    b"skills/ignored/SKILL.md\0"
                    if "ls-files" in command
                    else b""
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=stdout, stderr=b""
                )

            with self.assertRaisesRegex(RuntimeError, "ignored index files"):
                assert_prompt_content_clean(
                    root, ["skills"], runner=ignored_runner
                )

    def test_ignored_non_index_file_does_not_block_pull(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            skill = root / "skills" / "example"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: example\n---\n")
            (skill / "cache.bin").write_bytes(b"cache")

            def ignored_runner(*args, **kwargs):
                command = args[0]
                stdout = (
                    b"skills/example/cache.bin\0"
                    if "ls-files" in command
                    else b""
                )
                return subprocess.CompletedProcess(
                    command, 0, stdout=stdout, stderr=b""
                )

            assert_prompt_content_clean(
                root, ["skills"], runner=ignored_runner
            )

    def test_tracked_directory_symlink_escape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "repository"
            outside = base / "outside-skill"
            (root / "skills").mkdir(parents=True)
            outside.mkdir()
            (outside / "SKILL.md").write_text("---\nname: outside\n---\n")
            (root / "skills" / "linked").symlink_to(
                outside, target_is_directory=True
            )

            def clean_runner(*args, **kwargs):
                return subprocess.CompletedProcess(
                    args[0], 0, stdout=b"", stderr=b""
                )

            with self.assertRaisesRegex(RuntimeError, "is a symlink"):
                assert_prompt_content_clean(
                    root, ["skills"], runner=clean_runner
                )

    def test_in_repository_directory_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "skills").mkdir()
            target = root / "shared-skill"
            target.mkdir()
            (target / "SKILL.md").write_text("---\nname: shared\n---\n")
            (root / "skills" / "linked").symlink_to(
                target, target_is_directory=True
            )

            with self.assertRaisesRegex(RuntimeError, "is a symlink"):
                assert_prompt_content_clean(
                    root,
                    ["skills"],
                    runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                        args[0], 0, stdout=b"", stderr=b""
                    ),
                )

    def test_in_repository_index_file_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "canonical-skill.md"
            target.write_text("---\nname: shared\n---\n")
            skill = root / "skills" / "linked"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").symlink_to(target)

            with self.assertRaisesRegex(RuntimeError, "is a symlink"):
                assert_prompt_content_clean(
                    root,
                    ["skills"],
                    runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                        args[0], 0, stdout=b"", stderr=b""
                    ),
                )

    def test_non_prompt_support_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "repository"
            skill = root / "skills" / "example"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: example\n---\n")
            outside_assets = base / "assets"
            outside_assets.mkdir()
            (outside_assets / "logo.svg").write_text("<svg/>")
            (skill / "assets").symlink_to(
                outside_assets, target_is_directory=True
            )

            assert_prompt_content_clean(
                root,
                ["skills"],
                runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                    args[0], 0, stdout=b"", stderr=b""
                ),
            )


if __name__ == "__main__":
    unittest.main()
