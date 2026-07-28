from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent.parent
MODULE = SCRIPTS / "pr_pipeline" / "ci_health_watch.py"
TOPOLOGY = SCRIPTS / "pr_pipeline" / "ci_health_topology.json"
_COUNTER = 0


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
        self.boot_id = "boot-a"
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

    def __call__(self, cmd, **_kwargs):
        cmd = [str(part) for part in cmd]
        self.calls.append(cmd)
        if Path(cmd[0]).name == "orb" and cmd[1:2] == ["list"]:
            if self.orb_list_stdout is not None:
                return subprocess.CompletedProcess(cmd, 0, self.orb_list_stdout, "")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"vms": [{"name": "hermes-ci", "state": self.vm_state}]}), "")
        if Path(cmd[0]).name == "orb" and cmd[1:4] == ["exec", "hermes-ci", "cat"]:
            return subprocess.CompletedProcess(cmd, self.boot_rc, self.boot_id + "\n" if self.boot_rc == 0 else "", "boot failed")
        if Path(cmd[0]).name == "orb" and cmd[1:4] == ["exec", "hermes-ci", "cut"]:
            return subprocess.CompletedProcess(cmd, 0, "120\n", "")
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
        if Path(cmd[0]).name == "orb" and cmd[1:2] == ["restart"]:
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
        self.assertEqual(topology["recovery_allowlist"][0]["repo"], "colingreig/jdmbuysell-v4")
        self.assertEqual(topology["recovery_allowlist"][0]["workflow"], "Dead-image monitor")

        vm = topology["vm"]
        commands = [vm["status_command"], vm["boot_id_command"], vm["uptime_command"], *vm["managed_commands"].values()]
        self.assertTrue(all(command[0] == "/usr/local/bin/orb" for command in commands))

    def test_topology_is_manifest_managed(self):
        manifest = json.loads((SCRIPTS / "pr_pipeline" / "manifest.json").read_text(encoding="utf-8"))

        self.assertIn("ci_health_topology.json", manifest["legacy_flat_entrypoints"])
        self.assertIn("ci_health_topology.json", manifest["managed_root_patterns"])

    def test_boot_id_transition_logging_and_unknown_initiator(self):
        self._poll()
        self.runner.boot_id = "boot-b"

        self._poll()

        records = self._evidence()
        self.assertEqual(records[-1]["prior_boot_id"], "boot-a")
        self.assertEqual(records[-1]["current_boot_id"], "boot-b")
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
        self.assertEqual(self.runner.calls[-1], ["/usr/local/bin/orb", "restart", "hermes-ci"])

        self._poll()
        self.runner.boot_id = "boot-b"
        self._poll()
        self.assertEqual(self._evidence()[-1]["initiator"], "operator@example.com")

        self.runner.boot_id = "boot-c"
        self._poll()
        self.assertEqual(self._evidence()[-1]["initiator"], "unknown")

    def test_managed_intent_matching_is_action_specific(self):
        self.assertTrue(self.mod._transition_matches_intent("restart", "boot-a", "boot-b", True, True))
        self.assertFalse(self.mod._transition_matches_intent("restart", None, "boot-b", None, True))
        self.assertFalse(self.mod._transition_matches_intent("restart", "boot-a", "boot-a", True, True))
        self.assertTrue(self.mod._transition_matches_intent("stop", "boot-a", None, True, False))
        self.assertFalse(self.mod._transition_matches_intent("stop", None, None, None, False))
        self.assertTrue(self.mod._transition_matches_intent("start", None, "boot-a", False, True))
        self.assertFalse(self.mod._transition_matches_intent("start", None, "boot-a", None, True))

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

    def test_stopped_vm_json_is_not_available_and_skips_boot_probes(self):
        self.runner.vm_state = "stopped"

        report = self._poll()

        self.assertFalse(json.loads(self.state_path.read_text(encoding="utf-8"))["vm"]["available"])
        self.assertEqual(report["rerun_ids"], [])
        self.assertFalse(any(Path(call[0]).name == "orb" and call[1:2] == ["exec"] for call in self.runner.calls))

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

        with self.assertRaises(self.mod.MonitorError):
            self._poll()

        after = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(after, before)
        self.assertEqual(self.runner.reruns, [])

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

        with self.assertRaises(self.mod.MonitorError):
            self._poll()

        after = json.loads(self.state_path.read_text(encoding="utf-8"))
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

    def test_restart_interruption_correlation_dispatches_one_allowlisted_rerun_and_survives_reload(self):
        self._poll()
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
        self.runner.boot_id = "boot-b"
        first = self._poll()
        second = self._poll()

        self.assertEqual(first["rerun_ids"], [101])
        self.assertEqual(second["rerun_ids"], [])
        self.assertEqual(self.runner.reruns, ["101"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["recovery"]["101"]["original_run_id"], 101)
        self.assertEqual(state["recovery"]["101"]["attempt"], 1)
        self.assertIn("persisted_before_dispatch_at", state["recovery"]["101"])

    def test_post_restart_allowlisted_failure_is_not_recovered(self):
        self._poll()
        self.runner.boot_id = "boot-b"
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
        self.runner.boot_id = "boot-b"
        self.runner.runner_statuses["colingreig/topdynamicspartners"] = "offline"

        report = self._poll()

        self.assertEqual(report["rerun_ids"], [111])
        self.assertEqual(self.runner.reruns, ["111"])

    def test_refuses_to_rerun_non_allowlisted_workflow(self):
        self._poll()
        self.runner.boot_id = "boot-b"
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
        self.runner.boot_id = "boot-b"
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
