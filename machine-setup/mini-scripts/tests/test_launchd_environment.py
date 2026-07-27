#!/usr/bin/env python3
"""Hermetic contracts for canonical Hermes launchd environment deployment."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parent.parent
RECONCILER_SOURCE = SOURCE_ROOT / "reconcile_launchd_environment.py"
SPEC = importlib.util.spec_from_file_location(
    "reconcile_launchd_environment_under_test", RECONCILER_SOURCE
)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(module)


class LaunchdEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.home = self.root / "home"
        self.hermes = self.home / ".hermes"
        self.launch_agents = self.home / "Library" / "LaunchAgents"
        self.state = self.root / "state"
        self.hermes.mkdir(parents=True)
        self.home = self.home.resolve()
        self.hermes = self.home / ".hermes"
        self.launch_agents = self.home / "Library" / "LaunchAgents"
        wrapper = self.hermes / "scripts" / "gateway_secrets_wrap.sh"
        (self.hermes / "config.yaml").write_text(
            f"gateway:\n  launchd_secrets_wrapper: {wrapper}\n",
            encoding="utf-8",
        )
        self.reconciler = module.Reconciler(
            source_root=SOURCE_ROOT,
            home=self.home,
            launch_agents_dir=self.launch_agents,
            state_dir=self.state,
        )

    def _install_fake_runtime(
        self,
        *,
        resolver_status=0,
        mint_status=0,
        token="ghs_fake",
        include_openai=True,
    ):
        runtime = self.hermes / "runtime-current" / "venv" / "bin" / "python"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        openai_line = (
            "      printf '%s\\n' 'OPENAI_API_KEY_HERMES=\"fake-openai\"'\n"
            if include_openai
            else ""
        )
        runtime.write_text(
            "#!/bin/bash\n"
            "case \"${1:-}\" in\n"
            "  */op_sdk_resolve.py)\n"
            f"    if [ {resolver_status} -eq 0 ]; then\n"
            "      printf '%s\\n' 'GH_APP_PRIVATE_KEY=\"fake-private\"' "
            "'GH_APP_ID=\"123\"' 'GH_APP_INSTALLATION_ID=\"456\"'\n"
            f"{openai_line}"
            "    fi\n"
            f"    exit {resolver_status} ;;\n"
            "  */github_app_token.py)\n"
            f"    [ {mint_status} -eq 0 ] && printf '%s\\n' '{token}'\n"
            f"    exit {mint_status} ;;\n"
            "  -m)\n"
            "    printf '%s\\n' \"$*\" > \"$HOME/final-args\"\n"
            "    env | LC_ALL=C sort > \"$HOME/final-env\"\n"
            "    exit 0 ;;\n"
            "  *) exit 90 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        runtime.chmod(0o755)

    def _run_wrapper(self, name: str):
        env = {
            "HOME": str(self.home),
            "HERMES_HOME": str(self.hermes),
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(self.root),
        }
        return subprocess.run(
            ["/bin/bash", str(self.hermes / "scripts" / name)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_atomic_install_plists_and_source_identity(self):
        receipt = self.reconciler.install()
        self.assertTrue(receipt.is_file())
        self.reconciler.verify()

        for name in module.SCRIPT_ASSETS:
            source = SOURCE_ROOT / name
            deployed = self.hermes / "scripts" / name
            self.assertEqual(source.read_bytes(), deployed.read_bytes())
            self.assertTrue(deployed.stat().st_mode & 0o111)

        for label, wrapper_name in (
            (module.GATEWAY_LABEL, "gateway_secrets_wrap.sh"),
            (module.DASHBOARD_LABEL, "dashboard_secrets_wrap.sh"),
        ):
            plist_path = self.launch_agents / f"{label}.plist"
            payload = plistlib.loads(plist_path.read_bytes())
            self.assertEqual(
                payload["ProgramArguments"],
                ["/bin/bash", str(self.hermes / "scripts" / wrapper_name)],
            )
            self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
            serialized = plist_path.read_text(encoding="utf-8")
            for forbidden in (
                "github_app_token.py",
                "GH_TOKEN",
                "OPENAI_API_KEY",
                "VALIDATOR_",
                "op://",
            ):
                self.assertNotIn(forbidden, serialized)

        receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))
        for record in receipt_payload["files"]:
            self.assertEqual(record["source_sha256"], record["deployed_sha256"])

    def test_both_wrappers_mint_nonempty_token_and_gateway_only_exports(self):
        self.reconciler.install()
        self._install_fake_runtime()

        gateway = self._run_wrapper("gateway_secrets_wrap.sh")
        self.assertEqual(gateway.returncode, 0, gateway.stderr)
        gateway_env = (self.home / "final-env").read_text(encoding="utf-8")
        gateway_args = (self.home / "final-args").read_text(encoding="utf-8")
        self.assertIn("GH_TOKEN=ghs_fake\n", gateway_env)
        self.assertIn("OPENAI_API_KEY=fake-openai\n", gateway_env)
        expected_chain = (
            "openai-codex:gpt-5.4,minimax:MiniMax-M3,"
            "gemini:gemini-3.5-flash"
        )
        for tier in ("LOW", "MEDIUM", "HIGH"):
            self.assertIn(f"VALIDATOR_{tier}_CHAIN={expected_chain}\n", gateway_env)
        self.assertEqual(
            gateway_args.strip(),
            "-m hermes_cli.main gateway run --replace",
        )

        dashboard = self._run_wrapper("dashboard_secrets_wrap.sh")
        self.assertEqual(dashboard.returncode, 0, dashboard.stderr)
        dashboard_env = (self.home / "final-env").read_text(encoding="utf-8")
        dashboard_args = (self.home / "final-args").read_text(encoding="utf-8")
        self.assertIn("GH_TOKEN=ghs_fake\n", dashboard_env)
        self.assertNotIn("\nOPENAI_API_KEY=", "\n" + dashboard_env)
        self.assertNotIn("VALIDATOR_LOW_CHAIN=", dashboard_env)
        self.assertEqual(
            dashboard_args.strip(),
            "-m hermes_cli.main dashboard --no-open --host 127.0.0.1 "
            "--port 9119 --skip-build",
        )

    def test_empty_mint_fails_without_launching_service(self):
        self.reconciler.install()
        self._install_fake_runtime(token="")
        result = self._run_wrapper("gateway_secrets_wrap.sh")
        self.assertEqual(result.returncode, 75)
        self.assertFalse((self.home / "final-args").exists())
        log = (self.hermes / "logs" / "gateway.error.log").read_text()
        self.assertIn("classification=token-mint", log)

    def test_permanent_auth_parks_but_transient_failure_remains_retryable(self):
        self.reconciler.install()
        self._install_fake_runtime(resolver_status=77)
        parked = self._run_wrapper("gateway_secrets_wrap.sh")
        self.assertEqual(parked.returncode, 0)
        self.assertFalse((self.home / "final-args").exists())
        self.assertIn(
            "classification=permanent-auth",
            (self.hermes / "logs" / "gateway.error.log").read_text(),
        )

        shutil.rmtree(self.hermes / "runtime-current")
        self._install_fake_runtime(resolver_status=75)
        transient = self._run_wrapper("gateway_secrets_wrap.sh")
        self.assertEqual(transient.returncode, 75)

        shutil.rmtree(self.hermes / "runtime-current")
        self._install_fake_runtime(mint_status=77)
        mint_auth = self._run_wrapper("dashboard_secrets_wrap.sh")
        self.assertEqual(mint_auth.returncode, 0)
        self.assertFalse((self.home / "final-args").exists())
        self.assertIn(
            "classification=permanent-auth",
            (self.hermes / "logs" / "dashboard.error.log").read_text(),
        )

    def test_missing_gateway_openai_credential_parks_without_retry_loop(self):
        self.reconciler.install()
        self._install_fake_runtime(include_openai=False)
        parked = self._run_wrapper("gateway_secrets_wrap.sh")
        self.assertEqual(parked.returncode, 0)
        self.assertFalse((self.home / "final-args").exists())
        self.assertIn(
            "classification=permanent-auth",
            (self.hermes / "logs" / "gateway.error.log").read_text(),
        )

    def test_template_is_references_only_and_contains_no_secret_values(self):
        self.reconciler.install()
        template = (SOURCE_ROOT / module.REFERENCE_SOURCE).read_text()
        deployed = (self.hermes / "scripts" / module.REFERENCE_TARGET).read_text()
        self.assertEqual(template, deployed)
        for line in template.splitlines():
            if line and not line.startswith("#"):
                self.assertTrue(line.split("=", 1)[1].startswith("op://"))
        for fake_secret in ("fake-private", "ghs_fake", "fake-openai"):
            self.assertNotIn(fake_secret, template)
            for plist in self.launch_agents.glob("*.plist"):
                self.assertNotIn(fake_secret, plist.read_text())

    def test_failed_install_restores_exact_previous_files(self):
        target = self.hermes / "scripts" / "gateway_secrets_wrap.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"previous gateway wrapper\n")
        target.chmod(0o700)
        original_atomic_write = module._atomic_write
        writes = 0

        def fail_mid_install(path, data, mode):
            nonlocal writes
            if path in self.reconciler.desired():
                writes += 1
                if writes == 2:
                    raise OSError("injected install failure")
            return original_atomic_write(path, data, mode)

        with mock.patch.object(module, "_atomic_write", side_effect=fail_mid_install):
            with self.assertRaises(OSError):
                self.reconciler.install()
        self.assertEqual(target.read_bytes(), b"previous gateway wrapper\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_explicit_rollback_restores_exact_prior_target_set(self):
        target = self.hermes / "scripts" / "gateway_secrets_wrap.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"pre-contract wrapper\n")
        target.chmod(0o700)
        self.reconciler.install()
        self.assertNotEqual(target.read_bytes(), b"pre-contract wrapper\n")

        self.reconciler.rollback()
        self.assertEqual(target.read_bytes(), b"pre-contract wrapper\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertFalse(
            (self.hermes / "scripts" / "dashboard_secrets_wrap.sh").exists()
        )
        self.assertFalse(
            (self.launch_agents / f"{module.GATEWAY_LABEL}.plist").exists()
        )

    def test_reload_failure_restores_snapshot_and_reloads_previous_jobs(self):
        target = self.hermes / "scripts" / "gateway_secrets_wrap.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"previous wrapper\n")
        target.chmod(0o700)
        with mock.patch.object(
            self.reconciler,
            "reload",
            side_effect=(OSError("bootstrap failed"), None),
        ) as reload_mock:
            with self.assertRaises(OSError):
                self.reconciler.install_and_reload()
        self.assertEqual(reload_mock.call_count, 2)
        self.assertEqual(target.read_bytes(), b"previous wrapper\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertFalse(
            (self.launch_agents / f"{module.GATEWAY_LABEL}.plist").exists()
        )

    def test_bootstrap_recovers_eio_and_verifies_registration(self):
        calls = []
        bootstrap_count = 0

        def fake_run(args, **kwargs):
            nonlocal bootstrap_count
            calls.append(tuple(args))
            if args[1] == "bootstrap":
                bootstrap_count += 1
                if bootstrap_count == 1:
                    raise subprocess.CalledProcessError(5, args)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            self.reconciler._bootstrap_until_registered(
                "gui/501",
                module.GATEWAY_LABEL,
                self.reconciler.gateway_plist,
            )

        self.assertEqual(
            [call[1] for call in calls],
            ["bootstrap", "bootout", "bootstrap", "print"],
        )
        self.assertEqual(calls[-1][-1], f"gui/501/{module.GATEWAY_LABEL}")

    def test_bootstrap_registration_failure_is_bounded(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(tuple(args))
            return subprocess.CompletedProcess(
                args,
                1 if args[1] == "print" else 0,
            )

        with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "after 3 bounded"):
                self.reconciler._bootstrap_until_registered(
                    "gui/501",
                    module.GATEWAY_LABEL,
                    self.reconciler.gateway_plist,
                )
        self.assertEqual(sum(call[1] == "bootstrap" for call in calls), 3)
        self.assertEqual(sum(call[1] == "print" for call in calls), 3)
        self.assertEqual(sum(call[1] == "bootout" for call in calls), 2)

    def test_reload_verifies_gateway_and_dashboard_registration(self):
        self.reconciler.install()
        calls = []

        def fake_run(args, **kwargs):
            calls.append(tuple(args))
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            self.reconciler.reload()
        print_targets = [call[-1] for call in calls if call[1] == "print"]
        self.assertEqual(
            print_targets,
            [
                f"gui/{os.getuid()}/{module.GATEWAY_LABEL}",
                f"gui/{os.getuid()}/{module.DASHBOARD_LABEL}",
            ],
        )

    def test_registration_failure_restores_snapshot_before_returning_error(self):
        target = self.hermes / "scripts" / "gateway_secrets_wrap.sh"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"previous registered generation\n")
        target.chmod(0o700)

        def never_registered(args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                1 if args[1] == "print" else 0,
            )

        with mock.patch.object(
            module.subprocess,
            "run",
            side_effect=never_registered,
        ):
            with self.assertRaisesRegex(RuntimeError, "failed to register"):
                self.reconciler.install_and_reload()
        self.assertEqual(target.read_bytes(), b"previous registered generation\n")
        self.assertFalse(self.reconciler.gateway_plist.exists())
        self.assertFalse(self.reconciler.dashboard_plist.exists())

    def test_generic_gateway_regeneration_preserves_configured_wrapper(self):
        self.reconciler.install()
        import hermes_cli.gateway as gateway

        with (
            mock.patch.object(
                gateway,
                "read_raw_config",
                return_value={
                    "gateway": {
                        "launchd_secrets_wrapper": str(
                            self.hermes / "scripts" / "gateway_secrets_wrap.sh"
                        )
                    }
                },
            ),
            mock.patch.object(gateway, "get_hermes_home", return_value=self.hermes),
            mock.patch.object(gateway, "get_python_path", return_value="/runtime/python"),
            mock.patch.object(
                gateway,
                "_stable_service_working_dir",
                return_value=str(self.hermes / "runtime-current"),
            ),
            mock.patch.object(gateway, "_detect_venv_dir", return_value=None),
            mock.patch.object(gateway, "_build_service_path_dirs", return_value=[]),
            mock.patch.object(gateway.shutil, "which", return_value=None),
        ):
            generated = gateway.generate_launchd_plist()
        self.assertIn(str(self.hermes / "scripts" / "gateway_secrets_wrap.sh"), generated)
        self.assertNotIn("github_app_token.py", generated)
        self.assertNotIn("GH_TOKEN", generated)
        parsed = plistlib.loads(generated.encode("utf-8"))
        self.assertEqual(parsed["KeepAlive"], {"SuccessfulExit": False})

    def test_wrapper_syntax(self):
        for name in (
            "gateway_secrets_wrap.sh",
            "dashboard_secrets_wrap.sh",
            "gateway_launch_inner.sh",
        ):
            result = subprocess.run(
                ["/bin/bash", "-n", str(SOURCE_ROOT / name)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
