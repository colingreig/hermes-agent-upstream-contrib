from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS / "pr_pipeline" / "ci_health_watch.py"
TOPOLOGY = SCRIPTS / "pr_pipeline" / "ci_health_topology.json"
_COUNTER = 0
BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"
BOOT_C = "33333333-3333-4333-8333-333333333333"


def _load_module():
    global _COUNTER
    _COUNTER += 1
    spec = importlib.util.spec_from_file_location(f"ci_health_watch_ut_{_COUNTER}", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self):
        self.boot_id = BOOT_A
        self.vm_state = "running"
        self.runner_statuses = {
            "colingreig/jdmbuysell-v4": "online",
            "colingreig/topdynamicspartners": "online",
            "colingreig/elevatoruptime.com": "online",
        }
        self.runs: list[dict[str, object]] = []
        self.calls: list[list[str]] = []
        self.sent: list[str] = []
        self.reruns: list[str] = []
        self.orb_list_stdout: str | None = None
        self.boot_rc = 0
        self.gh_runner_api_forbidden = False
        self.listener_commands = {
            "colingreig/jdmbuysell-v4": "/home/colingreig/actions-runner/jdmbuysell-v4/bin/Runner.Listener run",
            "colingreig/topdynamicspartners": "/home/colingreig/actions-runner/topdynamicspartners/bin/Runner.Listener run",
            "colingreig/elevatoruptime.com": "/home/colingreig/actions-runner/elevatoruptime.com/bin/Runner.Listener run",
        }

    def __call__(self, cmd, **_kwargs):
        cmd = [str(part) for part in cmd]
        self.calls.append(cmd)
        if cmd[:2] == ["orb", "list"]:
            if self.orb_list_stdout is not None:
                return subprocess.CompletedProcess(cmd, 0, self.orb_list_stdout, "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"vms": [{"name": "hermes-ci", "state": self.vm_state}]}), "")
        if cmd[:5] == ["orb", "exec", "-m", "hermes-ci", "cat"]:
            return subprocess.CompletedProcess(cmd, self.boot_rc, self.boot_id + "\n" if self.boot_rc == 0 else "", "boot failed")
        if cmd[:5] == ["orb", "exec", "-m", "hermes-ci", "cut"]:
            return subprocess.CompletedProcess(cmd, 0, "120\n", "")
        if cmd[:6] == ["orb", "exec", "-m", "hermes-ci", "ps", "-eo"]:
            stdout = "\n".join(self.listener_commands.values()) + "\n"
            return subprocess.CompletedProcess(cmd, 0, stdout, "")
        if self.gh_runner_api_forbidden and cmd[:2] == ["gh", "api"]:
            return subprocess.CompletedProcess(cmd, 403, "", "HTTP 403: Resource not accessible by integration")
        if cmd[:3] == ["gh", "api", "repos/colingreig/jdmbuysell-v4/actions/runners"]:
            payload = {"runners": [{"name": "hermes-jdmbuysell-v4", "status": self.runner_statuses["colingreig/jdmbuysell-v4"], "busy": False}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[:3] == ["gh", "api", "repos/colingreig/topdynamicspartners/actions/runners"]:
            payload = {"runners": [{"name": "hermes-topdynamicspartners", "status": self.runner_statuses["colingreig/topdynamicspartners"], "busy": False}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[:3] == ["gh", "api", "repos/colingreig/elevatoruptime.com/actions/runners"]:
            payload = {"runners": [{"name": "hermes-elevatoruptime.com", "status": self.runner_statuses["colingreig/elevatoruptime.com"], "busy": False}]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        if cmd[:4] == ["gh", "run", "list", "--repo"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.runs), "")
        if cmd[:3] == ["gh", "run", "rerun"]:
            self.reruns.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "hermes" in Path(cmd[0]).name and cmd[1:4] == ["send", "--to", "slack:hermes"]:
            self.sent.append(cmd[4])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["orb", "restart"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected command: {cmd}")


class CiHealthWatchTests(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_path = root / "state.json"
        self.evidence_path = root / "lifecycle.jsonl"
        self.intent_path = root / "intent.jsonl"
        self.mod.EVIDENCE_LOG_PATH = self.evidence_path
        self.mod.INTENT_LOG_PATH = self.intent_path
        self.mod.STATE_PATH = self.state_path
        self.mod.HERMES_BIN = root / "hermes"
        self.mod._repos = lambda: []
        self.runner = FakeRunner()

    def _poll(self):
        return self.mod.poll_once(runner=self.runner, topology_path=TOPOLOGY, state_path=self.state_path)

    def _evidence(self):
        return [json.loads(line) for line in self.evidence_path.read_text(encoding="utf-8").splitlines()]

    def test_topology_is_fail_closed_and_declares_five_minute_cadence(self):
        topology = self.mod._load_topology(TOPOLOGY)

        self.assertEqual(topology["no_agent_interval_seconds"], 300)
        self.assertEqual(
            {(item["repo"], item["name"]) for item in topology["expected_runners"]},
            {
                ("colingreig/jdmbuysell-v4", "hermes-jdmbuysell-v4"),
                ("colingreig/topdynamicspartners", "hermes-topdynamicspartners"),
                ("colingreig/elevatoruptime.com", "hermes-elevatoruptime.com"),
            },
        )
        self.assertEqual(
            topology["expected_runners"][0]["listener_command"],
            ["/home/colingreig/actions-runner/jdmbuysell-v4/bin/Runner.Listener", "run"],
        )
        self.assertEqual(topology["recovery_allowlist"][0]["repo"], "colingreig/jdmbuysell-v4")
        self.assertEqual(topology["recovery_allowlist"][0]["workflow"], "Dead-image monitor")

    def test_topology_is_manifest_managed(self):
        manifest = json.loads((SCRIPTS / "pr_pipeline" / "manifest.json").read_text(encoding="utf-8"))

        self.assertIn("ci_health_topology.json", manifest["legacy_flat_entrypoints"])
        self.assertIn("ci_health_topology.json", manifest["managed_root_patterns"])

    def test_boot_uuid_validation_requires_canonical_kernel_form(self):
        self.assertEqual(self.mod._canonical_boot_uuid(BOOT_A), BOOT_A)
        self.assertEqual(self.mod._canonical_boot_uuid(BOOT_A.upper()), BOOT_A)
        self.assertIsNone(self.mod._canonical_boot_uuid("boot-a"))
        self.assertIsNone(self.mod._canonical_boot_uuid(BOOT_A.replace("-", "")))
        self.assertIsNone(self.mod._canonical_boot_uuid(f"{{{BOOT_A}}}"))

    def test_persisted_lifecycle_state_is_versioned_and_records_truth_fields(self):
        report = self._poll()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        lifecycle = state["lifecycle"]
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(lifecycle["schema_version"], 1)
        self.assertEqual(lifecycle["canonical_boot_uuid"], BOOT_A)
        self.assertEqual(lifecycle["classification"], "baseline")
        self.assertIsNotNone(self.mod._parse_time(lifecycle["observed_at"]))
        self.assertRegex(lifecycle["probe_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(report["health"], "OK")
        self.assertEqual(report["classification"], "baseline")
        self.assertEqual(report["probe_fingerprint"], lifecycle["probe_fingerprint"])

    def test_schema_less_valid_state_migrates_after_trustworthy_poll(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "vm": {
                        "available": True,
                        "boot_id": BOOT_A,
                        "uptime_seconds": 60,
                    }
                }
            ),
            encoding="utf-8",
        )

        report = self._poll()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["lifecycle"]["classification"], "stable")
        self.assertEqual(state["lifecycle"]["canonical_boot_uuid"], BOOT_A)
        self.assertEqual(report["classification"], "stable")

    def test_invalid_current_boot_id_preserves_state_and_emits_unknown(self):
        self._poll()
        before = self.state_path.read_text(encoding="utf-8")
        transition_count = len(
            [
                record
                for record in self._evidence()
                if record["event"] == "hermes-ci-lifecycle-transition"
            ]
        )
        self.runner.boot_id = "boot-a"

        report = self._poll()

        self.assertEqual(report["health"], "UNKNOWN")
        self.assertEqual(report["classification"], "unknown")
        self.assertEqual(report["reason"], "current-boot-id-invalid")
        self.assertTrue(report["state_preserved"])
        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        records = self._evidence()
        self.assertEqual(records[-1]["event"], "hermes-ci-lifecycle-health")
        self.assertEqual(records[-1]["health"], "UNKNOWN")
        self.assertEqual(
            len(
                [
                    record
                    for record in records
                    if record["event"] == "hermes-ci-lifecycle-transition"
                ]
            ),
            transition_count,
        )

    def test_invalid_stored_boot_id_uses_guarded_baseline_reset(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "vm": {
                        "available": True,
                        "boot_id": "boot-a",
                        "uptime_seconds": 60,
                    },
                    "last_transition": {
                        "recovery_eligible": True,
                        "interruption_started_at": self.mod._now_iso(),
                        "interruption_ended_at": self.mod._now_iso(),
                    },
                }
            ),
            encoding="utf-8",
        )
        self.runner.runs = [
            {
                "databaseId": 606,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "schedule",
                "headBranch": "main",
            }
        ]

        report = self._poll()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        reset = state["last_baseline_reset"]
        self.assertEqual(report["health"], "DEGRADED")
        self.assertEqual(report["classification"], "baseline-reset")
        self.assertTrue(
            any("baseline repaired (DEGRADED)" in msg for msg in self.runner.sent)
        )
        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, [])
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(state["vm"]["boot_id"], BOOT_A)
        self.assertEqual(state["lifecycle"]["classification"], "baseline-reset")
        self.assertEqual(reset["event"], "hermes-ci-lifecycle-baseline-reset")
        self.assertEqual(reset["prior_boot_id"], "boot-a")
        self.assertEqual(reset["canonical_boot_uuid"], BOOT_A)
        self.assertFalse(reset["recovery_eligible"])
        self.assertNotIn("last_transition", state)
        self.assertFalse(
            any(
                record["event"] == "hermes-ci-lifecycle-transition"
                for record in self._evidence()
            )
        )

    def test_invalid_stored_boot_baseline_reset_is_blocked_by_managed_intent(self):
        stored = {
            "vm": {"available": True, "boot_id": "boot-a", "uptime_seconds": 60},
            "last_managed_intent": {
                "timestamp": self.mod._now_iso(),
                "actor": "operator@example.com",
                "action": "restart",
                "reason": "managed restart",
                "status": "succeeded",
            },
        }
        before = json.dumps(stored, sort_keys=True)
        self.state_path.write_text(before, encoding="utf-8")

        report = self._poll()

        self.assertEqual(report["health"], "UNKNOWN")
        self.assertEqual(report["reason"], "baseline-reset-blocked")
        self.assertIn("managed-intent-active", report["blockers"])
        self.assertTrue(report["state_preserved"])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.runner.reruns, [])

    def test_invalid_stored_boot_baseline_reset_is_blocked_by_active_outage(self):
        stored = {
            "vm": {"available": True, "boot_id": "boot-a", "uptime_seconds": 60},
            "vm_outage": {
                "started_at": self.mod._now_iso(),
                "prior_boot_id": "boot-a",
            },
        }
        before = json.dumps(stored, sort_keys=True)
        self.state_path.write_text(before, encoding="utf-8")

        report = self._poll()

        self.assertEqual(report["health"], "UNKNOWN")
        self.assertEqual(report["reason"], "baseline-reset-blocked")
        self.assertIn("outage-active", report["blockers"])
        self.assertTrue(report["state_preserved"])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.runner.reruns, [])

    def test_corrupt_versioned_lifecycle_state_fails_closed(self):
        self._poll()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["lifecycle"]["probe_fingerprint"] = "sha256:not-a-digest"
        before = json.dumps(state, sort_keys=True)
        self.state_path.write_text(before, encoding="utf-8")

        with self.assertRaisesRegex(self.mod.MonitorError, "probe_fingerprint is invalid"):
            self._poll()

        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.runner.reruns, [])

    def test_unsupported_state_schema_fails_closed(self):
        before = json.dumps(
            {
                "schema_version": 99,
                "vm": {"available": True, "boot_id": BOOT_A},
            },
            sort_keys=True,
        )
        self.state_path.write_text(before, encoding="utf-8")

        with self.assertRaisesRegex(self.mod.MonitorError, "unsupported schema_version=99"):
            self._poll()

        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.runner.calls, [])

    def test_mismatched_versioned_lifecycle_boot_ids_fail_closed(self):
        self._poll()
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state["lifecycle"]["canonical_boot_uuid"] = BOOT_B
        before = json.dumps(state, sort_keys=True)
        self.state_path.write_text(before, encoding="utf-8")

        with self.assertRaisesRegex(self.mod.MonitorError, "boot IDs disagree"):
            self._poll()

        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.runner.reruns, [])

    def test_boot_id_transition_logging_and_unknown_initiator(self):
        self._poll()
        self.runner.boot_id = BOOT_B

        self._poll()

        records = self._evidence()
        self.assertEqual(records[-1]["prior_boot_id"], BOOT_A)
        self.assertEqual(records[-1]["current_boot_id"], BOOT_B)
        self.assertEqual(records[-1]["host_uptime_seconds"], 120)
        self.assertIn("colingreig/jdmbuysell-v4::hermes-jdmbuysell-v4=online", records[-1]["runner_status"])
        self.assertIn("orbstack_evidence", records[-1])
        self.assertEqual(records[-1]["initiator"], "unknown")

    def test_managed_lifecycle_records_actor_action_reason_before_acting(self):
        rc = self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )

        self.assertEqual(rc, 0)
        intent = json.loads(self.intent_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(intent["actor"], "operator@example.com")
        self.assertEqual(intent["action"], "restart")
        self.assertEqual(intent["reason"], "controlled idle-window verification")
        self.assertEqual(self.runner.calls[-1], ["orb", "restart", "hermes-ci"])

        self._poll()
        self.runner.boot_id = BOOT_B
        self._poll()
        self.assertEqual(self._evidence()[-1]["initiator"], "operator@example.com")
        record = self._evidence()[-1]
        self.assertEqual(record["interruption_started_at"], record["managed_intent_timestamp"])
        self.assertEqual(record["interruption_ended_at"], record["timestamp"])

        self.runner.boot_id = BOOT_C
        self._poll()
        self.assertEqual(self._evidence()[-1]["initiator"], "unknown")
        self.assertNotIn("interruption_started_at", self._evidence()[-1])

    def test_failed_managed_action_is_terminal_and_cannot_authorize_transition(self):
        def failed_runner(cmd, **kwargs):
            if list(cmd)[:2] == ["orb", "restart"]:
                return subprocess.CompletedProcess(cmd, 17, "", "restart failed")
            return self.runner(cmd, **kwargs)

        rc = self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled restart",
            runner=failed_runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )

        self.assertEqual(rc, 17)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        intent = state["last_managed_intent"]
        self.assertEqual(intent["status"], "failed")
        self.assertEqual(intent["command_returncode"], 17)
        self.assertIn("failed_at", intent)
        self.assertIsNone(self.mod._latest_managed_intent(state))

        self._poll()
        self.runner.boot_id = BOOT_B
        self._poll()
        self.assertEqual(self._evidence()[-1]["initiator"], "unknown")

    def test_poll_during_pending_managed_command_cannot_attribute_or_authorize_recovery(
        self,
    ):
        self._poll()
        command_started = threading.Event()
        release_command = threading.Event()
        outcomes: list[int] = []
        failures: list[BaseException] = []

        def blocking_runner(cmd, **kwargs):
            if list(cmd)[:2] == ["orb", "restart"]:
                command_started.set()
                if not release_command.wait(timeout=5):
                    raise TimeoutError("test did not release managed command")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return self.runner(cmd, **kwargs)

        def invoke_lifecycle():
            try:
                outcomes.append(
                    self.mod.record_managed_lifecycle(
                        "restart",
                        "operator@example.com",
                        "concurrent restart",
                        runner=blocking_runner,
                        topology_path=TOPOLOGY,
                        state_path=self.state_path,
                    )
                )
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=invoke_lifecycle, daemon=True)
        thread.start()
        self.assertTrue(command_started.wait(timeout=5))
        try:
            pending = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["last_managed_intent"]["status"], "pending")
            self.assertIsNone(self.mod._latest_managed_intent(pending))

            self.runner.boot_id = BOOT_B
            report = self._poll()

            transition = self._evidence()[-1]
            self.assertEqual(transition["initiator"], "unknown")
            self.assertFalse(transition["recovery_eligible"])
            self.assertEqual(report["rerun_ids"], [])
        finally:
            release_command.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(outcomes, [0])
        final_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            final_state["last_managed_intent"]["status"],
            "succeeded",
        )

    def test_managed_intent_matching_is_action_specific(self):
        self.assertTrue(self.mod._transition_matches_intent("restart", BOOT_A, BOOT_B, True, True))
        self.assertFalse(self.mod._transition_matches_intent("restart", None, BOOT_B, None, True))
        self.assertFalse(self.mod._transition_matches_intent("restart", BOOT_A, BOOT_A, True, True))
        self.assertTrue(self.mod._transition_matches_intent("stop", BOOT_A, None, True, False))
        self.assertFalse(self.mod._transition_matches_intent("stop", None, None, None, False))
        self.assertTrue(self.mod._transition_matches_intent("start", None, BOOT_A, False, True))
        self.assertFalse(self.mod._transition_matches_intent("start", None, BOOT_A, None, True))

    def test_offline_debounce_dedup_and_recovery_alert(self):
        self._poll()
        self.runner.runner_statuses["colingreig/jdmbuysell-v4"] = "offline"

        first = self._poll()
        second = self._poll()
        third = self._poll()

        self.assertEqual(first["runner_alerted"], 0)
        self.assertEqual(second["runner_alerted"], 1)
        self.assertEqual(third["runner_alerted"], 0)
        self.assertEqual(len([msg for msg in self.runner.sent if "runner offline" in msg]), 1)

        self.runner.runner_statuses["colingreig/jdmbuysell-v4"] = "online"
        recovered = self._poll()
        self.assertEqual(recovered["runner_recovered"], 1)
        self.assertEqual(len([msg for msg in self.runner.sent if "runner recovered" in msg]), 1)

    def test_vm_availability_recovery_sends_one_recovery_notification(self):
        self._poll()
        self.runner.vm_state = "stopped"
        self._poll()
        self.runner.vm_state = "running"

        report = self._poll()

        self.assertTrue(report["vm_alerted"])
        self.assertEqual(len([msg for msg in self.runner.sent if "VM recovered" in msg]), 1)

    def test_managed_restart_sends_one_restart_and_one_recovery_notification(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        self.runner.boot_id = BOOT_B

        self._poll()
        self._poll()

        self.assertEqual(len([msg for msg in self.runner.sent if "restart/outage observed" in msg]), 1)
        self.assertEqual(len([msg for msg in self.runner.sent if "VM recovered" in msg]), 1)

    def test_concurrent_polls_deduplicate_transition_jsonl_and_alerts(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        self.runner.boot_id = BOOT_B

        threads = [threading.Thread(target=self._poll) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        transitions = [record for record in self._evidence() if record.get("prior_boot_id") == BOOT_A and record.get("current_boot_id") == BOOT_B]
        self.assertEqual(len(transitions), 1)
        self.assertEqual(len([msg for msg in self.runner.sent if "restart/outage observed" in msg]), 1)
        self.assertEqual(len([msg for msg in self.runner.sent if "VM recovered" in msg]), 1)

    def test_runner_api_403_uses_exact_local_listener_fallback(self):
        self.runner.gh_runner_api_forbidden = True

        self._poll()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("colingreig/jdmbuysell-v4::hermes-jdmbuysell-v4=online", state["last_transition"]["runner_status"])
        self.assertIn("colingreig/topdynamicspartners::hermes-topdynamicspartners=online", state["last_transition"]["runner_status"])
        self.assertIn("colingreig/elevatoruptime.com::hermes-elevatoruptime.com=online", state["last_transition"]["runner_status"])

    def test_local_runner_fallback_is_exact_and_fail_closed(self):
        self.runner.gh_runner_api_forbidden = True
        self.runner.listener_commands["colingreig/jdmbuysell-v4"] = "/bin/sh -c /home/colingreig/actions-runner/jdmbuysell-v4/bin/Runner.Listener run"

        self._poll()

        record = self._evidence()[-1]
        self.assertIn("colingreig/jdmbuysell-v4::hermes-jdmbuysell-v4=missing", record["runner_status"])
        self.assertIn("colingreig/topdynamicspartners::hermes-topdynamicspartners=online", record["runner_status"])

    def test_local_runner_fallback_refuses_ambiguous_exact_listener(self):
        self.runner.gh_runner_api_forbidden = True
        command = self.runner.listener_commands["colingreig/jdmbuysell-v4"]
        self.runner.listener_commands["colingreig/jdmbuysell-v4"] = f"{command}\n{command}"

        self._poll()

        self.assertEqual(self._evidence()[-1]["runner_status"], "unknown")

    def test_stopped_vm_json_is_not_available_and_skips_boot_probes(self):
        self.runner.vm_state = "stopped"

        report = self._poll()

        self.assertFalse(json.loads(self.state_path.read_text(encoding="utf-8"))["vm"]["available"])
        self.assertEqual(report["rerun_ids"], [])
        self.assertFalse(any(call[:2] == ["orb", "exec"] for call in self.runner.calls))

    def test_first_poll_baseline_does_not_authorize_allowlisted_rerun(self):
        self.runner.runs = [
            {
                "databaseId": 707,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/707",
            }
        ]

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIs(state["last_transition"]["recovery_eligible"], False)

    def test_malformed_orb_list_fails_closed_without_state_overwrite_or_rerun(self):
        self._poll()
        before = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.runner.orb_list_stdout = "{not-json"
        self.runner.runs = [
            {
                "databaseId": 808,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "schedule",
                "headBranch": "main",
            }
        ]

        report = self._poll()

        after = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(report["health"], "UNKNOWN")
        self.assertEqual(report["classification"], "unknown")
        self.assertEqual(report["reason"], "vm-probe-invalid")
        self.assertTrue(report["state_preserved"])
        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(after, before)
        self.assertEqual(self.runner.reruns, [])
        self.assertTrue(
            any("lifecycle health UNKNOWN" in msg for msg in self.runner.sent)
        )

    def test_main_exits_nonzero_for_unknown_and_degraded_health(self):
        for health in ("UNKNOWN", "DEGRADED"):
            self.mod.poll_once = lambda **_kwargs: {"health": health}
            self.assertEqual(
                self.mod.main(["poll", "--topology", str(TOPOLOGY)]),
                1,
            )

    def test_boot_id_probe_failure_fails_closed_without_state_overwrite_or_rerun(self):
        self._poll()
        before = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.runner.boot_rc = 1
        self.runner.runs = [
            {
                "databaseId": 909,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "schedule",
                "headBranch": "main",
            }
        ]

        report = self._poll()

        after = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(report["health"], "UNKNOWN")
        self.assertEqual(report["classification"], "unknown")
        self.assertEqual(report["reason"], "vm-probe-invalid")
        self.assertTrue(report["state_preserved"])
        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(after, before)
        self.assertEqual(self.runner.reruns, [])

    def test_malformed_persisted_state_refuses_recovery(self):
        self.state_path.write_text("{not-json", encoding="utf-8")
        self.runner.runs = [
            {
                "databaseId": 404,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "schedule",
                "headBranch": "main",
            }
        ]

        with self.assertRaises(self.mod.MonitorError):
            self._poll()
        self.assertEqual(self.runner.reruns, [])

    def test_managed_restart_interruption_correlation_dispatches_one_allowlisted_rerun_and_survives_reload(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        now = self.mod._now()
        self.runner.runs = [
            {
                "databaseId": 101,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": (now - timedelta(minutes=5)).isoformat(),
                "updatedAt": now.isoformat(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/101",
            }
        ]
        self.runner.boot_id = BOOT_B
        first = self._poll()
        second = self._poll()

        self.assertEqual(first["rerun_ids"], [101])
        self.assertEqual(second["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, ["101"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["recovery"]["101"]["original_run_id"], 101)
        self.assertEqual(state["recovery"]["101"]["attempt"], 1)
        self.assertIn("persisted_before_dispatch_at", state["recovery"]["101"])

    def test_unmanaged_restart_interruption_does_not_authorize_allowlisted_rerun(self):
        self._poll()
        now = self.mod._now()
        self.runner.runs = [
            {
                "databaseId": 112,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": (now - timedelta(minutes=5)).isoformat(),
                "updatedAt": now.isoformat(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/112",
            }
        ]
        self.runner.boot_id = BOOT_B

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, [])
        record = self._evidence()[-1]
        self.assertFalse(record["recovery_eligible"])
        self.assertNotIn("interruption_started_at", record)

    def test_allowlisted_recovery_dispatches_at_most_one_rerun(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        now = self.mod._now()
        self.runner.runs = [
            {
                "databaseId": 121,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": (now - timedelta(minutes=5)).isoformat(),
                "updatedAt": now.isoformat(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/121",
            },
            {
                "databaseId": 122,
                "workflowName": "Dead-image monitor",
                "conclusion": "cancelled",
                "status": "completed",
                "createdAt": (now - timedelta(minutes=4)).isoformat(),
                "updatedAt": now.isoformat(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/122",
            },
        ]
        self.runner.boot_id = BOOT_B

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [121])
        self.assertEqual(self.runner.reruns, ["121"])

    def test_post_restart_allowlisted_failure_is_not_recovered(self):
        self._poll()
        self.runner.boot_id = BOOT_B
        self._poll()
        self.runner.runs = [
            {
                "databaseId": 102,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/102",
            }
        ]

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, [])

    def test_recovery_readiness_is_runner_specific(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        now = self.mod._now()
        self.runner.runs = [
            {
                "databaseId": 111,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": (now - timedelta(minutes=5)).isoformat(),
                "updatedAt": now.isoformat(),
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/111",
            }
        ]
        self.runner.boot_id = BOOT_B
        self.runner.runner_statuses["colingreig/topdynamicspartners"] = "offline"

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [111])
        self.assertEqual(self.runner.reruns, ["111"])

    def test_refuses_to_rerun_non_allowlisted_workflow(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        self.runner.boot_id = BOOT_B
        self._poll()
        self.runner.runs = [
            {
                "databaseId": 202,
                "workflowName": "Deploy production",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": self.mod._now_iso(),
                "updatedAt": self.mod._now_iso(),
                "event": "workflow_dispatch",
                "headBranch": "main",
                "url": "https://example/run/202",
            }
        ]

        self._poll()

        self.assertEqual(self.runner.reruns, [])
        self.assertTrue(any("recovery refused" in msg.lower() for msg in self.runner.sent))
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["recovery"]["202"]["reason"], "not-allowlisted")

    def test_restart_correlation_requires_overlap(self):
        self._poll()
        self.mod.record_managed_lifecycle(
            "restart",
            "operator@example.com",
            "controlled idle-window verification",
            runner=self.runner,
            topology_path=TOPOLOGY,
            state_path=self.state_path,
        )
        self.runner.boot_id = BOOT_B
        self._poll()
        old = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        self.runner.runs = [
            {
                "databaseId": 303,
                "workflowName": "Dead-image monitor",
                "conclusion": "failure",
                "status": "completed",
                "createdAt": old,
                "updatedAt": old,
                "event": "schedule",
                "headBranch": "main",
                "url": "https://example/run/303",
            }
        ]

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, [])


if __name__ == "__main__":
    unittest.main()
