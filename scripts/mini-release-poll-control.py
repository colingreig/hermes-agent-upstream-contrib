#!/usr/bin/env python3
"""Governed freeze control for the Hermes Mini release poller.

The control state lives beside release receipts, not in the active release, so
it survives cutover and rollback.  Every mutation requires an explicit actor
and reason and produces an immutable, content-addressed receipt.  Read or
integrity uncertainty is fail-closed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable


SCHEMA = "hermes-mini-release-poll-control/v1"
SHA_RE = re.compile(r"[0-9a-f]{40,64}\Z")
LATEST_NAME = ".mini-release-poll-control.json"
RECEIPT_PREFIX = ".mini-release-poll-control-receipt-"
LOCK_NAME = ".mini-release-poll-control.lock"


class ControlError(RuntimeError):
    """The poll-control state cannot be trusted or changed safely."""


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _receipt_id(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_text(value: str | None, field: str) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 500 or any(ord(char) < 32 for char in normalized):
        raise ControlError(f"{field} must be non-empty printable text (max 500 characters)")
    return normalized


def _validate_sha(value: str | None) -> str:
    normalized = (value or "").strip()
    if not SHA_RE.fullmatch(normalized):
        raise ControlError("certified SHA must be a full lowercase git object id")
    return normalized


def _assert_private_regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ControlError(f"control file is missing or not a regular file: {path}")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ControlError(f"control file permissions are not private: {path} mode={mode:o}")


def read_state(state_dir: Path) -> dict[str, Any]:
    """Read and verify the latest state plus its immutable receipt sibling."""
    latest = state_dir / LATEST_NAME
    _assert_private_regular(latest)
    try:
        payload = latest.read_bytes()
        state = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlError(f"cannot read poll-control state: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema") != SCHEMA:
        raise ControlError("poll-control state schema is invalid")
    if state.get("state") not in {"frozen", "unfrozen"}:
        raise ControlError("poll-control state value is invalid")
    if not isinstance(state.get("sequence"), int) or state["sequence"] < 1:
        raise ControlError("poll-control sequence is invalid")
    _validate_text(state.get("actor"), "recorded actor")
    _validate_text(state.get("reason"), "recorded reason")
    certified_sha = state.get("certified_sha")
    if state["state"] == "unfrozen":
        _validate_sha(certified_sha)
    elif certified_sha is not None:
        raise ControlError("frozen poll-control state must not carry a certified SHA")

    receipt_id = _receipt_id(payload)
    receipt = state_dir / f"{RECEIPT_PREFIX}{receipt_id}.json"
    _assert_private_regular(receipt)
    try:
        if receipt.read_bytes() != payload:
            raise ControlError("poll-control latest state differs from immutable receipt")
    except OSError as exc:
        raise ControlError(f"cannot verify poll-control receipt: {exc}") from exc
    return {**state, "receipt_id": receipt_id}


def _atomic_write(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ControlError(f"refusing symlinked poll-control target: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def change_state(
    state_dir: Path,
    *,
    state: str,
    actor: str,
    reason: str,
    certified_sha: str | None = None,
) -> dict[str, Any]:
    """Atomically record a frozen/unfrozen state and immutable receipt."""
    state_dir = state_dir.expanduser().resolve()
    if not state_dir.is_dir() or state_dir.is_symlink():
        raise ControlError(f"release state directory is unavailable: {state_dir}")
    actor = _validate_text(actor, "actor")
    reason = _validate_text(reason, "reason")
    if state == "unfrozen":
        certified_sha = _validate_sha(certified_sha)
    elif state == "frozen":
        certified_sha = None
    else:
        raise ControlError(f"unsupported poll-control state: {state}")

    lock = state_dir / LOCK_NAME
    try:
        lock.mkdir(mode=0o700)
    except OSError as exc:
        raise ControlError(f"cannot acquire poll-control lock: {exc}") from exc
    try:
        previous: dict[str, Any] | None = None
        latest = state_dir / LATEST_NAME
        if latest.exists() or latest.is_symlink():
            previous = read_state(state_dir)
        document = {
            "schema": SCHEMA,
            "sequence": int(previous["sequence"]) + 1 if previous else 1,
            "state": state,
            "actor": actor,
            "reason": reason,
            "certified_sha": certified_sha,
            "changed_at": datetime.now(timezone.utc).isoformat(),
            "previous_receipt_id": previous.get("receipt_id") if previous else None,
        }
        payload = _canonical_bytes(document)
        receipt_id = _receipt_id(payload)
        receipt = state_dir / f"{RECEIPT_PREFIX}{receipt_id}.json"
        if receipt.exists() or receipt.is_symlink():
            _assert_private_regular(receipt)
            if receipt.read_bytes() != payload:
                raise ControlError("poll-control receipt hash collision")
        else:
            _atomic_write(receipt, payload)
        _atomic_write(latest, payload)
        return {**document, "receipt_id": receipt_id}
    except OSError as exc:
        raise ControlError(f"cannot persist poll-control state: {exc}") from exc
    finally:
        try:
            lock.rmdir()
        except OSError as exc:
            raise ControlError(f"cannot release poll-control lock: {exc}") from exc


def _state_dir(value: str | None) -> Path:
    if value:
        return Path(value)
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "releases"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="override the releases directory (tests/operators)")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("status")
    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--print-certified-sha", action="store_true")
    for action in ("freeze", "unfreeze"):
        command = subparsers.add_parser(action)
        command.add_argument("--actor", required=True)
        command.add_argument("--reason", required=True)
        if action == "unfreeze":
            command.add_argument("--certified-sha", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = _state_dir(args.state_dir)
    try:
        if args.action == "freeze":
            result = change_state(
                state_dir, state="frozen", actor=args.actor, reason=args.reason
            )
        elif args.action == "unfreeze":
            result = change_state(
                state_dir,
                state="unfrozen",
                actor=args.actor,
                reason=args.reason,
                certified_sha=args.certified_sha,
            )
        else:
            result = read_state(state_dir)
            if args.action == "authorize" and result["state"] != "unfrozen":
                print(json.dumps(result, sort_keys=True), file=os.sys.stderr)
                return 3
            if args.action == "authorize" and args.print_certified_sha:
                print(result["certified_sha"])
                return 0
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (ControlError, OSError) as exc:
        print(json.dumps({"ok": False, "state": "unknown", "error": str(exc)}, sort_keys=True), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
