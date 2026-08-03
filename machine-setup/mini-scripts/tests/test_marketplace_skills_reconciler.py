"""Focused contracts for the governed external-skill reconciler."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
import warnings

import yaml

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from reconcile_marketplace_skills import (  # noqa: E402
    ANTHROPIC_LABEL,
    IGNITE_LABEL,
    INDEX_FLOOR,
    Reconciler,
    default_source_root,
)
from record_skill_pull_success import (  # noqa: E402
    validate_success_evidence,
    write_generation_state,
    write_success_evidence,
)


class MarketplaceSkillsReconcilerTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Reconciler, Path, Path, tuple[str, ...]]:
        root = root.resolve()
        home = root / "home"
        hermes = home / ".hermes"
        launch_agents = home / "Library" / "LaunchAgents"
        source = root / "source"
        (source / "launchd").mkdir(parents=True)
        launch_agents.mkdir(parents=True)
        (hermes / "scripts").mkdir(parents=True)
        for name in (
            "ignite-skills-pull.sh",
            "pull_anthropic_agent_skills.sh",
            "record_skill_pull_success.py",
            "skill_pull_guard.py",
            "reconcile_marketplace_skills.py",
        ):
            (source / name).write_bytes((SCRIPTS / name).read_bytes())
        wrappers = {
            IGNITE_LABEL: "ignite-skills-pull.sh",
            ANTHROPIC_LABEL: "pull_anthropic_agent_skills.sh",
        }
        cadences = {IGNITE_LABEL: 10800, ANTHROPIC_LABEL: 86400}
        for label, wrapper in wrappers.items():
            payload = {
                "Label": label,
                "ProgramArguments": ["/bin/bash", str(hermes / "scripts" / wrapper)],
                "RunAtLoad": True,
                "StartInterval": cadences[label],
            }
            (source / "launchd" / f"{label}.plist").write_bytes(
                plistlib.dumps(payload)
            )
        roots = tuple(str(root / "roots" / name) for name in ("ops", "content", "blog"))
        for raw in roots:
            Path(raw).mkdir(parents=True)
        config = {
            "model": {"default": "fixture"},
            "skills": {
                "external_dirs": ["/unsupported/ignite-code/skills"],
                "disabled": ["keep-me"],
            },
        }
        (hermes / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        stale = launch_agents / "com.ignite.skills-sync.plist"
        stale.write_text("legacy\n", encoding="utf-8")
        reconciler = Reconciler(
            source_root=source,
            home=home,
            hermes_home=hermes,
            launch_agents_dir=launch_agents,
            state_dir=hermes / "releases" / "marketplace-skills",
            external_dirs=roots,
        )
        return reconciler, hermes, launch_agents, roots

    def test_install_reconciles_exact_config_retires_stale_and_hashes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, hermes, launch_agents, roots = self._fixture(Path(tmp))
            stale = launch_agents / "com.ignite.skills-sync.plist"

            receipt = reconciler.install()

            config = yaml.safe_load((hermes / "config.yaml").read_text())
            self.assertEqual(config["skills"]["external_dirs"], list(roots))
            self.assertEqual(config["skills"]["index_floor"], INDEX_FLOOR)
            self.assertEqual(config["skills"]["disabled"], ["keep-me"])
            self.assertEqual(config["model"], {"default": "fixture"})
            self.assertFalse(stale.exists())
            payload = json.loads(receipt.read_text())
            for record in payload["files"]:
                target = Path(record["target"])
                self.assertEqual(
                    record["deployed_sha256"],
                    hashlib.sha256(target.read_bytes()).hexdigest(),
                )
                self.assertEqual(record["source_sha256"], record["deployed_sha256"])
            reconciler.verify()

    def test_rollback_restores_config_stale_plist_and_prior_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, hermes, launch_agents, _ = self._fixture(Path(tmp))
            original_config = (hermes / "config.yaml").read_bytes()
            prior_receipt = reconciler.state_dir / "last-receipt.json"
            prior_receipt.parent.mkdir(parents=True)
            prior_receipt.write_text('{"old":true}\n', encoding="utf-8")

            reconciler.install()
            reconciler.rollback()

            self.assertEqual((hermes / "config.yaml").read_bytes(), original_config)
            self.assertEqual(prior_receipt.read_text(), '{"old":true}\n')
            self.assertEqual(
                (launch_agents / "com.ignite.skills-sync.plist").read_text(),
                "legacy\n",
            )

    def test_missing_and_escaped_roots_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reconciler, _, _, roots = self._fixture(root)
            Path(roots[0]).rmdir()
            with self.assertRaisesRegex(RuntimeError, "root missing"):
                reconciler.install()

            Path(roots[0]).symlink_to(Path(roots[1]), target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "escapes canonical"):
                reconciler.install()

    def test_manifest_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, hermes, _, _ = self._fixture(Path(tmp))
            reconciler.install()
            (hermes / "scripts" / "ignite-skills-pull.sh").write_text("tampered\n")

            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                reconciler.verify()

    def test_malformed_plist_is_skipped_with_warning_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, _, _, _ = self._fixture(Path(tmp))
            malformed = reconciler.source_root / "launchd" / f"{IGNITE_LABEL}.plist"
            malformed.write_bytes(
                b'<?xml version="1.0"?><plist><dict><!-- broken -- ></dict></plist>'
            )

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                reconciler.validate_sources()

            warning = next(
                item for item in caught if "skipped unreadable plist" in str(item.message)
            )
            self.assertIn(malformed.name, str(warning.message))
            self.assertIn("not well-formed", str(warning.message))

    def test_deployed_script_uses_runtime_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / ".hermes"
            deployed = hermes / "scripts" / "reconcile_marketplace_skills.py"
            deployed.parent.mkdir(parents=True)
            deployed.touch()
            release = hermes / "releases" / "v1-fixture"
            physical_source = release / "machine-setup" / "mini-scripts"
            physical_source.mkdir(parents=True)
            (hermes / "runtime-current").symlink_to(release, target_is_directory=True)
            runtime = hermes / "runtime-current" / "machine-setup" / "mini-scripts"

            self.assertEqual(
                default_source_root(script_path=deployed, hermes_home=hermes),
                runtime,
            )
            reconciler = Reconciler(
                source_root=runtime,
                home=hermes.parent,
                hermes_home=hermes,
                external_dirs=(),
            )
            self.assertEqual(reconciler.source_root, physical_source.resolve())

    def test_omitted_deployed_source_root_fails_closed_when_absent_or_symlinked(self):
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / ".hermes"
            deployed = hermes / "scripts" / "reconcile_marketplace_skills.py"
            deployed.parent.mkdir(parents=True)
            deployed.touch()

            with self.assertRaisesRegex(RuntimeError, "missing or symlinked"):
                default_source_root(script_path=deployed, hermes_home=hermes)

            candidate = hermes / "runtime-current" / "machine-setup" / "mini-scripts"
            candidate.parent.mkdir(parents=True)
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            candidate.symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "missing or symlinked"):
                default_source_root(script_path=deployed, hermes_home=hermes)

    def test_non_deployed_script_uses_its_own_source_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "checkout" / "mini-scripts" / "reconcile_marketplace_skills.py"
            script.parent.mkdir(parents=True)
            script.touch()

            self.assertEqual(
                default_source_root(script_path=script, hermes_home=root / ".hermes"),
                script.parent,
            )

    def test_source_files_must_resolve_inside_terminal_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, _, _, _ = self._fixture(Path(tmp))
            launchd = reconciler.source_root / "launchd"
            outside = Path(tmp) / "outside-launchd"
            outside.mkdir()
            for source in tuple(launchd.iterdir()):
                (outside / source.name).write_bytes(source.read_bytes())
                source.unlink()
            launchd.rmdir()
            launchd.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(RuntimeError, "outside source root"):
                reconciler.validate_sources()

    def test_deployed_reconciler_tamper_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, hermes, _, _ = self._fixture(Path(tmp))
            reconciler.install()
            deployed = hermes / "scripts" / "reconcile_marketplace_skills.py"
            deployed.write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                reconciler.verify()

            deployed.unlink()
            deployed.symlink_to(reconciler.source_path("reconcile_marketplace_skills.py"))
            with self.assertRaisesRegex(RuntimeError, "missing or symlinked"):
                reconciler.verify()

    def test_verify_allows_unrelated_config_but_rejects_owned_field_mode_and_receipt_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, hermes, _, _ = self._fixture(Path(tmp))
            reconciler.install()
            config_path = hermes / "config.yaml"
            config = yaml.safe_load(config_path.read_text())
            config["model"]["fallback"] = "fixture-fallback"
            config["skills"]["disabled"].append("also-keep-me")
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            os.chmod(config_path, 0o600)
            reconciler.verify()

            config["skills"]["external_dirs"] = []
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            os.chmod(config_path, 0o600)
            with self.assertRaisesRegex(RuntimeError, "external_dirs mismatch"):
                reconciler.verify()

            config["skills"]["external_dirs"] = list(reconciler.external_dirs)
            config["skills"]["index_floor"] = INDEX_FLOOR - 1
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            os.chmod(config_path, 0o600)
            with self.assertRaisesRegex(RuntimeError, "index_floor mismatch"):
                reconciler.verify()

            config["skills"]["index_floor"] = INDEX_FLOOR
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            os.chmod(config_path, 0o644)
            with self.assertRaisesRegex(RuntimeError, "mode mismatch"):
                reconciler.verify()

            os.chmod(config_path, 0o600)
            receipt = reconciler.state_dir / "last-receipt.json"
            payload = json.loads(receipt.read_text())
            script_record = next(
                item for item in payload["files"]
                if item["source"] != "generated-config"
            )
            script_record["deployed_sha256"] = "0" * 64
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "receipt deployed hash mismatch"):
                reconciler.verify()

    def test_generated_config_receipt_hashes_are_exempt_but_semantics_and_mode_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, hermes, _, _ = self._fixture(Path(tmp))
            reconciler.install()
            config_path = hermes / "config.yaml"
            config = yaml.safe_load(config_path.read_text())
            config["unrelated"] = {"survives": True}
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            os.chmod(config_path, 0o600)

            receipt = reconciler.state_dir / "last-receipt.json"
            payload = json.loads(receipt.read_text())
            generated = next(
                item for item in payload["files"]
                if item["source"] == "generated-config"
            )
            generated["source_sha256"] = "0" * 64
            generated["deployed_sha256"] = "f" * 64
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            reconciler.verify()

            generated["mode"] = "0644"
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "receipt mode mismatch"):
                reconciler.verify()

    def test_receipt_mode_source_hash_malformed_and_duplicate_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, _, _, _ = self._fixture(Path(tmp))
            reconciler.install()
            receipt = reconciler.state_dir / "last-receipt.json"
            original = receipt.read_bytes()

            os.chmod(receipt, 0o600)
            with self.assertRaisesRegex(RuntimeError, "receipt mode mismatch"):
                reconciler.verify()
            os.chmod(receipt, 0o644)

            payload = json.loads(original)
            vendored = next(
                item for item in payload["files"]
                if item["source"] != "generated-config"
            )
            vendored["source_sha256"] = "0" * 64
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "receipt source hash mismatch"):
                reconciler.verify()

            malformed = {"schema_version": 1, "files": ["not-a-record"]}
            receipt.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "receipt malformed"):
                reconciler.verify()

            payload = json.loads(original)
            payload["files"].append(dict(payload["files"][0]))
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "duplicate receipt entry"):
                reconciler.verify()

    def test_success_evidence_is_parseable_explicit_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            root = root.resolve()
            evidence = Path(tmp) / "state" / "success.json"
            generation = Path(tmp) / "state" / "catalog-generation.json"

            write_success_evidence(
                evidence,
                source="fixture",
                root=root,
                commit="a" * 40,
                generation_target=generation,
                changed_from="b" * 40,
            )

            payload = json.loads(evidence.read_text())
            parsed = datetime.fromisoformat(
                payload["completed_at"].removesuffix("Z") + "+00:00"
            )
            self.assertEqual(parsed.tzinfo, timezone.utc)
            self.assertEqual(payload["root"], str(root))
            generation_payload = json.loads(generation.read_text())
            self.assertEqual(generation_payload["commit"], "a" * 40)
            self.assertEqual(generation_payload["changed_from"], "b" * 40)
            validated = validate_success_evidence(
                evidence,
                source="fixture",
                root=root,
                max_age=timedelta(hours=1),
                now=parsed + timedelta(minutes=30),
            )
            self.assertEqual(validated["commit"], "a" * 40)

    def test_generation_state_stays_non_stable_until_owned_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "repo").resolve()
            root.mkdir()
            evidence = Path(tmp) / "state" / "success.json"
            generation = Path(tmp) / "state" / "catalog-generation.json"
            operation_id = "operation-1"

            write_generation_state(
                generation,
                source="fixture",
                state="updating",
                operation_id=operation_id,
            )
            self.assertEqual(json.loads(generation.read_text())["state"], "updating")

            write_success_evidence(
                evidence,
                source="fixture",
                root=root,
                commit="a" * 40,
                generation_target=generation,
                changed_from="b" * 40,
                operation_id=operation_id,
            )
            stable = json.loads(generation.read_text())
            self.assertEqual(stable["state"], "stable")
            self.assertEqual(stable["operation_id"], operation_id)

            write_generation_state(
                generation,
                source="fixture",
                state="updating",
                operation_id="operation-2",
            )
            write_generation_state(
                generation,
                source="fixture",
                state="failed",
                operation_id="operation-2",
            )
            failed = json.loads(generation.read_text())
            self.assertEqual(failed["state"], "failed")
            self.assertEqual(
                failed["previous_generation"], stable["generation"]
            )

    def test_pull_wrappers_publish_updating_before_git_mutation(self):
        for wrapper_name in (
            "ignite-skills-pull.sh",
            "pull_anthropic_agent_skills.sh",
        ):
            with self.subTest(wrapper=wrapper_name):
                wrapper = (SCRIPTS / wrapper_name).read_text(encoding="utf-8")
                updating = wrapper.index("--generation-state updating")
                fetch = wrapper.index('git -C "$ROOT" fetch')
                merge = wrapper.index('git -C "$ROOT" merge')
                self.assertLess(updating, fetch)
                self.assertLess(updating, merge)
                self.assertIn("--generation-state failed", wrapper)
                self.assertIn("catalog-update.lock", wrapper)

    def test_stale_or_future_success_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "repo").resolve()
            root.mkdir()
            evidence = Path(tmp) / "success.json"
            write_success_evidence(
                evidence, source="fixture", root=root, commit="b" * 40
            )
            completed = datetime.fromisoformat(
                json.loads(evidence.read_text())["completed_at"][:-1] + "+00:00"
            )

            with self.assertRaisesRegex(ValueError, "freshness budget"):
                validate_success_evidence(
                    evidence,
                    source="fixture",
                    root=root,
                    max_age=timedelta(hours=1),
                    now=completed + timedelta(hours=1),
                )
            with self.assertRaisesRegex(ValueError, "freshness budget"):
                validate_success_evidence(
                    evidence,
                    source="fixture",
                    root=root,
                    max_age=timedelta(hours=1),
                    now=completed - timedelta(seconds=1),
                )


if __name__ == "__main__":
    unittest.main()
