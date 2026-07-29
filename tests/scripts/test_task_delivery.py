from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts" / "task_delivery.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("task_delivery_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


delivery = _load_module()


def _snapshot(*, lane: str = "mini", sha: str = "abc123") -> dict:
    value = {
        "observed_at": "2026-07-29T12:00:00+00:00",
        "task": {"id": "TASK-1", "lane": lane},
        "sources": {
            "clickup": {"status": "OK"},
            "github": {"status": "OK"},
            "mini": {"status": "OK"},
        },
        "executor": {"job_id": "job-1", "run_id": "run-1", "fencing_token": "17"},
        "ledger": {
            "execution_id": "execution-1",
            "job_id": "job-1",
            "run_id": "run-1",
            "fencing_token": "17",
            "owner_token": "cron-owner-1",
            "status": "completed",
        },
        "repository": "owner/repo",
        "governing_workflows": ["governing-ci"],
        "pull_requests": [
            {"repository": "owner/repo", "number": 12, "head_sha": sha}
        ],
        "ci": {
            "runs": [
                {
                    "repository": "owner/repo",
                    "workflow": "governing-ci",
                    "run_id": 99,
                    "head_sha": sha,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        "handoff": {"id": "handoff-1", "head_sha": sha},
        "validator": {"identity": "validator-1", "verdict": "PASS", "head_sha": sha},
        "deployment": {"target": "mini", "head_sha": sha},
        "release": {
            "authority": "mini-release-cut",
            "receipt_id": "receipt-1",
            "head_sha": sha,
        },
    }
    return value


def test_mini_chain_delivers_only_with_every_exact_identity():
    result = delivery.correlate(_snapshot())

    assert result["delivery_status"] == "DELIVERED"
    assert result["ledger_execution_id"] == "execution-1"
    assert result["pr_head_sha_set"] == ["abc123"]
    assert result["release"]["receipt_id"] == "receipt-1"


def test_source_failure_is_unknown_and_blocks_delivery():
    snapshot = _snapshot()
    snapshot["sources"]["github"] = {"status": "UNKNOWN", "error": "timeout"}

    result = delivery.correlate(snapshot)

    assert result["delivery_status"] == "UNKNOWN"
    assert result["unknown_sources"] == ["github"]


def test_claim_heartbeat_and_wrong_sha_ci_never_shortcut_delivery():
    snapshot = _snapshot()
    snapshot["executor"]["heartbeat_at"] = "2026-07-29T12:00:00Z"
    snapshot["ci"]["runs"][0]["head_sha"] = "wrong"

    result = delivery.correlate(snapshot)

    assert result["delivery_status"] == "INCOMPLETE"
    assert "ci_exact_terminal_success:abc123" in result["identity_mismatches"]


def test_stacked_pr_requires_exact_terminal_ci_for_every_head():
    snapshot = _snapshot(lane="repo-only", sha="base")
    snapshot["stacked"] = True
    snapshot["pull_requests"] = [
        {"repository": "owner/repo", "number": 12, "head_sha": "base", "stack_index": 0},
        {"repository": "owner/repo", "number": 13, "head_sha": "tip", "stack_index": 1},
    ]
    snapshot["ci"]["runs"].append(
        {
            "repository": "owner/repo",
            "workflow": "governing-ci",
            "run_id": 100,
            "head_sha": "tip",
            "status": "completed",
            "conclusion": "success",
        }
    )
    snapshot["handoff"]["head_sha"] = "tip"
    snapshot["validator"]["head_sha"] = "tip"

    delivered = delivery.correlate(snapshot)
    snapshot["ci"]["runs"].pop()
    incomplete = delivery.correlate(snapshot)

    assert delivered["delivery_status"] == "DELIVERED"
    assert delivered["pr_head_sha_set"] == ["base", "tip"]
    assert incomplete["delivery_status"] == "INCOMPLETE"
    assert "ci_exact_terminal_success:tip" in incomplete["identity_mismatches"]


def test_no_pr_path_requires_explicit_configuration_and_receipt_authority():
    snapshot = _snapshot()
    snapshot["pull_requests"] = []
    snapshot["delivery_head_sha"] = "abc123"
    snapshot["allow_no_pr"] = True
    snapshot["no_pr_authority"] = {
        "authority": "governed-no-pr-policy",
        "receipt_id": "no-pr-1",
        "head_sha": "abc123",
    }

    delivered = delivery.correlate(snapshot)
    snapshot["allow_no_pr"] = False
    incomplete = delivery.correlate(snapshot)

    assert delivered["delivery_status"] == "DELIVERED"
    assert delivered["no_pr"] is True
    assert delivered["no_pr_authority"]["receipt_id"] == "no-pr-1"
    assert incomplete["delivery_status"] == "INCOMPLETE"
    assert "explicit_no_pr_configuration" in incomplete["missing_evidence"]


def test_repo_only_does_not_require_deployment_or_release():
    snapshot = _snapshot(lane="repo-only")
    snapshot.pop("deployment")
    snapshot.pop("release")

    result = delivery.correlate(snapshot)

    assert result["delivery_status"] == "DELIVERED"


def test_cross_repository_or_unapproved_workflow_evidence_never_delivers():
    snapshot = _snapshot()
    snapshot["pull_requests"][0]["repository"] = "attacker/other"
    snapshot["ci"]["runs"][0]["repository"] = "attacker/other"
    snapshot["ci"]["runs"][0]["workflow"] = "unrelated"

    result = delivery.correlate(snapshot)

    assert result["delivery_status"] == "INCOMPLETE"
    assert "pull_requests[0].repository" in result["identity_mismatches"]
    assert "ci.runs[0].repository" in result["identity_mismatches"]
    assert "ci.runs[0].workflow" in result["identity_mismatches"]
    assert "ci_exact_terminal_success:abc123" in result["identity_mismatches"]


def test_stacked_pr_membership_must_be_unique():
    snapshot = _snapshot(lane="repo-only")
    snapshot["stacked"] = True
    snapshot["pull_requests"] = [
        {
            "repository": "owner/repo",
            "number": 12,
            "head_sha": "abc123",
            "stack_index": 0,
        },
        {
            "repository": "owner/repo",
            "number": 12,
            "head_sha": "abc123",
            "stack_index": 1,
        },
    ]

    result = delivery.correlate(snapshot)

    assert result["delivery_status"] == "INCOMPLETE"
    assert "stacked_pull_request_membership_unique" in result["identity_mismatches"]


def test_ledger_requires_terminal_status_and_exact_run_fence_identity():
    snapshot = _snapshot()
    snapshot["ledger"]["status"] = "running"
    snapshot["ledger"]["run_id"] = "other-run"
    snapshot["ledger"]["fencing_token"] = "99"

    result = delivery.correlate(snapshot)

    assert result["delivery_status"] == "INCOMPLETE"
    assert "ledger.status_completed" in result["missing_evidence"]
    assert "executor_ledger.run_id" in result["identity_mismatches"]
    assert "executor_ledger.fencing_token" in result["identity_mismatches"]
