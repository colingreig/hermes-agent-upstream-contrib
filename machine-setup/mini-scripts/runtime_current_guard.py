#!/usr/bin/env python3
"""Fail-closed terminal policy for Mini's governed runtime paths and read-only digest."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import sys

from governed_interpreter import SELECTOR_CONTRACT, select_governed_interpreter

FLEET_DIGEST_JOB_ID = "f23a03e9d1b2"
_TRUST_MANIFEST = Path(__file__).resolve().with_name("digest_trusted_scripts.json")
_READ_ONLY_PROGRAMS = {"cat", "ls", "stat", "readlink", "pwd", "printf", "echo", "wc", "du", "df", "uname", "date"}
_CUTTER_SHA256 = "596747b1d00f6f1db1c557e90232282f107a74c70f38a9106baadd115fdfe420"
_RELEASE_NAME = re.compile(r"^v[0-9][0-9A-Za-z.!+_-]*-[0-9a-f]{12,64}$")


def _expand(command: str, hermes: Path) -> str:
    home = hermes.parent
    expanded = command.replace("${HOME}", str(home)).replace("$HOME", str(home))
    expanded = expanded.replace("${HERMES_HOME}", str(hermes)).replace("$HERMES_HOME", str(hermes))
    return expanded.replace("~/.hermes", str(hermes))


def _governed_resources(command: str, hermes: Path, cwd: str = "") -> set[str]:
    expanded = _expand(command, hermes)
    # Relative references after a governed cwd/cd are significant too.
    context = f"{cwd} {expanded}"
    resources: set[str] = set()
    if re.search(r"(?:runtime-current|(?:^|[/\s'\";=$])releases(?:[/\s'\";]|$))", context):
        resources.add("runtime-release")
    if re.search(r"(?:^|[/\s'\";=$])scripts(?:[/\s'\";]|$)", context) and str(hermes) in context:
        resources.add("governed-mini-scripts")
    return resources


def _confidently_read_only(command: str) -> bool:
    """Allow only one simple, non-compound read command over governed paths."""
    if re.search(r"[;&|`$<>\n]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return True
    program = Path(words[0]).name
    return program in _READ_ONLY_PROGRAMS and not any(word in {"-exec", "-delete", "-ok"} for word in words)


def _fleet_session(payload: dict) -> bool:
    prefix = f"cron_{FLEET_DIGEST_JOB_ID}_"
    return any(isinstance(payload.get(key), str) and payload[key].startswith(prefix)
               for key in ("session_id", "parent_session_id"))


def _fleet_command_allowed(command: str, hermes: Path) -> bool:
    """Exact tool-layer allowlist for the read-only digest's trusted pinned scripts."""
    if re.search(r"[;&|`$<>\n\r]", command):
        return False
    try:
        words = shlex.split(_expand(command, hermes))
    except ValueError:
        return False
    if len(words) < 2:
        return _confidently_read_only(command)
    try:
        manifest = json.loads(_TRUST_MANIFEST.read_text(encoding="utf-8"))
        if manifest.get("interpreter_selector") != SELECTOR_CONTRACT:
            return False
        entries = manifest["scripts"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False
    script = Path(words[1])
    entry = entries.get(script.name) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return False
    expected = hermes / "scripts" / script.name
    # Lexical equality is intentional: reject traversal even when resolve()
    # would land on the same inode, then reject symlinks and byte drift.
    if script != expected or script.is_symlink() or not script.is_file():
        return False
    # Interpreter provenance is lexical and absolute. Re-select from physical
    # governed releases on every invocation so rollover/pruning is safe while
    # mutable runtime-current and symlinked release ancestry remain unusable.
    if not Path(words[0]).is_absolute():
        return False
    try:
        selected = select_governed_interpreter(hermes / "releases")
    except RuntimeError:
        return False
    if words[0] != str(selected):
        return False
    try:
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
    except OSError:
        return False
    if digest != entry.get("sha256"):
        return False
    arguments = entry.get("arguments")
    return isinstance(arguments, list) and words[2:] == arguments


def _trusted_cutter_bootstrap(command: str, hermes: Path) -> bool:
    if re.search(r"[;&|`$<>\n\r]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    declared = str(hermes / "runtime-current" / "scripts" / "mini-release-cut.sh")
    if words[0].replace("~/.hermes", str(hermes), 1) != declared:
        return False
    values = {"--ref", "--certified-sha", "--promotion-receipt-id"}
    booleans = {"--if-advanced", "--preflight", "--rollback", "--prune", "--dry-run", "--offline"}
    seen: set[str] = set()
    index = 1
    while index < len(words):
        flag = words[index]
        if flag in seen or flag not in values | booleans:
            return False
        seen.add(flag)
        if flag in values:
            if index + 1 >= len(words) or words[index + 1].startswith("-"):
                return False
            value = words[index + 1]
            if flag == "--certified-sha" and re.fullmatch(r"[0-9a-f]{40}", value) is None:
                return False
            if flag == "--promotion-receipt-id" and re.fullmatch(r"[0-9a-f]{64}", value) is None:
                return False
            if flag == "--ref" and re.fullmatch(r"[0-9A-Za-z._/-]+", value) is None:
                return False
            index += 2
        else:
            index += 1
    script = Path(declared)
    try:
        actual = script.resolve(strict=True)
        relative = actual.relative_to((hermes / "releases").resolve(strict=True))
        return (
            len(relative.parts) == 3
            and _RELEASE_NAME.fullmatch(relative.parts[0]) is not None
            and relative.parts[1:] == ("scripts", "mini-release-cut.sh")
            and not script.is_symlink()
            and hashlib.sha256(actual.read_bytes()).hexdigest() == _CUTTER_SHA256
        )
    except (OSError, ValueError):
        return False


def _references_cutter_by_alias_or_wrapper(command: str, hermes: Path) -> bool:
    """Detect a non-declared token that still resolves to the trusted cutter."""
    try:
        words = shlex.split(_expand(command, hermes))
        cutter = (hermes / "runtime-current" / "scripts" / "mini-release-cut.sh").resolve(strict=True)
    except (ValueError, OSError):
        return False
    for token in words:
        if token.startswith("-"):
            continue
        try:
            candidate = Path(token)
            if candidate.is_absolute() and candidate.resolve(strict=True) == cutter:
                return True
        except OSError:
            continue
    return False


def _has_matching_lease(*, hermes: Path, session_id: str, resources: set[str]) -> bool:
    if not session_id or not resources:
        return False
    database = hermes / "state" / "production-write-lease.db"
    if database.is_symlink() or not database.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True, timeout=0.2) as connection:
            rows = connection.execute(
                "SELECT actor,resources_json,expires_at FROM active_leases WHERE session_id=?", (session_id,)
            ).fetchall()
    except (OSError, sqlite3.Error):
        return False
    now = datetime.now(timezone.utc)
    for actor, raw_resources, expires_at in rows:
        try:
            held = set(json.loads(raw_resources))
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if actor == "mini-release-cut" and resources <= held and expiry > now:
            return True
    return False


def _block(resources: set[str], *, contract: bool = False) -> None:
    joined = ", ".join(sorted(resources)) or "fleet-health-digest read-only terminal contract"
    reason = (
        "GOVERNED PATH WRITE BLOCKED by runtime_current_guard (mechanical pre_tool_call gate): "
        + ("ambiguous or unapproved terminal command violates " if contract else "terminal command may mutate ")
        + joined
        + " without a matching active mini-release-cut production-write lease for this session. "
          "Do not retry or bypass the cutter."
    )
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, sort_keys=True))


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name") != "terminal":
        return 0
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return 0
    hermes = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser()
    if _trusted_cutter_bootstrap(command, hermes):
        return 0
    if _references_cutter_by_alias_or_wrapper(command, hermes):
        _block({"runtime-release", "governed-mini-scripts"})
        return 0
    if _fleet_session(payload):
        if not _fleet_command_allowed(command, hermes):
            _block(set(), contract=True)
        return 0
    resources = _governed_resources(command, hermes, str(payload.get("cwd") or ""))
    if not resources or _confidently_read_only(command):
        return 0
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and _has_matching_lease(hermes=hermes, session_id=session_id, resources=resources):
        return 0
    _block(resources)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
