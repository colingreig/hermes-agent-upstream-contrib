#!/usr/bin/env python3
"""Atomically record machine-readable UTC evidence for a successful skill pull."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile


def write_generation_state(
    target: Path,
    *,
    source: str,
    state: str,
    operation_id: str,
) -> None:
    """Publish a non-stable pull state without certifying catalog freshness."""
    if state not in {"updating", "failed"}:
        raise ValueError(f"unsupported non-stable generation state: {state}")
    if not operation_id:
        raise ValueError("operation_id is required")

    previous_generation = ""
    if target.is_file() and not target.is_symlink():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
            if state == "failed" and (
                current.get("state") != "updating"
                or current.get("operation_id") != operation_id
            ):
                raise ValueError("generation update is not owned by this operation")
            candidate = current.get("generation") or current.get(
                "previous_generation"
            )
            if isinstance(candidate, str) and len(candidate) == 64:
                previous_generation = candidate
        except ValueError:
            raise
        except Exception:
            if state == "failed":
                raise ValueError("could not validate active generation update")

    now = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    payload = {
        "schema_version": 1,
        "state": state,
        "source": source,
        "operation_id": operation_id,
        f"{state}_at": now,
    }
    if previous_generation:
        payload["previous_generation"] = previous_generation
    _atomic_json_write(target, payload)


def write_success_evidence(
    target: Path,
    *,
    source: str,
    root: Path,
    commit: str,
    generation_target: Path | None = None,
    changed_from: str | None = None,
    operation_id: str | None = None,
) -> None:
    if (
        len(commit) < 40
        or any(char not in "0123456789abcdef" for char in commit.lower())
    ):
        raise ValueError("commit must be a full hexadecimal object id")
    resolved = root.resolve(strict=True)
    if resolved != root:
        raise ValueError(f"skill root does not resolve exactly: {root} -> {resolved}")
    payload = {
        "schema_version": 1,
        "source": source,
        "root": str(root),
        "commit": commit.lower(),
        "completed_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    # The catalog state remains updating until both the success evidence and
    # stable generation can be published. If the latter fails, readers and the
    # verifier reject the still-updating marker despite the newer evidence.
    _atomic_json_write(target, payload)
    if generation_target is not None and changed_from and (
        operation_id
        or changed_from != commit
        or not generation_target.exists()
    ):
        previous_generation = ""
        if operation_id:
            current = json.loads(generation_target.read_text(encoding="utf-8"))
            if (
                current.get("state") != "updating"
                or current.get("operation_id") != operation_id
            ):
                raise ValueError("generation update is not owned by this operation")
            candidate = current.get("previous_generation")
            if isinstance(candidate, str) and len(candidate) == 64:
                previous_generation = candidate
        generation = (
            previous_generation
            if changed_from == commit and previous_generation
            else hashlib.sha256(f"{source}\0{commit.lower()}".encode()).hexdigest()
        )
        generation_payload = {
            "schema_version": 1,
            "state": "stable",
            "generation": generation,
            "source": source,
            "commit": commit.lower(),
            "changed_from": changed_from.lower(),
            "published_at": payload["completed_at"],
        }
        if operation_id:
            generation_payload["operation_id"] = operation_id
        _atomic_json_write(generation_target, generation_payload)


def _atomic_json_write(target: Path, payload: dict[str, object]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"refusing symlinked JSON target: {target}")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.swap.", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def validate_success_evidence(
    target: Path,
    *,
    source: str,
    root: Path,
    max_age: timedelta,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return validated evidence, rejecting stale, future, or mismatched data."""
    if not target.is_file() or target.is_symlink():
        raise ValueError(f"evidence missing or symlinked: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("wrong evidence schema")
    if payload.get("source") != source or payload.get("root") != str(root):
        raise ValueError("evidence source/root identity mismatch")
    commit = payload.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) < 40
        or any(char not in "0123456789abcdef" for char in commit.lower())
    ):
        raise ValueError("evidence commit is not a full hexadecimal object id")
    raw_completed = payload.get("completed_at")
    if not isinstance(raw_completed, str) or not raw_completed.endswith("Z"):
        raise ValueError("evidence timestamp is not explicit UTC")
    completed = datetime.fromisoformat(raw_completed[:-1] + "+00:00")
    observed_at = now or datetime.now(timezone.utc)
    age = observed_at - completed
    if age < timedelta(0) or age >= max_age:
        raise ValueError(f"evidence age outside freshness budget: {age}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path)
    parser.add_argument("--source", required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--generation-target", type=Path)
    parser.add_argument("--changed-from")
    parser.add_argument("--generation-state", choices=("updating", "failed"))
    parser.add_argument("--operation-id")
    args = parser.parse_args()
    if args.generation_state:
        if args.generation_target is None:
            parser.error("--generation-state requires --generation-target")
        write_generation_state(
            args.generation_target,
            source=args.source,
            state=args.generation_state,
            operation_id=args.operation_id or "",
        )
        return 0
    if args.target is None or args.root is None or args.commit is None:
        parser.error("success evidence requires --target, --root, and --commit")
    write_success_evidence(
        args.target,
        source=args.source,
        root=args.root,
        commit=args.commit,
        generation_target=args.generation_target,
        changed_from=args.changed_from,
        operation_id=args.operation_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
