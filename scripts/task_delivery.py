#!/usr/bin/env python3
"""Fail-closed task delivery evidence correlator.

``task_delivery/v1`` joins evidence identities across the task, executor,
execution ledger, pull request, CI, handoff, validator, deployment, and release
receipt boundaries.  It deliberately does not accept a claim, heartbeat, HTTP
response, branch name, or a successful run for a different SHA as delivery
proof.

The correlator is side-effect free.  Collection belongs to the independent
watcher; callers pass one normalized task snapshot to :func:`correlate`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


SCHEMA = "task_delivery/v1"
SOURCE_STATES = {"OK", "UNKNOWN"}
LANES = {"mini", "repo-only"}


class CorrelationError(ValueError):
    """The normalized snapshot is structurally unsafe to correlate."""


def _nonempty(value: object) -> bool:
    return isinstance(value, (str, int)) and str(value).strip() != ""


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_mappings(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_failures(snapshot: dict[str, Any]) -> list[str]:
    sources = snapshot.get("sources")
    if not isinstance(sources, dict) or not sources:
        return ["sources"]
    failed: list[str] = []
    for name, envelope in sources.items():
        state = _mapping(envelope).get("status")
        if state not in SOURCE_STATES or state != "OK":
            failed.append(str(name))
    return sorted(failed)


def _pull_requests(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    pull_requests = _list_of_mappings(snapshot.get("pull_requests"))
    normalized: list[dict[str, Any]] = []
    for index, pr in enumerate(pull_requests):
        normalized.append(
            {
                "repository": pr.get("repository"),
                "number": pr.get("number"),
                "head_sha": pr.get("head_sha"),
                "stack_index": pr.get("stack_index", index),
            }
        )
    return sorted(normalized, key=lambda item: item["stack_index"])


def _ci_runs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    ci = _mapping(snapshot.get("ci"))
    runs = _list_of_mappings(ci.get("runs"))
    if not runs and ci:
        runs = [ci]
    return [
        {
            "repository": run.get("repository"),
            "workflow": run.get("workflow"),
            "run_id": run.get("run_id"),
            "head_sha": run.get("head_sha"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        }
        for run in runs
    ]


def _append_missing(missing: list[str], condition: bool, name: str) -> None:
    if not condition:
        missing.append(name)


def correlate(snapshot: dict[str, Any], *, generated_at: str | None = None) -> dict[str, Any]:
    """Return one canonical ``task_delivery/v1`` correlation.

    Supported forms are:

    * ``mini``: PR or explicitly configured no-PR evidence plus exact CI,
      validation, deployment, and release receipt authority.
    * ``repo-only``: PR evidence plus exact CI and validation; deployment and
      release are intentionally not required.
    * stacked PRs: set ``stacked`` true and provide two or more ordered PRs.
    * no-PR: set ``allow_no_pr`` true and provide a nonempty
      ``no_pr_authority`` object.  Merely omitting a PR never selects this path.
    """
    if not isinstance(snapshot, dict):
        raise CorrelationError("snapshot must be a JSON object")

    task = _mapping(snapshot.get("task"))
    task_id = task.get("id")
    lane = task.get("lane")
    if not _nonempty(task_id):
        raise CorrelationError("task.id is required")
    if lane not in LANES:
        raise CorrelationError(f"task.lane must be one of {sorted(LANES)}")

    executor = _mapping(snapshot.get("executor"))
    ledger = _mapping(snapshot.get("ledger"))
    handoff = _mapping(snapshot.get("handoff"))
    validator = _mapping(snapshot.get("validator"))
    deployment = _mapping(snapshot.get("deployment"))
    release = _mapping(snapshot.get("release"))
    no_pr_authority = _mapping(snapshot.get("no_pr_authority"))
    pull_requests = _pull_requests(snapshot)
    ci_runs = _ci_runs(snapshot)
    expected_repository = snapshot.get("repository")
    governing_workflows = snapshot.get("governing_workflows")
    allowed_workflows = {
        str(item)
        for item in governing_workflows
        if _nonempty(item)
    } if isinstance(governing_workflows, list) else set()
    pr_head_shas = _unique(
        str(pr["head_sha"]) for pr in pull_requests if _nonempty(pr.get("head_sha"))
    )
    delivery_head_sha = snapshot.get("delivery_head_sha")
    if not _nonempty(delivery_head_sha) and pr_head_shas:
        delivery_head_sha = pr_head_shas[-1]

    missing: list[str] = []
    mismatches: list[str] = []
    unknown_sources = _source_failures(snapshot)

    _append_missing(missing, _nonempty(expected_repository), "repository")
    _append_missing(
        missing,
        bool(allowed_workflows),
        "governing_workflows",
    )
    _append_missing(missing, _nonempty(executor.get("job_id")), "executor.job_id")
    _append_missing(missing, _nonempty(executor.get("run_id")), "executor.run_id")
    _append_missing(missing, _nonempty(executor.get("fencing_token")), "executor.fencing_token")
    _append_missing(missing, _nonempty(ledger.get("execution_id")), "ledger.execution_id")
    _append_missing(missing, _nonempty(ledger.get("job_id")), "ledger.job_id")
    _append_missing(missing, _nonempty(ledger.get("run_id")), "ledger.run_id")
    _append_missing(missing, _nonempty(ledger.get("fencing_token")), "ledger.fencing_token")
    _append_missing(missing, _nonempty(ledger.get("owner_token")), "ledger.owner_token")
    _append_missing(missing, ledger.get("status") == "completed", "ledger.status_completed")

    for field in ("job_id", "run_id", "fencing_token"):
        if _nonempty(executor.get(field)) and _nonempty(ledger.get(field)):
            if str(executor[field]) != str(ledger[field]):
                mismatches.append(f"executor_ledger.{field}")

    allow_no_pr = snapshot.get("allow_no_pr") is True
    no_pr = not pull_requests
    if no_pr:
        _append_missing(missing, allow_no_pr, "explicit_no_pr_configuration")
        _append_missing(missing, _nonempty(no_pr_authority.get("authority")), "no_pr_authority.authority")
        _append_missing(missing, _nonempty(no_pr_authority.get("receipt_id")), "no_pr_authority.receipt_id")
        _append_missing(missing, _nonempty(no_pr_authority.get("head_sha")), "no_pr_authority.head_sha")
        if _nonempty(no_pr_authority.get("head_sha")):
            if _nonempty(delivery_head_sha) and str(no_pr_authority["head_sha"]) != str(delivery_head_sha):
                mismatches.append("no_pr_authority.head_sha")
            elif not _nonempty(delivery_head_sha):
                delivery_head_sha = no_pr_authority["head_sha"]
    else:
        for index, pr in enumerate(pull_requests):
            _append_missing(
                missing,
                _nonempty(pr.get("repository")),
                f"pull_requests[{index}].repository",
            )
            _append_missing(missing, _nonempty(pr.get("number")), f"pull_requests[{index}].number")
            _append_missing(missing, _nonempty(pr.get("head_sha")), f"pull_requests[{index}].head_sha")
            if (
                _nonempty(expected_repository)
                and _nonempty(pr.get("repository"))
                and str(pr["repository"]) != str(expected_repository)
            ):
                mismatches.append(f"pull_requests[{index}].repository")
        if snapshot.get("stacked") is True:
            _append_missing(missing, len(pull_requests) >= 2, "stacked_pull_request_chain")
            pr_memberships = [
                (str(pr.get("repository")), str(pr.get("number")))
                for pr in pull_requests
                if _nonempty(pr.get("repository")) and _nonempty(pr.get("number"))
            ]
            pr_heads = [
                str(pr["head_sha"])
                for pr in pull_requests
                if _nonempty(pr.get("head_sha"))
            ]
            if (
                len(pr_memberships) != len(set(pr_memberships))
                or len(pr_heads) != len(set(pr_heads))
            ):
                mismatches.append("stacked_pull_request_membership_unique")
        elif len(pull_requests) > 1:
            missing.append("explicit_stacked_configuration")

    _append_missing(missing, _nonempty(delivery_head_sha), "delivery_head_sha")

    ci_by_head: dict[str, list[dict[str, Any]]] = {}
    for run in ci_runs:
        if _nonempty(run.get("head_sha")):
            ci_by_head.setdefault(str(run["head_sha"]), []).append(run)
    required_ci_heads = pr_head_shas if pull_requests else (
        [str(delivery_head_sha)] if _nonempty(delivery_head_sha) else []
    )
    for index, run in enumerate(ci_runs):
        _append_missing(
            missing,
            _nonempty(run.get("repository")),
            f"ci.runs[{index}].repository",
        )
        _append_missing(
            missing,
            _nonempty(run.get("workflow")),
            f"ci.runs[{index}].workflow",
        )
        if (
            _nonempty(expected_repository)
            and _nonempty(run.get("repository"))
            and str(run["repository"]) != str(expected_repository)
        ):
            mismatches.append(f"ci.runs[{index}].repository")
        if (
            _nonempty(run.get("workflow"))
            and allowed_workflows
            and str(run["workflow"]) not in allowed_workflows
        ):
            mismatches.append(f"ci.runs[{index}].workflow")
        if (
            _nonempty(run.get("head_sha"))
            and required_ci_heads
            and str(run["head_sha"]) not in required_ci_heads
        ):
            mismatches.append(f"ci.runs[{index}].head_sha")

    for head_sha in required_ci_heads:
        exact_runs = ci_by_head.get(head_sha, [])
        terminal_success = [
            run
            for run in exact_runs
            if _nonempty(run.get("run_id"))
            and str(run.get("repository")) == str(expected_repository)
            and str(run.get("workflow")) in allowed_workflows
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
        ]
        if not terminal_success:
            mismatches.append(f"ci_exact_terminal_success:{head_sha}")

    _append_missing(missing, _nonempty(handoff.get("id")), "handoff.id")
    _append_missing(missing, _nonempty(handoff.get("head_sha")), "handoff.head_sha")
    _append_missing(missing, _nonempty(validator.get("identity")), "validator.identity")
    _append_missing(missing, validator.get("verdict") == "PASS", "validator.verdict_PASS")
    _append_missing(missing, _nonempty(validator.get("head_sha")), "validator.head_sha")
    if _nonempty(delivery_head_sha):
        for name, value in (
            ("handoff.head_sha", handoff.get("head_sha")),
            ("validator.head_sha", validator.get("head_sha")),
        ):
            if _nonempty(value) and str(value) != str(delivery_head_sha):
                mismatches.append(name)

    if lane == "mini":
        _append_missing(missing, _nonempty(deployment.get("target")), "deployment.target")
        _append_missing(missing, _nonempty(deployment.get("head_sha")), "deployment.head_sha")
        _append_missing(missing, _nonempty(release.get("authority")), "release.authority")
        _append_missing(missing, _nonempty(release.get("receipt_id")), "release.receipt_id")
        _append_missing(missing, _nonempty(release.get("head_sha")), "release.head_sha")
        if _nonempty(delivery_head_sha):
            for name, value in (
                ("deployment.head_sha", deployment.get("head_sha")),
                ("release.head_sha", release.get("head_sha")),
            ):
                if _nonempty(value) and str(value) != str(delivery_head_sha):
                    mismatches.append(name)

    if unknown_sources:
        delivery_status = "UNKNOWN"
    elif missing or mismatches:
        delivery_status = "INCOMPLETE"
    else:
        delivery_status = "DELIVERED"

    return {
        "schema": SCHEMA,
        "generated_at": generated_at or snapshot.get("observed_at") or _iso_now(),
        "task": {"id": task_id, "lane": lane},
        "executor": {
            "job_id": executor.get("job_id"),
            "run_id": executor.get("run_id"),
            "fencing_token": executor.get("fencing_token"),
        },
        "ledger_execution_id": ledger.get("execution_id"),
        "repository": expected_repository,
        "governing_workflows": sorted(allowed_workflows),
        "pull_requests": pull_requests,
        "pr_head_sha_set": pr_head_shas,
        "ci": {"runs": ci_runs},
        "handoff": {
            "id": handoff.get("id"),
            "head_sha": handoff.get("head_sha"),
        },
        "validator": {
            "identity": validator.get("identity"),
            "verdict": validator.get("verdict"),
            "head_sha": validator.get("head_sha"),
        },
        "deployment": {
            "target": deployment.get("target"),
            "head_sha": deployment.get("head_sha"),
        },
        "release": {
            "authority": release.get("authority"),
            "receipt_id": release.get("receipt_id"),
            "head_sha": release.get("head_sha"),
        },
        "no_pr_authority": {
            "authority": no_pr_authority.get("authority"),
            "receipt_id": no_pr_authority.get("receipt_id"),
            "head_sha": no_pr_authority.get("head_sha"),
        },
        "delivery_head_sha": delivery_head_sha,
        "delivery_status": delivery_status,
        "unknown_sources": unknown_sources,
        "missing_evidence": sorted(set(missing)),
        "identity_mismatches": sorted(set(mismatches)),
        "no_pr": no_pr,
        "stacked": snapshot.get("stacked") is True,
    }


__all__ = ["CorrelationError", "SCHEMA", "correlate"]
