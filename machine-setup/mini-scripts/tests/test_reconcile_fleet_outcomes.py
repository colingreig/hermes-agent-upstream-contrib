"""Transactional deployment tests for the fleet-outcome reconciler."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "reconcile_fleet_outcomes.py"
SPEC = importlib.util.spec_from_file_location("reconcile_fleet_outcomes", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FleetOutcomeReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.home = self.root / "home"
        self.hermes = self.home / ".hermes"
        self.launch_agents = self.home / "Library" / "LaunchAgents"
        self.state = self.root / "state"
        self.source.mkdir()
        (self.source / "launchd").mkdir()
        self.launch_agents.mkdir(parents=True)
        (self.hermes / "cron").mkdir(parents=True)

        (self.source / "probe.py").write_text("print('new probe')\n", encoding="utf-8")
        (self.source / "launchd" / "probe.plist").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>Label</key><string>com.colingreig.hermes.test-probe</string>
<key>ProgramArguments</key><array><string>/usr/bin/true</string></array>
</dict></plist>
""",
            encoding="utf-8",
        )
        self.manifest = self.source / "fleet_outcome_manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "source": "probe.py",
                            "destination_root": "scripts",
                            "destination": "probe.py",
                            "mode": "0755",
                            "sha256": sha256(self.source / "probe.py"),
                        },
                        {
                            "source": "launchd/probe.plist",
                            "destination_root": "launch_agents",
                            "destination": "probe.plist",
                            "mode": "0644",
                            "sha256": sha256(
                                self.source / "launchd" / "probe.plist"
                            ),
                        },
                    ],
                    "cron_updates": [
                        {
                            "id": "ci",
                            "name": "ci-health-watch",
                            "fields": {"script": "ci-health-watch-cron.py"},
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.hermes / "cron" / "jobs.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "ci",
                            "name": "ci-health-watch",
                            "script": "ci_health_watch.py",
                            "runtime": {"last_run": "preserve"},
                        }
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (self.hermes / "scripts").mkdir()
        (self.hermes / "scripts" / "probe.py").write_text(
            "print('old probe')\n", encoding="utf-8"
        )
        (self.launch_agents / "probe.plist").write_text(
            "<plist version=\"1.0\"><dict/></plist>\n", encoding="utf-8"
        )

    def reconciler(self):
        return module.Reconciler(
            source_root=self.source,
            manifest_path=self.manifest,
            home=self.home,
            hermes_home=self.hermes,
            launch_agents_dir=self.launch_agents,
            state_dir=self.state,
        )

    def test_install_updates_exact_assets_and_cron_field(self) -> None:
        reconciler = self.reconciler()
        with mock.patch.object(reconciler, "_registered", return_value=False):
            receipt = reconciler.install()

        self.assertEqual(
            (self.hermes / "scripts" / "probe.py").read_text(encoding="utf-8"),
            "print('new probe')\n",
        )
        self.assertEqual(
            (self.hermes / "scripts" / "fleet_outcome_manifest.json").read_bytes(),
            self.manifest.read_bytes(),
        )
        job = json.loads(
            (self.hermes / "cron" / "jobs.json").read_text(encoding="utf-8")
        )["jobs"][0]
        self.assertEqual(job["script"], "ci-health-watch-cron.py")
        self.assertEqual(job["runtime"], {"last_run": "preserve"})
        self.assertTrue(receipt.is_file())

    def test_source_hash_drift_fails_before_live_writes(self) -> None:
        before = (self.hermes / "scripts" / "probe.py").read_bytes()
        (self.source / "probe.py").write_text("tampered\n", encoding="utf-8")
        reconciler = self.reconciler()
        with self.assertRaisesRegex(module.ReconcileError, "source hash drift"):
            reconciler.install()
        self.assertEqual((self.hermes / "scripts" / "probe.py").read_bytes(), before)

    def test_verify_failure_restores_files_and_full_jobs_document(self) -> None:
        reconciler = self.reconciler()
        old_script = (self.hermes / "scripts" / "probe.py").read_bytes()
        old_plist = (self.launch_agents / "probe.plist").read_bytes()
        old_jobs = (self.hermes / "cron" / "jobs.json").read_bytes()
        with (
            mock.patch.object(reconciler, "_registered", return_value=False),
            mock.patch.object(
                reconciler,
                "verify",
                side_effect=module.ReconcileError("injected verify failure"),
            ),
            self.assertRaisesRegex(module.ReconcileError, "injected verify failure"),
        ):
            reconciler.install()
        self.assertEqual((self.hermes / "scripts" / "probe.py").read_bytes(), old_script)
        self.assertEqual((self.launch_agents / "probe.plist").read_bytes(), old_plist)
        self.assertEqual((self.hermes / "cron" / "jobs.json").read_bytes(), old_jobs)

    def test_reload_failure_restores_prior_load_state_and_bytes(self) -> None:
        reconciler = self.reconciler()
        old_script = (self.hermes / "scripts" / "probe.py").read_bytes()
        old_jobs = (self.hermes / "cron" / "jobs.json").read_bytes()
        load_calls: list[dict[str, bool]] = []

        def fake_set_loaded(value: dict[str, bool]) -> None:
            load_calls.append(value)
            if len(load_calls) == 1:
                raise module.ReconcileError("bootstrap failed")

        with (
            mock.patch.object(reconciler, "_registered", return_value=False),
            mock.patch.object(reconciler, "_set_loaded", side_effect=fake_set_loaded),
            self.assertRaisesRegex(module.ReconcileError, "bootstrap failed"),
        ):
            reconciler.install(reload=True)
        self.assertEqual(load_calls[-1], {"com.colingreig.hermes.test-probe": False})
        self.assertEqual((self.hermes / "scripts" / "probe.py").read_bytes(), old_script)
        self.assertEqual((self.hermes / "cron" / "jobs.json").read_bytes(), old_jobs)

    def test_explicit_rollback_restores_snapshot(self) -> None:
        reconciler = self.reconciler()
        old_script = (self.hermes / "scripts" / "probe.py").read_bytes()
        with mock.patch.object(reconciler, "_registered", return_value=False):
            reconciler.install()
            reconciler.rollback()
        self.assertEqual((self.hermes / "scripts" / "probe.py").read_bytes(), old_script)

    def test_verify_accepts_user_domain_registration(self) -> None:
        reconciler = self.reconciler()
        user_domain = module.Reconciler._launchd_domains()[1]

        def registered(domain: str, label: str) -> bool:
            return domain == user_domain and label == "com.colingreig.hermes.test-probe"

        with mock.patch.object(reconciler, "_registered", return_value=False):
            reconciler.install()
        with mock.patch.object(reconciler, "_registered", side_effect=registered):
            reconciler.verify(require_loaded=True)

    def test_verify_rejects_duplicate_domain_registration(self) -> None:
        reconciler = self.reconciler()
        with mock.patch.object(reconciler, "_registered", return_value=False):
            reconciler.install()
        with mock.patch.object(reconciler, "_registered", return_value=True):
            with self.assertRaisesRegex(
                module.ReconcileError,
                "duplicate domains",
            ):
                reconciler.verify(require_loaded=True)

    def test_set_loaded_bootstraps_resolved_domain(self) -> None:
        reconciler = self.reconciler()
        user_domain = module.Reconciler._launchd_domains()[1]
        bootstrap_calls: list[tuple[str, str]] = []
        (self.launch_agents / "probe.plist").write_text(
            (self.source / "launchd" / "probe.plist").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        def registered(domain: str, label: str) -> bool:
            return False

        def resolve(label: str) -> str:
            self.assertEqual(label, "com.colingreig.hermes.test-probe")
            return user_domain

        def wait_registered(domain: str, label: str, expected: bool) -> bool:
            if expected:
                bootstrap_calls.append((domain, label))
            return True

        with (
            mock.patch.object(reconciler, "_registered", side_effect=registered),
            mock.patch.object(reconciler, "_resolve_launchd_domain", side_effect=resolve),
            mock.patch.object(reconciler, "_wait_registered", side_effect=wait_registered),
            mock.patch.object(module, "subprocess") as subprocess_module,
        ):
            subprocess_module.run.return_value = mock.Mock(returncode=0)
            reconciler._set_loaded({"com.colingreig.hermes.test-probe": True})

        self.assertEqual(
            bootstrap_calls,
            [(user_domain, "com.colingreig.hermes.test-probe")],
        )
        bootstrap_calls = [
            call.args[0]
            for call in subprocess_module.run.call_args_list
            if call.args[0][:2] == ["launchctl", "bootstrap"]
        ]
        self.assertEqual(len(bootstrap_calls), 1)
        self.assertEqual(bootstrap_calls[0][:3], ["launchctl", "bootstrap", user_domain])
        self.assertEqual(
            Path(bootstrap_calls[0][3]).resolve(),
            (self.launch_agents / "probe.plist").resolve(),
        )


def test_repository_manifest_is_content_addressed() -> None:
    source_root = SCRIPT.parent
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        reconciler = module.Reconciler(
            source_root=source_root,
            manifest_path=source_root / "fleet_outcome_manifest.json",
            home=home,
        )
        reconciler.validate_sources()
        assert len(reconciler.manifest["files"]) == 11
        assert reconciler.manifest["cron_updates"] == [
            {
                "id": "e835c614cfb2",
                "name": "ci-health-watch",
                "fields": {"script": "ci-health-watch-cron.py"},
            }
        ]


if __name__ == "__main__":
    unittest.main()
