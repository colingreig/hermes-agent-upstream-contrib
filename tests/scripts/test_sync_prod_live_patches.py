"""Deterministic certification tests for prod-live-patches promotion."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "certify_prod_live_patches.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sync-prod-live-patches.yml"
HEAD = "a" * 40
OLDER_HEAD = "b" * 40
PROD_HEAD = "c" * 40


def _load_helper():
    spec = importlib.util.spec_from_file_location("certify_prod_live_patches_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


certifier = _load_helper()


def _run(
    *,
    run_id: int = 501,
    head_sha: str = HEAD,
    status: str = "completed",
    conclusion: str | None = "success",
    run_number: int = 50,
    run_attempt: int = 1,
) -> dict:
    return {
        "id": run_id,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "run_number": run_number,
        "run_attempt": run_attempt,
    }


def _job(
    *,
    run_id: int = 501,
    status: str = "completed",
    conclusion: str | None = "success",
    name: str = "All required checks pass",
) -> dict:
    return {
        "id": 9001,
        "run_id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def _evidence() -> dict:
    return {
        "repository": "owner/hermes-agent",
        "current_main_sha": HEAD,
        "trigger_run_id": 501,
        "freeze": {
            "schema": "prod_live_patches_freeze/v1",
            "frozen": False,
            "actor": "release-owner@example.com",
            "reason": "normal governed promotion",
            "changed_at": "2026-07-29T12:00:00Z",
        },
        "workflow_runs": {"workflow_runs": [_run()]},
        "jobs": {"jobs": [_job()]},
    }


def test_exact_current_main_push_ci_and_aggregate_issue_certificate():
    certificate = certifier.certify(_evidence())

    assert certificate["schema"] == "prod_live_patches_certificate/v1"
    assert certificate["head_sha"] == HEAD
    assert certificate["ci"]["run_id"] == "501"
    assert certificate["ci"]["aggregate_job"]["conclusion"] == "success"
    assert certificate["freeze"]["actor"] == "release-owner@example.com"


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "skipped"),
    ],
)
def test_missing_pending_failed_cancelled_or_skipped_ci_is_rejected(status, conclusion):
    evidence = _evidence()
    evidence["workflow_runs"]["workflow_runs"] = [
        _run(status=status, conclusion=conclusion)
    ]

    with pytest.raises(certifier.CertificationError):
        certifier.certify(evidence)


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("queued", None),
        ("in_progress", None),
        ("completed", "failure"),
        ("completed", "cancelled"),
        ("completed", "skipped"),
    ],
)
def test_non_success_aggregate_job_is_rejected(status, conclusion):
    evidence = _evidence()
    evidence["jobs"]["jobs"] = [_job(status=status, conclusion=conclusion)]

    with pytest.raises(certifier.CertificationError):
        certifier.certify(evidence)


def test_wrong_sha_and_ancestor_only_evidence_are_rejected():
    evidence = _evidence()
    evidence["workflow_runs"]["workflow_runs"] = [_run(head_sha=OLDER_HEAD)]

    with pytest.raises(certifier.CertificationError, match="no exact current-main"):
        certifier.certify(evidence)


def test_older_success_cannot_mask_newer_failed_exact_sha_run():
    evidence = _evidence()
    evidence["trigger_run_id"] = None
    evidence["workflow_runs"]["workflow_runs"] = [
        _run(run_id=500, run_number=49, conclusion="success"),
        _run(run_id=501, run_number=50, conclusion="failure"),
    ]
    evidence["jobs"]["jobs"] = [_job(run_id=501)]

    with pytest.raises(certifier.CertificationError, match="not success"):
        certifier.certify(evidence)


def test_workflow_run_trigger_must_be_latest_exact_run():
    evidence = _evidence()
    evidence["trigger_run_id"] = 499

    with pytest.raises(certifier.CertificationError, match="stale"):
        certifier.certify(evidence)


def test_missing_aggregate_job_is_rejected():
    evidence = _evidence()
    evidence["jobs"] = {"jobs": [_job(name="advisory")]}

    with pytest.raises(certifier.CertificationError, match="exactly one"):
        certifier.certify(evidence)


def test_aggregate_job_must_join_the_exact_workflow_run():
    evidence = _evidence()
    evidence["jobs"]["jobs"][0].pop("run_id")

    with pytest.raises(certifier.CertificationError, match="aggregate_job.run_id"):
        certifier.certify(evidence)


@pytest.mark.parametrize(
    "freeze",
    [
        {},
        {
            "schema": "prod_live_patches_freeze/v1",
            "frozen": True,
            "actor": "operator",
            "reason": "incident",
            "changed_at": "2026-07-29T12:00:00Z",
        },
        {
            "schema": "prod_live_patches_freeze/v1",
            "frozen": False,
            "actor": "",
            "reason": "",
            "changed_at": "not-a-time",
        },
    ],
)
def test_freeze_gate_is_structured_audited_and_fail_closed(freeze):
    evidence = _evidence()
    evidence["freeze"] = freeze

    with pytest.raises(certifier.CertificationError):
        certifier.certify(evidence)


def test_receipt_filename_is_content_sha_and_records_authorities(tmp_path):
    certificate = certifier.certify(_evidence())
    certificate_path = tmp_path / "certificate.json"
    certificate_path.write_text(
        json.dumps(certificate, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    receipt_path = certifier.write_receipt(
        certificate_path,
        tmp_path / "receipts",
        from_sha=PROD_HEAD,
        authority_run_id="700",
        authority_run_attempt="2",
    )
    content = receipt_path.read_bytes()
    receipt = json.loads(content)

    assert receipt_path.name == f"promotion-receipt-{hashlib.sha256(content).hexdigest()}.json"
    assert receipt["authority"] == ".github/workflows/sync-prod-live-patches.yml"
    assert receipt["authority_run_id"] == "700"
    assert receipt["head_sha"] == HEAD
    assert receipt["from_sha"] == PROD_HEAD
    assert receipt["freeze"]["reason"] == "normal governed promotion"
    assert receipt["ci"]["run_id"] == "501"


@pytest.fixture(scope="module")
def workflow() -> dict:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _triggers(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True)


def _step(workflow: dict, name: str) -> dict:
    steps = workflow["jobs"]["promote"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_workflow_waits_for_completed_ci_and_has_no_push_bypass(workflow):
    triggers = _triggers(workflow)

    assert "push" not in triggers
    assert triggers["workflow_run"]["workflows"] == ["CI"]
    assert triggers["workflow_run"]["branches"] == ["main"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers


def test_all_triggers_share_certificate_and_exact_sha_cas_path(workflow):
    assert list(workflow["jobs"]) == ["promote"]
    job = workflow["jobs"]["promote"]
    assert job["env"]["FREEZE_STATE"] == "${{ vars.PROD_LIVE_PATCHES_FREEZE }}"

    collect = _step(workflow, "Collect governing CI evidence")["run"]
    certify = _step(workflow, "Certify exact SHA, aggregate job, and freeze state")["run"]
    push = _step(workflow, "Re-fetch, CAS assert, and push certified SHA")["run"]

    assert "event=push" in collect
    assert "scripts/certify_prod_live_patches.py certify" in certify
    assert "git fetch --no-tags origin" in push
    assert '[ "$FRESH_MAIN" = "$CERTIFIED_SHA" ]' in push
    assert '[ "$FRESH_PROD" = "$EXPECTED_PROD_SHA" ]' in push
    assert "--force-with-lease=" in push
    assert "PUSHED_SHA" in push
    assert push.index("git fetch --no-tags origin") < push.index("FRESH_MAIN=")
    assert push.index("FRESH_MAIN=") < push.index("git push")


def test_workflow_publishes_content_addressed_receipt_artifact(workflow):
    receipt = _step(workflow, "Create content-addressed promotion receipt")
    publish = _step(workflow, "Publish immutable promotion receipt")

    assert "scripts/certify_prod_live_patches.py receipt" in receipt["run"]
    assert publish["if"] == "steps.prepare.outputs.changed == 'true'"
    assert publish["with"]["name"] == (
        "prod-live-patches-promotion-${{ steps.receipt.outputs.id }}"
    )
    assert publish["with"]["if-no-files-found"] == "error"
