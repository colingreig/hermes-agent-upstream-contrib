"""Operator CLI for the fenced production-write lease."""
from __future__ import annotations

import argparse
import json
from typing import Any


def _emit(value: Any) -> int:
    print(json.dumps(value, sort_keys=True))
    return 0


def _identity(parser: argparse.ArgumentParser, *, include_context: bool) -> None:
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--fencing-token", required=True, type=int)
    if include_context:
        parser.add_argument("--resources", required=True, nargs="+", help="registered resource IDs")
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--repo", required=True)
        parser.add_argument("--commit-sha", required=True)
        parser.add_argument("--reason", required=True)


def cmd_production_write_lease(args: argparse.Namespace) -> int:
    from cron import production_write_lease as lease
    try:
        action = args.production_write_lease_action
        if action == "acquire":
            value = lease.acquire(args.resources, args.actor, args.session_id, args.workspace, args.repo, args.commit_sha, args.reason, args.lease_seconds)
            return _emit(value.as_dict())
        if action == "heartbeat":
            value = lease.heartbeat(lease_id=args.lease_id, actor=args.actor, session_id=args.session_id, fencing_token=args.fencing_token, lease_seconds=args.lease_seconds)
            return _emit(value.as_dict())
        if action == "release":
            lease.release(lease_id=args.lease_id, actor=args.actor, session_id=args.session_id, fencing_token=args.fencing_token)
            return _emit({"released": True, "lease_id": args.lease_id, "fencing_token": args.fencing_token})
        if action == "status":
            return _emit(lease.status())
        if action == "recover":
            try:
                evidence = json.loads(args.evidence_json)
            except json.JSONDecodeError as exc:
                raise lease.ProductionWriteLeaseError(f"--evidence-json must be JSON: {exc}") from exc
            return _emit(lease.recover_expired(lease_id=args.lease_id, actor=args.actor, session_id=args.session_id, fencing_token=args.fencing_token, recovered_by=args.recovered_by, reason=args.reason, evidence=evidence))
        raise lease.ProductionWriteLeaseError(f"unknown production-write-lease action: {action}")
    except lease.ProductionWriteLeaseError as exc:
        print(json.dumps({"error": str(exc), "ok": False}, sort_keys=True))
        return 2


def register_parser(subparsers) -> None:
    parser = subparsers.add_parser("production-write-lease", help="Acquire and inspect fenced production writer leases")
    actions = parser.add_subparsers(dest="production_write_lease_action", required=True)
    acquire = actions.add_parser("acquire", help="acquire every registry resource for a writer")
    acquire.add_argument("--resources", required=True, nargs="+")
    acquire.add_argument("--actor", required=True)
    acquire.add_argument("--session-id", required=True)
    acquire.add_argument("--workspace", required=True)
    acquire.add_argument("--repo", required=True)
    acquire.add_argument("--commit-sha", required=True)
    acquire.add_argument("--reason", required=True)
    acquire.add_argument("--lease-seconds", type=int, default=120)
    heartbeat = actions.add_parser("heartbeat", help="extend an exactly-owned lease")
    _identity(heartbeat, include_context=False)
    heartbeat.add_argument("--lease-seconds", type=int, default=120)
    release = actions.add_parser("release", help="release an exactly-owned lease")
    _identity(release, include_context=False)
    actions.add_parser("status", help="read active production write leases")
    recover = actions.add_parser("recover", help="recover an expired lease with immutable evidence")
    _identity(recover, include_context=False)
    recover.add_argument("--recovered-by", required=True)
    recover.add_argument("--reason", required=True)
    recover.add_argument("--evidence-json", required=True)
    parser.set_defaults(func=cmd_production_write_lease)
