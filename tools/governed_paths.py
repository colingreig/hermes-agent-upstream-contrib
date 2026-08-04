"""Mechanical write boundary for operator-governed Hermes paths.

The shell hook is advisory/defense-in-depth.  Local terminal commands are
executed under the host's macOS sandbox, and file mutation tools are checked by
resolved target path before dispatch.  A valid production-write lease removes
only the governed-path denies for the governed cutter session.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import sys
from typing import Any

from hermes_constants import get_hermes_home

FLEET_DIGEST_JOB_ID = "f23a03e9d1b2"
_MINI_PRODUCTION_HOME = Path("/Users/colingreig/.hermes")
_GOVERNED = (
    ("runtime-release", "subpath", "runtime-current"),
    ("runtime-release", "subpath", "releases"),
    ("governed-mini-scripts", "subpath", "scripts"),
)
_V4_PATH = re.compile(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", re.MULTILINE)
_V4_MOVE = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", re.MULTILINE)


def _home() -> Path:
    return get_hermes_home().expanduser().absolute()


def _absolute(path: str, cwd: str | None = None) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(path))
    base = Path(cwd or os.getcwd())
    return Path(os.path.abspath(base / expanded if not os.path.isabs(expanded) else expanded))


def _policy_paths(target: str, task_id: str) -> tuple[Path, Path]:
    """Return the file tool's exact target plus its no-deref lexical spelling.

    ``_resolve_path_for_task`` is the production file-tool resolver and follows
    every existing symlink ancestor (including for a missing leaf).  The lexical
    companion preserves the special ``runtime-current`` alias boundary even
    when that pointer intentionally resolves outside ``releases``.
    """
    from tools.file_tools import _expand_tilde, _resolve_base_dir, _resolve_path_for_task

    resolved = _resolve_path_for_task(target, task_id)
    if not isinstance(resolved, Path):
        raise RuntimeError("governed path authorization requires native host paths")
    expanded = _expand_tilde(target)
    if os.path.isabs(expanded):
        lexical = Path(os.path.abspath(expanded))
    else:
        base = _resolve_base_dir(task_id, container_paths=False)
        if not isinstance(base, Path):
            raise RuntimeError("governed path authorization could not resolve task workspace")
        lexical = Path(os.path.abspath(base / expanded))
    # Resolve once more here so an OSError/symlink loop fails closed at the
    # authorization boundary rather than being deferred to the mutator.
    return lexical, resolved.resolve(strict=False)


def _resource_for(path: Path, hermes: Path) -> str | None:
    for resource, kind, relative in _GOVERNED:
        root = hermes / relative
        if kind == "literal" and path == root:
            return resource
        if kind == "subpath" and (path == root or root in path.parents):
            return resource
    return None


def _has_matching_lease(hermes: Path, session_id: str, resources: set[str]) -> bool:
    if not session_id or not resources:
        return False
    database = hermes / "state" / "production-write-lease.db"
    if database.is_symlink() or not database.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True, timeout=0.2) as connection:
            rows = connection.execute(
                "SELECT actor,resources_json,expires_at FROM active_leases WHERE session_id=?",
                (session_id,),
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


def _mutation_targets(tool_name: str, args: dict[str, Any]) -> list[str]:
    if tool_name == "write_file":
        return [args["path"]] if isinstance(args.get("path"), str) else []
    if tool_name != "patch":
        return []
    targets = [args["path"]] if isinstance(args.get("path"), str) else []
    if args.get("mode") == "patch" and isinstance(args.get("patch"), str):
        text = args["patch"]
        targets.extend(match.group(1).strip() for match in _V4_PATH.finditer(text))
        for match in _V4_MOVE.finditer(text):
            targets.extend((match.group(1).strip(), match.group(2).strip()))
    return targets


def check_file_mutation(
    tool_name: str, args: dict[str, Any], session_id: str, *, task_id: str = "default"
) -> str | None:
    """Return a block reason for a governed file-tool target, otherwise None."""
    if tool_name not in {"write_file", "patch"}:
        return None
    if session_id.startswith(f"cron_{FLEET_DIGEST_JOB_ID}_"):
        return "fleet-health-digest is mechanically read-only; file mutation tools are disabled"
    hermes = _home()
    resources: set[str] = set()
    for target in _mutation_targets(tool_name, args):
        for path in _policy_paths(target, task_id):
            resource = _resource_for(path, hermes)
            if resource is not None:
                resources.add(resource)
    if not resources or _has_matching_lease(hermes, session_id, resources):
        return None
    return (
        "GOVERNED PATH WRITE BLOCKED: file mutation targets "
        + ", ".join(sorted(resources))
        + " without a matching active mini-release-cut production-write lease for this session"
    )


def _sandbox_profile(hermes: Path, *, read_only: bool) -> str:
    if read_only:
        return "(version 1) (allow default) (deny file-write*)"
    clauses = []
    for _resource, kind, relative in _GOVERNED:
        operation = "literal" if kind == "literal" else "subpath"
        escaped = str(hermes / relative).replace("\\", "\\\\").replace('"', '\\"')
        clauses.append(f'({operation} "{escaped}")')
    return "(version 1) (allow default) (deny file-write* " + " ".join(clauses) + ")"


def _is_mini_production_context(hermes: Path) -> bool:
    """True only for the supported Hermes Mini production process scope."""
    return sys.platform == "darwin" and hermes == _MINI_PRODUCTION_HOME


_CUTTER_SHA256 = "5c297ff6f4bfaaa0de796d5fac1de44b4b97637b0f28a4529ebe0335d0204b61"
_RELEASE_NAME = re.compile(r"^v[0-9][0-9A-Za-z.!+_-]*-[0-9a-f]{12,64}$")
_CUTTER_VALUE_FLAGS = {"--ref", "--certified-sha", "--promotion-receipt-id"}
_CUTTER_BOOL_FLAGS = {"--if-advanced", "--preflight", "--rollback", "--prune", "--dry-run", "--offline"}


def _trusted_cutter_bootstrap(command: str, hermes: Path) -> bool:
    """Allow only the pinned cutter itself to bootstrap its internal lease."""
    if re.search(r"[;&|`$<>\n\r]", command):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words:
        return False
    declared = str(hermes / "runtime-current" / "scripts" / "mini-release-cut.sh")
    lexical = words[0].replace("~/.hermes", str(hermes), 1)
    if lexical != declared:
        return False
    seen: set[str] = set()
    index = 1
    while index < len(words):
        flag = words[index]
        if flag in seen or flag not in (_CUTTER_VALUE_FLAGS | _CUTTER_BOOL_FLAGS):
            return False
        seen.add(flag)
        if flag in _CUTTER_VALUE_FLAGS:
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
        if len(relative.parts) != 3 or not _RELEASE_NAME.fullmatch(relative.parts[0]):
            return False
        if relative.parts[1:] != ("scripts", "mini-release-cut.sh") or script.is_symlink():
            return False
        return hashlib.sha256(actual.read_bytes()).hexdigest() == _CUTTER_SHA256
    except (OSError, ValueError):
        return False


def wrap_terminal_command(command: str, session_id: str) -> tuple[str, str | None]:
    """Wrap a local command in the mandatory host filesystem sandbox.

    Returns ``(wrapped_command, error)``.  Missing sandbox support fails closed
    only for this configured governance boundary rather than silently executing.
    """
    hermes = _home()
    if not _is_mini_production_context(hermes):
        return command, None
    # The cutter must be able to start before it can acquire its internally
    # generated production-write lease. Its exact hash-pinned invocation is the
    # sole bootstrap path and does not depend on sandbox availability.
    if _trusted_cutter_bootstrap(command, hermes):
        return command, None
    sandbox = shutil.which("sandbox-exec")
    if not sandbox:
        return "", "governed path enforcement unavailable: sandbox-exec not found"
    read_only = session_id.startswith(f"cron_{FLEET_DIGEST_JOB_ID}_")
    resources = {resource for resource, _kind, _relative in _GOVERNED}
    if not read_only and _has_matching_lease(hermes, session_id, resources):
        return command, None
    profile = _sandbox_profile(hermes, read_only=read_only)
    wrapped = " ".join(
        shlex.quote(part) for part in (sandbox, "-p", profile, "/bin/bash", "-c", command)
    )
    return wrapped, None
